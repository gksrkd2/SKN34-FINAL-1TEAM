import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator

from app.project.schemas import ProjectCheckOut, ProjectListItem
from app.quote.schemas import BudgetOut, QuoteBriefOut, QuoteFileOut
from app.schedule.models import CREW_MAX, EXTRA_AMOUNT_MAX, EXTRA_PHOTOS_MAX, WISH_MAX
from app.vendor.schemas import ContactOut
from app.visit.schemas import CompareOut, VendorVisitDetailOut, VendorVisitRow

Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
# 공사 날짜 상태: 아직 안 냄 · 냄(수락 기다림) · 다른 날짜 요청 열림 · 수락함 · 다시 고름
# 수락한 뒤 날짜 바꾸기: 고객이 바꾸기 요청(change_requested) · 새 날짜 수락 기다림(change_proposed)
DatesState = Literal[
    "waiting", "proposed", "requested", "scheduled", "change_requested", "change_proposed", "closed"
]
# 공정표 칸: 확정(수락한 날짜) · 수락 전(낸 날짜) · 예상(날짜 없는 공정 · 건조 · 청소)
BlockState = Literal["confirmed", "pending", "expected"]


class PlanBlockOut(BaseModel):
    trade: str | None
    kind: str
    label: str
    start: date
    end: date
    days: int
    crew: int | None
    man_day: Decimal | None
    state: BlockState = "expected"
    deal_id: uuid.UUID | None = None
    vendor_name: str | None = None


class PlanDayOut(BaseModel):
    day: date
    trade: str | None
    kind: str
    names: list[str]
    holiday: str | None
    state: BlockState = "expected"


class PlanOut(BaseModel):
    project_id: uuid.UUID
    kind: str
    version: int
    project_rev: int
    start: date
    end: date
    work_days: int
    weekend_work: bool
    computed_at: datetime
    formula_version: str = ""
    basis: dict = Field(default_factory=dict)
    blocks: list[PlanBlockOut]
    days: list[PlanDayOut]
    changes: list["PlanChangeOut"] = Field(
        default=[], description="확정 공정표: 수락 기다리는 새 날짜마다 바꾸면 어떻게 되는지"
    )
    extras: list["TradeExtrasOut"] = Field(
        default=[], description="확정 공정표: 공정마다 업체가 알린 추가 금액 (확정 금액과 따로)"
    )
    deals: list["DealOut"] = Field(
        default=[], description="확정 공정표: 공정마다 공사 상태 · 끝낸 날 · 연락처 기한 · 남은 일"
    )


class PlanRedo(BaseModel):
    """날짜 바꿔 보기: 시작일 · 주말에도 공사"""

    start: date
    weekend_work: bool = False


class ConfirmIn(BaseModel):
    seen_ver: int = Field(ge=1, description="비교 화면을 연 때의 본 버전 (seen_ver 그대로)")


class DealOut(BaseModel):
    """업체 확정 한 건 (고객 쪽)"""

    id: uuid.UUID
    trade: str
    status: str
    vendor_id: uuid.UUID | None
    vendor_name: str
    owner_name: str | None = Field(description="업체 대표 이름 (연락처가 보일 때만)")
    visit_id: uuid.UUID
    quote: QuoteBriefOut = Field(description="확정한 견적서 버전 (바꾸지 않는다)")
    confirmed_at: datetime
    dates_due_at: datetime = Field(description="업체가 공사 날짜를 낼 기한")
    overdue: bool = Field(
        description="기한 지남: 날짜를 아직 안 냈거나 다른 날짜 요청에 답하지 않음 (상태가 아님)"
    )
    start_date: date | None
    end_date: date | None
    work_done_on: date | None = Field(description="업체가 마지막으로 '공사 끝냈어요'를 올린 날")
    contact_until: date | None = Field(
        description="서로 연락처가 보이는 마지막 날 (끝냈다고 올린 날 + 1년)"
    )
    fixing_note: str | None = Field(description="남은 일 (고객이 적은 글, 남은 일 상태일 때)")


class ConfirmOut(BaseModel):
    deal: DealOut
    closed: list[uuid.UUID] = Field(description="같은 공정에서 닫힌 다른 요청")
    quote_expired: bool = Field(
        description="확정한 견적서의 유효기간이 지났는지 (막지 않고 알린다)"
    )


class CompareDealOut(CompareOut):
    deal: DealOut | None = Field(description="그 공정의 업체 확정 (없으면 비어 있다)")


class InboxRowOut(VendorVisitRow):
    """업체 작업함 한 줄. 공사 탭은 확정 정보가 더 들어 있다."""

    deal_id: uuid.UUID | None = None
    deal_status: str | None = None
    confirmed_at: datetime | None = None
    dates_due_at: datetime | None = None
    overdue: bool = False
    start_date: date | None = None
    end_date: date | None = None
    dates_state: DatesState | None = None
    proposed_start: date | None = Field(default=None, description="수락 기다리는 날짜")
    proposed_end: date | None = None
    proposed_days: int | None = Field(default=None, description="날짜를 낸 뒤 지난 날 (공사 탭)")
    wish_dates: list[date] = Field(default=[], description="다른 날짜 요청: 고객이 고른 시작일")
    request_due_at: datetime | None = Field(default=None, description="다른 날짜 요청에 답할 기한")
    extras_pending: int = Field(default=0, description="고객 확인을 기다리는 추가 금액 수")
    work_done_on: date | None = None
    contact_until: date | None = Field(default=None, description="고객 연락처가 보이는 마지막 날")
    fixing_note: str | None = Field(default=None, description="고객이 알린 남은 일")


class InboxPageOut(BaseModel):
    items: list[InboxRowOut]
    next_before: datetime | None


# ---- 공사 날짜 ----


class ProposalOut(BaseModel):
    """업체가 낸 공사 날짜"""

    id: uuid.UUID
    start: date
    end: date
    workdays: int = Field(description="낸 날짜 안의 일하는 날 (믿집 계산)")
    crew: int
    method: Literal["app", "call"]
    call_on: date | None = Field(description="통화로 정했으면 통화한 날")
    kind: str
    reason: str | None
    status: str
    request_id: uuid.UUID | None = Field(description="답한 다른 날짜 요청")
    changed: bool = Field(description="앞서 낸 날짜를 바꿔 다시 냈는지 ('날짜가 바뀌었어요')")
    created_at: datetime
    decided_at: datetime | None


class DateRequestOut(BaseModel):
    id: uuid.UUID
    wish_dates: list[date] = Field(description="시작해도 되는 날")
    reason: str | None
    due_at: datetime = Field(description="업체가 새 날짜를 낼 기한 (요청 다음 날 18:00)")
    overdue: bool
    status: str
    created_at: datetime


class OtherTradeOut(BaseModel):
    """이 집 다른 공정의 확정 날짜 (겹침 확인용)"""

    trade: str
    vendor_name: str
    start: date
    end: date


class StartOptionOut(BaseModel):
    start: date
    end: date = Field(description="같은 일하는 날 수로 끝나는 날 (믿집 계산)")


class StepNeedOut(BaseModel):
    name: str
    half_days: int


class NeedOut(BaseModel):
    """하루 인원으로 센 필요한 기간: 품 ÷ 인원을 반나절로 올리고 준비 · 마감을 더해 하루로 올림"""

    man_day: Decimal
    crew: int
    days: int
    steps: list[StepNeedOut]


class FitOut(BaseModel):
    """날짜가 다른 공정과 겹치는지 · 겹치지 않는 자리"""

    conflicts: list[OtherTradeOut] = Field(description="겹치는 다른 공정 (있으면 수락할 수 없다)")
    start_from: date | None = Field(description="앞 공정이 끝난 다음 일하는 날")
    end_by: date | None = Field(description="뒤 공정 시작 전 마지막 일하는 날")


class TradeDatesOut(BaseModel):
    """한 공정의 공사 날짜 (고객 쪽)"""

    deal: DealOut
    dates_state: DatesState
    proposal: ProposalOut | None = Field(description="수락 기다리는 날짜")
    request: DateRequestOut | None = Field(description="열린 다른 날짜 요청")
    requests_left: int = Field(description="다른 날짜 요청 남은 수 (2번까지)")
    fit: FitOut | None = Field(description="수락 기다리는 날짜가 다른 공정과 맞는지")
    start_options: list[StartOptionOut] = Field(
        description="다른 날짜 요청 후보: 겹치지 않는 첫날부터, 같은 기간으로 끝나는 날"
    )
    extras: "TradeExtrasOut"


class HolidayOut(BaseModel):
    day: date
    name: str


class ScheduleOut(BaseModel):
    project_id: uuid.UUID
    weekend_work: bool = Field(description="주말에도 공사 (아니면 토 · 일은 쉬는 날)")
    holidays: list[HolidayOut] = Field(description="오늘부터 180일 안의 공휴일")
    final_version: int = Field(
        description="확정 공정표 최신 버전 (없으면 0). 수락할 때 그대로 보낸다"
    )
    trades: list[TradeDatesOut] = Field(description="목공 → 도배 순, 확정한 공정만")


class JobOut(BaseModel):
    """업체 공사 날짜 내기 화면"""

    deal_id: uuid.UUID
    trade: str
    status: str
    confirmed_at: datetime
    dates_due_at: datetime
    overdue: bool
    start_date: date | None = Field(description="수락한 공사 날짜")
    end_date: date | None
    visit: VendorVisitDetailOut = Field(description="집 · 고객 (연락처는 공개 규칙대로)")
    quote: QuoteBriefOut = Field(description="확정한 견적서 버전")
    plan_version: int = Field(description="지금 예상 공정표 버전")
    plan_start: date | None = Field(description="예상 공정표의 이 공정 시작일")
    plan_end: date | None
    need: NeedOut = Field(description="믿집 가정 인원으로 센 필요한 기간")
    others: list[OtherTradeOut]
    weekend_work: bool
    holidays: list[HolidayOut]
    dates_state: DatesState
    proposal: ProposalOut | None
    request: DateRequestOut | None
    requests_left: int
    request_options: list[StartOptionOut] = Field(
        description="수락한 날짜 바꾸기 요청: 고객이 고른 시작일마다 같은 기간으로 끝나는 날"
    )
    extras: "TradeExtrasOut"
    accepted_crew: int | None = Field(description="수락한 날짜의 하루 작업 인원")
    work_done_on: date | None
    contact_until: date | None
    fixing_note: str | None
    can_finish: bool = Field(
        description="'공사 끝냈어요'를 누를 수 있는지 (마지막 공사일부터, 공사 중 · 남은 일일 때)"
    )


class AcceptIn(BaseModel):
    seen_version: int = Field(
        ge=0, description="화면이 본 확정 공정표 버전 (없으면 0). 그사이 바뀌었으면 409"
    )


class DatesIn(BaseModel):
    """업체가 공사 날짜 내기 (수락 전에는 다시 낼 수 있다)"""

    start: date
    end: date
    crew: int = Field(ge=1, le=CREW_MAX, description="하루 작업 인원")
    method: Literal["app", "call"] = Field(description="믿집에서 내기 · 고객과 통화로 정함")
    kind: Literal["initial", "change"] = Field(
        default="initial",
        description="처음 내기 · 다시 내기(initial) 또는 수락한 날짜 바꾸기(change)",
    )
    call_on: date | None = Field(default=None, description="통화로 정했으면 통화한 날 (필수)")
    reason: Reason | None = Field(
        default=None, description="수락한 날짜를 바꿀 때 이유 (고객 요청에 답할 때는 없어도 된다)"
    )
    request_id: uuid.UUID | None = Field(
        default=None, description="화면이 본 열린 요청 (그사이 바뀌었으면 409)"
    )


class PreviewOut(BaseModel):
    """낼 날짜 미리 계산: 일하는 날 · 필요한 기간 · 겹침"""

    workdays: int
    need: NeedOut
    short: bool = Field(description="일하는 날이 필요한 기간보다 적음 (막지 않고 알린다)")
    fit: FitOut


class DateRequestIn(BaseModel):
    """고객의 다른 날짜 요청 (2번까지)"""

    wish_dates: list[date] = Field(min_length=1, max_length=WISH_MAX)
    reason: Reason | None = None

    @model_validator(mode="after")
    def _distinct(self):
        if len(set(self.wish_dates)) != len(self.wish_dates):
            raise ValueError("같은 날을 두 번 골랐어요.")
        return self


class PlanChangeOut(BaseModel):
    """수락한 날짜를 바꾸는 새 날짜: 지금 → 새 날짜, 바꾸면 건조 · 청소도 같이 밀린 공정표"""

    trade: str
    deal_id: uuid.UUID
    vendor_name: str
    current_start: date
    current_end: date
    proposal: ProposalOut
    fit: FitOut
    start: date
    end: date
    work_days: int
    blocks: list[PlanBlockOut]
    days: list[PlanDayOut]


# ---- 공사 중 추가 금액 ----


class ExtraIn(BaseModel):
    """업체가 추가 금액 알리기. 한 번 올리면 고치지 않는다(잘못 올렸으면 새로 올린다)."""

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)] = (
        Field(description="한 일")
    )
    reason: Reason = Field(description="이유 (고객에게 보여요)")
    entered_amount: int = Field(ge=0, le=EXTRA_AMOUNT_MAX, description="업체가 적은 금액 (원)")
    vat_mode: Literal["included", "excluded", "exempt"] = Field(
        default="excluded", description="부가세 빼고 적은 금액이 기본 (면세 업체는 면세만)"
    )
    file_ids: list[uuid.UUID] = Field(
        default=[],
        max_length=EXTRA_PHOTOS_MAX,
        description="공사 사진 (POST /files kind=work_photo)",
    )

    @model_validator(mode="after")
    def _distinct(self):
        if len(set(self.file_ids)) != len(self.file_ids):
            raise ValueError("같은 사진을 두 번 붙였어요.")
        return self


class AckIn(BaseModel):
    ack: Literal["ok", "talk"] = Field(description="확인했어요 · 업체와 이야기할게요")


class MoneyOut(BaseModel):
    supply: int
    vat: int
    total: int = Field(description="부가세 포함")


class ExtraPhotoOut(BaseModel):
    file_id: uuid.UUID
    filename: str
    content_type: str
    size: int


class ExtraOut(BaseModel):
    id: uuid.UUID
    title: str
    reason: str
    vat_mode: str
    entered_amount: int
    supply: int
    vat: int
    total: int = Field(description="고객에게 보이는 금액 (부가세 포함, 믿집 계산)")
    ack: Literal["pending", "ok", "talk"] = Field(
        description="확인 기다림 · 확인했어요 · 업체와 이야기 중"
    )
    acked_at: datetime | None
    created_at: datetime
    photos: list[ExtraPhotoOut]


class TradeExtrasOut(BaseModel):
    """한 공정의 추가 금액. 합계는 계산 엔진이 더한다 (확정 금액에 넣지 않는다)."""

    trade: str
    deal_id: uuid.UUID
    vendor_name: str
    items: list[ExtraOut] = Field(description="올린 순")
    ok: MoneyOut = Field(description="고객이 확인한 것")
    pending: MoneyOut = Field(description="확인을 기다리는 것")
    talk: MoneyOut = Field(description="업체와 이야기 중인 것")


PlanOut.model_rebuild()
TradeDatesOut.model_rebuild()
JobOut.model_rebuild()


class CheckIn(BaseModel):
    """고객이 공사 끝남 확인: 끝났어요(done) 또는 남은 일이 있어요(fixing, 남은 일 필수)"""

    result: Literal["done", "fixing"]
    note: Reason | None = Field(default=None, description="남은 일 (fixing일 때 필수)")


# ---- 내 공사 한눈에 · 목록 · 서류 ----


class StepOut(BaseModel):
    key: Literal["house", "standard", "pick", "visit", "deal", "dates"]
    label: str
    state: Literal["done", "now", "todo"]
    on: date | None = Field(description="끝난 날 · 지금 단계가 시작된 날 (서울)")
    count: int | None = Field(description="업체 고르기: 요청한 곳 · 실측: 받은 견적서 수")


class TodoOut(BaseModel):
    """지금 할 일 한 줄과 버튼 (판 문구)"""

    code: str
    trade: str | None
    text: str
    action: str = Field(description="버튼 글자")
    link: str = Field(description="버튼이 가는 화면")


class NextOut(BaseModel):
    kind: Literal["dates_due", "request_due", "start", "end"] = Field(
        description="업체가 날짜를 낼 기한 · 다른 날짜 요청 답 기한 · 공사 시작 · 공사 끝"
    )
    trade: str
    day: date
    at: datetime | None = Field(description="기한이면 그 시각")


class TradeProgressOut(BaseModel):
    trade: str
    vendor_name: str | None = Field(description="확정한 업체")
    deal_id: uuid.UUID | None
    deal_status: str | None
    phase: Literal["estimate", "progress", "work", "done"]
    current: int = Field(description="지금 단계 번호 (6단계 중)")
    steps: list[StepOut]
    todo: TodoOut
    overdue: bool
    start_date: date | None = Field(description="수락한 공사 날짜")
    end_date: date | None
    proposed_start: date | None = Field(description="수락 기다리는 날짜")
    proposed_end: date | None
    next: NextOut | None


class TradeMoneyOut(BaseModel):
    trade: str
    total: int = Field(description="부가세 포함")
    source: Literal["deal", "standard"] = Field(description="확정 견적서 · 표준견적서")


class MoneySumOut(BaseModel):
    """낼 금액: 확정한 공정은 확정 견적서, 아니면 표준견적서 (계산 엔진이 더함, 추가 금액은 빼고)"""

    total: int
    by_trade: list[TradeMoneyOut]
    all_confirmed: bool
    budget: BudgetOut | None = Field(description="넣은 예산과 견줌 (예산을 안 넣었으면 비어 있다)")


class PeriodOut(BaseModel):
    kind: Literal["expected", "final"]
    start: date
    end: date
    work_days: int


class VendorContactOut(BaseModel):
    trade: str
    vendor_name: str
    contact: ContactOut
    contact_until: date | None = Field(description="연락처가 보이는 마지막 날 (공사 끝낸 뒤)")


class OverviewOut(BaseModel):
    """한 공사 화면 (WProjectB)"""

    project_id: uuid.UUID
    code: str
    region: str
    sgg: str
    pyeong: Decimal | None
    phase: Literal["estimate", "progress", "work", "done"]
    todo: TodoOut | None = Field(description="맨 위 지금 할 일 (공정 중 가장 먼저 할 것)")
    next: NextOut | None = Field(description="가장 가까운 다음 날짜")
    trades: list[TradeProgressOut] = Field(description="목공 → 도배 순")
    money: MoneySumOut
    period: PeriodOut
    contacts: list[VendorContactOut]
    checks: list[ProjectCheckOut]


class MyProjectOut(ProjectListItem):
    """내 공사 목록 한 줄 (WMy)"""

    phase: Literal["estimate", "progress", "work", "done"]
    current: int = Field(description="가장 늦은 공정의 지금 단계 (6단계 중)")
    todo: TodoOut | None
    next: NextOut | None
    updated_at: datetime = Field(description="바뀐 날 (목록은 최근 순)")
    grades: dict[str, str | None] = Field(description="공정마다 도배 유형")


class StandardDocOut(BaseModel):
    trade: str
    version: int
    total: int
    computed_at: datetime
    latest: bool
    link: str


class PlanDocOut(BaseModel):
    kind: Literal["expected", "final"]
    version: int
    start: date
    end: date
    created_at: datetime
    latest: bool
    link: str


class QuoteVersionDocOut(BaseModel):
    id: uuid.UUID
    code: str
    version: int
    sent_at: datetime
    total: int | None
    confirmed: bool = Field(description="고객이 확정한 버전")
    files: list[QuoteFileOut]
    file_links: list[str] = Field(description="files와 같은 순서의 파일 주소")


class QuoteDocOut(BaseModel):
    visit_id: uuid.UUID
    trade: str
    vendor_name: str
    status: str
    close_reason: str | None
    closed: bool = Field(description="닫힘 · 기록 (다시 확정할 수 없다)")
    versions: list[QuoteVersionDocOut]


class DocsOut(BaseModel):
    """서류 모음 (WDocs)"""

    project_id: uuid.UUID
    code: str
    region: str
    standard: list[StandardDocOut]
    standard_total: int = Field(description="최신 표준견적서 공정 합계 (부가세 포함)")
    plans: list[PlanDocOut]
    quotes: list[QuoteDocOut]
    extras: list["TradeExtrasOut"]


DocsOut.model_rebuild()
