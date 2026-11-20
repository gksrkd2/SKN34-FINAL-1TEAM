import uuid
from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.files.models import File
from app.vendor.models import (
    SUBMIT_ACTIONS,
    Vendor,
    VendorArea,
    VendorFaq,
    VendorLink,
    VendorPortfolio,
    VendorPriceFile,
    VendorPriceLine,
    VendorPriceSet,
    VendorReview,
    VendorReviewLink,
    VendorTrade,
)


async def review_links_of(
    s: AsyncSession, vendor_ids: list[uuid.UUID], statuses: tuple[str, ...] | None = None
) -> dict[uuid.UUID, list[VendorReviewLink]]:
    """업체마다 외부 리뷰 링크 (번호 순). statuses를 주면 그 상태만 (고객 화면은 shown만)."""
    out: dict[uuid.UUID, list[VendorReviewLink]] = {i: [] for i in vendor_ids}
    if not vendor_ids:
        return out
    q = select(VendorReviewLink).where(VendorReviewLink.vendor_id.in_(vendor_ids))
    if statuses is not None:
        q = q.where(VendorReviewLink.status.in_(statuses))
    q = q.order_by(VendorReviewLink.vendor_id, VendorReviewLink.sort)
    for row in (await s.execute(q.execution_options(populate_existing=True))).scalars():
        out[row.vendor_id].append(row)
    return out


async def review_of(
    s: AsyncSession, user_id: uuid.UUID
) -> tuple[str, str | None, str | None] | None:
    """(심사 상태, 사유 코드, 업체에 보낼 말)"""
    row = (
        await s.execute(
            select(Vendor.status, Vendor.review_code, Vendor.review_note).where(
                Vendor.user_id == user_id
            )
        )
    ).first()
    return (row[0], row[1], row[2]) if row else None


async def list_with_users(
    s: AsyncSession, statuses: tuple[str, ...] | None, q: str | None
) -> list[tuple[Vendor, User]]:
    """상태 · 상호 · 사업자번호로 거른 업체 (정렬은 service가 마지막으로 낸 때로 한다)"""
    query = select(Vendor, User).join(User, User.id == Vendor.user_id)
    if statuses:
        query = query.where(Vendor.status.in_(statuses))
    if q:
        cond = Vendor.biz_name.icontains(q, autoescape=True)
        digits = "".join(c for c in q if c.isdigit())
        if digits:
            cond = cond | Vendor.biz_no.contains(digits)
        query = query.where(cond)
    return [(v, u) for v, u in (await s.execute(query)).all()]


async def status_counts(s: AsyncSession) -> dict[str, int]:
    rows = await s.execute(select(Vendor.status, func.count()).group_by(Vendor.status))
    return {st: n for st, n in rows.all()}


async def biz_no_taken(s: AsyncSession, biz_no: str) -> bool:
    return (await s.scalar(select(Vendor.user_id).where(Vendor.biz_no == biz_no))) is not None


async def get_with_user(s: AsyncSession, vendor_id: uuid.UUID) -> tuple[Vendor, User] | None:
    row = (
        await s.execute(
            select(Vendor, User)
            .join(User, User.id == Vendor.user_id)
            .where(Vendor.user_id == vendor_id)
            .execution_options(populate_existing=True)
        )
    ).first()
    return (row[0], row[1]) if row else None


async def trades_and_areas(s: AsyncSession, ids: list[uuid.UUID]) -> tuple[dict, dict]:
    trades: dict[uuid.UUID, list[str]] = {i: [] for i in ids}
    areas: dict[uuid.UUID, list[tuple[str, str]]] = {i: [] for i in ids}
    for vid, t in (
        await s.execute(
            select(VendorTrade.vendor_id, VendorTrade.trade).where(VendorTrade.vendor_id.in_(ids))
        )
    ).all():
        trades[vid].append(t)
    q = select(VendorArea.vendor_id, VendorArea.sido, VendorArea.sgg).where(
        VendorArea.vendor_id.in_(ids)
    )
    for vid, sido, sgg in (await s.execute(q)).all():
        areas[vid].append((sido, sgg))
    return trades, areas


async def set_status(
    s: AsyncSession, vendor_id: uuid.UUID, *, allowed_from: tuple[str, ...], values: dict
) -> bool:
    """지금 상태가 allowed_from일 때만 바꾼다 (동시에 두 관리자가 눌러도 한 번만 바뀜)."""
    res = await s.execute(
        update(Vendor)
        .where(Vendor.user_id == vendor_id, Vendor.status.in_(allowed_from))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1


async def replace_trades_areas(
    s: AsyncSession, vendor_id: uuid.UUID, trades: list[str], areas: list[tuple[str, str]]
) -> None:
    for model in (VendorTrade, VendorArea):
        await s.execute(delete(model).where(model.vendor_id == vendor_id))
    s.add_all([VendorTrade(vendor_id=vendor_id, trade=t) for t in trades])
    s.add_all([VendorArea(vendor_id=vendor_id, sido=a, sgg=b) for a, b in areas])


async def last_submits(s: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, VendorReview]:
    """업체마다 마지막으로 낸 이력 (가입 · 보완해서 다시 보냄 · 다시 신청)"""
    q = (
        select(VendorReview)
        .where(VendorReview.vendor_id.in_(ids), VendorReview.action.in_(SUBMIT_ACTIONS))
        .order_by(VendorReview.vendor_id, VendorReview.at.desc(), VendorReview.id.desc())
        .ext(distinct_on(VendorReview.vendor_id))
    )
    return {r.vendor_id: r for r in (await s.execute(q)).scalars()}


async def reviews_of(
    s: AsyncSession, vendor_id: uuid.UUID
) -> list[tuple[VendorReview, str | None]]:
    """처리 기록 (최근 순)과 한 사람 이름"""
    q = (
        select(VendorReview, User.name)
        .outerjoin(User, User.id == VendorReview.by_id)
        .where(VendorReview.vendor_id == vendor_id)
        .order_by(VendorReview.at.desc(), VendorReview.id.desc())
    )
    return [(r, name) for r, name in (await s.execute(q)).all()]


async def portfolio_file_id(
    s: AsyncSession, vendor_id: uuid.UUID, file_id: uuid.UUID
) -> uuid.UUID | None:
    q = select(VendorPortfolio.file_id).where(
        VendorPortfolio.vendor_id == vendor_id, VendorPortfolio.file_id == file_id
    )
    return await s.scalar(q)


async def files_by_id(s: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, File]:
    """사업자등록증 파일 정보 (파일 표는 읽기 조인에만 쓴다)"""
    return {f.id: f for f in (await s.execute(select(File).where(File.id.in_(ids)))).scalars()}


async def save_if_unchanged(
    s: AsyncSession, vendor_id: uuid.UUID, seen: datetime | None, values: dict
) -> bool:
    """화면이 읽은 뒤로 아무도 저장하지 않았을 때만 바꾼다 (행을 잠가 동시 저장은 한쪽만 된다)."""
    res = await s.execute(
        update(Vendor)
        .where(Vendor.user_id == vendor_id, Vendor.updated_at.is_not_distinct_from(seen))
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1


async def replace_parts(
    s: AsyncSession,
    vendor_id: uuid.UUID,
    trades: list[str],
    areas: list[tuple[str, str]],
    faqs: list[tuple[str, str]],
    links: list[tuple[str | None, str]],
) -> None:
    await replace_trades_areas(s, vendor_id, trades, areas)
    for model in (VendorFaq, VendorLink):
        await s.execute(delete(model).where(model.vendor_id == vendor_id))
    s.add_all([VendorFaq(vendor_id=vendor_id, key=k, answer=v) for k, v in faqs])
    s.add_all(
        [
            VendorLink(vendor_id=vendor_id, sort=i, label=lb, url=u)
            for i, (lb, u) in enumerate(links)
        ]
    )


async def faqs_links(
    s: AsyncSession, vendor_id: uuid.UUID
) -> tuple[dict[str, str], list[VendorLink]]:
    q = select(VendorFaq.key, VendorFaq.answer).where(VendorFaq.vendor_id == vendor_id)
    faqs = {k: a for k, a in (await s.execute(q)).all()}
    q = select(VendorLink).where(VendorLink.vendor_id == vendor_id).order_by(VendorLink.sort)
    return faqs, list((await s.execute(q)).scalars().all())


async def portfolio_of(s: AsyncSession, vendor_id: uuid.UUID) -> list[VendorPortfolio]:
    q = (
        select(VendorPortfolio)
        .where(VendorPortfolio.vendor_id == vendor_id)
        .order_by(VendorPortfolio.sort, VendorPortfolio.id)
    )
    return list((await s.execute(q)).scalars().all())


async def lock_vendor(s: AsyncSession, vendor_id: uuid.UUID) -> None:
    """같은 업체의 사진 더하기가 줄 서게 업체 행을 잠근다 (장수 한도를 동시에 넘지 않게)."""
    await s.execute(select(Vendor.user_id).where(Vendor.user_id == vendor_id).with_for_update())


async def portfolio_count_and_next(s: AsyncSession, vendor_id: uuid.UUID) -> tuple[int, int]:
    n, top = (
        await s.execute(
            select(func.count(), func.max(VendorPortfolio.sort)).where(
                VendorPortfolio.vendor_id == vendor_id
            )
        )
    ).one()
    return n, (top + 1 if top is not None else 0)


async def vendors_by_id(s: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, Vendor]:
    rows = (await s.execute(select(Vendor).where(Vendor.user_id.in_(ids)))).scalars()
    return {v.user_id: v for v in rows}


async def approved_ids(
    s: AsyncSession,
    trade: str | None,
    sido: str | None,
    sgg: str | None,
    vendor_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    """승인됐고 연락 방식을 정한 업체. 공정 · 지역(시 · 도 + 시 · 군 · 구) · 업체 한 곳으로 좁힌다."""
    q = select(Vendor.user_id).where(
        Vendor.status == "approved", Vendor.contact_method.is_not(None)
    )
    if vendor_id is not None:
        q = q.where(Vendor.user_id == vendor_id)
    if trade:
        q = q.where(
            select(VendorTrade.vendor_id)
            .where(VendorTrade.vendor_id == Vendor.user_id, VendorTrade.trade == trade)
            .exists()
        )
    if sido:
        cond = [VendorArea.vendor_id == Vendor.user_id, VendorArea.sido == sido]
        if sgg:
            cond.append(VendorArea.sgg == sgg)
        q = q.where(select(VendorArea.vendor_id).where(*cond).exists())
    return list((await s.execute(q.order_by(Vendor.reviewed_at, Vendor.user_id))).scalars().all())


async def named_public_dobae_ids(s: AsyncSession, text: str) -> list[uuid.UUID]:
    """질문에 상호가 나온 공개 도배 업체만 찾는다. 프로젝트·지역 제한은 없다."""
    q = (
        select(Vendor.user_id)
        .where(
            Vendor.status == "approved",
            Vendor.suspended_at.is_(None),
            Vendor.contact_method.is_not(None),
            Vendor.biz_name.is_not(None),
            func.length(Vendor.biz_name) >= 2,
            func.strpos(text, Vendor.biz_name) > 0,
            select(VendorTrade.vendor_id)
            .where(VendorTrade.vendor_id == Vendor.user_id, VendorTrade.trade == "dobae")
            .exists(),
        )
        .order_by(func.length(Vendor.biz_name).desc(), Vendor.user_id)
        .limit(5)
    )
    return list((await s.execute(q)).scalars().all())


async def price_sets(
    s: AsyncSession, vendor_ids: list[uuid.UUID], trade: str, statuses: tuple[str, ...]
) -> list[VendorPriceSet]:
    q = select(VendorPriceSet).where(
        VendorPriceSet.vendor_id.in_(vendor_ids),
        VendorPriceSet.trade == trade,
        VendorPriceSet.status.in_(statuses),
    )
    # 같은 요청 안에서 상태를 바꾼 뒤 다시 읽을 때 예전 값이 남지 않게
    q = q.execution_options(populate_existing=True)
    return list((await s.execute(q)).scalars().all())


async def price_lines(s: AsyncSession, set_ids: list[uuid.UUID]) -> dict[uuid.UUID, list]:
    out: dict[uuid.UUID, list[VendorPriceLine]] = {i: [] for i in set_ids}
    q = select(VendorPriceLine).where(VendorPriceLine.set_id.in_(set_ids))
    for ln in (await s.execute(q)).scalars():
        out[ln.set_id].append(ln)
    return out


async def next_price_version(s: AsyncSession, vendor_id: uuid.UUID, trade: str) -> int:
    top = await s.scalar(
        select(func.max(VendorPriceSet.version)).where(
            VendorPriceSet.vendor_id == vendor_id, VendorPriceSet.trade == trade
        )
    )
    return (top or 0) + 1


async def replace_price_lines(
    s: AsyncSession, set_id: uuid.UUID, lines: list[VendorPriceLine]
) -> None:
    await s.execute(delete(VendorPriceLine).where(VendorPriceLine.set_id == set_id))
    s.add_all(lines)


async def set_price_status(
    s: AsyncSession,
    vendor_id: uuid.UUID,
    trade: str,
    *,
    to: str,
    allowed_from: str,
    at: datetime | None = None,
    set_id: uuid.UUID | None = None,
) -> int:
    """지금 상태가 allowed_from인 묶음만 바꾼다. 바뀐 행 수를 돌려준다."""
    q = update(VendorPriceSet).where(
        VendorPriceSet.vendor_id == vendor_id,
        VendorPriceSet.trade == trade,
        VendorPriceSet.status == allowed_from,
    )
    if set_id is not None:
        q = q.where(VendorPriceSet.id == set_id)
    values = {"status": to} | ({"confirmed_at": at} if at is not None else {})
    res = await s.execute(q.values(**values).execution_options(synchronize_session=False))
    return res.rowcount


async def price_files(
    s: AsyncSession, vendor_ids: list[uuid.UUID], trade: str | None = None
) -> list[tuple[VendorPriceFile, File]]:
    """근거 파일과 파일 정보(이름 · 형식 · 크기). 파일 표는 읽기 조인에만 쓴다."""
    q = (
        select(VendorPriceFile, File)
        .join(File, File.id == VendorPriceFile.file_id)
        .where(VendorPriceFile.vendor_id.in_(vendor_ids))
        .order_by(VendorPriceFile.created_at, VendorPriceFile.file_id)
    )
    if trade:
        q = q.where(VendorPriceFile.trade == trade)
    return [(pf, f) for pf, f in (await s.execute(q)).all()]


# ---- 관리자 업체 멈춤 · 외부 리뷰 링크 확인 (D26 · A01). 조건부 UPDATE만, commit은 부르는 쪽 ----


async def suspend(
    s: AsyncSession, vendor_id: uuid.UUID, scope: str, at: datetime
) -> tuple[datetime, str] | None:
    """멈추지 않은 업체 → scope, 새 요청만(new) → 진행 중도(all)로 넓히기. 그 밖(이미 같거나 더 넓음)은 None"""
    allowed = Vendor.suspend_scope.is_(None)
    if scope == "all":
        allowed = or_(allowed, Vendor.suspend_scope == "new")
    q = (
        update(Vendor)
        .where(Vendor.user_id == vendor_id, allowed)
        .values(suspended_at=func.coalesce(Vendor.suspended_at, at), suspend_scope=scope)
        .returning(Vendor.suspended_at, Vendor.suspend_scope)
        .execution_options(synchronize_session=False)
    )
    row = (await s.execute(q)).first()
    return (row[0], row[1]) if row else None


async def unsuspend(s: AsyncSession, vendor_id: uuid.UUID) -> bool:
    q = (
        update(Vendor)
        .where(Vendor.user_id == vendor_id, Vendor.suspended_at.is_not(None))
        .values(suspended_at=None, suspend_scope=None)
        .returning(Vendor.user_id)
        .execution_options(synchronize_session=False)
    )
    return (await s.execute(q)).scalar_one_or_none() is not None


async def set_review_link_status(
    s: AsyncSession,
    vendor_id: uuid.UUID,
    sort: int,
    status: str,
    *,
    by: uuid.UUID,
    at: datetime,
    note: str | None,
    url: str | None,
) -> VendorReviewLink | None:
    """그 상태가 아닌 링크만 바꾼다(url을 주면 그 주소일 때만). 바뀐 행, 아니면 None"""
    q = update(VendorReviewLink).where(
        VendorReviewLink.vendor_id == vendor_id,
        VendorReviewLink.sort == sort,
        VendorReviewLink.status != status,
    )
    if url is not None:
        q = q.where(VendorReviewLink.url == url)
    values: dict = {"status": status, "checked_by": by, "checked_at": at}
    if note is not None:
        values["note"] = note
    q = q.values(**values).returning(VendorReviewLink)
    row = await s.scalar(q.execution_options(populate_existing=True))
    return row
