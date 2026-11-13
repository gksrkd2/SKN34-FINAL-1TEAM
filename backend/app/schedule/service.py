"""공정표: 예상 공정표(집 정보로 계산) · 확정 공정표 · 업체 확정.

예상 공정표는 처음 볼 때 계산해 저장하고, 집 정보(rev) · 산식 버전이 그대로면 저장본을 쓴다.
날짜 · 기간은 모두 계산 엔진(calc.schedule)이 만든다.
"""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi.responses import Response
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.calc.common import FORMULA_VERSION, Money, add_money, from_entered, sum_won
from app.calc.progress import TradeFacts, first_todo, progress, project_phase
from app.calc.quote_diff import budget_gap
from app.calc.schedule import (
    JOB_MAX_DAYS,
    WEEKEND,
    Dated,
    TradePlan,
    contact_until,
    count_workdays,
    days_since,
    due_at,
    fix_plan,
    free_range,
    is_off,
    lay_out,
    order_conflicts,
    start_choices,
    trade_days,
    workdays,
)
from app.files import service as files_service
from app.files.models import File
from app.notify import service as notify_service
from app.project import service as project_service
from app.project.schemas import ProjectOut
from app.quote import service as quote_service
from app.quote.schemas import BudgetOut
from app.refprice.holidays import HOLIDAYS, days_off
from app.schedule.models import (
    OPEN_DEALS,
    DateProposal,
    DateRequest,
    Deal,
    DealEvent,
    ExtraCharge,
    ExtraChargeFile,
    Plan,
    PlanDay,
    PlanStep,
)
from app.schedule.schemas import (
    AcceptIn,
    AckIn,
    CheckIn,
    CompareDealOut,
    ConfirmIn,
    ConfirmOut,
    DateRequestIn,
    DateRequestOut,
    DatesIn,
    DealOut,
    DocsOut,
    ExtraIn,
    ExtraOut,
    ExtraPhotoOut,
    FitOut,
    HolidayOut,
    InboxPageOut,
    InboxRowOut,
    JobOut,
    MoneyOut,
    MoneySumOut,
    MyProjectOut,
    NeedOut,
    NextOut,
    OtherTradeOut,
    OverviewOut,
    PeriodOut,
    PlanBlockOut,
    PlanChangeOut,
    PlanDayOut,
    PlanDocOut,
    PlanOut,
    PlanRedo,
    PreviewOut,
    ProposalOut,
    QuoteDocOut,
    QuoteVersionDocOut,
    ScheduleOut,
    StandardDocOut,
    StartOptionOut,
    StepNeedOut,
    StepOut,
    TodoOut,
    TradeDatesOut,
    TradeExtrasOut,
    TradeMoneyOut,
    TradeProgressOut,
    VendorContactOut,
)
from app.shared.clock import SEOUL, now_utc
from app.shared.errors import AppError
from app.shared.ids import uuid7
from app.shared.korean import i_ga
from app.vendor import service as vendor_service
from app.visit import service as visit_service
from app.visit.schemas import InboxSummaryOut, ReselectOut

LEAD_DAYS = 14  # 공사 시기를 아직 모를 때: 오늘부터 2주 뒤 첫 일하는 날에 시작한다고 잡는다
REDO_LOCKED = (
    "업체를 확정한 뒤에는 날짜를 바꿔 볼 수 없어요. 공사 날짜 화면에서 다른 날짜를 요청해 주세요."
)
LABEL = {"mokgong": "목공", "dobae": "도배"}


def _off(start: date) -> frozenset[date]:
    return days_off(start, start + timedelta(days=400))


def _compute(p: ProjectOut, start: date, weekend_work: bool):
    calcs = quote_service.calc_trades(p)
    trades = [TradePlan(c.trade, c.steps, c.crew) for c in calcs]
    weekend = () if weekend_work else (5, 6)
    return lay_out(trades, start, _off(start), weekend), {c.trade: c.man_day for c in calcs}


def default_start(p: ProjectOut, today: date) -> date:
    """집 정보의 공사 시기로 시작일을 잡는다. 이사 날짜만 있으면 그 전날까지 끝나게 거꾸로 잡는다."""
    earliest = workdays(today + timedelta(days=1), 1, _off(today))[0]
    if p.when.kind == "date" and p.when.start:
        return max(p.when.start, earliest)
    if p.when.kind == "movein" and p.when.start:
        start = p.when.start
        for _ in range(8):  # 끝나는 날이 이사 전날 안쪽이 될 때까지 앞당긴다
            plan, _ = _compute(p, start, False)
            over = (plan.end - p.when.start).days + 1
            if over <= 0:
                break
            start -= timedelta(days=over)
        return max(start, earliest)
    return workdays(today + timedelta(days=LEAD_DAYS), 1, _off(today))[0]


async def _latest(s: AsyncSession, project_id: uuid.UUID, kind: str) -> Plan | None:
    q = (
        select(Plan)
        .where(Plan.project_id == project_id, Plan.kind == kind)
        .order_by(Plan.version.desc())
        .limit(1)
    )
    return (await s.scalars(q)).first()


def _add_parts(
    s: AsyncSession,
    row: Plan,
    plan,
    man_days: dict[str, Decimal],
    deal_of: dict[str, uuid.UUID] | None = None,
) -> None:
    """공정표 칸 · 날짜별 할 일 행을 더한다. deal_of: 확정 공정표에서 수락한 공정의 업체 확정."""
    deal_of = deal_of or {}
    s.add_all(
        [
            PlanStep(
                plan_id=row.id,
                trade=b.trade,
                kind=b.kind,
                label=b.label or LABEL.get(b.trade or "", ""),
                start_date=b.start,
                end_date=b.end,
                days=b.days,
                crew=b.crew,
                man_day=man_days.get(b.trade) if b.trade else None,
                sort=i,
                deal_id=deal_of.get(b.trade or ""),
            )
            for i, b in enumerate(plan.blocks)
        ]
    )
    s.add_all(
        [
            PlanDay(plan_id=row.id, day=d.day, trade=d.trade, kind=d.kind, names=list(d.names))
            for d in plan.days
        ]
    )


async def _save(
    s: AsyncSession, p: ProjectOut, start: date, weekend_work: bool, prev: Plan | None
) -> None:
    plan, man_days = _compute(p, start, weekend_work)
    row = Plan(
        project_id=p.id,
        kind="expected",
        version=(prev.version + 1) if prev else 1,
        project_rev=p.rev,
        formula_ver=FORMULA_VERSION,
        start_date=plan.start,
        end_date=plan.end,
        work_days=plan.work_days,
        weekend_work=weekend_work,
    )
    s.add(row)
    try:
        await s.flush()
        _add_parts(s, row, plan, man_days)
        await s.commit()
    except IntegrityError:  # 동시에 두 번 만들면 먼저 저장한 버전을 쓴다
        await s.rollback()


async def ensure_expected(s: AsyncSession, p: ProjectOut, today: date) -> Plan:
    prev = await _latest(s, p.id, "expected")
    if prev and prev.formula_ver.startswith(("image-", "reference-")):
        if prev.project_rev != p.rev:
            from app.schedule.expected import missing

            raise missing()
        return prev
    if prev and prev.project_rev == p.rev and prev.formula_ver == FORMULA_VERSION:
        return prev
    await _save(s, p, default_start(p, today), False, prev)
    got = await _latest(s, p.id, "expected")
    assert got is not None
    return got


async def _out(s: AsyncSession, plan: Plan) -> PlanOut:
    steps = (
        await s.scalars(select(PlanStep).where(PlanStep.plan_id == plan.id).order_by(PlanStep.sort))
    ).all()
    days = (
        await s.scalars(select(PlanDay).where(PlanDay.plan_id == plan.id).order_by(PlanDay.day))
    ).all()
    return PlanOut(
        project_id=plan.project_id,
        kind=plan.kind,
        version=plan.version,
        project_rev=plan.project_rev,
        start=plan.start_date,
        end=plan.end_date,
        work_days=plan.work_days,
        weekend_work=plan.weekend_work,
        computed_at=plan.created_at,
        formula_version=plan.formula_ver,
        basis=plan.basis_snapshot,
        blocks=[
            PlanBlockOut(
                trade=st.trade,
                kind=st.kind,
                label=st.label,
                start=st.start_date,
                end=st.end_date,
                days=st.days,
                crew=st.crew,
                man_day=st.man_day,
            )
            for st in steps
        ],
        days=[
            PlanDayOut(
                day=d.day,
                trade=d.trade,
                kind=d.kind,
                names=list(d.names),
                holiday=HOLIDAYS.get(d.day),
            )
            for d in days
        ],
    )


async def plan_for(
    s: AsyncSession, user: User, project_id: uuid.UUID, today: date, kind: str | None = None
) -> PlanOut:
    """GET /projects/{id}/plan: 만든 사람 · 관리자만. 공사 날짜를 하나라도 수락했으면 확정 공정표,
    아니면 예상 공정표. kind로 골라 볼 수 있다."""
    if kind == "expected":
        from app.schedule.expected import read

        return await read(s, user, project_id)
    p = await project_service.get_for(s, user, project_id)
    expected = await ensure_expected(s, p, today)
    final = await _latest(s, p.id, "final")
    if kind == "expected" or (kind is None and final is None):
        return await _out(s, expected)
    if final is None:
        raise AppError(
            404, "not_found", "확정 공정표가 아직 없어요. 공사 날짜를 수락하면 만들어져요."
        )
    return await _final_out(s, p, expected, final, today)


async def redo(
    s: AsyncSession, user: User, project_id: uuid.UUID, data: PlanRedo, today: date
) -> PlanOut:
    """날짜 바꿔 보기: 시작일 · 주말 공사를 바꿔 예상 공정표 새 버전을 만든다.
    업체를 확정한 뒤에는 막는다. 최신 예상 공정표의 시작일 · 주말 공사가 업체가 낼 날짜의 규칙과
    확정 공정표의 예상 자리에 쓰여서, 그 뒤 날짜는 공사 날짜 요청으로만 바꾼다."""
    p = await project_service.get_for(s, user, project_id)
    if user.role != "customer":
        raise AppError(403, "forbidden", "고객만 공정표를 다시 계산할 수 있어요.")
    if data.start <= today or data.start > today + timedelta(days=730):
        raise AppError(422, "start", "내일부터 2년 안의 날짜를 골라 주세요.")
    dealt = select(Deal.id).where(Deal.project_id == p.id, Deal.status != "reselected").limit(1)
    if await s.scalar(dealt) is not None:
        raise AppError(409, "dates_locked", REDO_LOCKED)
    prev = await _latest(s, p.id, "expected")
    if prev and prev.formula_ver.startswith(("image-", "reference-")):
        from app.schedule.expected import missing

        raise missing()
    await _save(s, p, data.start, data.weekend_work, prev)
    got = await _latest(s, p.id, "expected")
    assert got is not None
    return await _out(s, got)


# ---- 업체 확정: 고객이 누른 요청으로만. 요청 상태는 visit.service, 확정(deal)은 여기서 ----

DATES_DUE_DAYS = 2  # 확정하고 2일 뒤 18:00까지 업체가 공사 날짜를 낸다
TRADE_LABEL = LABEL
ALREADY = "이미 업체를 확정했어요."


async def _open_deal(s: AsyncSession, project_id: uuid.UUID, trade: str) -> Deal | None:
    q = select(Deal).where(
        Deal.project_id == project_id, Deal.trade == trade, Deal.status != "reselected"
    )
    return (await s.execute(q.execution_options(populate_existing=True))).scalar_one_or_none()


async def _deal_out(
    s: AsyncSession, deals: list[Deal], today: date, now: datetime
) -> list[DealOut]:
    quotes = await quote_service.briefs_by_id(s, [d.quote_id for d in deals])
    visits = await visit_service.visits_by_id(s, [d.visit_id for d in deals])
    shown = [
        d.vendor_id
        for d in deals
        if d.vendor_id
        and d.visit_id in visits
        and visit_service.contact_visible(
            visits[d.visit_id].status, visits[d.visit_id].contact_until, today
        )
    ]
    contacts = await vendor_service.contacts(s, shown)
    ids = [d.id for d in deals]
    pend, reqs = await _pending(s, ids), await _open_requests(s, ids)
    notes = await _fixing_notes(s, [d.id for d in deals if d.status == "fixing"])
    return [
        DealOut(
            id=d.id,
            trade=d.trade,
            status=d.status,
            vendor_id=d.vendor_id,
            vendor_name=d.vendor_name,
            owner_name=contacts[d.vendor_id].owner_name if d.vendor_id in contacts else None,
            visit_id=d.visit_id,
            quote=quotes[d.quote_id],
            confirmed_at=d.created_at,
            dates_due_at=d.dates_due_at,
            overdue=_overdue(d, pend.get(d.id), reqs.get(d.id), now),
            start_date=d.start_date,
            end_date=d.end_date,
            work_done_on=d.work_done_on,
            contact_until=visits[d.visit_id].contact_until if d.visit_id in visits else None,
            fixing_note=notes.get(d.id),
        )
        for d in deals
    ]


async def confirm(
    s: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    visit_id: uuid.UUID,
    data: ConfirmIn,
    today: date,
) -> ConfirmOut:
    """업체 확정 (한 트랜잭션): 프로젝트 잠금 → 요청 quoted → chosen → 그 버전으로 확정 →
    같은 공정의 다른 요청 닫기 → 이력 · 알림. 공정마다 살아 있는 확정은 하나(부분 UQ)."""
    p = await project_service.get_for(s, user, project_id)
    await project_service.lock_for_visit(s, p.id)  # 같은 집의 확정 · 요청이 줄 선다
    v = await visit_service.in_project(s, p.id, visit_id)
    if await _open_deal(s, p.id, v.trade) is not None:
        raise AppError(409, "already_chosen", ALREADY)
    await visit_service.choose(s, user, v, data.seen_ver)
    quote = await quote_service.sent_by_version(s, v.id, data.seen_ver)
    if quote is None:
        raise AppError(409, "quote_changed", visit_service.QUOTE_CHANGED)
    now = now_utc()
    deal = Deal(
        id=uuid7(),
        project_id=p.id,
        trade=v.trade,
        vendor_id=v.vendor_id,
        vendor_name=v.vendor_name,
        visit_id=v.id,
        quote_id=quote.id,
        status="confirmed",
        dates_due_at=due_at(now, DATES_DUE_DAYS),
        created_at=now,
    )
    try:
        s.add(deal)
        await s.flush()
        s.add(DealEvent(deal_id=deal.id, from_status=None, to_status="confirmed", by_id=user.id))
        closed = await visit_service.close_others(s, p.id, v.trade, v.id, user)
        if v.vendor_id is not None:
            await notify_service.add(
                s,
                user_id=v.vendor_id,
                kind="deal_confirmed",
                body="고객이 확정했어요 · 공사 날짜를 내 주세요",
                link=f"/vendor/jobs/{deal.id}",
                dedup_key=f"deal:{deal.id}",
            )
        await s.commit()
    except IntegrityError as exc:  # 같은 공정을 동시에 두 번 확정하면 부분 UQ가 막는다
        await s.rollback()
        raise AppError(409, "already_chosen", ALREADY) from exc
    deal = await _open_deal(
        s, p.id, v.trade
    )  # 저장된 값으로 다시 읽는다 (시각 표기를 읽기와 맞춘다)
    (out,) = await _deal_out(s, [deal], today, now)
    expired = quote.valid_until is not None and quote.valid_until < today
    return ConfirmOut(deal=out, closed=closed, quote_expired=expired)


AFTER_DATES = "공사 날짜를 수락한 뒤에는 업체를 다시 고를 수 없어요."


async def reselect(
    s: AsyncSession, user: User, project_id: uuid.UUID, deal_id: uuid.UUID
) -> ReselectOut:
    """다시 고르기 (공사 날짜 수락 전만, 한 트랜잭션): 프로젝트 잠금 → 확정 confirmed → reselected →
    확정한 요청 · 남은 요청 닫기(reselected) → 이력 · 알림. 공정의 칸 3개가 다시 빈다."""
    p = await project_service.get_for(s, user, project_id)
    await project_service.lock_for_visit(s, p.id)  # 같은 집의 확정 · 요청 · 다시 고르기가 줄 선다
    deal = await s.get(Deal, deal_id, populate_existing=True)
    if deal is None or deal.project_id != p.id:
        raise AppError(404, "not_found", "업체 확정을 찾을 수 없어요.")
    if deal.status == "reselected":
        raise AppError(409, "invalid_state", "이미 업체를 다시 고르기로 했어요.")
    if deal.status != "confirmed":
        raise AppError(409, "invalid_state", AFTER_DATES)
    res = await s.execute(
        update(Deal)
        .where(Deal.id == deal.id, Deal.status == "confirmed")
        .values(status="reselected")
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:  # 그사이 공사 날짜를 수락했다
        raise AppError(409, "invalid_state", AFTER_DATES)
    s.add(
        DealEvent(deal_id=deal.id, from_status="confirmed", to_status="reselected", by_id=user.id)
    )
    await _close_dates(s, deal.id, now_utc())  # 업체가 낸 공사 날짜도 없어진다
    out = await visit_service.close_for_reselect(s, p.id, deal.trade, user)
    if deal.vendor_id is not None:
        await notify_service.add(
            s,
            user_id=deal.vendor_id,
            kind="deal_reselected",
            body=visit_service.RESELECTED,
            link=f"/vendor/requests/{deal.visit_id}",
            dedup_key=f"deal:{deal.id}:reselected",
        )
    await s.commit()
    return out


async def deals_for(
    s: AsyncSession, user: User, project_id: uuid.UUID, trade: str | None, today: date
) -> list[DealOut]:
    """이 집의 업체 확정 (다시 고르기 한 것은 빼고)"""
    p = await project_service.get_for(s, user, project_id)
    q = select(Deal).where(Deal.project_id == p.id, Deal.status != "reselected")
    if trade:
        q = q.where(Deal.trade == trade)
    deals = list((await s.execute(q.order_by(Deal.created_at))).scalars().all())
    return await _deal_out(s, deals, today, now_utc())


async def compare(
    s: AsyncSession, user: User, project_id: uuid.UUID, trade: str, today: date
) -> CompareDealOut:
    base = await visit_service.compare(s, user, project_id, trade, today)
    deal = await _open_deal(s, project_id, trade)
    out = (await _deal_out(s, [deal], today, now_utc()))[0] if deal else None
    return CompareDealOut(**base.model_dump(), deal=out)


async def vendor_inbox(
    s: AsyncSession,
    vendor: User,
    tab: str,
    today: date,
    before: datetime | None,
    limit: int,
) -> InboxPageOut:
    """업체 작업함. 공사 탭은 확정한 공사(끝남 · 다시 고르기 빼고), 확정한 순 최근 먼저."""
    if tab == "done":
        return await _done_tab(s, vendor, today, before, limit)
    if tab != "jobs":
        page = await visit_service.inbox(s, vendor, tab, today, before, limit)
        return InboxPageOut(
            items=[InboxRowOut(**r.model_dump()) for r in page.items], next_before=page.next_before
        )
    q = (
        select(Deal)
        .where(Deal.vendor_id == vendor.id, Deal.status.in_(OPEN_DEALS))
        .order_by(Deal.created_at.desc())
    )
    deals = list((await s.execute(q)).scalars().all())
    rows = await visit_service.rows_for(s, [d.visit_id for d in deals], today)
    until = {
        v.id: v.contact_until
        for v in (await visit_service.visits_by_id(s, [d.visit_id for d in deals])).values()
    }
    ids = [d.id for d in deals]
    pend, reqs = await _pending(s, ids), await _open_requests(s, ids)
    waiting = await _extras_pending(s, ids)
    notes = await _fixing_notes(s, [d.id for d in deals if d.status == "fixing"])
    now = now_utc()
    items = []
    for d in deals:
        if d.visit_id not in rows:
            continue
        x, r = pend.get(d.id), reqs.get(d.id)
        items.append(
            InboxRowOut(
                **rows[d.visit_id].model_dump(),
                deal_id=d.id,
                deal_status=d.status,
                confirmed_at=d.created_at,
                dates_due_at=d.dates_due_at,
                overdue=_overdue(d, x, r, now),
                start_date=d.start_date,
                end_date=d.end_date,
                dates_state=_state(d, x, r),
                proposed_start=x.start_date if x else None,
                proposed_end=x.end_date if x else None,
                proposed_days=days_since(x.created_at, today) if x else None,
                wish_dates=list(r.wish_dates) if r else [],
                request_due_at=r.due_at if r else None,
                extras_pending=waiting.get(d.id, 0),
                work_done_on=d.work_done_on,
                contact_until=until.get(d.visit_id),
                fixing_note=notes.get(d.id),
            )
        )
    return InboxPageOut(items=items, next_before=None)


async def vendor_summary(s: AsyncSession, vendor: User) -> InboxSummaryOut:
    base = await visit_service.inbox_summary(s, vendor)
    n = await s.scalar(
        select(func.count())
        .select_from(Deal)
        .where(Deal.vendor_id == vendor.id, Deal.status.in_(OPEN_DEALS))
    )
    done = await s.scalar(
        select(func.count())
        .select_from(Deal)
        .where(Deal.vendor_id == vendor.id, Deal.status == "done")
    )
    return base.model_copy(update={"jobs": int(n or 0), "done": base.done + int(done or 0)})


# ---- 공사 날짜: 업체가 내고 고객이 수락하거나 다른 날짜를 요청한다. 날짜 · 기간 · 기한은 calc가 만든다 ----

ORDER = ("mokgong", "dobae")  # 앞 공정이 끝난 다음 날부터 뒤 공정을 한다
AFTER_DATES_ACCEPTED = ("scheduled", "checking", "fixing", "done")
REQUEST_DUE_DAYS = 1  # 다른 날짜 요청: 업체는 요청 다음 날 18:00까지 다시 낸다
REQUESTS_MAX = 2  # deals.date_request_count CHECK와 같다
OPTIONS = 5  # 다른 날짜 요청 후보 수
HOLIDAY_DAYS = 180  # 화면 달력에 보여 줄 공휴일 범위
AHEAD_DAYS = 365  # 날짜는 1년 안으로
NO_DEAL = "공사를 찾을 수 없어요."
STALE = "그사이 상태가 바뀌었어요. 새로 불러올게요."


async def _pending(s: AsyncSession, deal_ids: list[uuid.UUID]) -> dict[uuid.UUID, DateProposal]:
    if not deal_ids:
        return {}
    q = select(DateProposal).where(
        DateProposal.deal_id.in_(deal_ids), DateProposal.status == "pending"
    )
    rows = (await s.execute(q.execution_options(populate_existing=True))).scalars()
    return {x.deal_id: x for x in rows}


async def _open_requests(
    s: AsyncSession, deal_ids: list[uuid.UUID]
) -> dict[uuid.UUID, DateRequest]:
    if not deal_ids:
        return {}
    q = select(DateRequest).where(DateRequest.deal_id.in_(deal_ids), DateRequest.status == "open")
    rows = (await s.execute(q.execution_options(populate_existing=True))).scalars()
    return {r.deal_id: r for r in rows}


async def _changed(s: AsyncSession, deal_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """앞서 낸 날짜를 바꿔 다시 낸 공사"""
    if not deal_ids:
        return set()
    q = select(DateProposal.deal_id).where(
        DateProposal.deal_id.in_(deal_ids), DateProposal.status == "superseded"
    )
    return set((await s.execute(q)).scalars())


def _state(d: Deal, pending: DateProposal | None, req: DateRequest | None) -> str:
    if d.status == "reselected":
        return "closed"
    if d.status == "confirmed":
        if req is not None:
            return "requested"
        return "proposed" if pending is not None else "waiting"
    if d.status == "scheduled":  # 수락한 뒤 날짜 바꾸기
        if req is not None:
            return "change_requested"
        if pending is not None:
            return "change_proposed"
    return "scheduled"


def _overdue(d: Deal, pending: DateProposal | None, req: DateRequest | None, now: datetime) -> bool:
    """기한 지남(상태가 아님): 날짜를 아직 안 냈는데 확정 기한이 지났거나, 열린 요청의 기한이 지났다."""
    if req is not None:  # 날짜 요청 · 바꾸기 요청 모두 기한이 있다
        return now > req.due_at
    return d.status == "confirmed" and pending is None and now > d.dates_due_at


def _weekend(plan: Plan | None) -> tuple[int, ...]:
    return () if plan is not None and plan.weekend_work else WEEKEND


def _holidays(today: date) -> list[HolidayOut]:
    end = today + timedelta(days=HOLIDAY_DAYS)
    return [HolidayOut(day=d, name=n) for d, n in sorted(HOLIDAYS.items()) if today <= d <= end]


def _proposal_out(x: DateProposal, changed: bool, weekend: tuple[int, ...]) -> ProposalOut:
    return ProposalOut(
        id=x.id,
        start=x.start_date,
        end=x.end_date,
        workdays=count_workdays(x.start_date, x.end_date, _off(x.start_date), weekend),
        crew=x.crew,
        method=x.method,
        call_on=x.call_on,
        kind=x.kind,
        reason=x.reason,
        status=x.status,
        request_id=x.request_id,
        changed=changed,
        created_at=x.created_at,
        decided_at=x.decided_at,
    )


def _request_out(r: DateRequest, now: datetime) -> DateRequestOut:
    return DateRequestOut(
        id=r.id,
        wish_dates=list(r.wish_dates),
        reason=r.reason,
        due_at=r.due_at,
        overdue=r.status == "open" and now > r.due_at,
        status=r.status,
        created_at=r.created_at,
    )


def _other(d: Deal) -> OtherTradeOut:
    return OtherTradeOut(
        trade=d.trade, vendor_name=d.vendor_name, start=d.start_date, end=d.end_date
    )


async def _others(s: AsyncSession, project_id: uuid.UUID, trade: str) -> list[Deal]:
    """이 집 다른 공정에서 고객이 수락한 공사 날짜"""
    q = select(Deal).where(
        Deal.project_id == project_id,
        Deal.trade != trade,
        Deal.status.in_(AFTER_DATES_ACCEPTED),
        Deal.start_date.is_not(None),
    )
    return list((await s.execute(q)).scalars().all())


def _fit(
    trade: str, start: date, end: date, others: list[Deal], weekend: tuple[int, ...], today: date
) -> FitOut:
    dated = [Dated(trade, start, end)] + [Dated(o.trade, o.start_date, o.end_date) for o in others]
    hit = {a if b == trade else b for a, b in order_conflicts(dated, ORDER) if trade in (a, b)}
    off = _off(min([today, start] + [o.start_date for o in others]))
    start_from, end_by = free_range(trade, dated, ORDER, off, weekend)
    return FitOut(
        conflicts=[_other(o) for o in others if o.trade in hit],
        start_from=start_from,
        end_by=end_by,
    )


def _check_range(start: date, end: date, today: date, weekend: tuple[int, ...]) -> int:
    """업체가 낼 날짜: 내일부터 1년 안, 끝 ≥ 시작, 120일 안, 시작 · 끝은 일하는 날. 일하는 날 수."""
    if start <= today or start > today + timedelta(days=AHEAD_DAYS):
        raise AppError(422, "start", "시작일은 내일부터 1년 안으로 골라 주세요.")
    if end < start:
        raise AppError(422, "end", "끝나는 날은 시작일과 같거나 뒤로 골라 주세요.")
    if (end - start).days > JOB_MAX_DAYS:
        raise AppError(422, "end", f"공사 날짜는 {JOB_MAX_DAYS}일 안으로 골라 주세요.")
    off = _off(start)
    if is_off(start, off, weekend) or is_off(end, off, weekend):
        raise AppError(
            422, "off_day", "시작일과 끝나는 날은 일하는 날로 골라 주세요. 주말 · 공휴일은 쉬어요."
        )
    return count_workdays(start, end, off, weekend)


def _need(p: ProjectOut, trade: str, crew: int | None) -> NeedOut:
    """그 인원으로 센 필요한 기간 (없으면 믿집 가정 인원)"""
    got = [
        c for c in quote_service.calc_trades(p, {trade: crew} if crew else None) if c.trade == trade
    ]
    if not got:
        raise AppError(409, "trade", "이 집에서 고른 공정이 아니에요.")
    c = got[0]
    return NeedOut(
        man_day=c.man_day,
        crew=c.crew,
        days=trade_days(c.steps),
        steps=[StepNeedOut(name=st.name, half_days=st.half_days) for st in c.steps],
    )


async def _lock_deal(s: AsyncSession, deal_id: uuid.UUID) -> Deal | None:
    q = select(Deal).where(Deal.id == deal_id).with_for_update()
    return (await s.execute(q.execution_options(populate_existing=True))).scalar_one_or_none()


async def _close_dates(s: AsyncSession, deal_id: uuid.UUID, now: datetime) -> None:
    """다시 고르기: 수락 기다리는 날짜 · 열린 요청을 withdrawn으로 (부르는 쪽 트랜잭션 안에서)"""
    await s.execute(
        update(DateProposal)
        .where(DateProposal.deal_id == deal_id, DateProposal.status == "pending")
        .values(status="withdrawn", decided_at=now)
        .execution_options(synchronize_session=False)
    )
    await s.execute(
        update(DateRequest)
        .where(DateRequest.deal_id == deal_id, DateRequest.status == "open")
        .values(status="withdrawn", closed_at=now)
        .execution_options(synchronize_session=False)
    )


async def _set(s: AsyncSession, model, row_id: uuid.UUID, frm: str, values: dict) -> None:
    """지금 상태가 frm일 때만 바꾼다. 1행이 아니면 409."""
    res = await s.execute(
        update(model)
        .where(model.id == row_id, model.status == frm)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        raise AppError(409, "stale", STALE)


def _base(d: Deal, x: DateProposal | None) -> tuple[date, date] | None:
    """다른 날짜 요청의 기준 날짜: 수락 전이면 낸 날짜, 수락 뒤면 지금 날짜 (그와 같은 기간으로 고른다)"""
    if d.status == "scheduled" and d.start_date and d.end_date:
        return d.start_date, d.end_date
    if d.status == "confirmed" and x is not None:
        return x.start_date, x.end_date
    return None


def _start_options(
    trade: str, base: tuple[date, date], others: list[Deal], weekend: tuple[int, ...], today: date
) -> list[StartOptionOut]:
    """요청 후보: 기준과 같은 일하는 날 수, 다른 공정과 겹치지 않는 자리(앞 공정 다음 날부터 뒤 공정
    전날까지) 안에서 내일부터. 기준 시작일은 뺀다."""
    tomorrow = today + timedelta(days=1)
    room = _fit(trade, *base, others, weekend, today)
    first = max(tomorrow, room.start_from or tomorrow)
    n = count_workdays(*base, _off(base[0]), weekend)
    return [
        StartOptionOut(start=a, end=b)
        for a, b in start_choices(
            first, max(n, 1), OPTIONS, {base[0]}, _off(first), weekend, room.end_by
        )
    ]


async def schedule_for(
    s: AsyncSession, user: User, project_id: uuid.UUID, today: date
) -> ScheduleOut:
    """GET /projects/{id}/schedule: 공정마다 업체 확정 · 낸 날짜 · 다른 날짜 요청 · 겹침 · 요청 후보"""
    p = await project_service.get_for(s, user, project_id)
    plan = await ensure_expected(s, p, today)
    weekend = _weekend(plan)
    q = select(Deal).where(Deal.project_id == p.id, Deal.status != "reselected")
    deals = sorted(
        (await s.execute(q.execution_options(populate_existing=True))).scalars().all(),
        key=lambda d: ORDER.index(d.trade),
    )
    now = now_utc()
    outs = await _deal_out(s, deals, today, now)
    ids = [d.id for d in deals]
    pend, reqs, changed = (
        await _pending(s, ids),
        await _open_requests(s, ids),
        await _changed(s, ids),
    )
    accepted = [d for d in deals if d.status in AFTER_DATES_ACCEPTED and d.start_date]
    extras = await _extras(s, deals)
    trades = []
    for d, out in zip(deals, outs, strict=True):
        x, r = pend.get(d.id), reqs.get(d.id)
        others = [o for o in accepted if o.trade != d.trade]
        fit = _fit(d.trade, x.start_date, x.end_date, others, weekend, today) if x else None
        base = _base(d, x)
        options = _start_options(d.trade, base, others, weekend, today) if base else []
        trades.append(
            TradeDatesOut(
                deal=out,
                dates_state=_state(d, x, r),
                proposal=_proposal_out(x, d.id in changed, weekend) if x else None,
                request=_request_out(r, now) if r else None,
                requests_left=REQUESTS_MAX - d.date_request_count,
                fit=fit,
                start_options=options,
                extras=extras[d.id],
            )
        )
    final = await _latest(s, p.id, "final")
    return ScheduleOut(
        project_id=p.id,
        weekend_work=plan.weekend_work,
        holidays=_holidays(today),
        final_version=final.version if final else 0,
        trades=trades,
    )


CHANGEABLE = ("confirmed", "scheduled")  # 공사 끝냈어요(24번) 전까지 날짜를 내고 바꾼다
DONE_DATES = "공사를 끝냈다고 올린 뒤에는 날짜를 바꿀 수 없어요."


def _changeable(d: Deal) -> None:
    if d.status == "reselected":
        raise AppError(409, "invalid_state", "다시 고른 업체의 공사예요.")
    if d.status not in CHANGEABLE:
        raise AppError(409, "invalid_state", DONE_DATES)


def _iso(a: date, b: date) -> str:
    return f"{a.isoformat()}~{b.isoformat()}"


async def _accepted_crews(s: AsyncSession, deal_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """수락한 날짜의 하루 인원 (가장 최근에 수락한 것)"""
    if not deal_ids:
        return {}
    q = (
        select(DateProposal)
        .where(DateProposal.deal_id.in_(deal_ids), DateProposal.status == "accepted")
        .order_by(DateProposal.deal_id, DateProposal.decided_at.desc())
    )
    out: dict[uuid.UUID, int] = {}
    for x in (await s.execute(q)).scalars():
        out.setdefault(x.deal_id, x.crew)
    return out


def _compose(
    p: ProjectOut,
    fixed: dict[str, tuple[date, date]],
    crews: dict[str, int],
    expected: Plan,
    today: date,
):
    """정한 날짜는 그대로, 날짜 없는 공정은 예상 자리에 놓은 공정표 (계산 엔진)"""
    calcs = quote_service.calc_trades(p, crews)
    trades = [TradePlan(c.trade, c.steps, c.crew) for c in calcs]
    start = max(expected.start_date, today + timedelta(days=1))
    off = _off(min([start, today] + [f[0] for f in fixed.values()]))
    return fix_plan(trades, fixed, start, off, _weekend(expected)), {
        c.trade: c.man_day for c in calcs
    }


def _plan_parts(plan, man_days, states: dict[str, str], deals: dict[str, Deal]):
    blocks = [
        PlanBlockOut(
            trade=b.trade,
            kind=b.kind,
            label=b.label or LABEL.get(b.trade or "", ""),
            start=b.start,
            end=b.end,
            days=b.days,
            crew=b.crew,
            man_day=man_days.get(b.trade) if b.trade else None,
            state=states.get(b.trade or "", "expected"),
            deal_id=deals[b.trade].id if b.trade in deals else None,
            vendor_name=deals[b.trade].vendor_name if b.trade in deals else None,
        )
        for b in plan.blocks
    ]
    days = [
        PlanDayOut(
            day=d.day,
            trade=d.trade,
            kind=d.kind,
            names=list(d.names),
            holiday=HOLIDAYS.get(d.day),
            state=states.get(d.trade or "", "expected"),
        )
        for d in plan.days
    ]
    return blocks, days


async def _final_parts(s: AsyncSession, project_id: uuid.UUID):
    """살아 있는 확정 · 수락한 확정 · 수락 기다리는 날짜 · 인원"""
    q = select(Deal).where(Deal.project_id == project_id, Deal.status != "reselected")
    deals = list((await s.execute(q.execution_options(populate_existing=True))).scalars().all())
    accepted = [d for d in deals if d.status in AFTER_DATES_ACCEPTED and d.start_date]
    ids = [d.id for d in deals]
    pend = await _pending(s, ids)
    crews = await _accepted_crews(s, [d.id for d in accepted])
    return deals, accepted, pend, crews


async def _save_final(
    s: AsyncSession, p: ProjectOut, expected: Plan, seen: int, today: date
) -> Plan:
    """확정 공정표 새 버전 (부르는 쪽 트랜잭션 안, 확정 날짜를 바꾼 뒤). 본 버전이 최신이 아니면 409.
    (project, final, version) UQ가 동시에 둘이 만드는 것도 막는다."""
    latest = await _latest(s, p.id, "final")
    if (latest.version if latest else 0) != seen:
        raise AppError(409, "plan_changed", "공정표가 바뀌었어요. 새로 불러올게요.")
    _, accepted, _, crews = await _final_parts(s, p.id)
    fixed = {d.trade: (d.start_date, d.end_date) for d in accepted}
    plan, man_days = _compose(
        p, fixed, {d.trade: crews[d.id] for d in accepted if d.id in crews}, expected, today
    )
    row = Plan(
        project_id=p.id,
        kind="final",
        version=seen + 1,
        project_rev=p.rev,
        formula_ver=FORMULA_VERSION,
        start_date=plan.start,
        end_date=plan.end,
        work_days=plan.work_days,
        weekend_work=expected.weekend_work,
    )
    s.add(row)
    await s.flush()
    _add_parts(s, row, plan, man_days, {d.trade: d.id for d in accepted})
    return row


async def _final_out(
    s: AsyncSession, p: ProjectOut, expected: Plan, final: Plan, today: date
) -> PlanOut:
    """확정 공정표: 수락한 날짜(확정) · 수락 기다리는 첫 날짜(수락 전) · 날짜 없는 공정(예상)을
    계산 엔진으로 얹는다. 수락한 날짜를 바꾸자는 새 날짜는 changes에 바꾼 뒤 모습으로."""
    deals, accepted, pend, crews = await _final_parts(s, p.id)
    by = {d.trade: d for d in deals}
    fixed = {d.trade: (d.start_date, d.end_date) for d in accepted}
    states = {d.trade: "confirmed" for d in accepted}
    use = {d.trade: crews[d.id] for d in accepted if d.id in crews}
    for d in deals:
        x = pend.get(d.id)
        if d.status == "confirmed" and x is not None:
            fixed[d.trade], states[d.trade], use[d.trade] = (
                (x.start_date, x.end_date),
                "pending",
                x.crew,
            )
    plan, man_days = _compose(p, fixed, use, expected, today)
    blocks, days = _plan_parts(plan, man_days, states, by)
    weekend = _weekend(expected)
    changed = await _changed(s, [d.id for d in accepted])
    changes = []
    for d in accepted:
        x = pend.get(d.id)
        if x is None:
            continue
        moved = {**fixed, d.trade: (x.start_date, x.end_date)}
        plan2, md2 = _compose(p, moved, {**use, d.trade: x.crew}, expected, today)
        b2, d2 = _plan_parts(plan2, md2, {**states, d.trade: "pending"}, by)
        others = [o for o in accepted if o.trade != d.trade]
        changes.append(
            PlanChangeOut(
                trade=d.trade,
                deal_id=d.id,
                vendor_name=d.vendor_name,
                current_start=d.start_date,
                current_end=d.end_date,
                proposal=_proposal_out(x, d.id in changed, weekend),
                fit=_fit(d.trade, x.start_date, x.end_date, others, weekend, today),
                start=plan2.start,
                end=plan2.end,
                work_days=plan2.work_days,
                blocks=b2,
                days=d2,
            )
        )
    return PlanOut(
        project_id=p.id,
        kind="final",
        version=final.version,
        project_rev=final.project_rev,
        start=plan.start,
        end=plan.end,
        work_days=plan.work_days,
        weekend_work=expected.weekend_work,
        computed_at=final.created_at,
        blocks=blocks,
        days=days,
        changes=changes,
        extras=list((await _extras(s, deals)).values()),
        deals=await _deal_out(
            s, sorted(deals, key=lambda d: ORDER.index(d.trade)), today, now_utc()
        ),
    )


async def accept_dates(
    s: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    deal_id: uuid.UUID,
    proposal_id: uuid.UUID,
    data: AcceptIn,
    today: date,
) -> ScheduleOut:
    """고객이 낸 날짜 수락 (한 트랜잭션). 잠금 순서: 프로젝트 → 확정 → 제안 · 요청 → 공정표(insert).
    처음이면 확정 confirmed → scheduled, 수락한 뒤 바꾸기면 scheduled 그대로 날짜만 바꾼다(이력 note).
    열린 요청은 닫고, 확정 공정표 새 버전을 만든다(본 버전 + 1)."""
    p = await project_service.get_for(s, user, project_id)
    expected = await ensure_expected(s, p, today)
    await project_service.lock_for_visit(s, p.id)  # 다른 공정 수락과 겹침 확인 · 버전이 줄 선다
    d = await _lock_deal(s, deal_id)
    if d is None or d.project_id != p.id:
        raise AppError(404, "not_found", NO_DEAL)
    x = await s.get(DateProposal, proposal_id, populate_existing=True)
    if x is None or x.deal_id != d.id:
        raise AppError(404, "not_found", "공사 날짜를 찾을 수 없어요.")
    if d.status == "reselected":
        raise AppError(409, "invalid_state", "다시 고른 업체의 날짜라 수락할 수 없어요.")
    if d.status not in CHANGEABLE:
        raise AppError(409, "invalid_state", DONE_DATES)
    if x.status == "superseded":
        raise AppError(409, "dates_changed", "날짜가 바뀌었어요. 새 날짜를 확인해 주세요.")
    if x.status == "accepted":
        raise AppError(409, "invalid_state", "이미 공사 날짜를 수락했어요.")
    if x.status != "pending":
        raise AppError(409, "invalid_state", "수락할 수 없는 날짜예요.")
    if x.start_date < today:
        raise AppError(409, "start_passed", "시작일이 지났어요. 다른 날짜를 요청해 주세요.")
    others = await _others(s, p.id, d.trade)
    if _fit(d.trade, x.start_date, x.end_date, others, _weekend(expected), today).conflicts:
        raise AppError(409, "overlap", "겹치는 날이 있어 수락할 수 없어요.")
    now = now_utc()
    first = d.status == "confirmed"
    try:
        await _set(s, DateProposal, x.id, "pending", {"status": "accepted", "decided_at": now})
        dates = {"start_date": x.start_date, "end_date": x.end_date}
        await _set(s, Deal, d.id, d.status, {"status": "scheduled", **dates})
        s.add(
            DealEvent(
                deal_id=d.id,
                from_status=d.status,
                to_status="scheduled",
                by_id=user.id,
                note=None
                if first
                else f"날짜 바꿈 {_iso(d.start_date, d.end_date)} → {_iso(x.start_date, x.end_date)}",
            )
        )
        await s.execute(
            update(DateRequest)
            .where(DateRequest.deal_id == d.id, DateRequest.status == "open")
            .values(status="withdrawn", closed_at=now)
            .execution_options(synchronize_session=False)
        )
        await _save_final(s, p, expected, data.seen_version, today)
        if d.vendor_id is not None:
            await notify_service.add(
                s,
                user_id=d.vendor_id,
                kind="dates_accepted",
                body="고객이 공사 날짜를 수락했어요"
                if first
                else "고객이 새 공사 날짜를 수락했어요",
                link=f"/vendor/jobs/{d.id}",
                dedup_key=f"date_proposal:{x.id}:accepted",
            )
        await s.commit()
    except IntegrityError as exc:  # 확정 공정표 버전 UQ
        await s.rollback()
        raise AppError(409, "plan_changed", "공정표가 바뀌었어요. 새로 불러올게요.") from exc
    return await schedule_for(s, user, project_id, today)


async def request_dates(
    s: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    deal_id: uuid.UUID,
    data: DateRequestIn,
    today: date,
) -> ScheduleOut:
    """다른 날짜 요청 · 수락한 날짜 바꾸기 요청 (합쳐 2번까지): 지금 날짜는 그대로 두고 업체가
    다음 날 18:00까지 새 날짜를 낸다. 수락한 뒤에는 이유가 필수다."""
    p = await project_service.get_for(s, user, project_id)
    weekend = _weekend(await ensure_expected(s, p, today))
    off = _off(today)
    for w in data.wish_dates:
        if w <= today or w > today + timedelta(days=AHEAD_DAYS):
            raise AppError(422, "wish_dates", "시작해도 되는 날은 내일부터 1년 안으로 골라 주세요.")
        if is_off(w, off, weekend):
            raise AppError(422, "wish_dates", "시작해도 되는 날은 일하는 날로 골라 주세요.")
    await project_service.lock_for_visit(s, p.id)
    d = await _lock_deal(s, deal_id)
    if d is None or d.project_id != p.id:
        raise AppError(404, "not_found", NO_DEAL)
    _changeable(d)
    change = d.status == "scheduled"
    if change and data.reason is None:
        raise AppError(422, "reason", "날짜를 바꾸고 싶은 이유를 적어 주세요.")
    x = (await _pending(s, [d.id])).get(d.id)
    if not change and x is None:
        raise AppError(409, "no_dates", "업체가 공사 날짜를 낸 뒤에 요청할 수 있어요.")
    base = _base(d, x)
    # 고른 날에 같은 기간으로 하면 수락한 다른 공정과 겹치는지 (겹치면 수락할 수 없다)
    if base is not None:
        others = await _others(s, p.id, d.trade)
        n = max(count_workdays(*base, _off(base[0]), weekend), 1)
        for w in data.wish_dates:
            end = workdays(w, n, _off(w), weekend)[-1]
            if _fit(d.trade, w, end, others, weekend, today).conflicts:
                raise AppError(
                    422,
                    "wish_dates",
                    "다른 공정과 겹치는 날이 있어요. 고를 수 있는 날 중에서 골라 주세요.",
                )
    if (await _open_requests(s, [d.id])).get(d.id) is not None:
        raise AppError(
            409, "already_requested", "이미 다른 날짜를 요청했어요. 업체 답을 기다려 주세요."
        )
    used = "다른 날짜 요청은 2번까지 할 수 있어요."
    if d.date_request_count >= REQUESTS_MAX:
        raise AppError(409, "requests_used", used)
    now = now_utc()
    try:
        res = await s.execute(
            update(Deal)
            .where(
                Deal.id == d.id,
                Deal.status == d.status,
                Deal.date_request_count < REQUESTS_MAX,
            )
            .values(date_request_count=Deal.date_request_count + 1)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount != 1:
            raise AppError(409, "requests_used", used)
        req = DateRequest(
            id=uuid7(),
            deal_id=d.id,
            reason=data.reason,
            wish_dates=sorted(data.wish_dates),
            due_at=due_at(now, REQUEST_DUE_DAYS),
            by_id=user.id,
        )
        s.add(req)
        await s.flush()
        if d.vendor_id is not None:
            await notify_service.add(
                s,
                user_id=d.vendor_id,
                kind="dates_requested",
                body="고객이 공사 날짜 바꾸기를 요청했어요"
                if change
                else "고객이 다른 공사 날짜를 요청했어요",
                link=f"/vendor/jobs/{d.id}",
                dedup_key=f"date_request:{req.id}",
            )
        await s.commit()
    except IntegrityError as exc:  # 열린 요청은 하나 (부분 UQ)
        await s.rollback()
        raise AppError(409, "already_requested", "이미 다른 날짜를 요청했어요.") from exc
    return await schedule_for(s, user, project_id, today)


def _request_options(
    d: Deal, r: DateRequest | None, weekend: tuple[int, ...]
) -> list[StartOptionOut]:
    """수락한 날짜 바꾸기 요청: 고객이 고른 시작일마다 지금과 같은 일하는 날 수로 끝나는 날"""
    if r is None or d.status != "scheduled" or d.start_date is None:
        return []
    n = count_workdays(d.start_date, d.end_date, _off(d.start_date), weekend)
    off = _off(min(r.wish_dates))
    return [
        StartOptionOut(start=w, end=workdays(w, max(n, 1), off, weekend)[-1])
        for w in sorted(r.wish_dates)
    ]


async def _vendor_deal(s: AsyncSession, vendor: User, deal_id: uuid.UUID) -> Deal:
    d = await s.get(Deal, deal_id, populate_existing=True)
    if d is None or d.vendor_id != vendor.id:
        raise AppError(404, "not_found", NO_DEAL)
    return d


async def job_for(s: AsyncSession, vendor: User, deal_id: uuid.UUID, today: date) -> JobOut:
    """GET /vendor/jobs/{id}: 공사 날짜 내기 화면 (내 확정만)"""
    d = await _vendor_deal(s, vendor, deal_id)
    p = await project_service.get_out(s, d.project_id)
    plan = await ensure_expected(s, p, today)
    weekend = _weekend(plan)
    block = (
        await s.scalars(
            select(PlanStep).where(
                PlanStep.plan_id == plan.id, PlanStep.trade == d.trade, PlanStep.kind == "work"
            )
        )
    ).first()
    visit = await visit_service.vendor_visit(s, vendor, d.visit_id, today)
    now = now_utc()
    (out,) = await _deal_out(s, [d], today, now)
    x = (await _pending(s, [d.id])).get(d.id)
    r = (await _open_requests(s, [d.id])).get(d.id)
    return JobOut(
        deal_id=d.id,
        trade=d.trade,
        status=d.status,
        confirmed_at=d.created_at,
        dates_due_at=d.dates_due_at,
        overdue=out.overdue,
        start_date=d.start_date,
        end_date=d.end_date,
        visit=visit,
        quote=out.quote,
        plan_version=plan.version,
        plan_start=block.start_date if block else None,
        plan_end=block.end_date if block else None,
        need=_need(p, d.trade, None),
        others=[_other(o) for o in await _others(s, d.project_id, d.trade)],
        weekend_work=plan.weekend_work,
        holidays=_holidays(today),
        dates_state=_state(d, x, r),
        proposal=_proposal_out(x, d.id in await _changed(s, [d.id]), weekend) if x else None,
        request=_request_out(r, now) if r else None,
        requests_left=REQUESTS_MAX - d.date_request_count,
        request_options=_request_options(d, r, weekend),
        extras=(await _extras(s, [d]))[d.id],
        accepted_crew=(await _accepted_crews(s, [d.id])).get(d.id),
        work_done_on=d.work_done_on,
        contact_until=out.contact_until,
        fixing_note=out.fixing_note,
        can_finish=d.status in FINISHABLE and d.end_date is not None and today >= d.end_date,
    )


async def preview_dates(
    s: AsyncSession,
    vendor: User,
    deal_id: uuid.UUID,
    start: date,
    end: date,
    crew: int,
    today: date,
) -> PreviewOut:
    """낼 날짜 미리 계산 (저장하지 않음): 일하는 날 · 그 인원의 필요한 기간 · 다른 공정과 겹침"""
    d = await _vendor_deal(s, vendor, deal_id)
    p = await project_service.get_out(s, d.project_id)
    weekend = _weekend(await ensure_expected(s, p, today))
    n = _check_range(start, end, today, weekend)
    need = _need(p, d.trade, crew)
    others = await _others(s, d.project_id, d.trade)
    return PreviewOut(
        workdays=n,
        need=need,
        short=n < need.days,
        fit=_fit(d.trade, start, end, others, weekend, today),
    )


async def propose_dates(
    s: AsyncSession, vendor: User, deal_id: uuid.UUID, data: DatesIn, today: date
) -> JobOut:
    """업체가 공사 날짜 내기 (한 트랜잭션). 잠금 순서: 프로젝트 → 확정 → 제안 · 요청.
    앞서 낸 날짜 superseded → 새 날짜 pending → 열린 요청 answered → 고객 알림. 겹쳐도 낼 수 있다.
    수락한 뒤(scheduled)에는 바꾸기(change): 고객 요청에 답하면 고객이 고른 시작일 중에서 같은 기간,
    스스로 바꾸면 이유 필수. 고객이 수락하기 전까지 지금 날짜는 그대로다."""
    d = await _vendor_deal(s, vendor, deal_id)
    p = await project_service.get_out(s, d.project_id)
    plan = await ensure_expected(s, p, today)
    weekend = _weekend(plan)
    _check_range(data.start, data.end, today, weekend)
    if data.method == "call":
        if data.call_on is None:
            raise AppError(422, "call_on", "통화한 날을 골라 주세요.")
        if data.call_on > today:
            raise AppError(422, "call_on", "통화한 날은 오늘까지만 고를 수 있어요.")
    await project_service.lock(s, d.project_id)  # 잠금 순서: 프로젝트 → 확정 → 제안 · 요청
    d = await _lock_deal(s, d.id)
    if d is None or d.vendor_id != vendor.id:
        raise AppError(404, "not_found", NO_DEAL)
    if d.status == "reselected":
        raise AppError(409, "invalid_state", "고객이 업체를 다시 고르기로 했어요.")
    if d.status not in CHANGEABLE:
        raise AppError(409, "invalid_state", DONE_DATES)
    change = d.status == "scheduled"
    if change and data.kind == "initial":  # 그사이 고객이 수락했다
        raise AppError(409, "invalid_state", "고객이 이미 공사 날짜를 수락했어요.")
    if not change and data.kind == "change":
        raise AppError(
            409, "invalid_state", "고객이 아직 수락하지 않은 날짜예요. 날짜 다시 내기로 내 주세요."
        )
    x = (await _pending(s, [d.id])).get(d.id)
    r = (await _open_requests(s, [d.id])).get(d.id)
    if data.request_id is not None and (r is None or r.id != data.request_id):
        raise AppError(409, "request_changed", "고객 요청이 바뀌었어요. 새로 불러올게요.")
    if change and r is not None:
        allowed = {(o.start, o.end) for o in _request_options(d, r, weekend)}
        if (data.start, data.end) not in allowed:
            raise AppError(
                422, "request_dates", "고객이 고른 시작일 중에서 같은 기간으로 골라 주세요."
            )
    if change and r is None and data.reason is None:
        raise AppError(422, "reason", "날짜를 바꾸는 이유를 적어 주세요. 고객에게 보여요.")
    owner = (await project_service.briefs(s, [d.project_id]))[d.project_id].owner_id
    now = now_utc()
    try:
        if x is not None:
            await _set(
                s, DateProposal, x.id, "pending", {"status": "superseded", "decided_at": now}
            )
        new = DateProposal(
            id=uuid7(),
            deal_id=d.id,
            start_date=data.start,
            end_date=data.end,
            crew=data.crew,
            method=data.method,
            call_on=data.call_on if data.method == "call" else None,
            request_id=r.id if r else None,
            kind="change" if change else "initial",
            reason=data.reason if change else None,
            plan_version=plan.version,
            by_id=vendor.id,
        )
        s.add(new)
        await s.flush()
        if r is not None:
            await _set(s, DateRequest, r.id, "open", {"status": "answered", "closed_at": now})
        who, label = i_ga(d.vendor_name), LABEL[d.trade]
        if not change:
            again = "다시 " if x is not None else ""
            body, link = f"{who} 공사 날짜를 {again}냈어요 · {label}", "schedule?trade=" + d.trade
        elif r is not None:
            body, link = f"{who} 새 {label} 날짜를 올렸어요", "plan"
        else:
            body, link = f"{who} {label} 날짜를 바꾸자고 했어요", "plan"
        await notify_service.add(
            s,
            user_id=owner,
            kind="dates_proposed",
            body=body,
            link=f"/projects/{d.project_id}/{link}",
            dedup_key=f"date_proposal:{new.id}",
            project_id=d.project_id,
        )
        await s.commit()
    except IntegrityError as exc:  # 수락 기다리는 날짜는 하나 (부분 UQ)
        await s.rollback()
        raise AppError(409, "stale", STALE) from exc
    return await job_for(s, vendor, deal_id, today)


# ---- 공사 중 추가 금액: 업체가 알리고 고객이 확인하거나 이야기하자고 한다. 결제 · 계약은 하지 않는다 ----

EXTRA_OPEN = ("scheduled", "fixing")  # 공사 날짜를 수락한 뒤 · 남은 일을 하는 동안


def _money(m: Money) -> MoneyOut:
    return MoneyOut(supply=m.supply, vat=m.vat, total=m.total)


async def _extras(s: AsyncSession, deals: list[Deal]) -> dict[uuid.UUID, TradeExtrasOut]:
    """공사마다 추가 금액 · 확인한 것 · 기다리는 것 · 이야기 중인 것 합계(계산 엔진)"""
    ids = [d.id for d in deals]
    rows = []
    photos: dict[uuid.UUID, list[ExtraPhotoOut]] = {}
    if ids:
        q = select(ExtraCharge).where(ExtraCharge.deal_id.in_(ids)).order_by(ExtraCharge.created_at)
        rows = list((await s.execute(q.execution_options(populate_existing=True))).scalars())
    if rows:
        q = (
            select(ExtraChargeFile, File)
            .join(File, File.id == ExtraChargeFile.file_id)
            .where(ExtraChargeFile.extra_id.in_([x.id for x in rows]))
            .order_by(ExtraChargeFile.sort)
        )
        for link, f in (await s.execute(q)).all():
            photos.setdefault(link.extra_id, []).append(
                ExtraPhotoOut(
                    file_id=f.id, filename=f.filename, content_type=f.content_type, size=f.size
                )
            )
    out = {}
    for d in deals:
        mine = [x for x in rows if x.deal_id == d.id]

        def sum_of(ack: str, mine=mine) -> MoneyOut:
            return _money(add_money(Money(x.supply, x.vat, x.total) for x in mine if x.ack == ack))

        out[d.id] = TradeExtrasOut(
            trade=d.trade,
            deal_id=d.id,
            vendor_name=d.vendor_name,
            items=[
                ExtraOut(
                    id=x.id,
                    title=x.title,
                    reason=x.reason,
                    vat_mode=x.vat_mode,
                    entered_amount=x.entered_amount,
                    supply=x.supply,
                    vat=x.vat,
                    total=x.total,
                    ack=x.ack,
                    acked_at=x.acked_at,
                    created_at=x.created_at,
                    photos=photos.get(x.id, []),
                )
                for x in mine
            ],
            ok=sum_of("ok"),
            pending=sum_of("pending"),
            talk=sum_of("talk"),
        )
    return out


async def _extras_pending(s: AsyncSession, deal_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not deal_ids:
        return {}
    q = (
        select(ExtraCharge.deal_id, func.count())
        .where(ExtraCharge.deal_id.in_(deal_ids), ExtraCharge.ack == "pending")
        .group_by(ExtraCharge.deal_id)
    )
    return {deal_id: int(n) for deal_id, n in (await s.execute(q)).all()}


async def add_extra(
    s: AsyncSession, vendor: User, deal_id: uuid.UUID, data: ExtraIn, today: date
) -> JobOut:
    """업체가 추가 금액 알리기 (한 트랜잭션). 잠금 순서: 프로젝트 → 확정 → 사진 → 추가 금액.
    금액은 업체가 적은 값이고 공급가 · 부가세 · 합계는 계산 엔진이 만든다. 고객 알림."""
    d = await _vendor_deal(s, vendor, deal_id)
    if await vendor_service.tax_kind_of(s, vendor.id) == "exempt" and data.vat_mode != "exempt":
        raise AppError(422, "vat_mode", "면세 업체는 면세로만 적을 수 있어요.")
    money = from_entered(data.entered_amount, data.vat_mode)
    await project_service.lock(s, d.project_id)
    d = await _lock_deal(s, d.id)
    if d is None or d.vendor_id != vendor.id:
        raise AppError(404, "not_found", NO_DEAL)
    if d.status == "reselected":
        raise AppError(409, "invalid_state", "다시 고른 업체의 공사예요.")
    if d.status not in EXTRA_OPEN:
        raise AppError(
            409, "invalid_state", "공사 날짜를 수락한 뒤 공사하는 동안에만 알릴 수 있어요."
        )
    files = await files_service.owned(s, vendor.id, data.file_ids, lock=True)
    if any(i not in files or files[i].kind != "work_photo" for i in data.file_ids):
        raise AppError(422, "file", "올린 공사 사진을 다시 확인해 주세요.")
    used = "다른 추가 금액에 붙인 사진이에요."
    if data.file_ids:
        q = select(ExtraChargeFile.file_id).where(ExtraChargeFile.file_id.in_(data.file_ids))
        if (await s.execute(q)).first():
            raise AppError(409, "file_used", used)
    owner = (await project_service.briefs(s, [d.project_id]))[d.project_id].owner_id
    try:
        x = ExtraCharge(
            id=uuid7(),
            deal_id=d.id,
            title=data.title,
            reason=data.reason,
            vat_mode=data.vat_mode,
            entered_amount=data.entered_amount,
            supply=money.supply,
            vat=money.vat,
            total=money.total,
            by_id=vendor.id,
        )
        s.add(x)
        await s.flush()
        s.add_all(
            [ExtraChargeFile(extra_id=x.id, file_id=i, sort=n) for n, i in enumerate(data.file_ids)]
        )
        await notify_service.add(
            s,
            user_id=owner,
            kind="extra_added",
            body=f"{i_ga(d.vendor_name)} 추가 금액을 알렸어요 · {LABEL[d.trade]}",
            link=f"/projects/{d.project_id}/plan",
            dedup_key=f"extra:{x.id}",
            project_id=d.project_id,
        )
        await s.commit()
    except IntegrityError as exc:  # 사진 하나는 한 줄에만 (UQ)
        await s.rollback()
        raise AppError(409, "file_used", used) from exc
    return await job_for(s, vendor, deal_id, today)


async def ack_extra(
    s: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    deal_id: uuid.UUID,
    extra_id: uuid.UUID,
    data: AckIn,
) -> TradeExtrasOut:
    """고객이 추가 금액에 답하기 (확인 기다림에서만 한 번). 잠금 순서: 프로젝트 → 확정 → 추가 금액."""
    p = await project_service.get_for(s, user, project_id)
    await project_service.lock(s, p.id)
    d = await _lock_deal(s, deal_id)
    if d is None or d.project_id != p.id:
        raise AppError(404, "not_found", NO_DEAL)
    x = await s.get(ExtraCharge, extra_id, populate_existing=True)
    if x is None or x.deal_id != d.id:
        raise AppError(404, "not_found", "추가 금액을 찾을 수 없어요.")
    answered = "이미 답한 추가 금액이에요."
    if x.ack != "pending":
        raise AppError(409, "invalid_state", answered)
    res = await s.execute(
        update(ExtraCharge)
        .where(ExtraCharge.id == x.id, ExtraCharge.ack == "pending")
        .values(ack=data.ack, acked_at=now_utc())
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        raise AppError(409, "invalid_state", answered)
    if d.vendor_id is not None:
        await notify_service.add(
            s,
            user_id=d.vendor_id,
            kind="extra_acked",
            body="고객이 추가 금액을 확인했어요"
            if data.ack == "ok"
            else "고객이 추가 금액을 이야기하고 싶어 해요",
            link=f"/vendor/jobs/{d.id}",
            dedup_key=f"extra:{x.id}:ack",
        )
    await s.commit()
    return (await _extras(s, [d]))[d.id]


async def _extra_photo(
    s: AsyncSession, deal: Deal, extra_id: uuid.UUID, file_id: uuid.UUID
) -> Response:
    q = (
        select(File)
        .join(ExtraChargeFile, ExtraChargeFile.file_id == File.id)
        .join(ExtraCharge, ExtraCharge.id == ExtraChargeFile.extra_id)
        .where(
            ExtraChargeFile.extra_id == extra_id,
            ExtraChargeFile.file_id == file_id,
            ExtraCharge.deal_id == deal.id,
        )
    )
    f = (await s.execute(q)).scalar_one_or_none()
    if f is None:
        raise AppError(404, "not_found", "사진을 찾을 수 없어요.")
    return await files_service.respond(f)


async def customer_extra_photo(
    s: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    deal_id: uuid.UUID,
    extra_id: uuid.UUID,
    file_id: uuid.UUID,
) -> Response:
    p = await project_service.get_for(s, user, project_id)
    d = await s.get(Deal, deal_id)
    if d is None or d.project_id != p.id:
        raise AppError(404, "not_found", NO_DEAL)
    return await _extra_photo(s, d, extra_id, file_id)


async def vendor_extra_photo(
    s: AsyncSession, vendor: User, deal_id: uuid.UUID, extra_id: uuid.UUID, file_id: uuid.UUID
) -> Response:
    return await _extra_photo(s, await _vendor_deal(s, vendor, deal_id), extra_id, file_id)


# ---- 공사 끝냈어요 · 고객 확인 · 남은 일: 연락처는 업체가 마지막으로 끝냈다고 올린 날부터 1년 ----

FINISHABLE = ("scheduled", "fixing")  # 공사 중 · 남은 일


async def _fixing_notes(s: AsyncSession, deal_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """남은 일: 공사마다 가장 최근에 고객이 적은 글"""
    if not deal_ids:
        return {}
    q = (
        select(DealEvent)
        .where(DealEvent.deal_id.in_(deal_ids), DealEvent.to_status == "fixing")
        .order_by(DealEvent.deal_id, DealEvent.at.desc())
    )
    out: dict[uuid.UUID, str] = {}
    for e in (await s.execute(q)).scalars():
        if e.note:
            out.setdefault(e.deal_id, e.note)
    return out


async def finish(s: AsyncSession, vendor: User, deal_id: uuid.UUID, today: date) -> JobOut:
    """업체 '공사 끝냈어요' · '남은 일 다 했어요' (한 트랜잭션). 잠금 순서: 프로젝트 → 확정 → 요청 행.
    마지막 공사일부터. 확정 → checking, 끝낸 날 = 오늘, 연락처 기한 = 오늘 + 1년(calc),
    수락 기다리던 날짜 바꾸기는 닫는다. 고객 알림."""
    d = await _vendor_deal(s, vendor, deal_id)
    await project_service.lock(s, d.project_id)
    d = await _lock_deal(s, d.id)
    if d is None or d.vendor_id != vendor.id:
        raise AppError(404, "not_found", NO_DEAL)
    if d.status == "checking":
        raise AppError(409, "invalid_state", "이미 끝냈다고 올렸어요. 고객 확인을 기다려요.")
    if d.status == "done":
        raise AppError(409, "invalid_state", "고객이 확인해서 끝난 공사예요.")
    if d.status not in FINISHABLE:
        raise AppError(409, "invalid_state", "공사 날짜를 수락한 뒤에 올릴 수 있어요.")
    if d.end_date is None or today < d.end_date:
        raise AppError(409, "too_early", "마지막 공사일부터 누를 수 있어요.")
    again = d.status == "fixing"
    now = now_utc()
    await _set(s, Deal, d.id, d.status, {"status": "checking", "work_done_on": today})
    ev = DealEvent(
        id=uuid7(), deal_id=d.id, from_status=d.status, to_status="checking", by_id=vendor.id
    )
    s.add(ev)
    await _close_dates(s, d.id, now)  # 끝낸 뒤에는 날짜를 바꾸지 않는다
    await visit_service.set_contact_until(s, d.visit_id, contact_until(today))
    owner = (await project_service.briefs(s, [d.project_id]))[d.project_id].owner_id
    who = i_ga(d.vendor_name)
    body = (
        f"{who} 남은 일을 다 했다고 알렸어요 · 확인해 주세요"
        if again
        else f"{who} 공사를 끝냈다고 알렸어요 · 확인해 주세요"
    )
    await notify_service.add(
        s,
        user_id=owner,
        kind="work_done",
        body=body,
        link=f"/projects/{d.project_id}/plan",
        dedup_key=f"deal_event:{ev.id}",  # 끝냄 · 확인은 여러 번 오갈 수 있어 이력마다
        project_id=d.project_id,
    )
    await s.commit()
    return await job_for(s, vendor, deal_id, today)


async def check_work(
    s: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    deal_id: uuid.UUID,
    data: CheckIn,
    today: date,
) -> DealOut:
    """고객이 공사 끝남 확인 (끝냈다고 올린 뒤에만): 끝났어요 → done, 남은 일이 있어요 → fixing(남은 일
    글은 이력 note). 잠금 순서: 프로젝트 → 확정. 업체 알림."""
    p = await project_service.get_for(s, user, project_id)
    if data.result == "fixing" and data.note is None:
        raise AppError(422, "note", "남은 일을 적어 주세요.")
    await project_service.lock(s, p.id)
    d = await _lock_deal(s, deal_id)
    if d is None or d.project_id != p.id:
        raise AppError(404, "not_found", NO_DEAL)
    if d.status != "checking":
        raise AppError(409, "invalid_state", "업체가 공사를 끝냈다고 올린 뒤에 확인할 수 있어요.")
    await _set(s, Deal, d.id, "checking", {"status": data.result})
    ev = DealEvent(
        id=uuid7(),
        deal_id=d.id,
        from_status="checking",
        to_status=data.result,
        by_id=user.id,
        note=data.note if data.result == "fixing" else None,
    )
    s.add(ev)
    if d.vendor_id is not None:
        await notify_service.add(
            s,
            user_id=d.vendor_id,
            kind="work_checked",
            body="고객이 공사 끝남을 확인했어요"
            if data.result == "done"
            else f"고객이 남은 일이 있대요 · {LABEL[d.trade]}",
            link=f"/vendor/jobs/{d.id}",
            dedup_key=f"deal_event:{ev.id}",  # 끝냄 · 확인은 여러 번 오갈 수 있어 이력마다
        )
    await s.commit()
    d = await s.get(Deal, d.id, populate_existing=True)
    (out,) = await _deal_out(s, [d], today, now_utc())
    return out


async def _done_tab(
    s: AsyncSession, vendor: User, today: date, before: datetime | None, limit: int
) -> InboxPageOut:
    """끝남 탭: 닫히거나 받지 않은 요청 + 고객이 확인해 끝난 공사, 끝난 때 최근 순으로 limit씩."""
    page = await visit_service.inbox(s, vendor, "done", today, before, limit)
    at = (
        select(DealEvent.deal_id, func.max(DealEvent.at).label("at"))
        .where(DealEvent.to_status == "done")
        .group_by(DealEvent.deal_id)
        .subquery()
    )
    q = (
        select(Deal, at.c.at)
        .join(at, at.c.deal_id == Deal.id)
        .where(Deal.vendor_id == vendor.id, Deal.status == "done")
    )
    if before is not None:
        q = q.where(at.c.at < before)
    pairs = list((await s.execute(q.order_by(at.c.at.desc()).limit(limit))).all())
    rows = await visit_service.rows_for(s, [d.visit_id for d, _ in pairs], today)
    until = {
        v.id: v.contact_until
        for v in (await visit_service.visits_by_id(s, [d.visit_id for d, _ in pairs])).values()
    }
    items = [InboxRowOut(**r.model_dump()) for r in page.items] + [
        InboxRowOut(
            **{**rows[d.visit_id].model_dump(), "closed_at": done_at},
            deal_id=d.id,
            deal_status=d.status,
            confirmed_at=d.created_at,
            dates_due_at=d.dates_due_at,
            start_date=d.start_date,
            end_date=d.end_date,
            work_done_on=d.work_done_on,
            contact_until=until.get(d.visit_id),
        )
        for d, done_at in pairs
        if d.visit_id in rows
    ]
    items.sort(key=lambda r: r.closed_at, reverse=True)
    items = items[:limit]
    more = len(items) == limit
    return InboxPageOut(items=items, next_before=items[-1].closed_at if more else None)


# ---- 내 공사 한눈에 · 목록 · 서류: visit · quote · project 공개 함수로 모은다. 단계 판정은 calc ----

STEP_LABEL = {
    "house": "집 정보 · 공정",
    "standard": "기준 금액",
    "pick": "업체 고르기",
    "visit": "실측",
    "deal": "업체 확정",
    "dates": "공사 날짜",
}
WEEKDAY = "월화수목금토일"


def _md(d: date) -> str:
    return f"{d:%m.%d}({WEEKDAY[d.weekday()]})"


def _mdt(at: datetime) -> str:
    x = at.astimezone(SEOUL)
    return f"{_md(x.date())} {x:%H:%M}"


def _day(at: datetime | None) -> date | None:
    return at.astimezone(SEOUL).date() if at else None


def _todo_out(pid: uuid.UUID, code: str, trade: str, d: Deal | None, ctx: dict) -> TodoOut:
    """calc가 고른 할 일 코드 → 판 문구 · 버튼 · 화면"""
    label = LABEL[trade]
    who = i_ga(d.vendor_name) if d else ""
    base = f"/projects/{pid}"
    x, r, f = ctx.get("proposal"), ctx.get("request"), ctx["facts"]
    texts = {
        "check": (
            f"{who} {label} 공사를 끝냈다고 알렸어요 · 확인해 주세요",
            "확인하기",
            f"{base}/plan",
        ),
        "dates_received": (
            f"{who} {label} 공사 날짜를 냈어요 — {_md(x.start_date)} ~ {_md(x.end_date)}"
            if x
            else "",
            "날짜 확인하기",
            f"{base}/schedule?trade={trade}",
        ),
        "dates_change": (
            f"{who} {label} 날짜를 {_md(x.start_date)} ~ {_md(x.end_date)}으로 바꾸자고 했어요"
            if x
            else "",
            "확인하기",
            f"{base}/plan",
        ),
        "extra": (f"{who} 추가 금액을 알렸어요 · 확인해 주세요", "확인하기", f"{base}/plan"),
        "quotes_arrived": (
            f"실측 견적서 {f.quotes if f else 0}개가 왔어요 · {label}",
            "비교하기",
            f"{base}/quotes?trade={trade}",
        ),
        "quotes_partial": (
            f"{' · '.join(f.quoted_names)} 견적서가 왔어요 · "
            f"{' · '.join(f.waiting_names)} 견적서를 기다리는 중이에요"
            if f
            else "",
            "비교하기",
            f"{base}/quotes?trade={trade}",
        ),
        "dates_overdue": (
            f"{who} {_mdt(d.dates_due_at)}까지 공사 날짜를 내지 않았어요" if d else "",
            "확인하기",
            f"{base}/schedule?trade={trade}",
        ),
        "pick": (
            f"업체를 골라 실측을 요청하세요 · {label}",
            "업체 고르기",
            f"{base}/vendors?trade={trade}",
        ),
        "visit_requested": (
            f"업체에 연락할 차례예요 · {label}",
            "연락처 보기",
            f"{base}/visits?trade={trade}",
        ),
        "before_work": (
            f"{label} 공사는 {_md(d.start_date)}에 시작해요 · 공사 전에 할 일을 확인하세요"
            if d and d.start_date
            else "",
            "공정표 보기",
            f"{base}/plan",
        ),
        "dates_waiting": (
            f"{who} 공사 날짜를 정하고 있어요 · {_mdt(d.dates_due_at)}까지" if d else "",
            "공사 날짜 보기",
            f"{base}/schedule?trade={trade}",
        ),
        "dates_requested": (
            f"다른 날짜를 요청했어요 · {d.vendor_name} 답을 기다려요 · {_mdt(r.due_at)}까지"
            if d and r
            else "",
            "공사 날짜 보기",
            f"{base}/schedule?trade={trade}",
        ),
        "working": (
            f"{label} 공사 중이에요 · {_md(d.end_date)}까지" if d and d.end_date else "",
            "공정표 보기",
            f"{base}/plan",
        ),
        "fixing": (f"{who} 남은 일을 하고 있어요 · {label}", "공정표 보기", f"{base}/plan"),
        "done": (
            f"공사가 끝났어요 · 하자가 보이면 {d.vendor_name}에 전화해요" if d else "",
            "연락처 보기",
            base,
        ),
    }
    text, action, link = texts[code]
    return TodoOut(code=code, trade=trade, text=text, action=action, link=link)


def _next(trade: str, d: Deal | None, r: DateRequest | None, x, today: date) -> NextOut | None:
    if d is None:
        return None
    if r is not None:
        return NextOut(kind="request_due", trade=trade, day=_day(r.due_at), at=r.due_at)
    if d.status == "confirmed" and x is None:
        return NextOut(kind="dates_due", trade=trade, day=_day(d.dates_due_at), at=d.dates_due_at)
    if d.status in ("scheduled", "fixing") and d.start_date and d.end_date:
        if today < d.start_date:
            return NextOut(kind="start", trade=trade, day=d.start_date, at=None)
        return NextOut(kind="end", trade=trade, day=d.end_date, at=None)
    return None


async def _accepted_on(s: AsyncSession, deal_ids: list[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    """처음 공사 날짜를 수락한 때"""
    if not deal_ids:
        return {}
    q = (
        select(DealEvent.deal_id, func.min(DealEvent.at))
        .where(DealEvent.deal_id.in_(deal_ids), DealEvent.to_status == "scheduled")
        .group_by(DealEvent.deal_id)
    )
    return {k: v for k, v in (await s.execute(q)).all()}


async def _progress_of(s: AsyncSession, p: ProjectOut, today: date, now: datetime):
    """공정마다 (calc 판정, 확정, 할 일 문맥). 목공 → 도배 순."""
    trades = [t for t in ORDER if getattr(p.trades, t) is not None]
    std = await quote_service.ensure_standard(s, p)
    facts = await visit_service.trade_facts(s, p.id)
    q = select(Deal).where(Deal.project_id == p.id, Deal.status != "reselected")
    deals = {
        d.trade: d for d in (await s.execute(q.execution_options(populate_existing=True))).scalars()
    }
    ids = [d.id for d in deals.values()]
    pend, reqs = await _pending(s, ids), await _open_requests(s, ids)
    waiting = await _extras_pending(s, ids)
    accepted = await _accepted_on(s, ids)
    out = []
    for t in trades:
        f, d = facts.get(t), deals.get(t)
        x = pend.get(d.id) if d else None
        r = reqs.get(d.id) if d else None
        tf = TradeFacts(
            trade=t,
            created_on=_day(p.created_at),
            standard_on=_day(std[t].created_at) if t in std else None,
            asked=f.asked if f else 0,
            asked_on=_day(f.asked_at) if f else None,
            quotes=f.quotes if f else 0,
            waiting=f.waiting if f else 0,
            quoted_on=_day(f.quoted_at) if f else None,
            deal_status=d.status if d else None,
            deal_on=_day(d.created_at) if d else None,
            dates_due_at=d.dates_due_at if d else None,
            proposed=x is not None,
            proposed_on=_day(x.created_at) if x else None,
            request_open=r is not None,
            accepted_on=_day(accepted.get(d.id)) if d else None,
            start=d.start_date if d else None,
            end=d.end_date if d else None,
            work_done_on=d.work_done_on if d else None,
            extras_pending=waiting.get(d.id, 0) if d else 0,
        )
        ctx = {"proposal": x, "request": r, "facts": f}
        out.append((progress(tf, today, now), d, ctx, f))
    return out, std, deals


def _trade_out(pid: uuid.UUID, pr, d: Deal | None, ctx: dict, today: date) -> TradeProgressOut:
    x = ctx["proposal"]
    return TradeProgressOut(
        trade=pr.trade,
        vendor_name=d.vendor_name if d else None,
        deal_id=d.id if d else None,
        deal_status=d.status if d else None,
        phase=pr.phase,
        current=pr.current,
        steps=[
            StepOut(key=st.key, label=STEP_LABEL[st.key], state=st.state, on=st.on, count=st.count)
            for st in pr.steps
        ],
        todo=_todo_out(pid, pr.todo, pr.trade, d, ctx),
        overdue=pr.overdue,
        start_date=d.start_date if d else None,
        end_date=d.end_date if d else None,
        proposed_start=x.start_date if x else None,
        proposed_end=x.end_date if x else None,
        next=_next(pr.trade, d, ctx["request"], x, today),
    )


def _soonest(items: list[NextOut | None]) -> NextOut | None:
    got = [n for n in items if n is not None]
    return min(got, key=lambda n: n.day) if got else None


async def overview_for(
    s: AsyncSession, user: User, project_id: uuid.UUID, today: date
) -> OverviewOut:
    """GET /projects/{id}/overview: 지금 할 일 · 공정별 단계 · 낼 금액 · 공사 기간 · 연락처 · 체크"""
    p = await project_service.get_for(s, user, project_id)
    now = now_utc()
    rows, std, deals = await _progress_of(s, p, today, now)
    trades = [_trade_out(p.id, pr, d, ctx, today) for pr, d, ctx, _ in rows]
    top = first_todo([pr for pr, *_ in rows])
    quotes = await quote_service.briefs_by_id(s, [d.quote_id for d in deals.values()])
    by_trade = [
        TradeMoneyOut(trade=t.trade, total=quotes[deals[t.trade].quote_id].total, source="deal")
        if t.trade in deals
        else TradeMoneyOut(trade=t.trade, total=std[t.trade].total, source="standard")
        for t in trades
        if t.trade in deals or t.trade in std
    ]
    total = sum_won(m.total for m in by_trade)
    budget = None
    if p.budget:
        status, over = budget_gap(total, p.budget)
        budget = BudgetOut(band=p.budget, status=status, over_by=over)
    final = await _latest(s, p.id, "final")
    plan = final or await ensure_expected(s, p, today)
    visits = await visit_service.visits_by_id(s, [d.visit_id for d in deals.values()])
    shown = {
        d.trade: d
        for d in deals.values()
        if d.vendor_id
        and d.visit_id in visits
        and visit_service.contact_visible(
            visits[d.visit_id].status, visits[d.visit_id].contact_until, today
        )
    }
    contacts = await vendor_service.contacts(s, [d.vendor_id for d in shown.values()])
    std_any = next(iter(std.values()), None)
    return OverviewOut(
        project_id=p.id,
        code=p.code,
        region=p.region,
        sgg=p.sgg,
        pyeong=std_any.pyeong if std_any else None,
        phase=project_phase([pr for pr, *_ in rows]),
        todo=next((t.todo for t in trades if top and t.trade == top.trade), None),
        next=_soonest([t.next for t in trades]),
        trades=trades,
        money=MoneySumOut(
            total=total,
            by_trade=by_trade,
            all_confirmed=bool(by_trade) and all(m.source == "deal" for m in by_trade),
            budget=budget,
        ),
        period=PeriodOut(
            kind=plan.kind, start=plan.start_date, end=plan.end_date, work_days=plan.work_days
        ),
        contacts=[
            VendorContactOut(
                trade=t,
                vendor_name=d.vendor_name,
                contact=contacts[d.vendor_id],
                contact_until=visits[d.visit_id].contact_until,
            )
            for t, d in sorted(shown.items(), key=lambda kv: ORDER.index(kv[0]))
            if d.vendor_id in contacts
        ],
        checks=await project_service.checks(s, p.id),
    )


async def _updated_at(s: AsyncSession, p: ProjectOut, deals: dict[str, Deal], facts) -> datetime:
    """바뀐 날: 만든 때 · 요청 상태 · 확정 이력 · 낸 날짜 · 추가 금액 중 가장 늦은 때"""
    ids = [d.id for d in deals.values()]
    times = [p.created_at] + [f.last_at for f in facts if f and f.last_at]
    if ids:
        for col, model in (
            (DealEvent.at, DealEvent),
            (DateProposal.created_at, DateProposal),
            (ExtraCharge.created_at, ExtraCharge),
        ):
            got = await s.scalar(select(func.max(col)).where(model.deal_id.in_(ids)))
            if got:
                times.append(got)
    return max(times)


async def my_projects(s: AsyncSession, user: User, today: date) -> list[MyProjectOut]:
    """GET /projects: 내 공사 목록 · 단계 요약 · 지금 할 일 · 다음 날짜 (바뀐 날 최근 순)"""
    if user.role != "customer":
        raise AppError(403, "forbidden", "고객만 볼 수 있어요.")
    now = now_utc()
    out = []
    for item in await project_service.list_mine(s, user):
        p = await project_service.get_for(s, user, item.id)
        rows, _, deals = await _progress_of(s, p, today, now)
        prs = [pr for pr, *_ in rows]
        top = first_todo(prs)
        todo = None
        if top is not None:
            pr, d, ctx, _ = next(r for r in rows if r[0].trade == top.trade)
            todo = _todo_out(p.id, pr.todo, pr.trade, d, ctx)
        nexts = [
            _next(pr.trade, d, ctx["request"], ctx["proposal"], today) for pr, d, ctx, _ in rows
        ]
        out.append(
            MyProjectOut(
                **item.model_dump(),
                phase=project_phase(prs),
                current=min((pr.current for pr in prs), default=1),
                todo=todo,
                next=_soonest(nexts),
                updated_at=await _updated_at(s, p, deals, [f for *_, f in rows]),
                grades={t: getattr(getattr(p.trades, t), "grade", None) for t in item.trades},
            )
        )
    out.sort(key=lambda m: m.updated_at, reverse=True)
    return out


async def docs_for(s: AsyncSession, user: User, project_id: uuid.UUID, today: date) -> DocsOut:
    """GET /projects/{id}/docs: 표준견적서 · 예상 · 확정 공정표 버전, 받은 실측 견적서 버전(닫힌 것도
    기록으로), 확정한 버전, 추가 금액 기록. 각 줄에 여는 화면 · 파일 주소."""
    p = await project_service.get_for(s, user, project_id)
    latest_std = await quote_service.ensure_standard(s, p)
    await ensure_expected(s, p, today)
    base = f"/projects/{p.id}"
    stds = await quote_service.standard_versions(s, p.id)
    plans = list(
        (
            await s.execute(
                select(Plan).where(Plan.project_id == p.id).order_by(Plan.kind, Plan.version)
            )
        ).scalars()
    )
    top = {
        k: max((x.version for x in plans if x.kind == k), default=0) for k in ("expected", "final")
    }
    q = select(Deal).where(Deal.project_id == p.id)
    deals = list((await s.execute(q)).scalars())
    chosen = {d.quote_id for d in deals if d.status != "reselected"}
    quotes = []
    for v in await visit_service.quote_docs(s, p.id):
        quotes.append(
            QuoteDocOut(
                visit_id=v.visit_id,
                trade=v.trade,
                vendor_name=v.vendor_name,
                status=v.status,
                close_reason=v.close_reason,
                closed=v.status not in ("quoted", "chosen"),
                versions=[
                    QuoteVersionDocOut(
                        id=q.id,
                        code=q.code,
                        version=q.version,
                        sent_at=q.sent_at,
                        total=q.total,
                        confirmed=q.id in chosen,
                        files=q.files,
                        file_links=[f"/api{base}/quotes/{q.id}/files/{f.file_id}" for f in q.files],
                    )
                    for q in v.versions
                ],
            )
        )
    live = [d for d in deals if d.status != "reselected"]
    return DocsOut(
        project_id=p.id,
        code=p.code,
        region=p.region,
        standard=[
            StandardDocOut(
                trade=x.trade,
                version=x.version,
                total=x.total,
                computed_at=x.created_at,
                latest=latest_std.get(x.trade) is not None and latest_std[x.trade].id == x.id,
                link=f"{base}/standard?trade={x.trade}",
            )
            for x in stds
        ],
        standard_total=sum_won(x.total for x in latest_std.values()),
        plans=[
            PlanDocOut(
                kind=x.kind,
                version=x.version,
                start=x.start_date,
                end=x.end_date,
                created_at=x.created_at,
                latest=x.version == top[x.kind],
                link=f"{base}/plan?kind={x.kind}",
            )
            for x in plans
        ],
        quotes=quotes,
        extras=list((await _extras(s, live)).values()),
    )
