"""입구 관문 v3 (코드, LLM 없음). agents.md 10.1절 순서.

1. 버튼 선택 · 정책 표시 · 분명한 범위 밖
2. 새 공종·복합 질문이나 다른 의도가 분명하면 새로 분류하고, 나머지는 기다리던 담당에게
3. 12공종 낱말 + 문장 나누기 → 여러 조각 · 여러 공종이면 multi
4. 의도 분류기(intent_lr_v1): 신뢰도 ≥ 기준이면 받고, 범위 밖인데 공종 낱말이 있으면 trade로 바꾼다
5. 기준 아래면 규칙, 규칙도 없으면 보던 화면으로 동점 깨기, 그래도 없으면 후보 2~3개 버튼(되묻기)
Jev는 무료 모드에서 부르지 않는다(L21). 되묻기는 한 번이고 다음은 버튼 선택지로 끝낸다.
"""

import re
from itertools import pairwise

from app.agent import intent as clf
from app.agent.v3 import policy, trades
from app.agent.v3.schemas import Button, Choice, GateResult, Route, TradeCode, View

THRESHOLD = 0.7  # settings.intent_confidence_threshold와 같은 값(E1 기준값 곡선으로 다시 봄)
LABEL_ROUTE: dict[str, Route] = {
    "orchestrator": "guide",
    "trade": "trade",
    "quote_explanation": "quote_explanation",
    "schedule": "schedule",
    "out_of_scope": "out_of_scope",
}
NEED_PROJECT: tuple[Route, ...] = ("quote_explanation", "schedule")

QUOTE_MINE = re.compile(
    r"표준\s*견적|가견적|실측\s*견적|견적서|견적\s*(?:이|가|에서|은|받았|차이)|합계|부가세|줄\s*하나|이\s*줄|"
    r"금액이\s*왜|왜\s*(?:이렇게|이\s*금액)|업체\s*(?:견적|가견적)이?\s*(?:왜|달라|차이)"
)
SCHEDULE_MINE = re.compile(
    r"일정|날짜|공정표|연기|미뤄|미루|언제\s*(?:시작|끝|와|들어|오)|끝나는\s*날|시작하는\s*날|"
    r"실측\s*(?:날|일정|요청|와|오|받)|희망\s*기간|반나절|"
    r"언제.{0,8}(?:오|와|들어)"
)
GUIDE_WORDS = re.compile(
    r"처음|순서|뭐부터|어떻게\s*진행|믿집|가입|뜻이|뭐예요|뭔가요|뭐에요|무슨\s*뜻|계약|관리사무소|"
    r"하자|업체\s*(?:고르|선택)|고르는|준비|체크|확인할\s*것|안녕|반갑|영수증|세금계산서"  # 영수증 · 세금계산서: 계약 · 비용(공통)
)
WHEN = re.compile(r"언제|날짜|며칠|몇\s*시|오전|오후|주말")
VAGUE = re.compile(r"^\s*(?:이거|이건|저거|여기|이\s*줄|이\s*금액|이게|왜)")
# 문장 나누기: 물음 · 이음말. '하고 · 랑'은 공종 낱말 사이에 있을 때만 나눈다(orchestrator.split)
# 공백 반복에 상한을 둬서 공백이 길게 이어진 입력에도 한 번만 훑어요(CodeQL ReDoS)
BREAK = re.compile(r"(?<=[?？!])\s*|\s{0,3}(?:그리고|그럼|그러면|아울러|또한|그다음에)\s{1,3}|\n+")

CHOICE_TEXT: dict[str, str] = {
    "guide": "처음이라 순서를 알고 싶어요",
    "trade": "공사 방법 · 자재가 궁금해요",
    "quote_explanation": "견적 금액이 왜 이런지 궁금해요",
    "schedule": "공사 · 실측 날짜가 궁금해요",
}
QUOTE_VIEWS: tuple[View, ...] = ("standard", "standard_guest", "quotes", "vendors", "vendor")
SCHEDULE_VIEWS: tuple[View, ...] = ("visits", "visit_request", "pick", "plan", "schedule")


def parts_of(text: str) -> list[str]:
    """물음 · 이음말로 나눈 조각(빈 것 빼고, 최대 3개)."""
    got = [p.strip(" ,.") for p in BREAK.split(text or "") if p and p.strip(" ,.")]
    return got[:3] if got else [(text or "").strip()]


def _strip(text: str, ignore: list[str]) -> str:
    for w in ignore:
        if w:
            text = text.replace(w, " ")
    return text


def _signals(text: str, found: list[str]) -> dict[str, bool]:
    return {
        "quote_explanation": bool(QUOTE_MINE.search(text)),
        "schedule": bool(SCHEDULE_MINE.search(text)),
        "trade": bool(found),
    }


# 두 공종 낱말 사이의 이음말('몰딩 바꾸고 도배도', '타일이랑 바닥', '도배, 장판'). 이음말 없이 붙은 '욕실 타일'은 한 공종 질문
JOIN = re.compile(r"고\s|하고|랑\s|이랑|그리고|및|,|·|도\s*(?:같이|함께)|이며")


def joined(text: str, found: list[str]) -> bool:
    spans = trades.mention_spans(text, found)
    return any(JOIN.search(text[a[1] : b[0]]) for a, b in pairwise(spans))


GREETING = re.compile(r"^\s*(?:안녕|하이|헬로|hi\b|hello|저기요|여보세요|반가)", re.IGNORECASE)
# 공통 지식 주제(계약 · 하자 · 관리사무소 · 이사 · 청소 · 예산). 분류기가 범위 밖이라 해도 가이드가 답한다
COMMON_TOPIC = re.compile(
    r"하자|계약|잔금|계약금|현금|영수증|세금계산서|부가세|보수|입주\s*청소|청소|새집|관리사무소|관리실|이사|예산|공사비|세입자|집주인|"
    r"원상복구|분쟁|업체\s*말|업체가|"
    r"동의서|이웃|옆집|앞집|아랫집|윗집|복도|소음|공사\s*(?:팀|시간)|치수|빈집"  # 이웃 · 공사 예절 · 입주 전(초보 질문 dev)
)


def rule_route(text: str, found: list[str]) -> Route | None:
    """분류기가 자신 없을 때 쓰는 낱말 규칙(라벨 정의에서 바로 씀)."""
    if policy.out_of_scope(text, bool(found)):
        return "out_of_scope"
    sig = _signals(text, found)
    if sum(sig.values()) >= 2 and (len(parts_of(text)) >= 2 or JOIN.search(text)):
        return "multi"
    for route, hit in sig.items():
        if hit:
            return route  # type: ignore[return-value]
    if GUIDE_WORDS.search(text) or policy.FLAGS["price"].search(text):
        return "guide"
    return None


def _candidates(probs: dict[str, float] | None) -> list[Choice]:
    order = sorted((probs or {}).items(), key=lambda p: -p[1])
    routes = [LABEL_ROUTE[k] for k, _ in order if LABEL_ROUTE[k] in CHOICE_TEXT]
    for r in ("guide", "trade", "quote_explanation"):  # 모자라면 기본 후보로 채운다
        if r not in routes:
            routes.append(r)
    return [Choice(text=CHOICE_TEXT[r], route=r) for r in routes[:3]]


def gate(
    text: str,
    *,
    view: View | None = None,
    waiting: Route | None = None,
    waiting_trade: TradeCode | None = None,
    button: Button | None = None,
    has_project: bool = False,
    ignore: list[str] | None = None,
) -> GateResult:
    """text는 가린 뒤의 고객 글. ignore: 업체 이름처럼 공종 낱말로 읽으면 안 되는 말(예: '마루와벽')."""
    plain = _strip(text or "", ignore or [])
    found = trades.trades_in(plain)
    flags = policy.policy_of(plain)
    if button is not None:
        tr = [button.trade] if button.trade else found
        return GateResult(
            route=button.route, trades=tr, policy=flags, confidence=1.0, source="button"
        )

    probs = clf.probs(plain)
    label, conf = max(probs.items(), key=lambda p: p[1]) if probs else (None, 0.0)
    model_route = LABEL_ROUTE.get(label) if label else None

    def done(route: Route, source: str, c: float = conf, keep: bool = False) -> GateResult:
        if route == "guide" and found:
            route = "trade"  # 공종이 분명하면 그 공종 담당이 답한다(가이드는 공통 · 처음 안내). 이음말 복합은 위에서 multi
        if route in NEED_PROJECT and not has_project:
            route = "guide"  # 내 견적 · 공정표가 없으면 가이드가 시작 순서로 안내해요
        if route == "out_of_scope" and found and not keep:
            route = "trade"  # v3: 다른 공종은 범위 밖이 아니라 공종 지식 답
        if route == "out_of_scope" or route == "ask_back":
            tr: list = []
        else:
            tr = list(found)
        return GateResult(
            route=route, trades=tr, policy=flags, confidence=round(float(c), 3),
            source=source, label=label,  # type: ignore[arg-type]
            candidates=_candidates(probs) if route == "ask_back" else [],
        )  # fmt: skip

    # 범위 밖: 믿집이 하지 않는 일 · 10공종 견적 계산 요청
    if policy.out_of_scope(plain, bool(found)):
        return done("out_of_scope", "rule", keep=True)
    if policy.calc_request(plain) and found and not trades.BY_CODE[found[0]].calc:
        return done("out_of_scope", "rule", keep=True)

    sig = _signals(plain, found)
    parts = parts_of(plain)
    part_trades = [trades.trades_in(p) for p in parts]
    many_parts = (
        len([p for p in part_trades if p]) >= 2 and len({t for p in part_trades for t in p}) >= 2
    )
    multi_trade = len(found) >= 2 and (many_parts or joined(plain, found))

    if "vendor_lookup" in flags and len(parts) == 1 and not multi_trade:
        return done("guide", "rule", 1.0)

    changed_trade = waiting_trade is not None and any(t != waiting_trade for t in found)
    moved = model_route is not None and model_route != waiting and conf >= THRESHOLD
    if (
        waiting is not None
        and waiting not in ("ask_back", "out_of_scope")
        and not (changed_trade or multi_trade or moved)
    ):
        return done(waiting, "waiting")

    if GREETING.search(plain):
        return done("guide", "rule")

    if model_route is not None and conf >= THRESHOLD:
        route = model_route
        if (route == "guide" and (sum(sig.values()) >= 2 or multi_trade)) or (
            route == "trade" and multi_trade
        ):
            route = "multi"
        elif route == "out_of_scope" and found:
            route = "multi" if multi_trade else "trade"
        elif (
            route in ("schedule", "quote_explanation")
            and sig["schedule"]
            and JOIN.search(plain)
            and (sig["quote_explanation"] or "price" in flags)
        ):
            route = "multi"  # '날짜랑 비용' · '견적서랑 일정'처럼 두 담당을 이어 물음
        elif route == "out_of_scope" and COMMON_TOPIC.search(plain):
            route = "guide"  # v1 분류기는 계약 · 결제 · 청소 문장을 범위 밖으로 배웠다. v3는 공통 지식으로 답한다
        return done(route, "model")

    ruled = rule_route(plain, found)
    if ruled == "trade" and multi_trade:
        ruled = "multi"
    if ruled is not None:
        return done(ruled, "rule")
    if view in QUOTE_VIEWS and (VAGUE.search(plain) or flags):
        return done("quote_explanation", "rule")
    if view in SCHEDULE_VIEWS and (VAGUE.search(plain) or "period" in flags or WHEN.search(plain)):
        return done("schedule", "rule")
    # 공통 지식 주제(이웃 · 동의서 · 잔금 …)는 일반 메뉴로 되묻지 않고 가이드가 답해요
    if COMMON_TOPIC.search(plain):
        return done("guide", "rule")
    if flags and model_route and model_route != "out_of_scope":
        return done(model_route, "model")
    return done("ask_back", "model")
