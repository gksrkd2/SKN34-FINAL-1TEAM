"""지식 자료 행(CSV 원본 = rag_chunks.provenance) → 검색 조각. DB를 모른다.

조각 번호(ref) 규칙
- 행에 `ref` 열이 있으면 그 번호 (화면 자료 K1~ · 로컬 추가 자료 K5001~)
- 없으면 팀 전처리 자료: dataset_key 순 → id 순으로 K2001부터
"""

import json

from app.knowledge.bm25 import Chunk, Doc

TEAM_START = 2001
# 팀 전처리 자료 → 출처 종류 · 공정. 없는 자료는 공공기관 · 공정 없음
TEAM: dict[str, tuple[str, tuple[str, ...]]] = {
    "04_lhcs_gypsum_rag": ("official", ("목공",)),
    "05_lhcs_wallpaper_rag": ("official", ("도배",)),
    "07_housing_management_rules_rag": ("official", ()),
    "08_kcs_carpentry_rag": ("kcs", ("목공",)),
    "09_kcs_insulation_rag": ("kcs", ("목공",)),
    "10_kcs_condensation_rag": ("kcs", ("목공", "도배")),
    "11_kcs_wallpaper_rag": ("kcs", ("도배",)),
    "12_housing_bylaws_commentary_rag": ("official", ()),
}


def _split(v: str | None) -> tuple[str, ...]:
    return tuple(x for x in (v or "").split(";") if x)


def _sec(r: dict[str, str], known: bool = True) -> str:
    """절 뒤에 쪽 번호. 쪽을 모르는 행(page_known false, page는 임시값 1)은 절만"""
    page = (r.get("page") or "") if known else ""
    return " · ".join(x for x in (r.get("section") or "", f"{page}쪽" if page else "") if x)


def _own(key: str, r: dict[str, str]) -> tuple[Chunk, Doc]:
    doc = Doc(
        title=r["source"], pub=r.get("source_org") or "", date=r.get("doc_date") or "",
        kind=r.get("source_type") or "", rel=r.get("rel") or "", url=r.get("source_url") or "",
    )  # fmt: skip
    chunk = Chunk(
        ref=r["ref"], doc=r["doc"], text=r["content"], sec=_sec(r, r.get("page_known") == "true"),
        hints=tuple(json.loads(r.get("hints") or "[]")), trades=_split(r.get("trades")),
        rid=f"{key}/{r['id']}", effective_date=r.get("effective_date") or "",
    )  # fmt: skip
    return chunk, doc


def assemble(
    datasets: list[tuple[str, list[dict[str, str]]]],
) -> tuple[list[Chunk], dict[str, Doc]]:
    """(dataset_key, 원본 행들) 목록 → 번호 순 조각과 문서. 번호가 겹치면 ValueError"""
    chunks: list[Chunk] = []
    docs: dict[str, Doc] = {}
    k = TEAM_START
    for key, rows in sorted(datasets, key=lambda d: d[0]):
        if not rows:
            continue
        if "ref" in rows[0]:
            for r in rows:
                c, d = _own(key, r)
                docs.setdefault(c.doc, d)
                chunks.append(c)
            continue
        kind, trades = TEAM.get(key, ("official", ()))
        doc = f"team:{key}"
        rows = sorted(rows, key=lambda r: r["id"])
        docs[doc] = Doc(
            title=rows[0]["source"].replace(".pdf", ""), pub=rows[0].get("source_org") or "",
            kind=kind, rel="high", url=rows[0].get("source_url") or "",
            date=rows[0].get("doc_date") or "",
        )  # fmt: skip
        for r in rows:
            chunks.append(
                Chunk(ref=f"K{k}", doc=doc, text=r["content"], sec=_sec(r), trades=trades,
                      rid=f"{key}/{r['id']}", effective_date=r.get("effective_date") or "")
            )  # fmt: skip
            k += 1
    chunks.sort(key=lambda c: int(c.ref[1:]))
    refs = [c.ref for c in chunks]
    if len(refs) != len(set(refs)):
        raise ValueError("지식 조각 번호가 겹쳐요")
    return chunks, docs
