"""지식 조각 읽기. 쓰기는 수동 적재(app.data_ingest)만 한다."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.models import RagChunk
from app.refprice.models import RefDatasetVersion


async def active_versions(s: AsyncSession) -> list[tuple[uuid.UUID, str]]:
    """활성 지식 버전 (id, dataset_key). dataset_key마다 활성은 하나다"""
    rows = await s.execute(
        select(RefDatasetVersion.id, RefDatasetVersion.dataset_key)
        .where(RefDatasetVersion.kind == "knowledge", RefDatasetVersion.status.is_(True))
        .order_by(RefDatasetVersion.dataset_key)
    )
    return [(r.id, r.dataset_key) for r in rows]


async def chunk_rows(
    s: AsyncSession, versions: list[tuple[uuid.UUID, str]]
) -> list[tuple[str, list[dict]]]:
    """버전별 원본 행. 본문 · 절은 저장된 열 값을 쓴다"""
    if not versions:
        return []
    keys = dict(versions)
    out: dict[uuid.UUID, list[dict]] = {v: [] for v in keys}
    rows = await s.execute(
        select(
            RagChunk.dataset_id,
            RagChunk.content,
            RagChunk.section,
            RagChunk.provenance,
            RefDatasetVersion.effective_date,
        )
        .join(RefDatasetVersion, RefDatasetVersion.id == RagChunk.dataset_id)
        .where(RagChunk.dataset_id.in_(keys))
        .order_by(RagChunk.dataset_id, RagChunk.record_key)
    )
    for r in rows:
        out[r.dataset_id].append(
            {
                **r.provenance,
                "content": r.content,
                "section": r.section,
                "effective_date": r.provenance.get("effective_date") or str(r.effective_date or ""),
            }
        )
    return [(keys[v], rs) for v, rs in out.items()]


async def nearest(
    s: AsyncSession, version_ids: list[uuid.UUID], model: str, vector: list[float], limit: int
) -> list[str]:
    """임베딩이 가까운 조각의 행 키(dataset_key/record_key). 같은 모델 · 차원으로 만든 버전만 본다"""
    if not version_ids:
        return []
    rows = await s.execute(
        select(RefDatasetVersion.dataset_key, RagChunk.record_key)
        .join(RefDatasetVersion, RefDatasetVersion.id == RagChunk.dataset_id)
        .where(
            RagChunk.dataset_id.in_(version_ids),
            RefDatasetVersion.embedding_model == model,
            RefDatasetVersion.embedding_dimensions == len(vector),
            RagChunk.embedding.is_not(None),
        )
        .order_by(RagChunk.embedding.cosine_distance(vector))
        .limit(limit)
    )
    return [f"{k}/{r}" for k, r in rows]
