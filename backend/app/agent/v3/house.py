"""이 집 답 모양(agent-08 2-0 · 2-0b의 무료 모드 판): 결론 한 줄 · 이 집 견적서 카드 · 업체에 요청할 말.

- 결론: 담당이 만든 첫 문장을 그대로 올린다(새 글을 짓지 않음).
- 이 집 견적서 카드: 질문과 맞는 표준견적서 줄을 계산 엔진 값 그대로 옮긴다(수량 · 단가 · 금액). 금액을 만들지 않는다.
- 업체에 요청할 말: 정해 둔 정중한 부탁 2줄. 숫자는 그 줄의 계산 값 글자만, 계산 값이 없으면 숫자를 쓰지 않는다.
  판정(하자예요 · 보상하세요)은 쓰지 않는다. 전문 공종이 제공한 문구를 쓰고, 기존 문구는 도배 · 목공 · 견적서 줄 질문에 붙인다.
"""

import re

from app.agent.v3.answer import Task
from app.agent.v3.quote_explain import match_line
from app.agent.v3.schemas import AgentAnswer, CompareTable, StandardLineFact, TableRow, UiCard
from app.agent.v3.templates import EXTRACT_SUMMARY_LEAD, josa

ON_ROUTES = ("trade", "quote_explanation")
ON_TRADES = ("dobae", "mokgong")
PROBLEM = re.compile(
    r"곰팡|들뜸|들떠|들뜨|금\s*갔|갈라|누수|물\s*자국|결로|썩|깨짐|깨졌|벗겨|안\s*닫|흔들|그을|냄새|벌어|빈\s*소리|하자|보수|울어|운\s*곳"
)
# 고객이 물은 작업 이름(질문 글자 그대로 쓴다). 긴 것부터
WORK = re.compile(
    r"(퍼티\s*작업|빠데\s*작업|퍼티|빠데|핸디코트|초배지?|정배|부분\s*도배|천장\s*도배|철거|몰딩|걸레받이|문선|석고\s*보드|평탄화|단열\s*벽지|곰팡이\s*방지\s*처리|합지|실크\s*벽지|실크)"
)
TRADE_WORD = re.compile(r"도배|목공")
MONEY = re.compile(
    r"견적|얼마(?!나\s*(?:가|걸|버텨|써|오래|지나|갈))|금액|비용|단가|수량|합계|비싸|싸요|왜\s*\d|몇\s*(?:평|㎡|품)|\d\s*품|평이|㎡"
)
# 기간 · 보증을 묻는 말(문제 낱말이 있어도 원인 확인 부탁을 쓰지 않음)
PERIOD = re.compile(r"기간|언제|며칠|몇\s*(?:년|개월|달)|보증|얼마나\s*(?:가|돼|걸려|버텨|써)")
# 문제 낱말 → 업체에 말할 이름
PROBLEM_NAME = {
    "곰팡": "곰팡이",
    "들뜸": "벽지 들뜸",
    "들떠": "벽지 들뜸",
    "들뜨": "벽지 들뜸",
    "누수": "물 새는 곳",
    "물": "물 자국",
    "결로": "물방울 맺힘(결로)",
    "갈라": "갈라진 곳",
    "금": "금 간 곳",
    "벌어": "벌어진 틈",
    "울어": "벽지가 운 곳",
    "운": "벽지가 운 곳",
}
STATE_NOTE = {
    "included": "합계에 들어가요",
    "bundled": "다른 줄에 묶여 있어요",
    "separate": "합계에 따로 들어가요",
    "optional": "고르면 더해요",
    "after_visit": "실측 뒤 업체가 정해요",
    "excluded": "이 집은 하지 않아요",
}


def _won(n: int) -> str:
    return f"{n:,}원"


def _qty(ln: StandardLineFact) -> str:
    if ln.qty is None:
        return ""
    q = int(ln.qty) if float(ln.qty).is_integer() else ln.qty
    return f"{q}{ln.unit}"


def _card(ln: StandardLineFact) -> UiCard | None:
    """이 집 견적서 줄 카드: 계산 값이 하나라도 있을 때만."""
    rows: list[TableRow] = []
    if ln.qty is not None or ln.unit_price is not None:
        here = " · ".join(x for x in (_qty(ln), _won(ln.unit_price) if ln.unit_price else "") if x)
        rows.append(
            TableRow(cells=["수량 · 단가", here, "실제로 할 곳만 셌는지, 공급 평 전체로 셌는지"])
        )
    if ln.amount is not None:
        rows.append(
            TableRow(cells=["금액", _won(ln.amount), f"업체 견적서에 {ln.name} 금액이 따로 있는지"])
        )
    elif ln.state == "after_visit":
        rows.append(
            TableRow(cells=["금액", "실측 뒤 업체가 정해요", "대략적인 범위를 먼저 물어보기"])
        )
    if not rows:
        return None
    rows.append(TableRow(cells=["합계", STATE_NOTE.get(ln.state, ""), "견적서 합계에 들었는지"]))
    return UiCard(
        kind="house",
        title=f"이 집 견적서의 {ln.name}",
        table=CompareTable(
            cols=["항목", "이 집", "확인할 점"], rows=rows, sources=["이 집 표준견적서(믿집 계산)"]
        ),
    )


def _vendor(q: str, ln: StandardLineFact | None) -> tuple[str, ...]:
    """업체에 그대로 보낼 정중한 부탁 2줄(고객이 먼저 줄 것 하나 · 마지막은 견적서에 적어 달라)."""
    if ln is not None:
        name = ln.name
        if ln.qty is not None:
            first = (
                f"이 집 견적서에는 {josa(name, '이', '가')} {_qty(ln)}로 잡혀 있는 걸로 알고 있어요. "
                "사진을 보내 드리면 필요한 작업 범위를 확인해 주실 수 있을까요?"
            )
        else:
            first = (
                f"{josa(name, '은', '는')} 실측 뒤에 정해진다고 들었어요. "
                "실측 날 집을 보시고 대략적인 범위를 알려 주실 수 있을까요?"
            )
        return (first, f"정해진 작업 범위와 금액을 {name} 줄에 나눠 견적서에 적어 주세요.")
    work = WORK.search(q)
    bad = PROBLEM.search(q)
    if bad and not PERIOD.search(
        q
    ):  # '보수 기간은 언제까지?'처럼 기간을 물으면 원인 확인 부탁이 아니에요
        stem = next((v for k, v in PROBLEM_NAME.items() if k in bad.group(0)), "")
        what = stem or (work.group(1) if work else "이 부분")
        return (
            f"{josa(what, '이', '가')} 왜 그런지 원인부터 확인해 주실 수 있을까요? 그 자리 사진을 먼저 보내 드릴게요.",
            "어떻게 고칠지와 그 비용을 견적서에 따로 나눠 적어 주세요.",
        )
    if work:
        w = work.group(1)
        return (
            f"우리 집에 {josa(w, '이', '가')} 필요한지 알고 싶어요. 벽 사진을 보내 드리면 확인해 주실 수 있을까요?",
            f"필요하면 작업 범위와 금액을 견적서에 {w} 줄로 나눠 적어 주세요.",
        )
    return ()


def shape(
    ans: AgentAnswer,
    task: Task,
    trade: str | None = None,
    *,
    vendor_request: tuple[str, ...] | None = None,
) -> AgentAnswer:
    """마지막 검사를 마친 답에 이 집 답 모양을 입힌다. 해당하지 않으면 답을 그대로 돌려준다."""
    trades = set(task.trades) | ({trade} if trade else set())
    has_house_rules = bool(trades & set(ON_TRADES)) or task.route == "quote_explanation"
    if task.route not in ON_ROUTES or not (has_house_rules or vendor_request):
        return ans
    if ans.failed or ans.ask is not None or not ans.sections or not ans.sections[0].sentences:
        return ans  # 되묻는 턴은 결론 · 부탁 줄을 붙이지 않는다
    ln = None
    # 견적 설명 길이거나 금액 · 수량을 묻는 말이 있을 때만 이 집 견적서 줄을 붙여요(곰팡이 질문에 '벽지' 줄이 붙지 않게)
    if (
        has_house_rules
        and task.facts
        and task.facts.standard
        and (task.route == "quote_explanation" or MONEY.search(task.text))
    ):
        # 공종 이름(도배 · 목공)만으로는 줄을 고르지 않아요('도배 얼마예요?'에 벽 도배 줄 하나만 붙지 않게)
        ln = match_line(TRADE_WORD.sub(" ", task.text), task.facts.standard.lines)
    # 칸 답 턴('네' · '거실')은 되물은 원래 질문과 합쳐 부탁 줄을 고른다
    question = f"{task.asked} {task.text}" if task.slot else task.text
    if vendor_request is not None and ln is None and not PERIOD.search(question):
        vendor = vendor_request
    else:
        vendor = _vendor(question, ln) if has_house_rules else ()
    card = _card(ln) if ln is not None else None
    if not vendor and card is None:
        return ans
    first, *rest = ans.sections
    said = list(first.sentences)
    if (
        said[0].text == EXTRACT_SUMMARY_LEAD
    ):  # 채움 글('믿집 자료를 보면 이래요.')은 결론이 아니에요
        said = said[1:]
    if not said:
        return ans
    lead = said[0].text
    remaining = said[1:]
    sections = rest
    if remaining or first.caution or first.tip or first.sources:
        sections = [first.model_copy(update={"sentences": remaining}), *rest]
    cards = list(ans.cards)
    if card is not None:
        cards.insert(0, card)
    return ans.model_copy(
        update={
            "lead": lead,
            "vendor": list(vendor),
            "sections": sections,
            "cards": cards,
        }
    )
