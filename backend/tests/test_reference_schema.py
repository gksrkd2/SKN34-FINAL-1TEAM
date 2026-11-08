"""테스트 전용 DB에서 스키마 제약을 검증한다. 원본·임베딩 API는 사용하지 않는다."""

import asyncio
import uuid
from decimal import Decimal

import asyncpg
import pytest
from conftest import query


def _version(kind="wage", *, key=None, version=1, status="draft", digest="a" * 64):
    ident = uuid.uuid4()
    query(
        """insert into ref_dataset_versions
        (id,dataset_key,kind,version,source,processed_path,processed_sha256,build_profile,
         historical,current_rule_usable,calculation_usable,reference_usable,
         embedding_model,embedding_dimensions,provenance,status)
        values ($1,$2,$3,$4,'synthetic.pdf','synthetic.csv',$5,'test-v1',
                false,false,false,false,$6,$7,'{}'::jsonb,$8)""",
        ident,
        key or str(ident),
        kind,
        version,
        digest,
        "test-model" if kind == "knowledge" else None,
        3 if kind == "knowledge" else None,
        status,
    )
    return ident


def _wage(ident, key, code="1001", hours=8, amount=None):
    query(
        """insert into ref_wage_rows
        (dataset_id,record_key,record_type,occupation_code,publication_date,
         basis_hours,normalized_value,page,source_text)
        values ($1,$2,'occupation_wage',$3,'2026-09-01',$4,$5,1,'검증용 직종')""",
        ident,
        key,
        code,
        hours,
        amount,
    )


def test_schema_shape_and_vector_extension():
    tables = {
        "ref_dataset_versions": 18,
        "ref_floorplan_rows": 14,
        "ref_unit_price_rows": 14,
        "ref_productivity_rows": 21,
        "ref_wage_rows": 16,
        "rag_chunks": 9,
        "ref_dataset_events": 7,
    }
    rows = query(
        """select table_name,count(*) as n from information_schema.columns
        where table_schema='public' and table_name=any($1::text[]) group by table_name""",
        list(tables),
    )
    assert {r["table_name"]: r["n"] for r in rows} == tables
    assert query("select extname from pg_extension where extname='vector'")
    assert (
        query(
            "select format_type(atttypid,atttypmod) as t from pg_attribute where attrelid='rag_chunks'::regclass and attname='embedding'"
        )[0]["t"]
        == "vector"
    )


def test_duplicate_import_and_active_version_are_rejected():
    _version(key="same", status="active")
    with pytest.raises(asyncpg.UniqueViolationError):
        _version(key="same", version=2, digest="b" * 64, status="active")
    # 모델 설정이 NULL인 정형 자료도 같은 내용의 중복 등록을 막는다.
    with pytest.raises(asyncpg.UniqueViolationError):
        _version(key="same", version=2)
    _version(key="same", version=2, digest="b" * 64)


@pytest.mark.parametrize(
    "assignment",
    [
        "embedding_model='test-model'",
        "embedding_dimensions=0",
        "provenance='[]'::jsonb",
        "status='unknown'",
        "processed_sha256='invalid'",
        "version=0",
    ],
)
def test_version_checks(assignment):
    ident = _version()
    with pytest.raises(asyncpg.CheckViolationError):
        query(f"update ref_dataset_versions set {assignment} where id=$1", ident)


def test_wage_identity_missing_value_and_hours():
    ident = _version()
    _wage(ident, "missing")
    assert query("select normalized_value from ref_wage_rows")[0]["normalized_value"] is None
    with pytest.raises(asyncpg.UniqueViolationError):
        _wage(ident, "duplicate")
    _wage(ident, "six-hours", hours=6, amount=0)
    assert (
        query("select normalized_value from ref_wage_rows where record_key='six-hours'")[0][
            "normalized_value"
        ]
        == 0
    )
    with pytest.raises(asyncpg.CheckViolationError):
        _wage(ident, "negative", code="1002", amount=-1)
    with pytest.raises(asyncpg.CheckViolationError):
        _wage(ident, "bad-hours", code="1003", hours=25)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        _wage(_version("floorplan"), "wrong-source")


def test_deferred_parent_and_exact_decimal():
    ident = _version("unit_price")
    value = Decimal("-0.12345678901234567890")
    # 원본 순서가 자식 먼저여도 같은 트랜잭션에서 부모가 생기면 유효하다.
    query(
        """insert into ref_unit_price_rows
        (dataset_id,record_key,parent_key,record_type,normalized_value)
        values ($1,'child','parent','component',$2),($1,'parent',null,'work_header',null)""",
        ident,
        value,
    )
    assert (
        query("select normalized_value from ref_unit_price_rows where record_key='child'")[0][
            "normalized_value"
        ]
        == value
    )
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        query(
            "insert into ref_unit_price_rows(dataset_id,record_key,parent_key,record_type) values ($1,'orphan','absent','component')",
            ident,
        )
    with pytest.raises(asyncpg.CheckViolationError):
        query(
            "insert into ref_unit_price_rows(dataset_id,record_key,parent_key,record_type) values ($1,'self','self','component')",
            ident,
        )
    other = _version("unit_price")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        query(
            "insert into ref_unit_price_rows(dataset_id,record_key,parent_key,record_type) values ($1,'cross','parent','component')",
            other,
        )
    with pytest.raises(asyncpg.CheckViolationError):
        query(
            "update ref_unit_price_rows set normalized_value='NaN'::numeric where dataset_id=$1 and record_key='child'",
            ident,
        )


@pytest.mark.parametrize(
    "pages,embedding,content",
    [
        ([], None, "본문"),
        ([0], None, "본문"),
        ([1, None], None, "본문"),
        ([1], "[0,0,0]", "본문"),
        ([1], None, "   "),
    ],
)
def test_rag_rejects_invalid_pages_vectors_and_blank_content(pages, embedding, content):
    ident = _version("knowledge")
    with pytest.raises(asyncpg.CheckViolationError):
        query(
            """insert into rag_chunks(dataset_id,record_key,section,content,page_numbers,content_hash,embedding)
        values ($1,'chunk','절',$2,$3,$4,$5::vector)""",
            ident,
            content,
            pages,
            "a" * 64,
            embedding,
        )


def test_rag_literal_vector_roundtrip_and_citation():
    ident = _version("knowledge")
    # 고정된 합성 벡터로 자료형만 확인한다. 모델 호출·임베딩 생성이 아니다.
    query(
        """insert into rag_chunks(dataset_id,record_key,section,content,page_numbers,content_hash,embedding)
    values ($1,'chunk','절','검증용 본문',array[1,2],$2,'[1,0,0]'::vector)""",
        ident,
        "a" * 64,
    )
    row = query(
        "select vector_dims(embedding) as dims,page_numbers,embedding <=> '[1,0,0]'::vector as distance from rag_chunks"
    )[0]
    assert row["dims"] == 3 and row["page_numbers"] == [1, 2] and row["distance"] == 0
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        query("delete from ref_dataset_versions where id=$1", ident)


@pytest.mark.parametrize(
    "seq,previous,current,actor",
    [
        (1, None, "active", "system"),
        (2, None, "active", "system"),
        (2, "active", "active", "system"),
        (1, None, "draft", " "),
    ],
)
def test_event_checks(seq, previous, current, actor):
    ident = _version()
    with pytest.raises(asyncpg.CheckViolationError):
        query(
            """insert into ref_dataset_events(dataset_id,event_no,from_status,to_status,actor,details)
        values ($1,$2,$3,$4,$5,'{}'::jsonb)""",
            ident,
            seq,
            previous,
            current,
            actor,
        )


def test_event_initial_and_following_rows():
    ident = _version()
    query(
        """insert into ref_dataset_events(dataset_id,event_no,from_status,to_status,actor,details)
    values ($1,1,null,'draft','system','{}'::jsonb),($1,2,'draft','approved','user:test','{}'::jsonb)""",
        ident,
    )
    assert (
        len(
            query(
                "select event_no from ref_dataset_events where dataset_id=$1 order by event_no",
                ident,
            )
        )
        == 2
    )


def test_rag_sqlalchemy_roundtrip():
    from app.db import SessionLocal
    from app.knowledge.models import RagChunk

    ident = _version("knowledge")

    async def run():
        async with SessionLocal() as session:
            chunk = RagChunk(
                dataset_id=ident,
                record_key="orm",
                section="절",
                content="합성 본문",
                page_numbers=[1, 2],
                content_hash="a" * 64,
                embedding=[1.0, 0.0, 0.0],
            )
            session.add(chunk)
            await session.commit()
            await session.refresh(chunk)
            assert list(chunk.embedding) == [1.0, 0.0, 0.0]
            assert chunk.page_numbers == [1, 2]

    asyncio.run(run())
