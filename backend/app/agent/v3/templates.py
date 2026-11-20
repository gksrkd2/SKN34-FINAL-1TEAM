"""정해 둔 글 (무료 · LLM 모드 공통). 해요체, 숫자 자리는 계산 엔진 결과로만 채운다.

여기 글에 든 고정 숫자(1372 소비자상담센터)는 출력 검사 허용 글(ALLOWED)에 넣는다.
화면 글 점검(UI/UX 글 담당)이 이 파일을 읽고 고칠 글을 낸다.
"""

from datetime import date

from app.agent.v3.schemas import Choice, Section, Sentence

WEEK = "월화수목금토일"
ALLOWED = "1372 소비자상담센터"


def s(text: str, cites: list[str] | None = None) -> Sentence:
    return Sentence(text=text, cites=cites or [])


def josa(word: str, with_final: str, without: str) -> str:
    """받침 있으면 with_final(이 · 은), 없으면 without(가 · 는). 한글로 안 끝나면 '이(가)'처럼 둘 다."""
    ch = (word or " ")[-1]
    if "가" <= ch <= "힣":
        return word + (with_final if (ord(ch) - 0xAC00) % 28 else without)
    return f"{word}{with_final}({without})"


def md(d: date) -> str:
    """12.15(화) 꼴 (copy-01 날짜 표기)."""
    return f"{d.month}.{d.day}({WEEK[d.weekday()]})"


def section(lines: list[str], *, title: str = "", route="guide", trade=None, source="template"):
    return Section(
        title=title, sentences=[s(x) for x in lines], source=source, route=route, trade=trade
    )


# ---- 범위 밖 ----
OUT = {
    "service": "그 일은 믿집이 도와드리기 어려워요. 믿집은 표준견적서 · 업체 가견적 비교 · 실측 일정 · 업체 채팅을 도와요.",
    "legal": "개별 사건의 법률 판단이나 대신 대응은 하지 않아요. 다툼이 생기면 1372 소비자상담센터에 물어볼 수 있어요.",
    "calc": "지금 믿집은 도배 · 목공만 금액을 계산해요. {ko} 금액은 실측 뒤 업체 견적서에서 확인해요.",
    "chat": "그 이야기는 제가 도와드리기 어려워요. 집 공사 이야기라면 물어봐 주세요.",
}

# ---- 정책 ----
PRICE_DEEP = "믿집이 이 집 조건으로 계산한 표준견적서 금액이에요. 업체 견적이 아니라 공공 기준으로 계산한 참고 금액이에요."
PRICE_DEEP_NO_PROJECT = (
    "기본 정보를 넣으면 표준견적서로 바로 계산해 드려요. 금액은 믿집 계산으로만 보여 드려요."
)
PRICE_KNOWLEDGE = (
    "지금 믿집은 도배 · 목공만 금액을 계산해요. {ko} 금액은 실측 뒤 업체 견적서에서 확인해요."
)
PERIOD_PLAN = "공사 날짜는 믿집이 계산한 예상 공정표로 보여 드려요."
PERIOD_NO_PROJECT = "공사 기간은 기본 정보를 넣으면 예상 공정표로 계산해 드려요."
PERIOD_KNOWLEDGE = (
    "{ko} 공사 기간은 집을 본 뒤 업체가 정해요. 믿집은 아직 {ko} 기간을 계산하지 않아요."
)
VENDOR_RECO = "특정 업체는 추천하지 않아요. 업체를 고를 때 볼 것을 알려 드릴게요."
BRAND_RECO = "특정 제품 · 브랜드는 추천하지 않아요. 고르는 기준을 알려 드릴게요."
# 뒤에 기준 카드가 붙지 않으면(답이 모름) '알려 드릴게요'를 뺀 글로 바꿔요
RECO_SHORT = {
    VENDOR_RECO: "특정 업체는 추천하지 않아요.",
    BRAND_RECO: "특정 제품 · 브랜드는 추천하지 않아요.",
}
DEFECT = (
    "하자인지는 믿집이 판정하지 않아요. 사진과 날짜를 남겨 두고 업체에 먼저 알려 주세요. "
    "합의가 어려우면 1372 소비자상담센터에 물어볼 수 있어요."
)
DIY_RISKY = "{ko} 일 중 전기 · 가스 · 구조 · 방수에 닿는 일은 전문 업체에 맡기는 게 안전해요."
MY_HOUSE = "집 상태는 직접 봐야 정확히 알 수 있어요. 실측 때 업체가 확인해요."
KNOWLEDGE_UNAVAILABLE = "지금은 믿집 자료를 확인하기 어려워요."
UNKNOWN = "이 질문은 믿집 자료로 확인하지 못했어요."
UNKNOWN_WHERE = "{where}에 물어보시면 정확해요."
ASK_BACK = "어떤 걸 물어보신 건지 골라 주세요."
EXTRACT_LEAD = "믿집 자료에는 이렇게 나와요."
EXTRACT_SUMMARY_LEAD = "믿집 자료를 보면 이래요."
PART_FAILED = "그 부분은 확인하지 못했어요."
# 여러 공종 중 못 찾은 공종을 한 줄로
PART_FAILED_WHICH = "{which} 쪽 자세한 내용은 믿집 자료로 확인하지 못했어요."
SLOT_LEAD = "답을 더 맞추려면 하나만 알려 주세요."

# ---- 견적 설명: 표준견적서 줄 상태 6종 ----
LINE_STATE = {
    "included": "{name} 줄은 합계에 들어가요.",
    "bundled": "{name}은 다른 줄에 묶여 들어가서 따로 금액을 적지 않았어요.",
    "separate": "{name}은 따로 계산해서 합계에 더했어요.",
    "optional": "{name}은 고르면 더하는 줄이라 지금 합계에는 없어요.",
    "after_visit": "{name}은 집을 봐야 양을 알 수 있어서 실측 뒤 업체가 정해요. 괄호 금액은 참고 금액이에요.",
    "excluded": "{name}은 이번 공사에 넣지 않았어요.",
}
LINE_STATE_SPECIAL = {
    # 9판 C09 · D01 화면 글 그대로
    (
        "demolish",
        "after_visit",
    ): "뜯어낼 벽지 양은 집을 봐야 알 수 있어서 실측 뒤 업체가 정해요. 괄호 금액은 참고 금액이에요.",
}
STANDARD_IS = "표준견적서는 공공 기준 단가로 믿집이 계산한 참고 금액이에요. 업체 견적이 아니에요."
ESTIMATE_GAP = "업체들 가견적은 같은 믿집 수량으로 계산했어요. 차이는 업체가 확인한 단가에서 나요."
ESTIMATE_GAP_TOP = "{vendor} {lines} 단가가 다른 곳보다 높아요."
ESTIMATE_SEPARATE = "{vendor} {lines} 줄을 따로 받아요."
QUOTE_COMPARE = (
    "실측 견적서는 업체 양식 파일이라 줄끼리 견주지 않아요. "
    "무엇이 들어갔는지는 파일을 열거나 채팅으로 물어보세요."
)
NO_STANDARD = "아직 표준견적서가 없어요. 기본 정보를 넣으면 바로 계산해요."

# ---- 일정 ----
PLAN_READ = "예상 공정표는 이렇게 잡혔어요. 날짜는 믿집 계산이에요."
PLAN_NONE = "아직 공정표가 없어요. 기본 정보를 넣으면 예상 공정표를 계산해요."
DRAFT_BEFORE = (
    "공사 시작 희망일에 맞춰 실측 희망 기간을 계산했어요. 요청 보내기는 직접 눌러 주세요."
)
DRAFT_NO_WISH = "공사 시작 희망일을 알려 주시면 실측 희망 기간을 계산해 드려요."
DRAFT_AFTER = "{vendor_ga} {range}에 된대요. 공사 시작 희망 {wish}에 맞추면 {day}이 무난해요."
DRAFT_RULE = "AI는 초안만 만들어요. 보내기 · 날짜 확정은 직접 눌러 주세요."

# ---- 다음 할 일 버튼 (단계별) ----
NEXT: dict[str, list[Choice]] = {
    "info": [Choice(text="기본 정보 넣기", action="start")],
    "standard": [
        Choice(text="표준견적서 보기", action="standard"),
        Choice(text="업체 보기", action="vendors"),
    ],
    "vendors": [
        Choice(text="업체 보기", action="vendors"),
        Choice(text="실측 일정 잡기", action="visits_new"),
    ],
    "visit": [Choice(text="실측 일정 보기", action="visits")],
    "after_visit": [Choice(text="실측 견적서 비교", action="quotes")],
    "construction": [Choice(text="공정표 보기", action="plan")],
    "done": [Choice(text="공정표 보기", action="plan")],
}
NO_PROJECT_NEXT = [
    Choice(text="기본 정보 넣기", action="start"),
    Choice(text="표준견적서 보기", action="standard"),
]
