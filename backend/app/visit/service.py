"""이 집 기준 업체 고르기 · 업체 자세히 · 실측 요청 (읽기 · v2 길 · v3 공용 도우미).

업체는 승인됐고 그 공정을 하고 이 집 지역(구)에서 일하는 곳만 보인다(v3: 멈춘 업체 빼고, 0곳이면
가까운 구). 가견적은 업체가 확인한 단가 × 이 집 수량(계산 엔진)이다. 요청할 때 그 값을 사본으로 고정한다.
v2 요청은 연결만 한다(연락처 공개는 이 파일의 contact_visible 한 곳). v3 요청(희망 기간을 보낸 요청,
is_v3)은 연락처를 주고받지 않고 믿집이 반나절 날짜를 중계한다: 상태 바꾸기는 visit/flow.py, 기한 일
처리 함수는 visit/jobs.py에 있다.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi.responses import Response
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.calc import deadline, slots, visit_view
from app.calc import rating as rating_calc
from app.calc.quote_diff import percent_vs
from app.calc.schedule import JOB_MAX_DAYS, days_since, years_since
from app.calc.visit_view import grade_diff
from app.files import service as files_service
from app.jobs import service as jobs_service
from app.notify import service as notify_service
from app.notify.catalog import link, work_name
from app.project import service as project_service
from app.project.schemas import ProjectBrief, ProjectOut, WhenIn
from app.quote import service as quote_service
from app.quote.models import QUOTE_FILES_MAX, VAT_MODES, StandardQuote, StandardQuoteLine
from app.quote.schemas import QuoteBriefOut, QuoteDraftIn
from app.realtime import service as realtime
from app.shared.clock import SEOUL, now_utc
from app.shared.errors import AppError, app_error
from app.shared.ids import uuid7
from app.shared.korean import i_ga
from app.shared.regions import neighbors
from app.thread import service as thread_service
from app.vendor import service as vendor_service
from app.vendor.schemas import (
    ContactOut,
    EstimateOut,
    EstimateV3Out,
    PublicCardOut,
    RatingOut,
    ReviewLinkOut,
)
from app.visit import repository as repo
from app.visit.models import (
    ATTEMPTS_MAX,
    LIVE,
    SLOTS,
    Shortlist,
    Visit,
    VisitChange,
    VisitEvent,
    VisitHold,
    VisitProposal,
    VisitWindow,
)
from app.visit.schemas import (
    BoardRowOut,
    BookedOut,
    CancelIn,
    ChangeOut,
    CompareOut,
    CompareRowOut,
    DayHalvesOut,
    Direction,
    DraftVendorOut,
    EstimateDetailOut,
    FailedOut,
    InboxSummaryOut,
    MyVisitOut,
    OtherWindowOut,
    QuoteBoardOut,
    QuoteVersionBrief,
    RecentOut,
    RecommendOut,
    RequestTextOut,
    ReselectIn,
    ReselectOut,
    ShortlistCountOut,
    ShortlistItemOut,
    ShortlistOut,
    SlotOut,
    StateTagOut,
    VendorCardOut,
    VendorDetailOut,
    VendorListOut,
    VendorNextOut,
    VendorRowOut,
    VendorSlotOut,
    VendorVisitDetailOut,
    VendorVisitPage,
    VendorVisitRow,
    VisitBrief,
    VisitCanOut,
    VisitCreateIn,
    VisitCreateOut,
    VisitDraftOut,
    VisitFactsOut,
    VisitHeadOut,
    VisitListOut,
    VisitNextOut,
    VisitOut,
    VisitPickOut,
    VisitProposalOut,
    VisitQuotesOut,
    WindowOut,
)

log = logging.getLogger(__name__)
NOT_LISTED = "이 집에서 고를 수 있는 업체가 아니에요."
# 연락처 보내기 동의 문구 버전. 문구를 바꾸면 올리고, 화면은 요청할 때 이 값을 그대로 보낸다
CONSENT_VER = "2026-11-01"
TRADE_LABEL = {"dobae": "도배", "mokgong": "목공"}
GRADE_LABEL = {"economy": "저가형", "basic": "기본형"}
MOK_LABEL = {
    "molding": "천장 몰딩",
    "base": "걸레받이",
    "door": "방문 교체",
    "flatten": "천장 평탄화",
}
BASIS_SHORT = {"supply": "공급", "exclusive": "전용"}
WEEKDAY = "월화수목금토일"
DONE = ("cancelled", "declined", "closed", "expired", "no_show")  # 끝남 탭 (v3: 기한 지남 · 안 옴)
# 작업함 탭 (V03 · D04, N4 C). v2 요청은 requested · quoted만 쓴다
TABS = {
    "todo": ("requested", "picked", "visited"),  # 기간 답하기 · 날짜 수락 · 견적서 보내기
    "waiting": ("quoted", "offered", "proposed"),  # 고객 차례
    "sched": ("booked",),  # 실측 확정 (일정 탭)
    "done": DONE,
}
RECENT_DAYS = 30
# ---- v3 실측 일정 ----
HALF_LABEL = {"am": "오전", "pm": "오후"}
HALF_TO = {"am": "오전으로", "pm": "오후로"}
CANCELLABLE = ("requested", "offered", "picked", "proposed", "booked", "visited")  # C30
AGAIN_FROM = ("expired", "cancelled")  # 한 번 더 요청은 답 없음 · 고르기 기한 지남 · 취소 뒤에만
# 요청 한 건을 가리키는 기한 일 (ref_id = 요청 id). 제안 · 바꾸기 기한은 그 행 id를 가리킨다
VISIT_JOBS = (
    "visit_reply_due",
    "visit_pick_due",
    "visit_accept_due",
    "visit_reminder",
    "visit_quote_due",
)
NOT_FOUND = "실측 요청을 찾을 수 없어요."
# v3 단가(dobae_v3)를 아직 확인하지 않은 업체도 지금 가견적(v2)이 있으면 담을 수 있게 둔다.
# D06 A를 엄격히('v3 묶음이 없으면 가견적 준비 중') 하려면 False로 바꾼다 (보고서 '확인 필요').
V2_FALLBACK = True


def contact_visible(status: str, contact_until: date | None, today: date) -> bool:
    """연락처를 서로 보여도 되는지: 요청함 · 견적서 옴이면 보이고, 확정이면 contact_until까지
    (없으면 보임), 취소 · 못 받음 · 닫힘이면 가린다. 고객 쪽 · 업체 쪽이 같이 쓴다."""
    if status in ("requested", "quoted"):
        return True
    if status == "chosen":
        return contact_until is None or today <= contact_until
    return False


def customer_contact_visible(v: Visit, today: date) -> bool:
    """업체에게 고객 연락처(이름 · 휴대폰 · 동 · 호수)를 보여도 되는지: 고객이 보내기로 고른 요청만."""
    return v.share_contact and contact_visible(v.status, v.contact_until, today)


def is_v3(v: Visit) -> bool:
    """v3 요청(희망 기간을 보낸 요청)인지. v3는 연락처를 주고받지 않고 채팅으로 이야기한다."""
    return v.wish_start is not None


def _brief(v: Visit | None) -> VisitBrief | None:
    if v is None:
        return None
    return VisitBrief(
        id=v.id, status=v.status, decline_code=v.decline_code, close_reason=v.close_reason
    )


def _day(at: datetime | None) -> date | None:
    return at.astimezone(SEOUL).date() if at else None


async def _standard(
    s: AsyncSession, user: User, project_id: uuid.UUID, trade: str
) -> tuple[ProjectOut, StandardQuote, list[StandardQuoteLine]]:
    """내 프로젝트(남의 것은 404)와 그 공정의 최신 표준견적서 · 줄"""
    p = await project_service.get_for(s, user, project_id)
    got = await quote_service.standard_trade(s, p, trade)
    if sa_inspect(user).expired_attributes:  # 같은 집을 동시에 열어 표준견적서 저장이 되돌아갔다
        await s.refresh(user)  # (rollback이 세션 객체를 만료시켜 user.id가 비동기 밖에서 읽힌다)
    if got is None:
        raise AppError(422, "trade", "이 집에서 고른 공정이 아니에요.")
    return p, *got


def _estimate(
    trade: str,
    std: StandardQuote,
    lines: list[StandardQuoteLine],
    cp: vendor_service.ConfirmedPrices | None,
    grade: str | None = None,
) -> EstimateOut | None:
    """확인한 단가가 없거나 이 집에 필요한 줄의 단가가 빠졌으면 가견적 없음(None).
    grade를 주면 그 벽지로 센다(비면 표준견적서 벽지)."""
    if cp is None:
        return None
    e = vendor_service.estimate_out(
        trade, lines, cp.prices, grade=grade or std.grade, tax_kind=cp.tax_kind, sources=cp.sources
    )
    return None if e.missing or not e.lines else e


def _vs(total: int, standard: int) -> tuple[Decimal | None, Direction | None]:
    if standard <= 0:
        return None, None
    pct = percent_vs(total, standard)
    return pct, "less" if pct < 0 else "more" if pct > 0 else "same"


# ---- v3 업체 카드: 가견적(v3 단가가 있으면 v3, 없으면 지금 가견적) · 담을 수 있는지 ----


@dataclass(frozen=True)
class Card:
    """업체 한 곳의 가견적 · 담기 상태 (이 집 · 이 공정). 금액은 계산 엔진 값 그대로."""

    total: int | None
    set_id: uuid.UUID | None
    pct: Decimal | None
    state: str  # ok · no_estimate · paper_unsupported · blocked_retry
    v3: EstimateV3Out | None


def _grade(p: ProjectOut, std: StandardQuote | None, trade: str) -> str | None:
    """고객이 고른 벽지 (도배만)"""
    if trade != "dobae":
        return None
    if std is not None and std.grade:
        return std.grade
    return p.trades.dobae.grade if p.trades.dobae else None


async def _cards(
    s: AsyncSession,
    p: ProjectOut,
    std: StandardQuote,
    lines: list[StandardQuoteLine],
    trade: str,
    vendor_ids: list[uuid.UUID],
    grade: str | None = None,
) -> dict[uuid.UUID, Card]:
    """업체마다 가견적 · 담기 상태. 가견적은 v3 단가(1201~1208)를 확인한 업체면 v3, 아니면 지금 가견적.
    고른 벽지 줄을 '빼기'로 둔 업체는 paper_unsupported(D07 A), 두 번 요청했는데 살아 있는 요청이
    없으면 blocked_retry(C18). grade를 주면 그 벽지로 센다(C11, 비면 공사 벽지)."""
    if not vendor_ids:
        return {}
    v3s = await vendor_service.estimates_v3_for(
        s, vendor_ids=vendor_ids, project=p, trade=trade, grade=grade
    )
    prices = await vendor_service.confirmed_prices(s, vendor_ids, trade)
    tried = await repo.attempts(s, p.id, trade, vendor_ids)
    live = {v.vendor_id for v in await repo.live(s, p.id, trade)}
    out: dict[uuid.UUID, Card] = {}
    for vid in vendor_ids:
        v3 = v3s.get(vid)
        if v3 is not None:
            total, set_id, pct = v3.total, v3.set_id, v3.vs_standard_pct
        else:
            e = _estimate(trade, std, lines, prices.get(vid), grade) if V2_FALLBACK else None
            total = e.total if e else None
            set_id = prices[vid].set_id if e else None
            pct = _vs(e.total, std.total)[0] if e else None
        if tried.get(vid, 0) >= ATTEMPTS_MAX and vid not in live:
            state = "blocked_retry"
        elif v3 is not None and not v3.paper_ok:
            state = "paper_unsupported"
        elif total is None:
            state = "no_estimate"
        else:
            state = "ok"
        out[vid] = Card(total, set_id, pct, state, v3)
    return out


async def cards_by_grade(
    s: AsyncSession,
    p: ProjectOut,
    std: StandardQuote,
    lines: list[StandardQuoteLine],
    trade: str,
    pairs: list[tuple[uuid.UUID, str | None]],
) -> dict[uuid.UUID, Card]:
    """(업체, 고른 벽지)마다 카드. 같은 벽지끼리 묶어 한 번에 센다 (벽지가 비면 공사 벽지)."""
    groups: dict[str | None, list[uuid.UUID]] = {}
    for vid, g in pairs:
        groups.setdefault(g if trade == "dobae" else None, []).append(vid)
    out: dict[uuid.UUID, Card] = {}
    for g, ids in groups.items():
        out.update(await _cards(s, p, std, lines, trade, ids, g))
    return out


async def rating_of(s: AsyncSession, vendor_ids: list[uuid.UUID]) -> dict[uuid.UUID, RatingOut]:
    """업체마다 별점 요약 (숨긴 리뷰 빼고, 계산 엔진 calc.rating)"""
    out = {}
    for vid, (total, count, tags, done) in (await repo.ratings(s, vendor_ids)).items():
        x = rating_calc.summary(total, count, done, tags)
        out[vid] = RatingOut(avg=x.avg, count=x.count, done_count=x.done_count, tags=x.tags)
    return out


async def _listed(
    s: AsyncSession, p: ProjectOut, trade: str
) -> tuple[list[PublicCardOut], list[tuple[PublicCardOut, str]], list[str]]:
    """이 집 구 업체(멈춘 업체 빼고), 0곳이면 가까운 구 업체와 그 구 이름 (C12, B6 neighbors)"""
    local = await vendor_service.list_public(s, trade, p.sido, p.sgg)
    facts = await repo.vendor_facts(s, [c.vendor_id for c in local])
    local = [c for c in local if not facts[c.vendor_id].suspended]
    near: list[tuple[PublicCardOut, str]] = []
    names: list[str] = []
    if local:
        return local, near, names
    seen: set[uuid.UUID] = set()
    for sido, sgg in neighbors(p.sido, p.sgg):
        found = [
            c
            for c in await vendor_service.list_public(s, trade, sido, sgg)
            if c.vendor_id not in seen
        ]
        more = await repo.vendor_facts(s, [c.vendor_id for c in found])
        found = [c for c in found if not more[c.vendor_id].suspended]
        if found:
            names.append(sgg)
        for c in found:
            seen.add(c.vendor_id)
            near.append((c, sgg))
    return local, near, names


def _used_slots(actives: list[Shortlist], live: list[Visit]) -> set[int]:
    """쓰고 있는 칸: 담은 업체 + 담기 없이 보낸 옛(v2) 살아 있는 요청"""
    return {x.slot for x in actives} | {v.slot for v in live if v.shortlist_id is None}


def _shortlist_count(actives: list[Shortlist], live: list[Visit]) -> ShortlistCountOut:
    used = _used_slots(actives, live)
    return ShortlistCountOut(count=len(actives), left=max(SLOTS - len(used), 0))


async def vendors_for(
    s: AsyncSession, user: User, project_id: uuid.UUID, trade: str
) -> VendorListOut:
    """업체 고르기: 가견적 낮은 순, 가견적 없음은 맨 뒤, 같으면 상호 순.
    v3 칸(local · nearby · shortlist)은 담기(3칸)와 카드 상태를 같이 준다."""
    p, std, lines = await _standard(s, user, project_id, trade)
    cards = await vendor_service.list_public(s, trade, p.sido, p.sgg)
    ids = [c.vendor_id for c in cards]
    prices = await vendor_service.confirmed_prices(s, ids, trade)
    visits = await repo.latest_by_vendor(s, p.id, trade, ids) if ids else {}
    v3 = await _v3_cards(s, p, std, lines, trade)
    rows = []
    for c in cards:
        e = _estimate(trade, std, lines, prices.get(c.vendor_id))
        pct, way = _vs(e.total, std.total) if e else (None, None)
        rows.append(
            VendorRowOut(
                vendor_id=c.vendor_id,
                biz_name=c.biz_name,
                trades=c.trades,
                areas=c.areas,
                verified_on=_day(c.verified_at),
                visit_fee=c.visit_fee,
                visit_fee_refund=c.visit_fee_refund,
                contact_method=c.contact_method,
                call_start=c.call_start,
                call_end=c.call_end,
                estimate_total=e.total if e else None,
                vs_standard=pct,
                vs_direction=way,
                visit=_brief(visits.get(c.vendor_id)),
            )
        )
    rows.sort(key=lambda r: (r.estimate_total is None, r.estimate_total or 0, r.biz_name or ""))
    return VendorListOut(
        project_id=p.id,
        trade=trade,
        region=p.region,
        area_name=p.sgg,
        standard_total=std.total,
        slots_left=SLOTS - len(await repo.live(s, p.id, trade)),
        consent_ver=CONSENT_VER,
        vendors=rows,
        **v3,
    )


def _card_order(c: VendorCardOut) -> tuple:
    return (c.estimate_total is None, c.estimate_total or 0, c.name)


async def _v3_cards(
    s: AsyncSession,
    p: ProjectOut,
    std: StandardQuote,
    lines: list[StandardQuoteLine],
    trade: str,
) -> dict:
    """업체 목록 v3 칸 (C10 · C12 · D06): local · nearby(local이 0곳일 때) · nearby_sgg · shortlist"""
    local, near, names = await _listed(s, p, trade)
    ids = [c.vendor_id for c in local] + [c.vendor_id for c, _ in near]
    cards = await _cards(s, p, std, lines, trade, ids)
    facts = await repo.vendor_facts(s, ids)
    stars = await rating_of(s, ids)
    actives = await repo.shortlists_active(s, p.id, trade)
    live = await repo.live(s, p.id, trade)
    picked = {x.vendor_id for x in actives}

    def card(c: PublicCardOut, sgg: str) -> VendorCardOut:
        x, f, r = cards[c.vendor_id], facts[c.vendor_id], stars[c.vendor_id]
        return VendorCardOut(
            vendor_id=c.vendor_id,
            name=f.name,
            sgg=sgg,
            estimate_total=x.total,
            vs_standard_pct=x.pct,
            rating=r.avg,
            review_count=r.count,
            done_count=r.done_count,
            visit_fee=c.visit_fee,
            instant_badge=f.instant_default,
            state=x.state,
            shortlisted=c.vendor_id in picked,
        )

    return {
        "local": sorted((card(c, p.sgg) for c in local), key=_card_order),
        "nearby": sorted((card(c, sgg) for c, sgg in near), key=_card_order),
        "nearby_sgg": names,
        "shortlist": _shortlist_count(actives, live),
    }


async def choosable(s: AsyncSession, p: ProjectOut, trade: str, vendor_id: uuid.UUID) -> bool:
    """이 집에서 고를 수 있는 업체인지 (이 구 업체, 0곳이면 가까운 구 업체 · 멈춘 업체 빼고)"""
    local, near, _ = await _listed(s, p, trade)
    return vendor_id in {c.vendor_id for c in local} | {c.vendor_id for c, _ in near}


async def vendor_detail(
    s: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    vendor_id: uuid.UUID,
    trade: str,
    today: date,
    grade: str | None = None,
) -> VendorDetailOut:
    """업체 자세히. grade를 주면 그 벽지로 가견적 v3를 미리 본다(C11). 고른 벽지와 차이는 계산 엔진."""
    p, std, lines = await _standard(s, user, project_id, trade)
    if not await choosable(s, p, trade, vendor_id):
        raise AppError(404, "not_found", NOT_LISTED)
    profile = await vendor_service.public_profile(s, vendor_id)
    cp = (await vendor_service.confirmed_prices(s, [vendor_id], trade)).get(vendor_id)
    e = _estimate(trade, std, lines, cp)
    estimate = None
    if e is not None and cp is not None:
        pct, way = _vs(e.total, std.total)
        estimate = EstimateDetailOut(
            **e.model_dump(),
            set_id=cp.set_id,
            confirmed_on=_day(cp.confirmed_at),
            confirmed_at=cp.confirmed_at,
            exempt=cp.tax_kind == "exempt",
            vs_standard=pct,
            vs_direction=way,
        )
    left = set(e.left_out) if e else set()
    not_included = [ln.name for ln in lines if ln.state == "after_visit" or ln.key in left]
    card = (await _cards(s, p, std, lines, trade, [vendor_id]))[vendor_id]
    v3, diff = await _grade_view(s, p, std, trade, vendor_id, card.v3, grade)
    shown = (grade or _grade(p, std, trade)) if trade == "dobae" else None
    facts = (await repo.vendor_facts(s, [vendor_id]))[vendor_id]
    picked = {x.vendor_id for x in await repo.shortlists_active(s, p.id, trade)}
    return VendorDetailOut(
        project_id=p.id,
        trade=trade,
        standard_total=std.total,
        profile=profile,
        verified_on=_day(profile.verified_at),
        years_open=years_since(profile.opened_on, today) if profile.opened_on else None,
        in_area=await vendor_service.listed(s, vendor_id, trade, p.sido, p.sgg),
        area_name=p.sgg,
        dong=p.emd,
        estimate=estimate,
        visit=_brief((await repo.latest_by_vendor(s, p.id, trade, [vendor_id])).get(vendor_id)),
        not_included=not_included,
        estimate_v3=v3,
        grade=shown,
        grade_diff=diff,
        reply_hours=visit_view.reply_hours(await repo.reply_waits(s, vendor_id)),
        state=card.state,
        instant_badge=facts.instant_default,
        rating=(await rating_of(s, [vendor_id]))[vendor_id],
        review_links=[
            ReviewLinkOut(sort=x.sort, label=x.label, url=x.url, status=x.status)
            for x in await repo.shown_review_links(s, vendor_id)
        ],
        shortlisted=vendor_id in picked,
    )


async def _grade_view(
    s: AsyncSession,
    p: ProjectOut,
    std: StandardQuote,
    trade: str,
    vendor_id: uuid.UUID,
    base: EstimateV3Out | None,
    grade: str | None,
) -> tuple[EstimateV3Out | None, int | None]:
    """보여 줄 가견적 v3와 차이(다른 벽지 − 보여 준 벽지, C11). grade를 주면 그 벽지로 미리 보고
    (다른 벽지 = 공사 벽지), 안 주면 공사 벽지를 보여 주고 다른 벽지와 견준다."""
    mine = _grade(p, std, trade)
    if mine is None or base is None:
        if grade is not None and mine is not None and grade != mine:
            preview = await vendor_service.estimate_v3_for(
                s, vendor_id=vendor_id, project=p, trade=trade, grade=grade
            )
            return preview, None
        return base, None
    if grade is not None and grade != mine:
        preview = await vendor_service.estimate_v3_for(
            s, vendor_id=vendor_id, project=p, trade=trade, grade=grade
        )
        return preview, grade_diff(base.total, preview.total) if preview else None
    other = "economy" if mine == "basic" else "basic"
    alt = await vendor_service.estimate_v3_for(
        s, vendor_id=vendor_id, project=p, trade=trade, grade=other
    )
    return base, grade_diff(alt.total, base.total) if alt else None


async def vendor_photo(
    s: AsyncSession, user: User, project_id: uuid.UUID, vendor_id: uuid.UUID, file_id: uuid.UUID
) -> Response:
    """이 집에서 고를 수 있는 업체의 시공 사진만 내준다. 근거 파일 · 등록증 · 남의 사진은 404."""
    p = await project_service.get_for(s, user, project_id)
    trades = [t for t in ("dobae", "mokgong") if getattr(p.trades, t) is not None]
    if not any([await choosable(s, p, t, vendor_id) for t in trades]):
        raise AppError(404, "not_found", NOT_LISTED)
    f = await vendor_service.portfolio_file(s, vendor_id, file_id)
    if f is None:
        raise AppError(404, "not_found", "사진을 찾을 수 없어요.")
    return await files_service.respond(f)


def _md(d: date) -> str:
    return f"{d.month:02d}.{d.day:02d}({WEEKDAY[d.weekday()]})"


def _number(x: Decimal) -> str:
    """32.0 → 32, 30.8 → 30.8"""
    return format(x.normalize(), "f")


def _when(w: WhenIn) -> str:
    """희망 시기 문구 (요청 문구 · 업체 쪽 자세히가 같이 쓴다)"""
    if w.kind == "date" and w.start:
        return f"{_md(w.start)} ~ {_md(w.end)}" if w.end else f"{_md(w.start)}부터"
    if w.kind == "movein" and w.start:
        return f"이사 {_md(w.start)} 전까지"
    return "1~2달 안" if w.kind == "months" else "아직 몰라요"


def request_text(p: ProjectOut, std: StandardQuote) -> str:
    """업체에 연락할 때 보낼 문구. 평 · 면적은 표준견적서(계산 엔진)에 저장한 값을 쓴다.
    고객 이름 · 연락처 · 동 · 호수는 넣지 않는다. 동네(법정동) 칸이 없어 구까지만 쓴다."""
    if std.trade == "dobae":
        work = f"도배 {GRADE_LABEL.get(std.grade or '', '')}".strip()
    else:
        items = p.trades.mokgong.items if p.trades.mokgong else []
        work = " ".join(["목공", " · ".join(MOK_LABEL[i] for i in items if i in MOK_LABEL)]).strip()
    home = (
        f"{p.sgg} 아파트 {_number(std.pyeong)}평"
        f"({BASIS_SHORT.get(std.area_basis, '')} {_number(std.area_m2)}㎡)"
    )
    occupied = "짐이 있는 집이에요." if p.occupied else "빈 집이에요."
    return (
        f"[믿집] 실측 요청이에요. 요청 번호 {p.code} · {work} · {home}"
        f" · 희망 시기 {_when(p.when)} · {occupied}"
    )


def _visit_out(
    v: Visit,
    contacts: dict[uuid.UUID, ContactOut],
    today: date,
    closed: dict[uuid.UUID, datetime] | None = None,
    quotes: dict[uuid.UUID, QuoteBriefOut] | None = None,
) -> VisitOut:
    visible = contact_visible(v.status, v.contact_until, today) and not is_v3(v)
    return VisitOut(
        id=v.id,
        trade=v.trade,
        vendor_id=v.vendor_id,
        vendor_name=v.vendor_name,
        status=v.status,
        slot=v.slot,
        created_at=v.created_at,
        share_contact=v.share_contact,
        message=v.message,
        decline_code=v.decline_code,
        close_reason=v.close_reason,
        estimate_total=v.estimate_total,
        contact_visible=visible,
        contact=contacts.get(v.vendor_id) if visible and v.vendor_id else None,
        closed_at=(closed or {}).get(v.id) if v.status in DONE else None,
        quote=(quotes or {}).get(v.id),
    )


async def request_visits(
    s: AsyncSession, user: User, project_id: uuid.UUID, data: VisitCreateIn, today: date
) -> VisitCreateOut:
    """실측 요청 보내기. 프로젝트 행을 잠가 같은 집의 요청이 줄 서고, 공정마다 3곳까지만 받는다.
    지금 고를 수 없는 업체 · 이미 요청한 업체는 만들지 않고 failed로 돌려준다."""
    p, std, lines = await _standard(s, user, project_id, data.trade)
    share = set(data.share_contact_ids)
    if share:
        if not user.phone:
            raise AppError(422, "phone_required", "업체에 보내려면 휴대폰 번호가 필요해요.")
        if not (data.apt_name and data.dong and data.ho):
            raise AppError(422, "address_required", "아파트 이름 · 동 · 호수를 적어 주세요.")
        if data.consent_ver != CONSENT_VER:
            raise AppError(409, "consent_changed", "동의 문구가 바뀌었어요. 다시 확인해 주세요.")
    cards = {c.vendor_id: c for c in await vendor_service.list_public(s, data.trade, p.sido, p.sgg)}
    failed = [FailedOut(vendor_id=i, code="unavailable") for i in data.vendor_ids if i not in cards]
    ok = [i for i in data.vendor_ids if i in cards]
    prices = await vendor_service.confirmed_prices(s, ok, data.trade) if ok else {}

    unit = (data.apt_name, data.dong, data.ho) if share else None
    await project_service.lock_for_visit(s, p.id, unit)  # 첫 요청이면 집 정보 고치기를 막는다
    live = await repo.live(s, p.id, data.trade)
    if any(v.status == "chosen" for v in live):  # 확정이 살아 있으면 다시 고르기부터
        raise AppError(409, "already_chosen", NOT_CHOOSABLE["chosen"])
    taken = {v.vendor_id for v in live}
    failed += [FailedOut(vendor_id=i, code="already") for i in ok if i in taken]
    new = [i for i in ok if i not in taken]
    if not new:
        if all(f.code == "already" for f in failed):
            raise AppError(409, "already", "이미 실측을 요청한 업체예요.")
        raise AppError(409, "unavailable", "지금 믿집에서 일할 수 없는 업체예요.")
    free = [n for n in range(1, SLOTS + 1) if n not in {v.slot for v in live}]
    if len(new) > len(free):
        raise AppError(409, "slots_full", "3곳까지 담을 수 있어요. 하나를 빼면 담을 수 있어요.")

    now = now_utc()
    tried = await repo.attempts(s, p.id, data.trade, new)
    rows: list[Visit] = []
    for vid, slot in zip(new, free, strict=False):
        cp = prices.get(vid)
        e = _estimate(data.trade, std, lines, cp)
        shared = vid in share
        rows.append(
            Visit(
                id=uuid7(),
                project_id=p.id,
                trade=data.trade,
                vendor_id=vid,
                vendor_name=cards[vid].biz_name or "업체",
                slot=slot,
                attempt=tried.get(vid, 0) + 1,
                status="requested",
                share_contact=shared,
                consent_ver=CONSENT_VER if shared else None,
                consent_at=now if shared else None,
                message=data.message,
                estimate_set_id=cp.set_id if e and cp else None,
                estimate_total=e.total if e else None,
                created_at=now,
            )
        )
    try:
        s.add_all(rows)
        await s.flush()
        s.add_all(
            [
                VisitEvent(
                    visit_id=v.id, from_status=None, to_status="requested", by_id=user.id, at=now
                )
                for v in rows
            ]
        )
        for v in rows:  # 업체 알림: 고객 이름 · 연락처는 넣지 않는다
            await notify_service.add(
                s,
                user_id=v.vendor_id,
                kind="visit_requested",
                body=f"새 실측 요청이 왔어요 · {TRADE_LABEL[data.trade]}",
                link=f"/vendor/requests/{v.id}",
                dedup_key=f"visit:{v.id}",
            )
        await s.commit()
    except IntegrityError as exc:  # 칸 · 업체 부분 UQ: 다른 곳에서 먼저 같은 칸을 채웠다
        await s.rollback()
        raise AppError(409, "stale", "다른 곳에서 먼저 요청했어요. 다시 열어 주세요.") from exc
    contacts = await vendor_service.contacts(s, new)
    return VisitCreateOut(created=[_visit_out(v, contacts, today) for v in rows], failed=failed)


async def visits_for(
    s: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    trade: str | None,
    today: date,
    now: datetime | None = None,
) -> VisitListOut:
    """이 집 실측 요청 목록 · 공정마다 보낼 문구 · 남은 칸 · v3 상태판(head · board)"""
    p = await project_service.get_for(s, user, project_id)
    trades = [t for t in ("mokgong", "dobae") if getattr(p.trades, t) is not None]
    if trade is not None and trade not in trades:
        raise AppError(422, "trade", "이 집에서 고른 공정이 아니에요.")
    every = await repo.of_project(s, p.id, None)
    rows = [v for v in every if trade is None or v.trade == trade]
    contacts = await vendor_service.contacts(s, [v.vendor_id for v in rows if v.vendor_id])
    closed = await repo.last_events(s, [v.id for v in rows if v.status in DONE])
    quotes = await quote_service.sent_briefs(s, [v.id for v in rows])
    texts = []
    for t in [trade] if trade else trades:
        got = await quote_service.standard_trade(s, p, t)
        if got is not None:
            texts.append(RequestTextOut(trade=t, text=request_text(p, got[0])))
    head, board = await board_rows(s, rows, now or now_utc())
    return VisitListOut(
        project_id=p.id,
        consent_ver=CONSENT_VER,
        slots_left={
            t: SLOTS - sum(1 for v in every if v.trade == t and v.status in LIVE) for t in trades
        },
        request_texts=texts,
        visits=[_visit_out(v, contacts, today, closed, quotes) for v in rows],
        head=head,
        board=board,
    )


# ---- 상태 바꾸기: 고객 취소 · 업체 못 받아요 (허용 전이 → 조건부 UPDATE → 같은 트랜잭션에 이력 · 알림) ----


async def _move(
    s: AsyncSession,
    v: Visit,
    *,
    to: str,
    by: User,
    values: dict | None = None,
    code: str | None = None,
    message: str,
) -> None:
    """requested에서만 바꾼다. 1행이 아니면 409(다른 곳에서 먼저 바꿈)."""
    ok = await repo.set_status(
        s, v.id, allowed_from=("requested",), values={"status": to, **(values or {})}
    )
    if not ok:
        raise AppError(409, "invalid_state", message)
    s.add(VisitEvent(visit_id=v.id, from_status="requested", to_status=to, by_id=by.id, code=code))


async def cancel(
    s: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    visit_id: uuid.UUID,
    today: date,
    data: CancelIn | None = None,
    now: datetime | None = None,
) -> VisitOut:
    """고객이 요청 취소. 몸통이 없으면(v2) 견적서가 오기 전 요청함만. 몸통(v3 C30 이유)이 있으면
    요청함 ~ 실측 받음에서 취소: 잡기 풀기 · 담기 칸 비움 · 기한 일 취소 · 대화 읽기만 · 업체 알림."""
    now = now or now_utc()
    p = await project_service.get_for(s, user, project_id)
    if data is None:
        v = await repo.get(s, visit_id)
        if v is None or v.project_id != p.id:
            raise AppError(404, "not_found", NOT_FOUND)
        if v.status == "quoted":
            raise AppError(409, "invalid_state", "견적서가 온 뒤에는 취소할 수 없어요.")
        await _move(s, v, to="cancelled", by=user, message="이미 닫힌 요청이에요.")
    else:
        await project_service.lock(
            s, p.id
        )  # 프로젝트 → 요청 순서로 잠가 업체 답 · 기한 일과 줄 선다
        v = await repo.lock(s, visit_id)
        if v is None or v.project_id != p.id:
            raise AppError(404, "not_found", NOT_FOUND)
        if v.status not in CANCELLABLE:
            raise visit_error(v, "이미 끝났거나 견적서가 온 요청이라 취소할 수 없어요.")
        values = {"status": "cancelled", "cancel_code": data.code, "cancel_note": data.note}
        if not await repo.set_status(s, v.id, allowed_from=(v.status,), values=values):
            raise app_error("stale")
        s.add(
            VisitEvent(
                visit_id=v.id,
                from_status=v.status,
                to_status="cancelled",
                by_id=user.id,
                code="cancelled",
                at=now,
            )
        )
        tid = await thread_service.find(s, v.project_id, v.vendor_id) if v.vendor_id else None
        if tid is not None:
            seq = await thread_service.post_system(s, tid, code="visit_cancelled", ref_id=v.id)
            card_later(s, tid, seq)
    await end_visit(s, v, now=now, shortlist_code="cancelled", thread_reason="cancelled")
    if v.vendor_id is not None:
        work = await _work_name(s, v) if is_v3(v) else TRADE_LABEL[v.trade]  # v3: 공사 이름
        await notify_service.add(
            s,
            user_id=v.vendor_id,
            kind="visit_cancelled",
            body=f"고객이 실측 요청을 취소했어요 · {work}",
            link=f"/vendor/requests/{v.id}",
            dedup_key=f"visit:{v.id}:cancelled",
        )
    await s.commit()
    push_later(s, [user.id, v.vendor_id], v)
    v = await repo.get(s, visit_id)
    closed = await repo.last_events(s, [v.id])
    return _visit_out(v, {}, today, closed)


async def _mine(s: AsyncSession, vendor: User, visit_id: uuid.UUID) -> Visit:
    v = await repo.get(s, visit_id)
    if v is None or v.vendor_id != vendor.id:  # 다른 업체 요청은 있는지도 알려 주지 않는다
        raise AppError(404, "not_found", "실측 요청을 찾을 수 없어요.")
    return v


async def decline(
    s: AsyncSession,
    vendor: User,
    visit_id: uuid.UUID,
    code: str,
    today: date,
    note: str | None = None,
) -> VendorVisitDetailOut:
    """업체가 이 요청을 못 받아요 (견적서를 보내기 전 · v3는 기간 답하기 전). 고객에게 고른 이유가
    보이고 담기 칸이 빈다. '그 밖'이면 한 줄(note)을 같이 남긴다."""
    v = await _mine(s, vendor, visit_id)
    await project_service.lock(s, v.project_id)  # 확정 · 다시 고르기와 같은 잠금 순서
    await _move(
        s,
        v,
        to="declined",
        by=vendor,
        values={"decline_code": code, "decline_note": note or None},
        code=code,
        message="이미 닫혔거나 견적서를 보낸 요청이에요.",
    )
    await end_visit(s, v, now=now_utc(), shortlist_code="declined", thread_reason="declined")
    owner = (await project_service.briefs(s, [v.project_id]))[v.project_id].owner_id
    await notify_service.add(
        s,
        user_id=owner,
        kind="visit_declined",
        body=f"{i_ga(v.vendor_name)} 이번 요청을 받지 못해요 · 다른 업체를 담아 주세요",
        link=f"/projects/{v.project_id}/visits",
        dedup_key=f"visit:{v.id}:declined",
        project_id=v.project_id,
    )
    await s.commit()
    push_later(s, [owner, vendor.id], v)
    return await vendor_visit(s, vendor, visit_id, today)


async def mark_seen(s: AsyncSession, vendor: User, visit_id: uuid.UUID) -> None:
    """'새 요청' 표시 지우기: 처음 열었을 때만 시각을 찍는다 (화면이 요청을 열 때 부른다)."""
    v = await _mine(s, vendor, visit_id)
    await repo.mark_seen(s, v.id, now_utc())
    await s.commit()


# ---- 업체 작업함 ----


def _home(sgg: str, std: StandardQuote | None, occupied: bool) -> str:
    """노원구 · 32평 · 빈 집"""
    parts = [sgg]
    if std is not None:
        parts.append(f"{_number(std.pyeong)}평")
    parts.append("짐 있는 집" if occupied else "빈 집")
    return " · ".join(parts)


def _row(
    v: Visit,
    b: ProjectBrief,
    std: StandardQuote | None,
    last: datetime | None,
    today: date,
) -> dict:
    """업체가 보는 요청 줄 칸 (B3: 집은 구 · 동까지만, 고객 이름 · 연락처 · 동 · 호 없음)"""
    return {
        "id": v.id,
        "received_at": v.created_at,
        "code": b.code,
        "trade": v.trade,
        "grade": v.grade or (std.grade if std is not None else None),
        "region": b.region,
        "area_name": b.sgg,
        "pyeong": std.pyeong if std is not None else None,
        "status": v.status,
        "new": v.status == "requested" and v.vendor_seen_at is None,
        "share_contact": v.share_contact,
        "closed_at": last if v.status in DONE else None,
        "decline_code": v.decline_code,
        "close_reason": v.close_reason,
        "waiting_days": days_since(last, today) if v.status == "quoted" and last else None,
    }


async def _context(
    s: AsyncSession, visits: list[Visit]
) -> tuple[dict[uuid.UUID, ProjectBrief], dict[tuple[uuid.UUID, str], StandardQuote]]:
    ids = list({v.project_id for v in visits})
    return await project_service.briefs(s, ids), await quote_service.latest_of(s, ids)


async def inbox(
    s: AsyncSession,
    vendor: User,
    tab: str,
    today: date,
    before: datetime | None = None,
    limit: int = 20,
) -> VendorVisitPage:
    """할 일: 새 요청부터, 그다음 받은 날 순 · 기다림: 오래 기다린 순 · 끝남: 최근 순(limit씩)."""
    done = tab == "done"
    pairs = await repo.of_vendor(
        s, vendor.id, TABS[tab], before if done else None, limit if done else None
    )
    if tab == "todo":
        pairs.sort(key=lambda x: (x[0].vendor_seen_at is not None, x[0].created_at))
    elif tab == "waiting":
        pairs.sort(key=lambda x: x[1])
    briefs, stds = await _context(s, [v for v, _ in pairs])
    items = [
        VendorVisitRow(
            **_row(v, briefs[v.project_id], stds.get((v.project_id, v.trade)), at, today)
        )
        for v, at in pairs
    ]
    more = done and len(pairs) == limit
    return VendorVisitPage(items=items, next_before=pairs[-1][1] if more else None)


async def inbox_summary(s: AsyncSession, vendor: User) -> InboxSummaryOut:
    n = await repo.counts_of_vendor(s, vendor.id)
    recent = await repo.recent_moves(s, vendor.id, now_utc() - timedelta(days=RECENT_DAYS))
    return InboxSummaryOut(
        todo=sum(n.get(st, 0) for st in TABS["todo"]),
        waiting=sum(n.get(st, 0) for st in TABS["waiting"]),
        done=sum(n.get(st, 0) for st in DONE),
        sched=sum(n.get(st, 0) for st in TABS["sched"]),
        jobs=0,
        recent=RecentOut(
            requested=recent.get("requested", 0),
            quoted=recent.get("quoted", 0),
            chosen=recent.get("chosen", 0),
        ),
    )


async def vendor_visit(
    s: AsyncSession,
    vendor: User,
    visit_id: uuid.UUID,
    today: date,
    preview: tuple[date, date, list[date]] | None = None,
) -> VendorVisitDetailOut:
    """업체가 보는 요청 자세히: 집(구 · 동 · 면적) · 희망 시기 · 남긴 말 · 사진.
    v3(V04 · D14): 예산 · 가견적 · 내 달력(확정 · 잡아 둠 · 다른 요청 기간) · 열린 반나절 수 ·
    다른 날 제안 고를 거리. preview(start, end, 빼는 날)를 주면 그 기간으로 열린 칸을 미리 센다.
    고객 이름 · 휴대폰 · 동 · 호는 넣지 않는다(B3, 연락은 실측이 확정되면 채팅으로)."""
    v = await _mine(s, vendor, visit_id)
    briefs, stds = await _context(s, [v])
    b, std = briefs[v.project_id], stds.get((v.project_id, v.trade))
    last = (await repo.last_events(s, [v.id])).get(v.id)
    now = now_utc()
    x = await load_ctx(s, [v.id])
    h = (await repo.home_facts(s, [(v.project_id, v.trade)])).get((v.project_id, v.trade))
    extra: dict = {}
    when = _when(b.when)
    if is_v3(v):
        when = f"{_md(v.wish_start)} ~ {_md(v.wish_end)}"
        extra = await _vendor_calendar(s, vendor, v, x, now, preview)
    photos = (await repo.photos_of(s, [v.id])).get(v.id, [])
    return VendorVisitDetailOut(
        **_row(v, b, std, last, today),
        **vendor_v3(v, h, std, x, now),
        area_m2=std.area_m2 if std is not None else None,
        area_basis=std.area_basis if std is not None else None,
        occupied=b.occupied,
        home_text=_home(b.sgg, std, b.occupied),
        when_text=when,
        message=v.message,
        photos=[ph.file_id for ph in photos],
        photo_labels=[ph.label for ph in photos],
        **extra,
    )


async def _taken(
    s: AsyncSession, vendor_id: uuid.UUID, start: date, end: date, but: uuid.UUID
) -> dict[tuple[date, str], int]:
    """그 업체의 반나절마다 쓴 칸 수 (but 요청이 잡은 칸은 빼고, L03)"""
    holds = await repo.vendor_holds(s, vendor_id, start, end)
    return slots.taken_count((h.day, h.half) for h in holds if h.visit_id != but)


async def _vendor_calendar(
    s: AsyncSession,
    vendor: User,
    v: Visit,
    x: "Ctx",
    now: datetime,
    preview: tuple[date, date, list[date]] | None = None,
) -> dict:
    """기간 답하기 달력 (V04 · D14): 잡힌 반나절 · 다른 요청 기간 · 열린 칸 수(미리 세기) ·
    다른 날 제안 고를 거리(V07, 수락 기다릴 때만)"""
    f = (await repo.vendor_facts(s, [vendor.id])).get(vendor.id)
    cap = f.capacity if f else 1
    w = x.windows.get(v.id)
    span = [d for d in (v.wish_start, v.wish_end, w and w.start_date, w and w.end_date) if d]
    today = today_of(now)
    if preview is not None:
        start, end, excluded = preview
        taken = await _taken(s, vendor.id, start, end, v.id)
        days = slots.open_halves(start, end, excluded, taken, cap, today)
    elif w is not None:
        days = await open_days(s, v, w, cap, today)
    else:
        taken = await _taken(s, vendor.id, v.wish_start, v.wish_end, v.id)
        days = slots.open_halves(v.wish_start, v.wish_end, [], taken, cap, today)
    options: list[SlotOut] = []
    hold = x.holds.get(v.id)
    if v.status == "picked":
        taken = await _taken(s, vendor.id, v.wish_start, v.wish_end, v.id)
        skip = [(hold.day, hold.half)] if hold else []
        options = [
            SlotOut(day=d, half=h)
            for d, h in visit_view.propose_options(
                v.wish_start, v.wish_end, taken, cap, today, skip
            )
        ]
    others = [
        o
        for o in await repo.visits_of_vendor(s, vendor.id, ("offered", "picked", "proposed"))
        if o.id != v.id
    ]
    wins = await repo.windows(s, [o.id for o in others])
    return {
        "capacity": cap,
        "instant_default": f.instant_default if f else False,
        "window": window_out(w),
        "booked": booked_out(x.holds.get(v.id)),
        "proposal": proposal_out(_first_proposal(v, x), x),
        "change": change_out(x.changes[v.id], x) if v.id in x.changes else None,
        "taken": await vendor_slots(s, vendor.id, min(span), max(span)),
        "other_windows": [
            OtherWindowOut(visit_id=o.id, start=wins[o.id].start_date, end=wins[o.id].end_date)
            for o in others
            if o.id in wins
        ],
        "open_count": slots.count_open(days),
        "propose_options": options,
        "reply_due_at": v.reply_due_at,
        "thread_id": await thread_service.find(s, v.project_id, vendor.id),
    }


# ---- 업체 실측 견적서: 임시 저장 · 보내기 (견적서 칸은 quote.service, 요청 상태는 여기서) ----

CLOSED_FOR_QUOTE = {
    "cancelled": "고객이 실측 요청을 취소해서 보낼 수 없어요.",
    "declined": "못 받는다고 한 요청이에요.",
    "closed": "닫힌 요청이라 견적서를 보낼 수 없어요.",
    "chosen": "고객이 확정해서 다시 올릴 수 없어요.",
    "expired": "기한이 지난 요청이라 견적서를 보낼 수 없어요.",
    "no_show": "실측에 오지 않았다고 기록된 요청이에요.",
}
QUOTE_LATER = "실측 날짜가 정해진 뒤에 견적서를 보낼 수 있어요."


def _first_quote_from(v: Visit) -> tuple[str, ...]:
    """처음 보내기를 할 수 있는 상태: v2는 요청함, v3는 실측 확정 · 실측 받음(J7)"""
    return ("booked", "visited") if is_v3(v) else ("requested",)


def _quote_blocked(v: Visit) -> AppError:
    return AppError(409, "invalid_state", CLOSED_FOR_QUOTE.get(v.status, QUOTE_LATER))


async def _locked_mine(s: AsyncSession, vendor: User, visit_id: uuid.UUID) -> Visit:
    v = await repo.lock(s, visit_id)
    if v is None or v.vendor_id != vendor.id:
        raise AppError(404, "not_found", "실측 요청을 찾을 수 없어요.")
    return v


async def quote_board(s: AsyncSession, vendor: User, visit_id: uuid.UUID) -> QuoteBoardOut:
    v = await _mine(s, vendor, visit_id)
    tax = await vendor_service.tax_kind_of(s, vendor.id)
    draft, sent = await quote_service.vendor_quotes(s, v.id)
    return QuoteBoardOut(
        visit_id=v.id,
        visit_status=v.status,
        tax_kind=tax,
        vat_modes=["exempt"] if tax == "exempt" else list(VAT_MODES),
        files_max=QUOTE_FILES_MAX,
        days_max=JOB_MAX_DAYS,
        draft=draft,
        sent=sent,
    )


async def save_quote(
    s: AsyncSession, vendor: User, visit_id: uuid.UUID, data: QuoteDraftIn, today: date
) -> QuoteBoardOut:
    """임시 저장 (v2 요청함 · v3 실측 확정 · 실측 받음 · 견적서 옴일 때만). 금액은 계산 엔진이
    공급가 · 부가세 · 합계로 나눈다."""
    v = await _locked_mine(s, vendor, visit_id)
    if v.status != "quoted" and v.status not in _first_quote_from(v):
        raise _quote_blocked(v)
    exempt = await vendor_service.tax_kind_of(s, vendor.id) == "exempt"
    try:
        await quote_service.save_draft(s, v.id, vendor.id, data, today, exempt)
        await s.commit()
    except IntegrityError as exc:  # 그사이 다른 곳에서 먼저 저장해 DB 제약에 걸렸다
        await s.rollback()
        raise AppError(409, "stale", "다른 곳에서 먼저 바꿨어요. 다시 열어 주세요.") from exc
    return await quote_board(s, vendor, visit_id)


async def send_quote(
    s: AsyncSession, vendor: User, visit_id: uuid.UUID, today: date
) -> QuoteBoardOut:
    """견적서 보내기. 처음이면 v2 requested · v3 booked · visited → quoted, 견적서 옴이면 다시 올리기
    (quoted → quoted). 다시 올리기는 visits.quote_ver가 지금 본 버전일 때만 새 버전으로(확정 뒤 409).
    v3는 견적서 기한 일 · 전날 알림을 거두고 채팅에 '견적서 보냄' 카드를 남긴다."""
    await project_service.lock(s, (await _mine(s, vendor, visit_id)).project_id)  # 프로젝트 → 요청
    v = await _locked_mine(s, vendor, visit_id)
    first = _first_quote_from(v)
    if v.status != "quoted" and v.status not in first:
        raise _quote_blocked(v)
    again = v.status == "quoted"
    if again and not await quote_service.has_draft(s, v.id):
        raise AppError(
            409, "already_sent", "이미 보낸 견적서예요. 고쳐서 임시 저장한 뒤 다시 올려 주세요."
        )
    q = await quote_service.seal_draft(s, v.id, today)
    ok = await repo.set_quote_ver(
        s, v.id, again=again, seen=v.quote_ver, new=q.version, first_from=first
    )
    if not ok:
        raise AppError(409, "invalid_state", "다른 곳에서 먼저 바꿨어요. 다시 열어 주세요.")
    now = now_utc()
    s.add(
        VisitEvent(
            visit_id=v.id,
            from_status=v.status,
            to_status="quoted",
            by_id=vendor.id,
            code="resent" if again else ("quote_sent" if is_v3(v) else None),
            at=now,
        )
    )
    if is_v3(v):
        await jobs_service.cancel_refs(
            s, now=now, ref_ids=[v.id], kinds=("visit_quote_due", "visit_reminder")
        )
        tid = await thread_service.find(s, v.project_id, vendor.id)
        if tid is not None:
            seq = await thread_service.post_system(s, tid, code="quote_sent", ref_id=q.id)
            card_later(s, tid, seq)
    owner = (await project_service.briefs(s, [v.project_id]))[v.project_id].owner_id
    verb = "다시 올렸어요" if again else "보냈어요"
    await notify_service.add(
        s,
        user_id=owner,
        kind="quote_resent" if again else "quote_sent",
        body=f"{i_ga(v.vendor_name)} 실측 견적서를 {verb} · {TRADE_LABEL[v.trade]}",
        link=f"/projects/{v.project_id}/quotes?trade={v.trade}",
        dedup_key=f"quote:{q.id}",
        project_id=v.project_id,
    )
    await s.commit()
    push_later(s, [owner, vendor.id], v)
    return await quote_board(s, vendor, visit_id)


# ---- 고객 견적서 비교 · 견적서 파일 내주기 ----

COMPARED = (
    "requested",
    "quoted",
    "chosen",
)  # 기다림 · 받음 · 확정 (닫힌 요청은 비교에 넣지 않는다)
COMPARED_V3 = ("booked", "visited", "quoted", "chosen")  # v3: 실측 확정 · 실측 받음이 견적서 기다림


def _compared(v: Visit) -> bool:
    return v.status in (COMPARED_V3 if is_v3(v) else COMPARED)


def _changed(latest, before) -> list[str]:
    if before is None:
        return []
    out = [k for k in ("total", "days", "valid_until") if getattr(latest, k) != getattr(before, k)]
    return out + (["files"] if len(latest.files) != len(before.files) else [])


async def compare(
    s: AsyncSession,
    user: User,
    project_id: uuid.UUID,
    trade: str,
    today: date,
    now: datetime | None = None,
) -> CompareOut:
    """한 공정의 실측 견적 비교. 받은 견적서는 처음 받은 순, 그다음 견적서를 기다리는 곳.
    v3: 가견적과 차이 · 실측일 · 유효기간 3일 안 · 알림 시각 · 다시 받기 · 별점 · 두 곳 차이(계산 엔진)."""
    now = now or now_utc()
    p, std, _ = await _standard(s, user, project_id, trade)
    every = await repo.of_project(s, p.id, None)
    rows = [v for v in every if v.trade == trade and _compared(v)]
    versions = await quote_service.sent_versions(s, [v.id for v in every])
    stars = await rating_of(s, [v.vendor_id for v in rows if v.vendor_id])
    out: list[CompareRowOut] = []
    for v in rows:
        sent = versions.get(v.id, [])
        latest = sent[0] if sent else None
        pct, way = _vs(latest.total, std.total) if latest else (None, None)
        until = latest.valid_until if latest else None
        out.append(
            CompareRowOut(
                visit_id=v.id,
                vendor_id=v.vendor_id,
                vendor_name=v.vendor_name,
                status=v.status,
                requested_at=v.created_at,
                seen_ver=v.quote_ver,
                latest=latest,
                history=[
                    QuoteVersionBrief(
                        version=q.version,
                        sent_at=q.sent_at,
                        total=q.total,
                        days=q.days,
                        valid_until=q.valid_until,
                        file_count=len(q.files),
                    )
                    for q in sent[1:]
                ],
                changed=_changed(latest, sent[1]) if len(sent) > 1 else [],
                estimate_total=v.estimate_total,
                vs_standard=pct,
                vs_direction=way,
                expired=bool(latest and latest.valid_until and latest.valid_until < today),
                diff_vs_estimate=visit_view.quote_vs_estimate(latest.total, v.estimate_total)
                if latest and v.estimate_total is not None
                else None,
                visit_day=v.booked_day,
                valid_soon=bool(until and visit_view.valid_soon(until, today)),
                valid_soon_at=[at for at in deadline.valid_soon_at(until) if at > now]
                if until
                else [],
                requote_at=v.requote_at,
                rating=stars.get(v.vendor_id) if v.vendor_id else None,
            )
        )
    first_sent = {v.id: versions[v.id][-1].sent_at for v in rows if versions.get(v.id)}
    out.sort(
        key=lambda r: (
            r.latest is None,
            first_sent.get(r.visit_id) or r.requested_at,
            r.requested_at,
        )
    )
    trades = [t for t in ("mokgong", "dobae") if getattr(p.trades, t) is not None]
    return CompareOut(
        project_id=p.id,
        trade=trade,
        standard_total=std.total,
        formula_ver=std.formula_ver,
        price_ver=std.price_ver,
        computed_at=std.created_at,
        sent_counts={
            t: sum(1 for v in every if v.trade == t and _compared(v) and versions[v.id])
            for t in trades
        },
        vendors=out,
        gap_total=visit_view.quote_gap([r.latest.total for r in out if r.latest is not None]),
    )


async def customer_quote_file(
    s: AsyncSession, user: User, project_id: uuid.UUID, quote_id: uuid.UUID, file_id: uuid.UUID
) -> Response:
    """그 요청의 고객에게 보낸 버전의 파일만 내준다. 임시 저장본 · 다른 집 파일은 404."""
    p = await project_service.get_for(s, user, project_id)
    got = await quote_service.quote_file(s, file_id, quote_id)
    v = await repo.get(s, got[0].visit_id) if got else None
    if (
        got is None
        or got[0].id != quote_id
        or got[0].sent_at is None
        or v is None
        or v.project_id != p.id
    ):
        raise AppError(404, "not_found", "파일을 찾을 수 없어요.")
    return await files_service.respond(got[1])


async def vendor_quote_file(
    s: AsyncSession, vendor: User, visit_id: uuid.UUID, file_id: uuid.UUID
) -> Response:
    """업체 본인의 견적서 파일 (임시 저장본 포함). 다른 요청 · 다른 업체 파일은 404."""
    v = await _mine(s, vendor, visit_id)
    got = await quote_service.quote_file(s, file_id)
    if got is None or got[0].visit_id != v.id:
        raise AppError(404, "not_found", "파일을 찾을 수 없어요.")
    return await files_service.respond(got[1])


async def vendor_visit_photo(
    s: AsyncSession, vendor: User, visit_id: uuid.UUID, file_id: uuid.UUID
) -> Response:
    """요청을 받은 업체만 그 요청 사진을 본다 (B3). 다른 요청 · 다른 업체 · 사진이 아닌 파일은 404."""
    v = await _mine(s, vendor, visit_id)
    f = None
    if await repo.has_photo(s, v.id, file_id):  # 요청 사진은 그 공사 고객이 올린 파일
        owner = (await project_service.briefs(s, [v.project_id]))[v.project_id].owner_id
        f = (await files_service.owned(s, owner, [file_id])).get(file_id)
    if f is None or f.kind != "photo":
        raise AppError(404, "not_found", "사진을 찾을 수 없어요.")
    return await files_service.respond(f)


# ---- 업체 확정에 쓰는 공개 함수 (deal은 schedule.service가 만든다) ----

QUOTE_CHANGED = "업체가 견적서를 다시 올렸어요. 새 견적서를 보고 다시 확정해 주세요."
NOT_CHOOSABLE = {
    "requested": "아직 견적서가 오지 않았어요.",
    "cancelled": "취소된 실측의 견적서라 고를 수 없어요.",
    "declined": "업체가 받지 못한 요청이라 고를 수 없어요.",
    "closed": "닫힌 실측의 견적서라 고를 수 없어요.",
    "chosen": "이미 업체를 확정했어요.",
}


async def in_project(s: AsyncSession, project_id: uuid.UUID, visit_id: uuid.UUID) -> Visit:
    """그 집의 요청 (아니면 404). 권한은 부르는 쪽이 프로젝트로 먼저 확인한다."""
    v = await repo.get(s, visit_id)
    if v is None or v.project_id != project_id:
        raise AppError(404, "not_found", "실측 요청을 찾을 수 없어요.")
    return v


async def choose(s: AsyncSession, user: User, v: Visit, seen_ver: int) -> None:
    """견적서 옴 → 확정. 고객이 본 버전(seen_ver)이 지금 버전일 때만 (다시 올렸으면 409)."""
    if v.status != "quoted":
        code = "already_chosen" if v.status == "chosen" else "not_choosable"
        raise AppError(409, code, NOT_CHOOSABLE[v.status])
    if v.quote_ver != seen_ver or not await repo.choose(s, v.id, seen_ver):
        raise AppError(409, "quote_changed", QUOTE_CHANGED)
    s.add(VisitEvent(visit_id=v.id, from_status="quoted", to_status="chosen", by_id=user.id))


async def close_others(
    s: AsyncSession, project_id: uuid.UUID, trade: str, keep: uuid.UUID, by: User
) -> list[uuid.UUID]:
    """다른 업체를 확정하면 같은 공정의 살아 있는 요청(확정 빼고 모두)을 닫는다(other_chosen).
    v3: 잡기 풀기 · 담기 칸 비움(closed, 확정한 업체 칸은 그대로) · 열린 제안 · 바꾸기 닫기 ·
    기한 일 취소 · 대화 읽기만. 연락처는 바로 가려진다. commit은 부르는 쪽이."""
    closed = []
    now = now_utc()
    kept = await repo.get(s, keep)
    for v in await repo.live(s, project_id, trade):
        if v.id == keep or v.status == "chosen":
            continue
        ok = await repo.set_status(
            s,
            v.id,
            allowed_from=(v.status,),
            values={"status": "closed", "close_reason": "other_chosen"},
        )
        if not ok:
            raise AppError(409, "stale", "그사이 상태가 바뀌었어요. 새로 불러올게요.")
        s.add(
            VisitEvent(
                visit_id=v.id,
                from_status=v.status,
                to_status="closed",
                by_id=by.id,
                code="other_chosen",
            )
        )
        await end_visit(s, v, now=now, shortlist_code="closed", thread_reason="other_chosen")
        if v.vendor_id is not None:
            await notify_service.add(
                s,
                user_id=v.vendor_id,
                kind="visit_closed",
                body="고객이 다른 업체로 정했어요",
                link=f"/vendor/requests/{v.id}",
                dedup_key=f"visit:{v.id}:closed",
            )
        closed.append(v.id)
    # 아직 요청을 보내지 않은 담은 업체도 칸을 비운다 (확정한 업체 칸만 남는다)
    keep_ids = {kept.shortlist_id} if kept is not None and kept.shortlist_id else set()
    await repo.remove_shortlists_of(s, project_id, trade, "closed", now, keep_ids)
    return closed


async def visits_by_id(s: AsyncSession, visit_ids: list[uuid.UUID]) -> dict[uuid.UUID, Visit]:
    return await repo.by_ids(s, visit_ids)


async def rows_for(
    s: AsyncSession, visit_ids: list[uuid.UUID], today: date
) -> dict[uuid.UUID, VendorVisitRow]:
    """업체 작업함 줄 (공사 탭이 확정 정보를 붙여 쓴다). 고객 연락처 규칙은 그대로."""
    visits = list((await repo.by_ids(s, visit_ids)).values())
    briefs, stds = await _context(s, visits)
    last = await repo.last_events(s, [v.id for v in visits])
    return {
        v.id: VendorVisitRow(
            **_row(
                v, briefs[v.project_id], stds.get((v.project_id, v.trade)), last.get(v.id), today
            )
        )
        for v in visits
    }


# ---- 다시 고르기: 이 공정의 살아 있는 요청을 모두 닫는다 (확정 전은 여기서, 확정 뒤는 schedule.service) ----

RESELECTED = "고객이 업체를 다시 고르기로 했어요"


async def close_for_reselect(
    s: AsyncSession, project_id: uuid.UUID, trade: str, by: User
) -> ReselectOut:
    """살아 있는 요청(확정 포함)을 모두 닫는다(reselected). 연락처는 바로 가려지고 칸 3개가 다시 빈다.
    v3: 잡기 풀기 · 담기 칸 모두 비움 · 열린 제안 · 바꾸기 닫기 · 기한 일 취소 · 대화 읽기만(cancelled).
    받은 견적서는 지우지 않는다. 확정했던 업체 알림은 부르는 쪽(deal)이 보낸다. commit은 부르는 쪽이."""
    closed = []
    now = now_utc()
    for v in await repo.live(s, project_id, trade):
        ok = await repo.set_status(
            s,
            v.id,
            allowed_from=(v.status,),
            values={"status": "closed", "close_reason": "reselected"},
        )
        if not ok:
            raise AppError(409, "stale", "그사이 상태가 바뀌었어요. 새로 불러올게요.")
        s.add(
            VisitEvent(
                visit_id=v.id,
                from_status=v.status,
                to_status="closed",
                by_id=by.id,
                code="reselected",
            )
        )
        await end_visit(s, v, now=now, shortlist_code="closed", thread_reason="cancelled")
        if v.status != "chosen" and v.vendor_id is not None:
            await notify_service.add(
                s,
                user_id=v.vendor_id,
                kind="visit_closed",
                body=RESELECTED,
                link=f"/vendor/requests/{v.id}",
                dedup_key=f"visit:{v.id}:closed",
            )
        closed.append(v.id)
    await repo.remove_shortlists_of(s, project_id, trade, "closed", now, set())
    return ReselectOut(trade=trade, closed=closed, slots_left=SLOTS)


async def reselect(
    s: AsyncSession, user: User, project_id: uuid.UUID, data: ReselectIn
) -> ReselectOut:
    """견적 비교에서 다시 고르기 (확정 전): 프로젝트 잠금 → 이 공정의 요청을 모두 닫기 → 이력 · 알림.
    확정이 살아 있으면 409(공사 날짜 화면의 다시 고르기를 쓴다)."""
    p = await project_service.get_for(s, user, project_id)
    await project_service.lock_for_visit(s, p.id)
    live = await repo.live(s, p.id, data.trade)
    if any(v.status == "chosen" for v in live):
        raise AppError(409, "already_chosen", NOT_CHOOSABLE["chosen"])
    if not live:
        raise AppError(409, "invalid_state", "닫을 실측 요청이 없어요.")
    out = await close_for_reselect(s, p.id, data.trade, user)
    await s.commit()
    return out


async def set_contact_until(s: AsyncSession, visit_id: uuid.UUID, until: date) -> None:
    """확정한 요청의 연락처 기한을 적는다 (업체가 '공사 끝냈어요'를 올릴 때마다 새로).
    서로 연락처를 보여도 되는지는 contact_visible이 이 값으로 정한다. commit은 부르는 쪽이."""
    if not await repo.set_status(
        s, visit_id, allowed_from=("chosen",), values={"contact_until": until}
    ):
        raise AppError(409, "stale", "그사이 상태가 바뀌었어요. 새로 불러올게요.")


# ---- 내 공사 한눈에 · 서류에 쓰는 공개 함수 ----


async def trade_facts(s: AsyncSession, project_id: uuid.UUID) -> dict[str, VisitFactsOut]:
    """공정마다 이번 고르기의 요청 · 견적서 수와 때 (권한은 부르는 쪽이 먼저 확인한다)"""
    every = [
        v
        for v in await repo.of_project(s, project_id, None)
        if not (v.status == "closed" and v.close_reason == "reselected")
    ]
    versions = await quote_service.sent_versions(s, [v.id for v in every])
    last = await repo.last_events(s, [v.id for v in every])
    out: dict[str, VisitFactsOut] = {}
    for t in {v.trade for v in every}:
        mine = sorted((v for v in every if v.trade == t), key=lambda v: v.created_at)
        sent = [v for v in mine if versions.get(v.id)]
        live = [v for v in sent if v.status in ("quoted", "chosen")]
        waiting = [v for v in mine if v.status == "requested"]
        out[t] = VisitFactsOut(
            trade=t,
            asked=len(mine),
            asked_at=mine[0].created_at if mine else None,
            quotes=len(sent),
            waiting=len(waiting),
            quoted_at=max((versions[v.id][0].sent_at for v in sent), default=None),
            quoted_names=[v.vendor_name for v in live],
            waiting_names=[v.vendor_name for v in waiting],
            last_at=max((last[v.id] for v in mine if v.id in last), default=None),
        )
    return out


async def quote_docs(s: AsyncSession, project_id: uuid.UUID) -> list[VisitQuotesOut]:
    """서류: 견적서를 보낸 요청마다 버전들 (닫힌 요청 포함, 처음 받은 순)"""
    every = await repo.of_project(s, project_id, None)
    versions = await quote_service.sent_versions(s, [v.id for v in every])
    rows = [v for v in every if versions.get(v.id)]
    rows.sort(key=lambda v: versions[v.id][-1].sent_at)
    return [
        VisitQuotesOut(
            visit_id=v.id,
            trade=v.trade,
            vendor_name=v.vendor_name,
            status=v.status,
            close_reason=v.close_reason,
            versions=versions[v.id],
        )
        for v in rows
    ]


# ======== v3 실측 일정: 읽기 묶음 · 화면 모양 · 정리 (dev-04) ========


def today_of(now: datetime) -> date:
    """지금 시각의 서울 날짜"""
    return now.astimezone(SEOUL).date()


def slot_text(day: date, half: str) -> str:
    """12.15(화) 오전"""
    return f"{day.month}.{day.day}({WEEKDAY[day.weekday()]}) {HALF_LABEL[half]}"


def slot_to(day: date, half: str) -> str:
    """12.15(화) 오전으로 · 12.15(화) 오후로"""
    return f"{day.month}.{day.day}({WEEKDAY[day.weekday()]}) {HALF_TO[half]}"


def work_start(p: ProjectOut) -> date | None:
    """공사 시작 희망일 (v3 기본 정보 · 옛 '날짜' 희망 시기). 모르면 None"""
    if p.home is not None and p.home.start_date is not None:
        return p.home.start_date
    return p.when.start if p.when.kind == "date" else None


@dataclass
class Ctx:
    """v3 요청 화면에 쓰는 행 묶음 (요청 id → 행). 제안 칸은 제안 id → [(날, 반나절)]"""

    windows: dict[uuid.UUID, VisitWindow]
    holds: dict[uuid.UUID, VisitHold]
    proposals: dict[uuid.UUID, VisitProposal]
    changes: dict[uuid.UUID, VisitChange]
    slots: dict[uuid.UUID, list[tuple[date, str]]]


async def load_ctx(s: AsyncSession, visit_ids: list[uuid.UUID]) -> Ctx:
    proposals = await repo.proposals_open(s, visit_ids)
    return Ctx(
        windows=await repo.windows(s, visit_ids),
        holds=await repo.holds_of_visits(s, visit_ids),
        proposals=proposals,
        changes=await repo.changes_open(s, visit_ids),
        slots=await repo.proposal_slots(s, [x.id for x in proposals.values()]),
    )


def window_out(w: VisitWindow | None) -> WindowOut | None:
    if w is None:
        return None
    return WindowOut(
        start=w.start_date,
        end=w.end_date,
        excluded=sorted(w.excluded or []),
        instant=w.instant,
        answered_at=w.answered_at,
        pick_due_at=w.pick_due_at,
    )


def booked_out(h: VisitHold | None) -> BookedOut | None:
    if h is None:
        return None
    return BookedOut(day=h.day, half=h.half, kind=h.kind, hold_until=h.until)


def proposal_out(p: VisitProposal | None, x: Ctx) -> VisitProposalOut | None:
    if p is None:
        return None
    return VisitProposalOut(
        id=p.id,
        reason_code=p.reason_code,
        note=p.note,
        slots=[SlotOut(day=d, half=h) for d, h in x.slots.get(p.id, [])],
        status=p.status,
        due_at=p.due_at,
        change_id=p.change_id,
    )


def change_out(c: VisitChange, x: Ctx, proposal: VisitProposal | None = None) -> ChangeOut:
    """일정 바꾸기 요청 (업체가 다른 날을 냈으면 그 제안도)"""
    if proposal is None:
        open_ = x.proposals.get(c.visit_id)
        proposal = open_ if open_ is not None and open_.change_id == c.id else None
    return ChangeOut(
        id=c.id,
        visit_id=c.visit_id,
        from_day=c.from_day,
        from_half=c.from_half,
        to_day=c.to_day,
        to_half=c.to_half,
        status=c.status,
        due_at=c.due_at,
        proposal=proposal_out(proposal, x),
        created_at=c.created_at,
    )


def _first_proposal(v: Visit, x: Ctx) -> VisitProposal | None:
    """처음 제안(V07)만 (바꾸기 요청에 낸 다른 날은 change 칸에)"""
    p = x.proposals.get(v.id)
    return p if p is not None and p.change_id is None else None


def next_of(v: Visit, x: Ctx, now: datetime) -> VisitNextOut:
    """고객 화면의 다음 할 일 · 기한. 지남 · 곧 지남은 계산 엔진으로 센다(worker가 늦어도 맞게)."""
    kind, who, due = "none", "none", None
    st = v.status
    if st == "requested":
        kind, who, due = "reply", "vendor", v.reply_due_at
    elif st == "offered":
        w = x.windows.get(v.id)
        kind, who, due = "pick", "customer", w.pick_due_at if w else None
    elif st == "picked":
        h = x.holds.get(v.id)
        kind, who, due = "accept", "vendor", h.until if h else None
    elif st == "proposed":
        p = _first_proposal(v, x)
        kind, who, due = "proposal", "customer", p.due_at if p else None
    elif st == "booked":
        c = x.changes.get(v.id)
        if c is not None and c.status == "open":
            kind, who, due = "change", "vendor", c.due_at
        elif c is not None:  # countered: 업체가 낸 다른 날을 고객이 고를 차례
            p = x.proposals.get(v.id)
            kind, who, due = "change", "customer", p.due_at if p else c.due_at
        elif v.booked_day is not None:
            kind, who = "visit", "vendor"
            due = deadline.half_end(v.booked_day, v.booked_half)
    elif st == "visited":
        kind, who, due = "quote", "vendor", v.quote_due_at
    elif st == "quoted":
        kind, who = "confirm", "customer"
    return VisitNextOut(
        kind=kind,
        who=who,
        due_at=due,
        overdue=deadline.is_over(now, due),
        soon=deadline.is_soon(now, due),
    )


def can_of(
    v: Visit,
    x: Ctx,
    now: datetime,
    *,
    latest: bool,
    has_live: bool,
    quote: QuoteBriefOut | None,
) -> VisitCanOut:
    """지금 누를 수 있는 단추 (고객)"""
    st, today = v.status, today_of(now)
    w, p = x.windows.get(v.id), _first_proposal(v, x)
    booked = st == "booked" and v.booked_day is not None
    passed = booked and deadline.visited_ok(now, v.booked_day, v.booked_half)
    return VisitCanOut(
        pick=(st == "offered" and w is not None and not deadline.is_over(now, w.pick_due_at))
        or (st == "proposed" and p is not None and not deadline.is_over(now, p.due_at)),
        cancel=st in CANCELLABLE,
        again=st in AGAIN_FROM and latest and not has_live and v.attempt < ATTEMPTS_MAX,
        visited=passed,
        change=booked and not passed and v.id not in x.changes,
        requote=st == "quoted"
        and quote is not None
        and quote.valid_until is not None
        and quote.valid_until < today
        and v.requote_at is None,
    )


async def board_rows(
    s: AsyncSession, visits: list[Visit], now: datetime
) -> tuple[VisitHeadOut | None, list[BoardRowOut]]:
    """실측 일정 상태판 (C14 · D03): 업체마다 가장 마지막 시도 한 줄, 처음 요청한 순.
    v3 요청(희망 기간을 보낸 요청)만 보고, 다시 고르기로 닫힌 요청은 뺀다."""
    mine = [
        v
        for v in visits
        if is_v3(v) and not (v.status == "closed" and v.close_reason == "reselected")
    ]
    if not mine:
        return None, []
    groups: dict[tuple, list[Visit]] = {}
    for v in mine:
        groups.setdefault((v.trade, v.vendor_id or v.id), []).append(v)
    latest = {k: max(g, key=lambda v: (v.attempt, v.created_at)) for k, g in groups.items()}
    first = {k: min(v.created_at for v in g) for k, g in groups.items()}
    rows = [latest[k] for k in sorted(groups, key=lambda k: (first[k], str(k[1])))]
    x = await load_ctx(s, [v.id for v in rows])
    quotes = await quote_service.sent_briefs(s, [v.id for v in rows])
    out = []
    for v in rows:
        live_here = any(o.status in LIVE for o in groups[(v.trade, v.vendor_id or v.id)])
        tid = await thread_service.find(s, v.project_id, v.vendor_id) if v.vendor_id else None
        out.append(
            BoardRowOut(
                visit_id=v.id,
                vendor_id=v.vendor_id,
                vendor_name=v.vendor_name,
                attempt=v.attempt,
                status=v.status,
                next=next_of(v, x, now),
                window=window_out(x.windows.get(v.id)),
                booked=booked_out(x.holds.get(v.id)),
                proposal=proposal_out(_first_proposal(v, x), x),
                change=change_out(x.changes[v.id], x) if v.id in x.changes else None,
                can=can_of(v, x, now, latest=True, has_live=live_here, quote=quotes.get(v.id)),
                estimate_total=v.estimate_total,
                thread_id=tid,
                quote=quotes.get(v.id),
                cancel_code=v.cancel_code,
                expired_reason=v.expired_reason,
                expired_due_at=expired_due(v, x.windows.get(v.id)),
            )
        )
    head_v = min(mine, key=lambda v: (v.created_at, v.attempt))
    photos = (await repo.photos_of(s, [head_v.id])).get(head_v.id, [])
    head = VisitHeadOut(
        trade=head_v.trade,
        sent_at=min(first.values()),
        wish_start=head_v.wish_start,
        wish_end=head_v.wish_end,
        work_wish_on=head_v.work_wish_on,
        photos=[x.file_id for x in photos],
        photo_labels=[x.label for x in photos],
    )
    return head, out


async def board_row(s: AsyncSession, v: Visit, now: datetime) -> BoardRowOut:
    """요청 한 건의 상태판 줄 (한 번 더 요청 응답)"""
    same = [
        o
        for o in await repo.of_project(s, v.project_id, v.trade)
        if o.vendor_id == v.vendor_id and is_v3(o)
    ]
    _, rows = await board_rows(s, same or [v], now)
    return next(r for r in rows if r.visit_id == v.id)


# ---- 고객 읽기 (에이전트 도구도 부른다, contract 5장) ----


async def shortlist_out(
    s: AsyncSession,
    p: ProjectOut,
    std: StandardQuote,
    lines: list[StandardQuoteLine],
    trade: str,
) -> ShortlistOut:
    actives = await repo.shortlists_active(s, p.id, trade)
    live = await repo.live(s, p.id, trade)
    ids = [x.vendor_id for x in actives]
    cards = await cards_by_grade(s, p, std, lines, trade, [(x.vendor_id, x.grade) for x in actives])
    facts = await repo.vendor_facts(s, ids)
    by_vendor = {v.vendor_id: v for v in live}
    items = []
    for x in actives:
        v = by_vendor.get(x.vendor_id)
        f = facts.get(x.vendor_id)
        items.append(
            ShortlistItemOut(
                vendor_id=x.vendor_id,
                name=f.name if f else "업체",
                slot=x.slot,
                grade=x.grade,
                estimate_total=cards[x.vendor_id].total,
                state=cards[x.vendor_id].state,
                visit_id=v.id if v else None,
                visit_status=v.status if v else None,
                added_at=x.created_at,
            )
        )
    count = _shortlist_count(actives, live)
    return ShortlistOut(
        project_id=p.id, trade=trade, items=items, count=count.count, left=count.left
    )


async def shortlist_for(
    s: AsyncSession, user: User, project_id: uuid.UUID, trade: str
) -> ShortlistOut:
    """담은 업체 · 칸 (공사 · 공종마다 3칸)"""
    p, std, lines = await _standard(s, user, project_id, trade)
    return await shortlist_out(s, p, std, lines, trade)


async def draft_for(
    s: AsyncSession, user: User, project_id: uuid.UUID, trade: str, today: date
) -> VisitDraftOut:
    """요청서 · AI 초안 카드: 희망 기간(계산 엔진 공사 시작 희망일 − 28일 ~ − 14일)과 보낼 업체
    (담았고 아직 살아 있는 요청이 없는 업체). 희망일이 없으면 기간은 비운다(UC-VIS-01 A2)."""
    p, std, lines = await _standard(s, user, project_id, trade)
    basis = work_start(p)
    wish = slots.draft_wish(basis, today)
    actives = await repo.shortlists_active(s, p.id, trade)
    live = {v.vendor_id for v in await repo.live(s, p.id, trade)}
    waiting = [x for x in actives if x.vendor_id not in live]
    cards = await cards_by_grade(s, p, std, lines, trade, [(x.vendor_id, x.grade) for x in waiting])
    facts = await repo.vendor_facts(s, [x.vendor_id for x in waiting])
    grade = _grade(p, std, trade)
    return VisitDraftOut(
        trade=trade,
        wish_start=wish[0] if wish else None,
        wish_end=wish[1] if wish else None,
        basis_start=basis,
        vendors=[
            DraftVendorOut(
                vendor_id=x.vendor_id,
                name=facts[x.vendor_id].name if x.vendor_id in facts else "업체",
                estimate_total=cards[x.vendor_id].total,
                grade=(x.grade or grade) if trade == "dobae" else None,
            )
            for x in waiting
        ],
    )


async def open_days(
    s: AsyncSession, v: Visit, w: VisitWindow, capacity: int, today: date
) -> list[slots.DayOpen]:
    """업체 기간의 날마다 남은 칸 (이 요청이 잡은 칸은 빼고 센다, L03)"""
    holds = await repo.vendor_holds(s, v.vendor_id, w.start_date, w.end_date)
    taken = slots.taken_count((h.day, h.half) for h in holds if h.visit_id != v.id)
    return slots.open_halves(w.start_date, w.end_date, w.excluded or [], taken, capacity, today)


async def capacity_of(s: AsyncSession, vendor_id: uuid.UUID | None) -> int:
    f = (await repo.vendor_facts(s, [vendor_id])).get(vendor_id) if vendor_id else None
    return f.capacity if f else 1


async def pick_view(
    s: AsyncSession, user: User, project_id: uuid.UUID, visit_id: uuid.UUID, now: datetime
) -> VisitPickOut:
    """날짜 고르기 · 확정 · 제안 · 겹침 없음 화면 (C16 · C19 · C21 · C22 · D15~D17).
    열린 반나절 · 추천은 계산 엔진(calc.slots), AI는 설명만 한다."""
    p = await project_service.get_for(s, user, project_id)
    v = await in_project(s, p.id, visit_id)
    x = await load_ctx(s, [v.id])
    w = x.windows.get(v.id)
    today = today_of(now)
    days: list[slots.DayOpen] = []
    if w is not None and v.vendor_id is not None:
        days = await open_days(s, v, w, await capacity_of(s, v.vendor_id), today)
    excluded = set(w.excluded or []) if w else set()
    full = [
        SlotOut(day=d.day, half=h)
        for d in days
        if d.day > today and d.day not in excluded
        for h in slots.HALVES
        if d.left(h) == 0
    ]
    rec = slots.recommend(v.work_wish_on, days) if days else None
    overlap = bool(
        w is not None
        and v.wish_start is not None
        and slots.overlaps(v.wish_start, v.wish_end, w.start_date, w.end_date)
    )
    why = (
        visit_view.no_overlap_reason(
            v.wish_start,
            v.wish_end,
            v.work_wish_on,
            w.start_date,
            w.end_date,
            slots.count_open(days),
        )
        if w is not None and v.wish_start is not None and v.status == "offered"
        else None
    )
    others = [
        o
        for o in await repo.of_project(s, p.id, None)
        if o.id != v.id and o.status in ("picked", "booked", "visited")
    ]
    held = await repo.holds_of_visits(s, [o.id for o in others])
    return VisitPickOut(
        visit_id=v.id,
        project_id=p.id,
        trade=v.trade,
        vendor_id=v.vendor_id,
        vendor_name=v.vendor_name,
        attempt=v.attempt,
        status=v.status,
        next=next_of(v, x, now),
        wish_start=v.wish_start,
        wish_end=v.wish_end,
        work_wish_on=v.work_wish_on,
        window=window_out(w),
        days=[
            DayHalvesOut(day=d.day, am=d.am > 0, pm=d.pm > 0, am_left=d.am, pm_left=d.pm)
            for d in days
        ],
        full=full,
        mine=[
            MyVisitOut(visit_id=o.id, vendor_name=o.vendor_name, day=h.day, half=h.half)
            for o in others
            if (h := held.get(o.id)) is not None
        ],
        instant=bool(w and w.instant),
        recommend=RecommendOut(day=rec.day, half=rec.half, target=rec.target, basis=rec.basis)
        if rec
        else None,
        overlap=overlap,
        ways=["ask_other", "other_vendor", "change_wish"]
        if w is not None and (not overlap or why is not None)
        else [],
        no_overlap_reason=why,
        booked=booked_out(x.holds.get(v.id)),
        proposal=proposal_out(_first_proposal(v, x), x),
        change=change_out(x.changes[v.id], x) if v.id in x.changes else None,
        thread_id=await thread_service.find(s, p.id, v.vendor_id) if v.vendor_id else None,
    )


# ---- 업체 화면 v3 칸 (V03 · V04 · D14). 고객 이름 · 연락처 · 동 · 호는 넣지 않는다 ----


def area_label(h: repo.HomeFacts | None, std: StandardQuote | None) -> str | None:
    """노원구 상계동 · 공급 69.4㎡ (계산 엔진 calc.visit_view.area_label). 면적 · 기준은 표준견적서에
    저장한 값, 없으면 기본 정보 값"""
    if h is None:
        return None
    if std is not None and std.area_m2 is not None:
        return visit_view.area_label(h.sgg, h.emd, std.area_m2, std.area_basis)
    return visit_view.area_label(h.sgg, h.emd, h.area_m2, h.area_basis)


async def _work_name(s: AsyncSession, v: Visit) -> str:
    """업체 알림 끝 공사 이름: '노원구 상계동 도배' (이름 · 아파트 · 동호수 없음)"""
    h = (await repo.home_facts(s, [(v.project_id, v.trade)])).get((v.project_id, v.trade))
    return work_name(h.sgg, h.emd, v.trade) if h else TRADE_LABEL[v.trade]


def home_label(h: repo.HomeFacts, trade: str) -> str:
    """아파트 · 도배 전체 · 빈 집"""
    work = TRADE_LABEL[trade]
    if trade == "dobae" and h.scope:
        work += " 전체" if h.scope == "full" else " 부분"
    return " · ".join(["아파트", work, "사는 중" if h.occupied else "빈 집"])


def _tag(icon: str, label: str) -> StateTagOut:
    return StateTagOut(icon=icon, label=label)


def vendor_state(
    v: Visit, x: Ctx, now: datetime
) -> tuple[StateTagOut | None, VendorNextOut | None]:
    """업체 작업함 상태 (아이콘 + 글자, N4 C)와 다음 할 일 · 기한"""
    st = v.status
    due: datetime | None = None
    nxt: str | None = None
    if st == "requested":
        tag, nxt, due = _tag("todo", "새 실측 요청"), "기간 답하기", v.reply_due_at
    elif st == "offered":
        w = x.windows.get(v.id)
        tag, nxt, due = (
            _tag("wait", "고객 날짜 고르는 중"),
            "고객 고르기 기다림",
            w and w.pick_due_at,
        )
    elif st == "picked":
        h = x.holds.get(v.id)
        tag, nxt, due = _tag("todo", "날짜 수락"), "날짜 수락", h.until if h else None
    elif st == "proposed":
        p = _first_proposal(v, x)
        tag, nxt, due = _tag("wait", "제안한 날 기다림"), "고객 고르기 기다림", p and p.due_at
    elif st == "booked":
        c = x.changes.get(v.id)
        if c is not None and c.status == "open":
            tag, nxt, due = _tag("todo", "일정 바꾸기 요청"), "바꾸기 답하기", c.due_at
        elif c is not None:
            tag, nxt = _tag("wait", "다른 날 보냄"), "고객 고르기 기다림"
        else:
            tag, nxt = _tag("ok", "실측 확정"), "실측 가기"
            if v.booked_day is not None:
                due = deadline.half_end(v.booked_day, v.booked_half)
    elif st == "visited":
        tag, nxt, due = _tag("todo", "실측 견적서 보내기"), "견적서 보내기", v.quote_due_at
    elif st == "quoted":
        tag, nxt = _tag("wait", "견적서 보냄"), "고객 확정 기다림"
    elif st == "chosen":
        tag = _tag("ok", "업체 확정")
    elif st == "expired":
        tag = _tag("late", "답 기한 지남" if v.expired_reason == "reply" else "고르기 기한 지남")
        nxt = "재요청 오면 답하기" if v.expired_reason == "reply" else None
    else:
        tag = _tag(
            "done",
            {"declined": "못 받아요", "cancelled": "고객이 취소함", "no_show": "안 왔다고 함"}.get(
                st, "닫힘"
            ),
        )
    if tag.icon in ("todo", "wait") and deadline.is_over(now, due):
        tag = _tag("late", tag.label)
    out = VendorNextOut(label=nxt, due_at=due, overdue=deadline.is_over(now, due)) if nxt else None
    return tag, out


def vendor_v3(
    v: Visit, h: repo.HomeFacts | None, std: StandardQuote | None, x: Ctx, now: datetime
) -> dict:
    """VendorVisitRow v3 칸 (작업함 줄 · 자세히 공통)"""
    state, nxt = vendor_state(v, x, now) if is_v3(v) else (None, None)
    return {
        "area_label": area_label(h, std),
        "home": home_label(h, v.trade) if h else None,
        "wish_start": v.wish_start,
        "wish_end": v.wish_end,
        "work_wish_on": v.work_wish_on,
        "estimate_total": v.estimate_total,
        "budget_won": h.budget_won if h else None,
        "state": state,
        "next": nxt,
        "attempt": v.attempt,
    }


async def vendor_fields(
    s: AsyncSession, visits: list[Visit], now: datetime
) -> dict[uuid.UUID, dict]:
    """업체 작업함 줄의 v3 칸을 여러 요청에 한꺼번에 (schedule의 작업함 목록이 같은 글 · 기한을 쓰게).
    돌려주는 값: 요청 id → VendorVisitRow v3 칸 dict (고객 이름 · 연락처 · 동 · 호 없음)."""
    x = await load_ctx(s, [v.id for v in visits])
    homes = await repo.home_facts(s, [(v.project_id, v.trade) for v in visits])
    stds = await quote_service.latest_of(s, list({v.project_id for v in visits}))
    return {
        v.id: vendor_v3(
            v, homes.get((v.project_id, v.trade)), stds.get((v.project_id, v.trade)), x, now
        )
        for v in visits
    }


async def vendor_slots(
    s: AsyncSession, vendor_id: uuid.UUID, start: date, end: date
) -> list[VendorSlotOut]:
    """업체 달력의 잡힌 · 잡아 둔 반나절 (날 · 반나절 · 동네 순, 고객 이름 없음)"""
    holds = await repo.vendor_holds(s, vendor_id, start, end)
    visits = await repo.by_ids(s, list({h.visit_id for h in holds}))
    homes = await repo.home_facts(s, [(v.project_id, v.trade) for v in visits.values()])
    stds = await quote_service.latest_of(s, list({v.project_id for v in visits.values()}))
    out = []
    for h in holds:
        v = visits.get(h.visit_id)
        key = (v.project_id, v.trade) if v else None
        out.append(
            VendorSlotOut(
                visit_id=h.visit_id,
                day=h.day,
                half=h.half,
                kind=h.kind,
                seat=h.seat,
                area_label=area_label(homes.get(key), stds.get(key)) or "",
                hold_until=h.until,
            )
        )
    out.sort(key=lambda x: (x.day, slots.HALVES.index(x.half), x.area_label, x.seat))
    return out


# ---- 상태가 끝날 때 같이 하는 정리 · commit 뒤 신호 ----


# 업체 멈춤(D26)으로 닫는 요청: 업체 확정 전 진행 중인 것 (확정한 공사는 schedule이 다룬다)
SUSPEND_CLOSES = ("requested", "offered", "picked", "proposed", "booked", "visited", "quoted")
SUSPENDED = "지금 믿집에서 일할 수 없는 업체라 요청이 닫혔어요 · 다른 업체를 담아 주세요"


async def close_for_vendor(s: AsyncSession, vendor_id: uuid.UUID, now: datetime) -> int:
    """업체 멈춤(B1b 관리자가 부른다): 그 업체의 진행 중 실측 요청(업체 확정 전)을 닫고(closed ·
    reselected, 아래 '부탁') 고객에게 '다시 고르기' 알림, 잡기 · 기한 일 · 열린 제안 · 바꾸기를 정리하고
    그 업체를 담은 칸도 비운다. 부르는 쪽 트랜잭션 안에서(commit 없음). 대화는 부르는 쪽
    (thread.service.close_for_vendor)이 닫으니, 잠금 순서(프로젝트 → 요청 → 대화)대로 이 함수를 먼저 부른다.
    돌려주는 값: 닫은 요청 수."""
    closed = 0
    for row in await repo.visits_of_vendor(s, vendor_id, SUSPEND_CLOSES):
        await project_service.lock(s, row.project_id)
        v = await repo.lock(s, row.id)
        if v is None or v.status not in SUSPEND_CLOSES:
            continue  # 그사이 고객이 먼저 바꿨다
        values = {"status": "closed", "close_reason": "vendor_stopped"}
        if not await repo.set_status(s, v.id, allowed_from=(v.status,), values=values):
            continue
        s.add(
            VisitEvent(
                visit_id=v.id,
                from_status=v.status,
                to_status="closed",
                by_id=None,
                code="closed",
                at=now,
            )
        )
        await end_visit(s, v, now=now, shortlist_code="closed")
        owner = (await project_service.briefs(s, [v.project_id]))[v.project_id].owner_id
        await notify_service.add(
            s,
            user_id=owner,
            kind="visit_vendor_stopped",
            body=f"{i_ga(v.vendor_name)} {SUSPENDED}",
            link=link("visit_vendor_stopped", pid=v.project_id),
            dedup_key=f"visit_suspended:{v.id}",
            project_id=v.project_id,
        )
        push_later(s, [owner, vendor_id], v)
        closed += 1
    await repo.remove_vendor_shortlists(s, vendor_id, "closed", now)
    return closed


async def end_visit(
    s: AsyncSession,
    v: Visit,
    *,
    now: datetime,
    shortlist_code: str | None,
    thread_reason: str | None = None,
) -> None:
    """요청이 끝나거나 닫힐 때: 반나절 잡기 풀기 · 담기 칸 비우기 · 열린 제안 · 바꾸기 닫기 ·
    기한 일 취소 · 대화 읽기만. 이미 그렇게 됐으면 아무것도 바꾸지 않는다(같이 불러도 괜찮다)."""
    await repo.release_holds(s, v.id, now)
    if shortlist_code is not None:
        await repo.remove_shortlist(s, v.shortlist_id, shortlist_code, now)
    refs: list[uuid.UUID | None] = [v.id]
    p = (await repo.proposals_open(s, [v.id])).get(v.id)
    if p is not None:
        await repo.close_proposal(s, p.id, "expired", now)
        refs.append(p.id)
    c = (await repo.changes_open(s, [v.id])).get(v.id)
    if c is not None:
        await repo.set_change(
            s,
            c.id,
            allowed_from=("open", "countered"),
            values={"status": "withdrawn", "closed_at": now},
        )
        refs.append(c.id)
    await jobs_service.cancel_refs(s, now=now, ref_ids=refs)
    if thread_reason is not None and v.vendor_id is not None:
        await thread_service.set_read_only(
            s, reason=thread_reason, project_id=v.project_id, vendor_id=v.vendor_id
        )


# commit 뒤에 보낼 신호('인자 없는 async 함수')를 남기는 s.info 칸. 실측 router의 visit_session이
# 엔드포인트가 끝나면(응답 전에) 차례로 보내고, 오류로 끝나면 버린다. service를 바로 부르는 곳(시연
# 데이터 · 테스트)은 보내지 않는다: 신호는 지워져도 되는 것이고, 다른 이벤트 루프에서 공용 Redis 연결을
# 만들어 남기지 않게.
LATER = "visit_signals"


def _later(s: AsyncSession, fn: Callable[[], Awaitable[object]]) -> None:
    s.info.setdefault(LATER, []).append(fn)


def state_later(
    s: AsyncSession,
    users: list[uuid.UUID | None],
    ref_id: uuid.UUID,
    project_id: uuid.UUID | None,
    *,
    unread: bool = False,
) -> None:
    """commit 뒤 화면 다시 읽기 신호(state)를 남긴다. unread면 안 읽은 알림 수(notif)도."""
    who = list(users)
    _later(s, lambda: realtime.push_state(who, "visit", ref_id, project_id))
    if unread:
        _later(s, lambda: notify_service.push_unread(s, who))


def push_later(s: AsyncSession, users: list[uuid.UUID | None], v: Visit) -> None:
    """commit 뒤 이 요청의 화면 신호(state) · 안 읽은 알림 수(notif)를 남긴다."""
    state_later(s, users, v.id, v.project_id, unread=True)


def card_later(s: AsyncSession, thread_id: uuid.UUID, seq: int) -> None:
    """commit 뒤 대화 두 사람에게 시스템 카드 알림(msg)을 남긴다 (thread.service.post_system 계약)."""
    _later(s, lambda: thread_service.notify_message(s, thread_id, seq))


def drop_later(s: AsyncSession) -> None:
    s.info.pop(LATER, None)


async def flush_later(s: AsyncSession) -> None:
    """남긴 신호를 보낸다. 실패해도 로그만 남긴다(이미 저장된 요청을 실패로 돌리지 않는다)."""
    for fn in s.info.pop(LATER, None) or ():
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001 - 신호는 화면이 다시 읽어 맞춘다
            log.warning("실측 신호 실패: %s", type(exc).__name__)


def expired_due(v: Visit, window: VisitWindow | None) -> datetime | None:
    """기한이 지나 끝난 요청(expired)의 그 기한: 답 없음 = 업체 답 기한, 고르기 없음 = 고르기 기한"""
    if v.status != "expired":
        return None
    if v.expired_reason == "reply":
        return v.reply_due_at
    if v.expired_reason == "pick" and window is not None:
        return window.pick_due_at
    return None


def visit_error(v: Visit, message: str) -> AppError:
    """지금 상태에서 못 하는 일: 기한이 지나 끝났으면 expired, 그 밖은 stale"""
    return app_error("expired" if v.status == "expired" else "stale", message)
