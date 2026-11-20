"""AI 칸 도구: 기능 service 공개 함수로 프로젝트 사실(ProjectFacts)을 읽어 에이전트에 넘긴다.

- 에이전트(app.agent)는 기능 service를 import하지 않는다. 여기(chat)가 읽어서 넘긴다.
- 숫자 · 날짜는 계산 엔진이 이미 만든 값만 옮긴다(표준견적서 · 공정표 · 실측 초안). 여기서 계산하지 않는다.
- 남의 프로젝트는 기능 service가 404로 막는다(project.service.get_for). 동 · 호 · 연락처는 옮기지 않는다.
- 부르는 함수는 B1a 계약(docs/v3/contract.md 5장)만: project.get_for · quote.standard_for · schedule.plan_for ·
  schedule.overview_for · visit.vendors_for · visit.draft_for · visit.pick_view(날짜 고르기 화면, context.ids의 실측 id).
  1단계 함수가 아직 없거나 그 칸이 비면 그 사실을 비운다
  (답은 '계산 없음' 정해 둔 글로). 업체끼리 줄 차이는 계산 엔진 함수가 생기기 전까지 넘기지 않는다(여기서 빼기 계산 안 함).
"""

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.v3.schemas import (
    EstimateFacts,
    PlanBlockFact,
    PlanFacts,
    ProjectFacts,
    PublicVendorFact,
    StandardFacts,
    StandardLineFact,
    VendorAvailability,
    VendorRangeFact,
    VisitDraftFacts,
    VisitRecommendFact,
)
from app.agent.v3.templates import md
from app.auth.models import User
from app.shared.clock import now_utc
from app.shared.errors import AppError
from app.shared.regions import is_region

STATES = {"included", "bundled", "separate", "optional", "after_visit", "excluded"}
TRADES = {"dobae", "mokgong"}
# overview_for phase → 에이전트 단계(다음 할 일 버튼)
PHASE = {"estimate": "standard", "progress": "visit", "work": "construction", "done": "done"}


async def public_vendor_mentions(
    s: AsyncSession, text: str, screen: str | None = None, ids: Sequence[str] = ()
) -> list[PublicVendorFact]:
    """사용자가 명시했거나 업체 상세 화면에서 보고 있는 공개 도배 업체. 공사·지역 제한 없음."""
    from app.vendor import service as vendor_service

    profiles = await vendor_service.public_dobae_mentions(s, text)
    vendor_question = any(
        w in text for w in ("업체", "어때", "소개", "정보", "상세", "이곳", "여기")
    )
    if not profiles and screen == "vendor" and vendor_question:
        for raw in ids[:5]:
            try:
                vendor_id = uuid.UUID(str(raw))
            except ValueError:
                continue
            try:
                profiles = [await vendor_service.public_dobae_detail(s, vendor_id)]
            except AppError as exc:
                if exc.status != 404:
                    raise
            if profiles:
                break
    return [
        PublicVendorFact(
            vendor_id=p.vendor_id,
            name=p.biz_name or "업체",
            areas=[a.label for a in p.areas],
            intro=p.intro,
        )
        for p in profiles
    ]


async def project_facts(
    s: AsyncSession, user: User, project_id: uuid.UUID, today: date, ids: Sequence[str] = ()
) -> ProjectFacts:
    """프로젝트 사실. 남의 것 · 없는 것은 404(AppError)를 그대로 올린다.
    ids(보던 실측 id)가 있으면 업체가 답한 기간 · 추천 반나절도 옮긴다(날짜 고르기 화면)."""
    from app.project import service as project_service

    p = await project_service.get_for(s, user, project_id)
    trades = [t for t in ("dobae", "mokgong") if getattr(p.trades, t, None) is not None]
    dobae = getattr(p.trades, "dobae", None)
    facts = ProjectFacts(
        stage="visit" if getattr(p, "locked", False) else "standard",
        area_m2=float(p.size.m2) if p.size.m2 is not None else None,
        area_basis=p.size.basis,
        trades=trades,  # type: ignore[arg-type]
        grade=getattr(dobae, "grade", None),
        living="living" if getattr(p, "occupied", False) else "vacant",
        start_wish=p.when.start if p.when else None,
    )
    if p.home and p.home.faces == ["wall"]:
        facts.brief["dobae.ceiling"] = "벽만 해요"
    elif p.home and "ceiling" in p.home.faces:
        facts.brief["dobae.ceiling"] = "천장도 해요"
    facts.standard = await _standard(s, user, project_id)
    facts.plan = await _plan(s, user, project_id, today)
    facts.visit_draft = await _visit_draft(s, user, project_id, today)
    picked = await _visit_pick(s, user, project_id, ids)
    if picked is not None:
        d = facts.visit_draft or VisitDraftFacts()
        d.ranges, d.recommend = picked[0], picked[1]
        d.basis_start = d.basis_start or picked[2]
        facts.visit_draft = d
    if not is_region(p.sido, p.sgg):
        facts.dobae_vendors = "missing_region"
    elif "dobae" in trades:
        facts.dobae_vendors, facts.estimates = await _dobae_vendors(s, user, project_id)
    stage = await _stage(s, user, project_id, today)
    if stage:
        facts.stage = stage  # type: ignore[assignment]
    return facts


async def _stage(s: AsyncSession, user: User, project_id: uuid.UUID, today: date) -> str | None:
    from app.schedule import service as schedule_service

    try:
        o = await schedule_service.overview_for(s, user, project_id, today)
    except AppError:
        return None
    return PHASE.get(getattr(o, "phase", ""))


async def _dobae_vendors(
    s: AsyncSession, user: User, project_id: uuid.UUID
) -> tuple[VendorAvailability, list[EstimateFacts]]:
    """업체 보기와 같은 목록에서 후보 유무와 견적 설명용 가견적을 한 번에 읽는다."""
    from app.visit import service as visit_service

    try:
        lst = await visit_service.vendors_for(s, user, project_id, "dobae")
    except AppError:
        return "unavailable", []
    availability: VendorAvailability = "local" if lst.local else "nearby" if lst.nearby else "empty"
    cards = [*lst.local, *lst.nearby]
    picked = [c for c in cards if c.shortlisted] or cards[:3]
    return availability, [
        EstimateFacts(
            vendor=c.name,
            total=c.estimate_total,
            vs_standard=None if c.vs_standard_pct is None else int(c.vs_standard_pct),
        )
        for c in picked[:3]
        if c.estimate_total is not None
    ]


async def _standard(s: AsyncSession, user: User, project_id: uuid.UUID) -> StandardFacts | None:
    from app.quote import service as quote_service

    try:
        st = await quote_service.standard_for(s, user, project_id)
    except AppError:
        return None
    lines = [
        StandardLineFact(
            key=ln.key,
            name=ln.name,
            state=ln.state,  # type: ignore[arg-type]
            qty=float(ln.qty) if ln.qty is not None else None,
            unit=ln.unit,
            unit_price=ln.unit_price,
            amount=ln.amount,
            trade=t.trade,  # type: ignore[arg-type]
        )
        for t in st.trades
        if t.trade in TRADES
        for ln in t.lines
        if ln.state in STATES
    ]
    return StandardFacts(total=st.total, supply=st.supply, vat=st.vat, lines=lines)


async def _plan(
    s: AsyncSession, user: User, project_id: uuid.UUID, today: date
) -> PlanFacts | None:
    from app.schedule import service as schedule_service

    try:
        p = await schedule_service.plan_for(s, user, project_id, today)
    except AppError:
        return None
    blocks = [
        PlanBlockFact(trade=b.trade, ko=b.label, start=b.start, end=b.end)  # type: ignore[arg-type]
        for b in p.blocks
        if b.trade in TRADES
    ]
    kind = "final" if p.kind == "final" else "expected"
    return PlanFacts(kind=kind, start=p.start, end=p.end, work_days=p.work_days, blocks=blocks)


async def _visit_draft(
    s: AsyncSession, user: User, project_id: uuid.UUID, today: date
) -> VisitDraftFacts | None:
    """B1a `visit.service.draft_for`(GET /projects/{id}/visits/draft와 같은 값, 1단계)가 생기면 그대로 옮긴다.

    산식 글은 계산 엔진 상수(calc.slots)로 적는다(날짜를 새로 계산하지 않음)."""
    from app.calc import slots
    from app.visit import service as visit_service

    fn = getattr(visit_service, "draft_for", None)
    if fn is None:
        return None
    try:
        d = await fn(s, user, project_id, "dobae", today)
    except AppError:
        return None
    vendors = [getattr(v, "name", "") for v in getattr(d, "vendors", []) or []]
    return VisitDraftFacts(
        wish_start=getattr(d, "wish_start", None),
        wish_end=getattr(d, "wish_end", None),
        basis_start=getattr(d, "basis_start", None),
        formula=f"공사 시작 희망일 − {slots.DRAFT_FROM_DAYS}일 ~ − {slots.DRAFT_TO_DAYS}일",
        vendors=[v for v in vendors if v],
    )


def pick_facts(pick) -> tuple[list[VendorRangeFact], VisitRecommendFact, date | None] | None:
    """날짜 고르기 화면 값(visit.service.pick_view) → 초안 재료. 기간 · 추천일 · 기준 날은 계산 엔진 값 그대로
    옮기고, 산식 글은 그 값과 계산 엔진 상수(calc.slots)로 적기만 한다."""
    from app.calc import slots

    w, rec = getattr(pick, "window", None), getattr(pick, "recommend", None)
    name = getattr(pick, "vendor_name", "") or ""
    if w is None or rec is None or not name:
        return None
    wish = getattr(pick, "work_wish_on", None)
    if rec.basis == "start_minus_20" and wish is not None:
        basis = f"{md(wish)} − {slots.RECOMMEND_BEFORE_DAYS}일 = {md(rec.target)}"
    else:
        basis = "업체가 된다고 한 날 중 가장 이른 반나절"
    return (
        [VendorRangeFact(vendor=name, start=w.start, end=w.end)],
        VisitRecommendFact(vendor=name, day=rec.day, half=rec.half, basis=basis),
        wish,
    )


async def _visit_pick(
    s: AsyncSession, user: User, project_id: uuid.UUID, ids: Sequence[str]
) -> tuple[list[VendorRangeFact], VisitRecommendFact, date | None] | None:
    """보던 실측 하나(앞에서부터)의 업체 기간 · 추천. 실측 id가 아니거나 남의 것이면 건너뛴다(404는 올리지 않음)."""
    from app.visit import service as visit_service

    for raw in ids:
        try:
            vid = uuid.UUID(str(raw))
        except ValueError:
            continue
        try:
            pick = await visit_service.pick_view(s, user, project_id, vid, now_utc())
        except AppError:
            continue
        got = pick_facts(pick)
        if got is not None:
            return got
    return None
