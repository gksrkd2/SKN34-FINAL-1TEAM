"""지식 검색: 활성 지식 버전에서 키워드(BM25)와 의미 검색(pgvector)으로 찾아 합친다.

- 키워드 색인은 활성 버전 목록이 바뀔 때만 다시 만든다 (프로세스마다 한 벌)
- 검색어 임베딩은 부르는 쪽이 DB 세션을 열기 전에 만들어 넘긴다 (app.agent.llm.embed_query). 없으면 키워드로만
"""

import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge import bm25, repository, suggest
from app.knowledge.sources import assemble

_cache: tuple[tuple[str, ...], bm25.Index] | None = None


def _build(datasets: list[tuple[str, list[dict]]], version: str) -> bm25.Index:
    chunks, docs = assemble(datasets)
    return bm25.build(chunks, docs, version)


async def index(s: AsyncSession) -> bm25.Index:
    return (await _index(s))[0]


async def is_current(s: AsyncSession, ix: bm25.Index) -> bool:
    versions = await repository.active_versions(s)
    return ix.version == ",".join(sorted(str(v) for v, _ in versions))


async def _index(s: AsyncSession) -> tuple[bm25.Index, list[uuid.UUID]]:
    """키워드 색인과 그 색인을 만든 활성 버전 id들"""
    global _cache
    versions = await repository.active_versions(s)
    ids = [v for v, _ in versions]
    key = tuple(sorted(str(v) for v in ids))
    if _cache and _cache[0] == key:
        return _cache[1], ids
    datasets = await repository.chunk_rows(s, versions)
    # 색인 만들기는 0.3초쯤 걸려 작업 스레드로 넘긴다. 동시에 두 번 만들어도 결과는 같다
    ix = await asyncio.to_thread(_build, datasets, ",".join(key))
    _cache = (key, ix)
    return ix, ids


async def search(
    s: AsyncSession,
    q: str,
    *,
    queries: list[str] | tuple[str, ...] = (),
    trade: str | None = None,
    k: int = 6,
    policy_text: str | None = None,
    embedding: tuple[str, list[float]] | None = None,
) -> bm25.Result:
    """질문(+ 검색어 여러 개)으로 근거 조각을 찾는다. embedding = (모델, 질문 벡터). 활성 자료가 없으면 빈 결과"""
    ix, ids = await _index(s)
    dense: list[str] = []
    if embedding and ix.items:
        model, vector = embedding
        dense = await repository.nearest(s, ids, model, vector, bm25.CONF["dense_n"])
    return bm25.search(
        ix, q, queries=queries, trade=trade, k=k, policy_text=policy_text, dense=dense
    )


async def verify_cites(s: AsyncSession, text: str) -> tuple[str, int]:
    """[K번호] 인용 중 문장과 맞지 않는 것을 뗀다. (고친 글, 뗀 수)"""
    return bm25.verify_cites(await index(s), text)


async def suggest_candidates(
    s: AsyncSession,
    question: str,
    *,
    shown: list[str],
    cited: set[str],
    llm: list[str] | tuple[str, ...] = (),
) -> list[suggest.Pick]:
    """이어 물을 질문 후보 (질문 은행 + LLM). 고르기는 suggest_questions"""
    ix = await index(s)
    return await asyncio.to_thread(
        suggest.candidates, ix, question, shown=shown, cited=cited, llm=llm
    )


async def suggest_questions(
    s: AsyncSession,
    question: str,
    *,
    cands: list[suggest.Pick],
    cited: set[str],
    sims: dict[str, float] | None = None,
) -> list[suggest.Pick]:
    """후보에서 답할 수 있고 이어지는 것만 0~3개. sims = 후보 글 → 방금 질문과 뜻 가까움(없으면 글자 규칙)"""
    ix = await index(s)
    return await asyncio.to_thread(suggest.pick, ix, question, cands=cands, cited=cited, sims=sims)
