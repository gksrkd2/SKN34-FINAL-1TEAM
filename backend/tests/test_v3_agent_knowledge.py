"""v3 지식: 조각 번호 겹침 · 출처 주소 · 12공종 검색 무게 · 문장 뽑기 표시 허락."""

import pytest
from v3_kb import datasets, repo_index

from app.agent.v3.trades import TAG_TO_CODE
from app.knowledge import bm25, extract, search3
from app.knowledge.sources import assemble
from app.knowledge.v3_csv import SIGONG_MAX, START, kind_of, rows


def _sigong(refs: list[int]) -> tuple[str, list[dict[str, str]]]:
    return (
        "sigong_ilji_rag",
        [
            {"id": f"s{r}", "ref": f"K{r}", "doc": "sigong", "content": "시공일지 조각", "section": "",
             "source": "시공일지", "trades": "도배", "hints": "[]", "page": "1", "page_known": "false"}
            for r in refs
        ],
    )  # fmt: skip


def test_v3_knowledge_refs_do_not_overlap_sigong_or_book():
    assert START > SIGONG_MAX  # 시공일지 K6000 + 쪽×50 + 순번(218쪽)
    v3 = [r for k, rs in datasets() if k == "v3_knowledge_rag" for r in rs]
    assert v3 and v3[0]["ref"] == f"K{START}"
    assert int(v3[-1]["ref"][1:]) <= 99999  # 번호 칸 다섯 자리(bm25.REF)
    # 시공일지 첫 · 중간(K7154 실제로 있음) · 마지막 번호와 같이 켜도 멈추지 않는다
    chunks, _ = assemble([*datasets(), _sigong([6050, 7154, SIGONG_MAX])])
    refs = [c.ref for c in chunks]
    assert len(refs) == len(set(refs))


def test_overlapping_refs_stop_assembly():
    with pytest.raises(ValueError):
        assemble([*datasets(), _sigong([START])])


def test_repo_paths_are_not_kept_as_links():
    out = rows(
        [{"id": "x", "trade": "dobae", "summary": "요지예요.", "source_name": "2026 표준품셈",
          "source_url": "repo:data/processed/03_standard_productivity.csv", "license": "공공저작물"}],
        "0" * 64,
    )  # fmt: skip
    assert out[0]["source_url"] == ""
    assert kind_of("2026 건설공사 표준품셈", "공공저작물") == "pumsem"
    assert (
        kind_of("공동주택 하자의 조사 ... (국토교통부고시 제2026-360호)", "공공저작물")
        == "gov_notice"
    )


def test_trade_weight_lifts_that_trades_chunks():
    ix = repo_index()
    sc = search3.scopes(ix, TAG_TO_CODE)
    q = "장판이 들떴어요 왜 그래요?"
    on = search3.search(ix, q, trades=["floor"], tags=TAG_TO_CODE, k=5).hits
    off = search3.search(ix, q, trades=[], tags=TAG_TO_CODE, k=5).hits
    n_on = sum(1 for h in on if "floor" in sc[h.ref])
    assert n_on >= 2 and n_on >= sum(1 for h in off if "floor" in sc[h.ref])


def test_book_and_pumsem_sentences_are_never_quoted():
    ix = repo_index()
    hits = [
        bm25.Hit(ref=x.chunk.ref, source_type="", title=x.doc.title, publisher="", date="",
                 section="", text=x.chunk.text, score=1.0)
        for x in ix.items
        if x.doc.kind in ("book", "pumsem")
    ][:6]  # fmt: skip
    assert hits
    q = " ".join(h.text[:30] for h in hits)
    assert extract.pick(ix, q, hits, min_score=0.0) == []
    assert extract.display_of("law") == "full" and extract.display_of("maker") == "short"


def test_rule_text_comes_from_calc_order_and_cites_are_allowed():
    from app.agent.v3.check import CITE
    from app.agent.v3.trades import ORDER_CITES, ORDER_RULES, rules
    from app.calc.v2_plan import PLAN_ORDER

    r = rules()["rule:plan-order"]
    assert r["kind"] == "rule" and r["text"].count("→") == len(PLAN_ORDER) - 1
    assert set(ORDER_CITES) == set(ORDER_RULES)
    assert all(CITE.match(c) for cs in ORDER_CITES.values() for c in cs)


def test_installed_summaries_match_their_chunks_and_are_haeyo():
    import json
    import re

    from app.knowledge import summaries

    ix = repo_index()
    rows = [
        json.loads(x) for x in summaries.PATH.read_text(encoding="utf-8").splitlines() if x.strip()
    ]
    assert len(rows) >= 400
    matched = 0
    for r in rows:
        assert 1 <= len(r["summary"]) <= 3 and all(re.search(r"요[.!?]?$", s) for s in r["summary"])
        it = ix.by_ref.get(r["ref"])
        if it is not None and summaries.sha(it.chunk.text) == r["sha"]:
            matched += 1
            nums = set(re.findall(r"\d+(?:[.,]\d+)*", it.chunk.text.replace(",", "")))
            for s in r["summary"]:
                assert set(re.findall(r"\d+(?:[.,]\d+)*", s.replace(",", ""))) <= nums, (
                    r["ref"],
                    s,
                )
    assert matched >= len(rows) * 0.9  # 저장소 말뭉치와 같은 조각 글


def test_summary_is_used_only_for_the_same_chunk_text():
    from app.knowledge import summaries

    ref, sha_, sents = next((k, v[0], v[1]) for k, v in summaries._load().items())
    item = repo_index().by_ref[ref]
    assert summaries.sha(item.chunk.text) == sha_ and summaries.of(ref, item.chunk.text) == sents
    assert summaries.of(ref, item.chunk.text + " 바뀜") == ()


def test_iter2_refs_follow_v3_without_overlap_and_doc_home_links_are_hidden():
    from app.knowledge.v3_csv import DOC_HOME, START2

    sets = dict(datasets())
    v3, it2 = sets["v3_knowledge_rag"], sets["v3_knowledge_iter2_rag"]
    last_v3 = max(int(r["ref"][1:]) for r in v3)
    nums = [int(r["ref"][1:]) for r in it2]
    assert START2 > last_v3 and START2 > SIGONG_MAX and min(nums) == START2 and max(nums) <= 99999
    chunks, _ = assemble(datasets())  # 겹치면 '지식 조각 번호가 겹쳐요'로 멈춘다
    assert len({c.ref for c in chunks}) == len(chunks)
    homes = [r for r in it2 if r["doc"].split(":", 1)[1].startswith(DOC_HOME)]
    assert homes and all(
        r["source_url"] == "" for r in homes
    )  # 품셈 · LH 시방서 · KCS는 출처 이름만
    assert any(r["source_url"].startswith("https://") for r in it2)


def test_search_keeps_original_source_date_and_trade_constraints():
    from datetime import date

    q = "벽지 초배"
    chunks = [
        bm25.Chunk(ref, ref, q + " 준비 순서", trades=(trade,))
        for ref, trade in [
            ("K1", "도배"),
            ("K2", "도배"),
            ("K3", "목공"),
            ("K4", "도배"),
            ("K5", "도배"),
            ("K6", "도배"),
        ]
    ]
    docs = {
        "K1": bm25.Doc(kind="kcs", date="2025-01-01"),
        "K2": bm25.Doc(kind="blog", date="2025-01-01"),
        "K3": bm25.Doc(kind="kcs", date="2025-01-01"),
        "K4": bm25.Doc(kind="kcs", date="2027-01-01"),
        "K5": bm25.Doc(kind="kcs", date="2024-01-01"),
        "K6": bm25.Doc(kind="kcs", date="2025-01-01", effective_date="2027-01-01"),
    }
    result = search3.search(
        bm25.build(chunks, docs),
        q,
        tags=TAG_TO_CODE,
        trades=["dobae"],
        policy_text="2025년 표준시방서에서 도배 안전 기준을 알려줘",
        today=date(2026, 11, 20),
    )
    assert [hit.ref for hit in result.hits] == ["K1"]
    assert result.enough


def test_extract_uses_one_document_and_asks_to_check_when_topic_is_missing():
    from app.agent.v3.answer import KB, Part, knowledge
    from app.knowledge import cards

    ix = bm25.build(
        [
            bm25.Chunk(
                "K1", "a", "초배 보양 점검 절차는 벽면을 말린 뒤 진행한다.", trades=("도배",)
            ),
            bm25.Chunk(
                "K2", "b", "초배 보양 점검 절차는 젖은 상태에서 진행한다.", trades=("도배",)
            ),
        ],
        {"a": bm25.Doc(title="시방서 A", kind="kcs"), "b": bm25.Doc(title="시방서 B", kind="kcs")},
    )
    kb = KB(ix=ix, cards=cards.default())
    extract = Part()
    assert (
        knowledge(extract, kb, "초배 보양 점검 절차", trade="dobae", route="trade", intents=())
        == "extract"
    )
    assert len(extract.sections[0].sources) == 1
    unknown = Part()
    assert (
        knowledge(unknown, kb, "제올라이트 흡착 성능", trade="dobae", route="trade", intents=())
        == "unknown"
    )
    assert unknown.unknown and not unknown.sections
