"""지식 조각 검색 점수 매기기: 글자 두 개 묶음 BM25 + 의미 검색 순위를 RRF로 합친다. DB · HTTP를 모르는 순수 함수.

키워드 부분은 예전 화면 검색(ragSearch7)을 옮겼다. 의미 검색 순위는 부르는 쪽이 DB(pgvector)에서 뽑아 넘긴다.
무게는 evaluation/rag/hybrid.py로 dev 질문(90 + 300 세트의 dev)에서 골랐다 (2026-11-13).
공정 · 공통 · 범위 밖 무게는 evaluation/rag/agent_view.py로 골랐다 (2026-11-16).
"""

import math
import re
from collections.abc import Collection
from dataclasses import dataclass, field

CONF = {
    "version": "rag7-2026.11.16",
    "rrf_k": 60,
    "tier_w": 0.0008,
    "high_w": 0.0004,
    "enough_cov": 0.35,
    "cite_min": 0.18,
    # 의미 검색(임베딩) 순위를 RRF에 더할 때 무게와 볼 개수
    "dense_w": 0.75,
    "dense_n": 30,
    # 질문 공정과 같은 공정 조각에 더하는 점수 (RRF 1위가 1/61 ≈ 0.016)
    "trade_w": 0.004,
    # 공정을 알 때 공통 조각에 더하는 점수와 범위 밖 공정 조각에서 빼는 점수
    "common_w": 0.002,
    "out_w": 0.002,
}
# 공정을 넘겨받지 못하면 질문 낱말로 코드가 고른다 (LLM 없이)
# '방문'은 방문판매 · 실측 방문 · 사전 방문을 빼고 문으로만 본다
TRADE_WORDS = {
    "도배": "도배|벽지|초배|정배|합지|실크|도배풀|풀자국|벽면 시공|퍼티",
    "목공": (
        "목공|목수|목재|몰딩|문틀|문선|(?<!사전)(?<!실측 )방문(?!판매)|문짝|경첩|도어(?!락|록)|석고|"
        "다루끼|각재|가벽|칸막이|천장틀|평탄화|단내림|커튼박스|걸레받이|단열|웨인스코팅|분리대"
    ),
}
# 조각의 공정 표시(16가지 낱말)를 네 갈래로 모은다: 도배 · 목공 · 공통 · 범위 밖
SCOPES = ("도배", "목공")
COMMON_TAGS = frozenset({"전체", "공통"})
COMMON, OUT = "공통", "범위밖"
TIER = {
    "law": 1.0, "gov_notice": 1.0, "gov_guide": 1.0, "gov_qa": 1.0, "kcs": 1.0, "pumsem": 1.0,
    "standard_contract": 1.0, "consumer_agency": 1.0, "official": 1.0, "public_data": 1.0,
    "gov": 0.9, "maker": 0.8, "maker_guide": 0.8, "industry": 0.8, "guide": 0.8, "book": 0.7,
    "news": 0.5, "experience": 0.5, "platform": 0.4, "blog": 0.3, "community": 0.3,
}  # fmt: skip
TYPE_KO = {
    "law": "법령", "gov_notice": "정부 고시", "gov_guide": "정부 안내", "gov_qa": "정부 안내", "kcs": "표준시방서",
    "pumsem": "표준품셈", "standard_contract": "표준계약서", "consumer_agency": "소비자원", "official": "공공기관",
    "public_data": "공공 자료", "gov": "공공기관", "maker": "제조사", "maker_guide": "제조사", "industry": "학회 · 협회",
    "guide": "공공기관", "book": "시공 안내서", "news": "뉴스", "experience": "경험담 · 후기", "platform": "후기 · 문답",
    "blog": "블로그", "community": "후기 · 문답",
}  # fmt: skip
_CASUAL = ("blog", "community", "platform", "experience")
POLICY = {
    "law": (
        "법|계약|하자|분쟁|소송|보증|배상|위약|등록|신고|허가|규약|민원|책임|환불|지체상금",
        _CASUAL,
    ),
    "safety": (
        "안전|석면|화재|가스|누전|감전|누수|곰팡이|환기|유해|새집증후군|폼알데|라돈|추락",
        _CASUAL,
    ),
    "money": (
        "얼마|비용|가격|금액|견적|단가|평당|예산|싸|비싸|부가세|계약금|잔금|아껴|절약",
        _CASUAL[:3],
    ),
}
SYN = [
    ["벽지", "도배"], ["걸레받이", "굽도리"], ["샷시", "창호", "섀시"], ["장판", "바닥재"], ["마루", "바닥재"],
    ["계약금", "선금"], ["하자", "보수", "AS"], ["실측", "현장 측정"], ["반셀프", "직영", "직접 발주"], ["턴키", "일괄"],
    ["철거", "뜯기"], ["문선", "문틀"], ["필름", "시트지"], ["페인트", "도장"], ["줄눈", "메지"], ["퍼티", "빠데"],
    ["초배", "밑지"], ["관리사무소", "관리실"], ["승강기", "엘리베이터"], ["보양", "보호"], ["덧방", "덧바르기"],
    ["합지", "종이벽지"], ["실크", "PVC 벽지"],
]  # fmt: skip
K1, B = 1.2, 0.75
_NOT_WORD = re.compile(r"[^0-9a-z가-힣]+")
_ASCII = re.compile(r"[0-9a-z]+")
REF = re.compile(r"K\d{1,5}")


@dataclass(frozen=True)
class Doc:
    title: str = ""
    pub: str = ""
    date: str = ""
    kind: str = ""
    rel: str = ""
    url: str = ""
    effective_date: str = ""


@dataclass(frozen=True)
class Chunk:
    ref: str
    doc: str
    text: str
    sec: str = ""
    hints: tuple[str, ...] = ()
    trades: tuple[str, ...] = ()
    rid: str = ""  # DB 행: dataset_key/record_key (의미 검색 결과를 조각에 잇는다)
    effective_date: str = ""


@dataclass(eq=False)
class Item:
    chunk: Chunk
    doc: Doc
    tf: dict[str, int]
    length: int
    scope: tuple[str, ...] = ()


@dataclass
class Index:
    items: list[Item]
    df: dict[str, int]
    avg: float
    version: str = ""
    by_ref: dict[str, Item] = field(default_factory=dict)
    by_rid: dict[str, Item] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class Hit:
    ref: str
    source_type: str
    title: str
    publisher: str
    date: str
    section: str
    text: str
    score: float
    url: str = ""
    official: bool = False  # 출처 등급 0.8 이상 (공공 · 표준 · 제조사 · 협회)


@dataclass(frozen=True)
class Result:
    hits: list[Hit]
    enough: bool
    policy: list[str]


def scope_of(trades: tuple[str, ...]) -> tuple[str, ...]:
    """도배 · 목공 표시가 있으면 그 공정들, 없고 전체 · 공통뿐이거나 비었으면 공통, 다른 공정뿐이면 범위 밖"""
    own = tuple(t for t in SCOPES if t in trades)
    if own:
        return own
    return (COMMON,) if set(trades) <= COMMON_TAGS else (OUT,)


def grams(text: str) -> list[str]:
    out: list[str] = []
    for w in _NOT_WORD.sub(" ", (text or "").lower()).split(" "):
        if not w:
            continue
        if _ASCII.fullmatch(w) or len(w) == 1:
            out.append(w)
            continue
        out.extend(w[i : i + 2] for i in range(len(w) - 1))
    return out


def build(chunks: list[Chunk], docs: dict[str, Doc], version: str = "") -> Index:
    items: list[Item] = []
    df: dict[str, int] = {}
    total = 0
    for c in chunks:
        d = docs.get(c.doc) or Doc()
        hints = " ".join(c.hints)
        toks = grams(f"{c.text} {c.sec} {hints} {d.title}")
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        for t in tf:
            df[t] = df.get(t, 0) + 1
        items.append(Item(c, d, tf, len(toks), scope_of(c.trades)))
        total += len(toks)
    avg = total / max(len(items), 1)
    return Index(
        items, df, avg, version, {x.chunk.ref: x for x in items}, {x.chunk.rid: x for x in items}
    )


def _uniq(xs) -> list:
    return list(dict.fromkeys(xs))


def _bm25(ix: Index, q: str, trade: str | None = None, n: int = 30, only=None) -> list[Item]:
    qs = _uniq(grams(q))
    out: list[tuple[float, Item]] = []
    for x in ix.items:
        if only and not only(x):
            continue
        s = 0.0
        for t in qs:
            f = x.tf.get(t)
            if not f:
                continue
            m = ix.df.get(t, 0)
            idf = math.log(1 + (ix.n - m + 0.5) / (m + 0.5))
            s += (idf * (f * (K1 + 1))) / (f + K1 * (1 - B + (B * x.length) / ix.avg))
        if trade and trade in x.scope:
            s *= 1.15
        if s > 0:
            out.append((s, x))
    out.sort(key=lambda p: -p[0])
    return [x for _, x in out[:n]]


def _syn_query(q: str) -> str | None:
    alt, hit = q or "", False
    for g in SYN:
        w = next((v for v in g if v in alt), None)
        if w:
            alt = alt.replace(w, next(v for v in g if v != w), 1)
            hit = True
    return alt if hit else None


def policy_of(q: str) -> tuple[list[str], set[str]]:
    kinds = [k for k, (pat, _) in POLICY.items() if re.search(pat, q or "")]
    return kinds, {e for k in kinds for e in POLICY[k][1]}


def _coverage(q: str, x: Item) -> float:
    qs = _uniq(grams(q))
    return sum(1 for t in qs if t in x.tf) / len(qs) if qs else 0.0


def trades_of(q: str) -> list[str]:
    """질문 낱말에 걸리는 공정들"""
    return [t for t, pat in TRADE_WORDS.items() if re.search(pat, q or "")]


def trade_of(q: str) -> str | None:
    """공정 하나만 걸리면 그 공정. 둘 다 걸리거나 안 걸리면 고르지 않는다"""
    hit = trades_of(q)
    return hit[0] if len(hit) == 1 else None


def search(
    ix: Index,
    q: str,
    *,
    queries: list[str] | tuple[str, ...] = (),
    trade: str | None = None,
    k: int = 6,
    policy_text: str | None = None,
    dense: list[str] | tuple[str, ...] = (),
) -> Result:
    """검색어 여러 개(+ 동의어)를 BM25로 찾고, 의미 검색 순위(dense: DB 행 키 rid 목록)와 RRF로 합친다.
    법 · 안전 · 금액 질문은 블로그 · 후기를 뺀다"""
    if not ix.items:
        return Result([], False, [])
    main = (q or "")[:80]
    qs_all = _uniq(x for x in [main, *(str(x)[:80] for x in queries)] if x)[:4]
    syn = _syn_query(main)
    if syn and syn not in qs_all:
        qs_all.append(syn)
    kinds, exclude = policy_of(policy_text or main)
    tr = trade or trade_of(" ".join(qs_all))
    fused: dict[Item, float] = {}
    for qq in qs_all:
        for r, x in enumerate(_bm25(ix, qq, tr)):
            if x.doc.kind in exclude:
                continue
            fused[x] = fused.get(x, 0.0) + 1 / (CONF["rrf_k"] + r + 1)
    if CONF["dense_w"]:
        near = [ix.by_rid[r] for r in dense if r in ix.by_rid][: CONF["dense_n"]]
        for r, x in enumerate(x for x in near if x.doc.kind not in exclude):
            fused[x] = fused.get(x, 0.0) + CONF["dense_w"] / (CONF["rrf_k"] + r + 1)
    my_trades = [trade] if trade else trades_of(" ".join(qs_all))

    def bonus(x: Item) -> float:
        b = CONF["tier_w"] * TIER.get(x.doc.kind, 0.5) + (
            CONF["high_w"] if x.doc.rel == "high" else 0
        )
        if my_trades:
            if any(t in my_trades for t in x.scope):
                b += CONF["trade_w"]
            elif x.scope == (COMMON,):
                b += CONF["common_w"]
            elif x.scope == (OUT,):
                b -= CONF["out_w"]
        return b

    ranked = [(s + bonus(x), x) for x, s in fused.items()]
    ranked.sort(key=lambda p: -p[0])
    ranked = ranked[:k]
    best = max(_coverage(qq, ranked[0][1]) for qq in qs_all) if ranked else 0.0
    hits = [
        Hit(
            ref=x.chunk.ref,
            source_type=TYPE_KO.get(x.doc.kind, "기타"),
            title=x.doc.title,
            publisher=x.doc.pub,
            date=x.doc.date,
            section=x.chunk.sec,
            text=x.chunk.text,
            score=round(s * 1000, 1),
            url=x.doc.url,
            official=TIER.get(x.doc.kind, 0.5) >= 0.8,
        )
        for s, x in ranked
    ]
    return Result(hits, best >= CONF["enough_cov"], kinds)


def cite_matches(sentence_grams: set[str], chunk_grams: Collection[str]) -> bool:
    return bool(sentence_grams) and (
        sum(1 for token in sentence_grams if token in chunk_grams) / len(sentence_grams)
        >= CONF["cite_min"]
    )


def verify_cites(ix: Index, text: str) -> tuple[str, int]:
    """문장과 인용 조각의 글자 두 개 묶음 겹침이 cite_min보다 적으면 그 [K] 표시를 뗀다"""
    dropped = 0
    lines = []
    for line in (text or "").split("\n"):
        if not REF.search(line):
            lines.append(line)
            continue
        sg = set(grams(re.sub(r"\[K\d{1,5}\]", "", line)))

        def keep(m: re.Match, sg: set[str] = sg) -> str:
            nonlocal dropped
            x = ix.by_ref.get(m.group(1))
            if not x or not sg:
                return m.group(0)
            if not cite_matches(sg, x.tf):
                dropped += 1
                return ""
            return m.group(0)

        lines.append(re.sub(r"\[(K\d{1,5})\]", keep, line))
    return "\n".join(lines), dropped
