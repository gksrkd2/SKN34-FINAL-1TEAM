"""v3 검색: 지금 키워드 검색(bm25)을 12공종 무게로 돌린다. DB · HTTP를 모르는 순수 함수.

bm25.search는 도배 · 목공 두 공정만 알아서 다른 공종 조각을 '범위 밖'으로 깎는다(v2 규칙, 그대로 둠).
여기서는 조각의 공정 표시를 부르는 쪽이 넘긴 표(tags: 표시 → 공종 코드)로 읽어,
대상 공종과 공통 자료만 검색하고 기존 공종·공통 무게를 더한다.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from app.knowledge import bm25

_SOURCE_KINDS = (
    (r"\bKCS\b", {"kcs"}),
    (r"\bLHCS\b", {"official"}),
    (r"표준품셈|품셈", {"pumsem"}),
    (r"법령|법률|시행령", {"law"}),
    (r"고시", {"gov_notice"}),
    (r"제조사|제품 설명서", {"maker", "maker_guide"}),
    (r"블로그", {"blog"}),
    (r"후기|경험담", {"community", "experience", "platform"}),
    (r"시공\s*안내서", {"book"}),
    (r"공공\s*(?:기관|자료|기준)", {kind for kind, tier in bm25.TIER.items() if tier >= 0.9}),
)
_DATE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})(?:[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})|년(?:\s*(\d{1,2})월\s*(\d{1,2})일)?)"
)
_DOC_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})")


def _date(year: str, month: str, day: str) -> date | None:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _doc_date(text: str) -> date | None:
    match = _DOC_DATE.search(text)
    return _date(*match.groups()) if match else None


@dataclass(frozen=True)
class Limits:
    kinds: frozenset[str]
    excluded: frozenset[str]
    cutoff: date | None
    as_of: date | None
    years: frozenset[int]

    @property
    def dated(self) -> bool:
        return bool(self.as_of or self.years)

    def allows_kind(self, kind: str) -> bool:
        return kind not in self.excluded and (not self.kinds or kind in self.kinds)

    def allows(self, item: bm25.Item) -> bool:
        published = _doc_date(item.doc.date)
        effective = _doc_date(item.chunk.effective_date or item.doc.effective_date)
        if self.cutoff and any(d and d > self.cutoff for d in (published, effective)):
            return False
        applied = effective or published
        if self.dated and not applied:
            return False
        return self.allows_kind(item.doc.kind) and (
            not self.years or bool(applied and applied.year in self.years)
        )


def limits(q: str, today: date | None, excluded: frozenset[str] = frozenset()) -> Limits:
    _, blocked = bm25.policy_of(q)
    kinds = frozenset(
        k for pattern, kinds in _SOURCE_KINDS if re.search(pattern, q, re.IGNORECASE) for k in kinds
    )
    if "시방서" in q and not kinds.intersection({"kcs", "official"}):
        kinds |= {"kcs", "official"}
    dates = []
    years = set()
    for year, month, day, ko_month, ko_day in _DATE.findall(q):
        if month or ko_month:
            if d := _date(year, month or ko_month, day or ko_day):
                dates.append(d)
        else:
            years.add(int(year))
    as_of = min(dates, default=None)
    cutoff = min((d for d in (today, as_of) if d), default=None)
    return Limits(kinds, frozenset(blocked) | excluded, cutoff, as_of, frozenset(years))


def scope(item: bm25.Item, tags: Mapping[str, str]) -> frozenset[str]:
    return frozenset(t for label in item.chunk.trades if (t := tags.get(label)) and t != "common")


def scopes(ix: bm25.Index, tags: Mapping[str, str]) -> dict[str, frozenset[str]]:
    return {x.chunk.ref: scope(x, tags) or frozenset({"common"}) for x in ix.items}


def search(
    ix: bm25.Index,
    q: str,
    *,
    trades: list[str] | tuple[str, ...] = (),
    tags: Mapping[str, str],
    k: int = 6,
    policy_text: str | None = None,
    exclude_kinds: frozenset[str] = frozenset(),
    today: date | None = None,
) -> bm25.Result:
    """검색어(+ 동의어)를 BM25로 찾아 RRF로 합치고 출처 등급 · 공종 무게를 더한다.

    법 · 안전 · 금액 질문은 블로그 · 후기를 뺀다(bm25.POLICY). exclude_kinds: 더 뺄 출처 종류(예: 책).
    """
    if not ix.items:
        return bm25.Result([], False, [])
    main = (q or "")[:80]
    qs_all = [main]
    syn = bm25._syn_query(main)
    if syn and syn not in qs_all:
        qs_all.append(syn)
    original = policy_text or q
    kinds, _ = bm25.policy_of(original)
    restriction = limits(original, today, exclude_kinds)
    want = set(trades) - {"common"}
    fused: dict[bm25.Item, float] = {}
    for qq in qs_all:
        for r, x in enumerate(bm25._bm25(ix, qq, None)):
            item_scope = scope(x, tags)
            if not restriction.allows(x) or (want and item_scope and not (want & item_scope)):
                continue
            fused[x] = fused.get(x, 0.0) + 1 / (bm25.CONF["rrf_k"] + r + 1)

    def bonus(x: bm25.Item) -> float:
        b = bm25.CONF["tier_w"] * bm25.TIER.get(x.doc.kind, 0.5)
        b += bm25.CONF["high_w"] if x.doc.rel == "high" else 0.0
        if want:
            b += bm25.CONF["trade_w"] if scope(x, tags) else bm25.CONF["common_w"]
        return b

    ranked = sorted(((s + bonus(x), x) for x, s in fused.items()), key=lambda p: -p[0])[:k]
    best = max(bm25._coverage(qq, ranked[0][1]) for qq in qs_all) if ranked else 0.0
    hits = [
        bm25.Hit(
            ref=x.chunk.ref,
            source_type=bm25.TYPE_KO.get(x.doc.kind, "기타"),
            title=x.doc.title,
            publisher=x.doc.pub,
            date=x.doc.date,
            section=x.chunk.sec,
            text=x.chunk.text,
            score=round(s * 1000, 1),
            url=x.doc.url,
            official=bm25.TIER.get(x.doc.kind, 0.5) >= 0.8,
        )
        for s, x in ranked
    ]
    return bm25.Result(hits, best >= bm25.CONF["enough_cov"], kinds)
