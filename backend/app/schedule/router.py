import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import AwareDatetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import current_user, require_role
from app.auth.models import User
from app.db import get_session
from app.schedule import service
from app.schedule.expected import ExpectedInput, ReferenceOption
from app.schedule.expected import create as create_expected
from app.schedule.expected import options as expected_options
from app.schedule.generation import ExpectedStatus, GenerateInput, generate, status_for
from app.schedule.models import CREW_MAX
from app.schedule.schemas import (
    AcceptIn,
    AckIn,
    CheckIn,
    CompareDealOut,
    ConfirmIn,
    ConfirmOut,
    DateRequestIn,
    DatesIn,
    DealOut,
    DocsOut,
    ExtraIn,
    InboxPageOut,
    JobOut,
    MyProjectOut,
    OverviewOut,
    PlanOut,
    PlanRedo,
    PreviewOut,
    ScheduleOut,
    TradeExtrasOut,
)
from app.shared import ratelimit
from app.shared.clock import today_seoul
from app.vendor.deps import approved_vendor
from app.visit.schemas import InboxSummaryOut, ReselectOut

router = APIRouter(tags=["공정표 · 업체 확정"])
customer_only = require_role("customer")
Trade = Literal["dobae", "mokgong"]


@router.get(
    "/projects/{project_id}/expected-plan",
    response_model=ExpectedStatus,
    summary="저장된 예상 공정표와 재생성용 버전 조회",
)
async def expected_status(
    project_id: uuid.UUID,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> ExpectedStatus:
    return await status_for(s, user, project_id)


@router.post(
    "/projects/{project_id}/expected-plan/generate",
    response_model=PlanOut,
    summary="사용자 집 정보로 LLM이 DB 근거를 선택하고 계산 엔진으로 예상 공정표 생성",
)
async def generate_plan(
    project_id: uuid.UUID,
    data: GenerateInput,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> PlanOut:
    user_id = user.id
    # 인증 조회가 연 트랜잭션도 LLM 대기 전에 반환한다.
    await s.close()
    await ratelimit.hit(
        ratelimit.key("schedule_generate", str(user_id)),
        10,
        3600,
        "공정표 생성 요청이 많아요. 잠시 뒤 다시 시도해 주세요.",
    )
    return await generate(user_id, project_id, data, today_seoul())


@router.get(
    "/projects/{project_id}/expected-plan/inputs",
    response_model=list[ReferenceOption],
    summary="예상 공정표의 사용 가능 품·규격·적용 조건",
)
async def measured_inputs(
    project_id: uuid.UUID,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> list[ReferenceOption]:
    return await expected_options(s, user, project_id)


@router.post(
    "/projects/{project_id}/expected-plan",
    response_model=PlanOut,
    summary="사용자 물량·인원과 DB 품으로 예상 공정표 생성",
)
async def measured_plan(
    project_id: uuid.UUID,
    data: ExpectedInput,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> PlanOut:
    return await create_expected(s, user, project_id, data, today_seoul())


@router.get(
    "/projects/{project_id}/plan",
    response_model=PlanOut,
    summary="공정표: 공사 날짜를 수락했으면 확정 공정표, 아니면 예상 공정표 (만든 사람 · 관리자)",
)
async def plan(
    project_id: uuid.UUID,
    kind: Literal["expected", "final"] | None = None,
    user: User = Depends(current_user),
    s: AsyncSession = Depends(get_session),
) -> PlanOut:
    return await service.plan_for(s, user, project_id, today_seoul(), kind)


@router.post(
    "/projects/{project_id}/plan",
    response_model=PlanOut,
    summary="예상 공정표 다시 계산 (시작일 · 주말 공사)",
)
async def redo_plan(
    project_id: uuid.UUID,
    data: PlanRedo,
    user: User = Depends(current_user),
    s: AsyncSession = Depends(get_session),
) -> PlanOut:
    return await service.redo(s, user, project_id, data, today_seoul())


@router.post(
    "/projects/{project_id}/quotes/{visit_id}/confirm",
    response_model=ConfirmOut,
    summary="업체 확정 (본 버전 seen_ver일 때만, 같은 공정의 다른 요청은 닫힘)",
)
async def confirm(
    project_id: uuid.UUID,
    visit_id: uuid.UUID,
    data: ConfirmIn,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> ConfirmOut:
    return await service.confirm(s, user, project_id, visit_id, data, today_seoul())


@router.post(
    "/projects/{project_id}/deals/{deal_id}/reselect",
    response_model=ReselectOut,
    summary="다시 고르기 (공사 날짜 수락 전만): 확정을 풀고 이 공정의 요청을 모두 닫는다",
)
async def reselect(
    project_id: uuid.UUID,
    deal_id: uuid.UUID,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> ReselectOut:
    return await service.reselect(s, user, project_id, deal_id)


@router.get(
    "/projects/{project_id}/deals",
    response_model=list[DealOut],
    summary="이 집 업체 확정 (공정마다, 다시 고르기 한 것은 빼고)",
)
async def deals(
    project_id: uuid.UUID,
    trade: Trade | None = None,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> list[DealOut]:
    return await service.deals_for(s, user, project_id, trade, today_seoul())


@router.get(
    "/projects/{project_id}/quotes",
    response_model=CompareDealOut,
    summary="실측 견적 비교 · 표준견적서와 업체가 보낸 견적서 · 확정 (한 공정)",
)
async def compare_quotes(
    project_id: uuid.UUID,
    trade: Trade,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> CompareDealOut:
    return await service.compare(s, user, project_id, trade, today_seoul())


@router.get(
    "/vendor/visits",
    response_model=InboxPageOut,
    summary="업체 작업함 · 할 일 · 기다림 · 공사 · 끝남 (끝남은 before 커서로 limit씩)",
)
async def vendor_inbox(
    tab: Literal["todo", "waiting", "jobs", "done"] = "todo",
    before: AwareDatetime | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> InboxPageOut:
    return await service.vendor_inbox(s, vendor, tab, today_seoul(), before, limit)


@router.get("/vendor/visits/summary", response_model=InboxSummaryOut, summary="업체 작업함 탭별 수")
async def vendor_inbox_summary(
    vendor: User = Depends(approved_vendor), s: AsyncSession = Depends(get_session)
) -> InboxSummaryOut:
    return await service.vendor_summary(s, vendor)


# ---- 공사 날짜 ----


@router.get(
    "/projects/{project_id}/schedule",
    response_model=ScheduleOut,
    summary="공사 날짜: 공정마다 낸 날짜 · 다른 날짜 요청 · 겹침 · 요청 후보",
)
async def schedule(
    project_id: uuid.UUID,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> ScheduleOut:
    return await service.schedule_for(s, user, project_id, today_seoul())


@router.post(
    "/projects/{project_id}/deals/{deal_id}/dates/{proposal_id}/accept",
    response_model=ScheduleOut,
    summary="낸 공사 날짜 · 새 날짜 수락 → 확정 공정표 새 버전 (겹침 · 다시 냄 · 공정표 바뀜은 409)",
)
async def accept_dates(
    project_id: uuid.UUID,
    deal_id: uuid.UUID,
    proposal_id: uuid.UUID,
    data: AcceptIn,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> ScheduleOut:
    return await service.accept_dates(
        s, user, project_id, deal_id, proposal_id, data, today_seoul()
    )


@router.post(
    "/projects/{project_id}/deals/{deal_id}/date-requests",
    status_code=201,
    response_model=ScheduleOut,
    summary="다른 공사 날짜 요청 (2번까지, 업체는 다음 날 18:00까지 다시 낸다)",
)
async def request_dates(
    project_id: uuid.UUID,
    deal_id: uuid.UUID,
    data: DateRequestIn,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> ScheduleOut:
    return await service.request_dates(s, user, project_id, deal_id, data, today_seoul())


@router.get(
    "/vendor/jobs/{deal_id}", response_model=JobOut, summary="공사 날짜 내기 화면 (내 공사만)"
)
async def job(
    deal_id: uuid.UUID,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> JobOut:
    return await service.job_for(s, vendor, deal_id, today_seoul())


@router.get(
    "/vendor/jobs/{deal_id}/dates/preview",
    response_model=PreviewOut,
    summary="낼 날짜 미리 계산: 일하는 날 · 그 인원의 필요한 기간 · 겹침 (저장하지 않음)",
)
async def preview_dates(
    deal_id: uuid.UUID,
    start: date,
    end: date,
    crew: Annotated[int, Query(ge=1, le=CREW_MAX)],
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> PreviewOut:
    return await service.preview_dates(s, vendor, deal_id, start, end, crew, today_seoul())


@router.post(
    "/vendor/jobs/{deal_id}/dates",
    response_model=JobOut,
    summary="공사 날짜 내기 · 다시 내기 (고객이 수락하기 전까지)",
)
async def propose_dates(
    deal_id: uuid.UUID,
    data: DatesIn,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> JobOut:
    return await service.propose_dates(s, vendor, deal_id, data, today_seoul())


# ---- 공사 중 추가 금액 ----


@router.post(
    "/vendor/jobs/{deal_id}/extras",
    status_code=201,
    response_model=JobOut,
    summary="추가 금액 알리기 (공사 날짜 수락 뒤 공사하는 동안, 고치지 않고 새로 올린다)",
)
async def add_extra(
    deal_id: uuid.UUID,
    data: ExtraIn,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> JobOut:
    return await service.add_extra(s, vendor, deal_id, data, today_seoul())


@router.get(
    "/vendor/jobs/{deal_id}/extras/{extra_id}/files/{file_id}",
    summary="추가 금액 공사 사진 (내 공사만)",
)
async def vendor_extra_photo(
    deal_id: uuid.UUID,
    extra_id: uuid.UUID,
    file_id: uuid.UUID,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> Response:
    return await service.vendor_extra_photo(s, vendor, deal_id, extra_id, file_id)


@router.post(
    "/projects/{project_id}/deals/{deal_id}/extras/{extra_id}/ack",
    response_model=TradeExtrasOut,
    summary="추가 금액에 답하기: 확인했어요 · 업체와 이야기할게요 (한 번만)",
)
async def ack_extra(
    project_id: uuid.UUID,
    deal_id: uuid.UUID,
    extra_id: uuid.UUID,
    data: AckIn,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> TradeExtrasOut:
    return await service.ack_extra(s, user, project_id, deal_id, extra_id, data)


@router.get(
    "/projects/{project_id}/deals/{deal_id}/extras/{extra_id}/files/{file_id}",
    summary="추가 금액 공사 사진 (내 집만)",
)
async def customer_extra_photo(
    project_id: uuid.UUID,
    deal_id: uuid.UUID,
    extra_id: uuid.UUID,
    file_id: uuid.UUID,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> Response:
    return await service.customer_extra_photo(s, user, project_id, deal_id, extra_id, file_id)


# ---- 공사 끝냈어요 · 고객 확인 ----


@router.post(
    "/vendor/jobs/{deal_id}/done",
    response_model=JobOut,
    summary="공사 끝냈어요 · 남은 일 다 했어요 (마지막 공사일부터, 연락처는 오늘부터 1년)",
)
async def finish(
    deal_id: uuid.UUID,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> JobOut:
    return await service.finish(s, vendor, deal_id, today_seoul())


@router.post(
    "/projects/{project_id}/deals/{deal_id}/check",
    response_model=DealOut,
    summary="공사 끝남 확인: 끝났어요 · 남은 일이 있어요 (업체가 끝냈다고 올린 뒤에만)",
)
async def check_work(
    project_id: uuid.UUID,
    deal_id: uuid.UUID,
    data: CheckIn,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> DealOut:
    return await service.check_work(s, user, project_id, deal_id, data, today_seoul())


# ---- 내 공사 한눈에 · 목록 · 서류 ----


@router.get(
    "/projects",
    response_model=list[MyProjectOut],
    summary="내 공사 목록 · 단계 요약 · 지금 할 일 · 다음 날짜 (바뀐 날 최근 순)",
)
async def my_projects(
    user: User = Depends(customer_only), s: AsyncSession = Depends(get_session)
) -> list[MyProjectOut]:
    return await service.my_projects(s, user, today_seoul())


@router.get(
    "/projects/{project_id}/overview",
    response_model=OverviewOut,
    summary="한 공사 화면: 지금 할 일 · 공정별 단계 · 낼 금액 · 공사 기간 · 업체 연락처 · 체크",
)
async def overview(
    project_id: uuid.UUID,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> OverviewOut:
    return await service.overview_for(s, user, project_id, today_seoul())


@router.get(
    "/projects/{project_id}/docs",
    response_model=DocsOut,
    summary="서류 모음: 표준견적서 · 공정표 버전 · 받은 실측 견적서 · 확정 견적서 · 추가 금액",
)
async def docs(
    project_id: uuid.UUID,
    user: User = Depends(customer_only),
    s: AsyncSession = Depends(get_session),
) -> DocsOut:
    return await service.docs_for(s, user, project_id, today_seoul())
