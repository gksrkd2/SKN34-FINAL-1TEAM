"""도배 상담에 쓰는 키워드 검색."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.knowledge import bm25, search3

from .question import FURNITURE, ONE_WALL, Conditions, conditions_for

if TYPE_CHECKING:
    from app.agent.v3.answer import KB

_TILE_WALLPAPER = re.compile(
    r"(?:타일\s*(?:벽(?:면)?|바탕(?:면)?)\s*(?:위|에|의)|타일\s*위)"
    r"[^\n.?!]{0,45}(?:도배|벽지)"
)
_FURNITURE_PREP = re.compile(r"옮기|이동|보양|보호|덮어")
_PARTIAL_WALLPAPER = re.compile(r"부분\s*(?:도배|시공|보수)")


def _search_query(question: str, conditions: Conditions) -> str:
    # 기존 검색은 앞 80자만 쓰므로 시공 대상 바탕을 앞에 보탠다.
    if conditions.tile_surface:
        return f"타일 바탕면 {question}"
    terms = []
    if conditions.lifting:
        terms.append("벽지 들뜸 박리 바탕면")
    if conditions.moisture:
        terms.append("벽지 습기 바탕면 점검")
    if conditions.furniture:
        terms.append("거주 중 도배 가구 이동 보양")
    if conditions.one_wall:
        terms.append("부분 도배 벽 한 면 경계 마감")
    return " ".join([*terms, question])


def is_tile_wallpaper_evidence(kb: KB, ref: str) -> bool:
    item = kb.ix.by_ref.get(ref)
    if item is None or item.doc.kind not in {
        "kcs",
        "official",
        "gov_guide",
        "maker",
        "maker_guide",
    }:
        return False
    scope = search3.scopes(kb.ix, kb.tags).get(ref, frozenset())
    if "dobae" not in scope and scope != {"common"}:
        return False
    # 바닥 타일에 접한 일반 벽의 기준과 타일 자체에 도배하는 기준을 구분한다.
    return bool(_TILE_WALLPAPER.search(item.chunk.text))


def retrieve(kb: KB, question: str) -> bm25.Result:
    conditions = conditions_for(kb, question)
    query = _search_query(conditions.lookup_text or question, conditions)
    # 검색어를 보강해도 출처 제한은 고객의 원래 질문으로 판단한다.
    result = search3.search(
        kb.ix,
        query,
        trades=["dobae"],
        tags=kb.tags,
        k=8,
        policy_text=question if query != question else None,
    )
    if conditions.tile_surface:
        hits = [hit for hit in result.hits if is_tile_wallpaper_evidence(kb, hit.ref)]
    elif conditions.furniture or conditions.one_wall:
        hits = []
        for hit in result.hits:
            text = kb.ix.by_ref[hit.ref].chunk.text
            if conditions.furniture and not (
                FURNITURE.search(text) and _FURNITURE_PREP.search(text)
            ):
                continue
            if conditions.one_wall and not (
                _PARTIAL_WALLPAPER.search(text) or ONE_WALL.search(text)
            ):
                continue
            hits.append(hit)
    else:
        return result
    enough = bool(hits and result.enough and hits[0].ref == result.hits[0].ref)
    return bm25.Result(hits, enough, result.policy)
