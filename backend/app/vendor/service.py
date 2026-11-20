"""업체 가입 신청 · 관리자 심사(승인 · 보완 요청 · 거절) · 다시 내기 · 업체 정보.

가입 신청서와 사업자등록증을 관리자가 맞춰 보고 정한다. 승인돼야 업체 기능을 쓴다.
상태는 여기서만 바꾸고, 바꿀 때마다 같은 트랜잭션에 심사 이력(vendor_reviews)을 남긴다.
업체 정보(연락 방식 · 실측 비용 · 자주 묻는 5가지 · 링크 · 사진)는 승인된 업체 본인만 고친다.
고객에게는 public_profile · list_public로 보여 주고, 휴대폰 · 채널 주소는 넣지 않는다.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from fastapi import UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service as auth_service
from app.auth.models import User
from app.calc import dobae, estimate, mokgong
from app.calc.schedule import days_since
from app.calc.space_share import SPACE_SHARE_2026_11
from app.calc.standard_quote import house_area
from app.calc.visit_view import area_text
from app.files import service as files_service
from app.files.models import File
from app.notify import service as notify_service
from app.refprice.book import DOBAE_2026H2, MOKGONG_2026H2
from app.refprice.house import HOUSE_2026H2
from app.refprice.vendor_baseline import price_warning, vendor_base
from app.refprice.wallpaper_market import paper_market
from app.shared.clock import now_utc
from app.shared.errors import AppError, app_error
from app.shared.ids import uuid7
from app.shared.regions import is_region, region_label
from app.vendor import repository as repo
from app.vendor.models import (
    FAQ_KEYS,
    PORTFOLIO_MAX,
    PRICE_FILES_MAX,
    ROWS_VER_OF,
    Vendor,
    VendorPortfolio,
    VendorPriceFile,
    VendorPriceLine,
    VendorPriceSet,
    VendorReview,
    VendorReviewLink,
)
from app.vendor.schemas import (
    ApplicationOut,
    AreaIn,
    AreaOut,
    ConfirmPricesIn,
    ContactOut,
    DecisionOut,
    EstimateLineOut,
    EstimateOut,
    EstimateV3LineOut,
    EstimateV3Out,
    FaqIn,
    LicenseOut,
    LinkIn,
    OpenFieldOut,
    PortfolioIn,
    PortfolioMeta,
    PortfolioOut,
    PriceFileIn,
    PriceFileOut,
    PriceLineOut,
    PriceRowOut,
    PriceRowV3Out,
    PriceSetIn,
    PriceSetOut,
    PricesOut,
    PricesV3Out,
    ProfileIn,
    ProfileOut,
    PublicCardOut,
    PublicDobaeDetailOut,
    PublicProfileOut,
    ReapplyIn,
    ResubmitIn,
    ReviewIn,
    ReviewLinkAdminOut,
    ReviewLinkOut,
    ReviewLinksIn,
    ReviewOut,
    ReviewSummaryOut,
    VendorAdminDetailOut,
    VendorAdminOut,
    VendorSignupIn,
)

if TYPE_CHECKING:
    from app.project.schemas import ProjectOut

STALE = "다른 곳에서 먼저 바꿨어요. 다시 열어 주세요."
DONE = ("approved", "rejected")


BIZ_NO_TAKEN = "이미 가입 신청한 사업자등록번호예요."
TAKEN_DECISION = "다른 관리자가 먼저 이 업체를 정했어요. 새로 열어 주세요."
# 보완 요청 사유마다 업체에게 열리는 칸과 필수 여부. 사업자등록번호는 열리지 않는다
OPEN_FIELDS: dict[str, dict[str, bool]] = {
    "blurry": {"license_file_id": True},
    "other": {"license_file_id": True},
    "name_mismatch": {"biz_name": True, "license_file_id": False},
    "owner_mismatch": {"owner_name": True, "license_file_id": False},
}
LIST_STATUSES = {"pending": ("pending",), "needs_info": ("needs_info",), "done": DONE}
DECIDED = {"approve": "approved", "needs_info": "needs_info", "reject": "rejected"}
DECISIONS = tuple(DECIDED)
NOTICE = {
    "approve": "가입 심사가 끝났어요. 이제 고객 요청을 받을 수 있어요.",
    "needs_info": "관리자가 보완을 요청했어요.",
    "reject": "관리자가 이번 신청을 거절했어요.",
}


def _areas_out(pairs: list[tuple[str, str]]) -> list[AreaOut]:
    return [AreaOut(sido=a, sgg=b, label=region_label(a, b)) for a, b in sorted(pairs)]


def _check_areas(areas: list[AreaIn]) -> list[tuple[str, str]]:
    if not all(is_region(a.sido, a.sgg) for a in areas):
        raise AppError(422, "region", "활동 지역을 다시 골라 주세요.")
    return list(dict.fromkeys((a.sido, a.sgg) for a in areas))


def _license(f: File | None) -> LicenseOut | None:
    if f is None:
        return None
    return LicenseOut(
        id=f.id,
        filename=f.filename,
        content_type=f.content_type,
        size=f.size,
        created_at=f.created_at,
    )


async def signup(
    s: AsyncSession, data: VendorSignupIn, license_file: UploadFile, ip: str
) -> tuple[User, str]:
    """가입 신청: 회원 · 업체 행 · 공정 · 지역 · 사업자등록증 · 신청 이력을 한 트랜잭션에 남긴다."""
    await auth_service.signup_limit(ip)
    areas = _check_areas(data.areas)
    if await repo.biz_no_taken(s, data.biz_no):
        raise AppError(409, "biz_no_taken", BIZ_NO_TAKEN)
    lic = await files_service.prepare(
        "license", license_file
    )  # 검사 · 위치 정보 지우기는 DB 밖에서
    user = await auth_service.create_user(
        s,
        email=data.email,
        name=data.name or data.owner_name,
        password=data.password,
        role="vendor",
        phone=data.phone,
    )
    row = await files_service.persist(s, user.id, lic)
    try:
        s.add(
            Vendor(
                user_id=user.id,
                license_file_id=row.id,
                biz_name=data.biz_name,
                owner_name=data.owner_name,
                biz_no=data.biz_no,
                phone=data.phone,
            )
        )
        await s.flush()
        await repo.replace_trades_areas(s, user.id, sorted(set(data.trades)), areas)
        s.add(VendorReview(vendor_id=user.id, action="apply", by_id=user.id, at=now_utc()))
        token = await auth_service.start_session(s, user)
        await s.commit()
    except IntegrityError as exc:  # 같은 사업자등록번호로 동시에 신청하면 UNIQUE가 막는다
        files_service.discard(row)
        await s.rollback()
        raise AppError(409, "biz_no_taken", BIZ_NO_TAKEN) from exc
    except Exception:
        files_service.discard(row)  # 가입이 안 됐으면 사업자등록증도 남기지 않는다
        raise
    return user, token


async def _admin_out(
    s: AsyncSession, pairs: list[tuple[Vendor, User]], today: date
) -> list[VendorAdminOut]:
    ids = [v.user_id for v, _ in pairs]
    trades, areas = await repo.trades_and_areas(s, ids)
    subs = await repo.last_submits(s, ids)
    files = await repo.files_by_id(s, [v.license_file_id for v, _ in pairs])
    out = []
    for v, u in pairs:
        sub = subs.get(v.user_id)
        again = sub is not None and sub.action != "apply"
        out.append(
            VendorAdminOut(
                user_id=v.user_id,
                name=u.name,
                email=u.email,
                status=v.status,
                review_code=v.review_code,
                review_note=v.review_note,
                created_at=v.created_at,
                reviewed_at=v.reviewed_at,
                approved_at=v.approved_at,
                opened_on=v.opened_on,
                submitted_at=sub.at if sub else None,
                waiting_days=days_since(sub.at, today) if sub else None,
                resubmitted=again,
                changes=sub.changes if again else None,
                biz_name=v.biz_name,
                owner_name=v.owner_name,
                biz_no=v.biz_no,
                phone=v.phone,
                address=v.address,
                site_url=v.site_url,
                trades=sorted(trades[v.user_id]),
                areas=_areas_out(areas[v.user_id]),
                intro=v.intro,
                license_file_id=v.license_file_id,
                license=_license(files.get(v.license_file_id)),
            )
        )
    return out


async def list_for_admin(
    s: AsyncSession, status: str | None, q: str | None, today: date
) -> list[VendorAdminOut]:
    """심사 목록: 기다림 · 보완 요청은 오래 기다린 순(마지막으로 낸 때), 처리함은 최근에 정한 순."""
    pairs = await repo.list_with_users(s, LIST_STATUSES.get(status or ""), (q or "").strip())
    out = await _admin_out(s, pairs, today)
    if status == "done":
        out.sort(key=lambda x: x.reviewed_at or x.created_at, reverse=True)
    else:
        out.sort(key=lambda x: x.submitted_at or x.created_at)
    return out


async def review_summary(s: AsyncSession) -> ReviewSummaryOut:
    n = await repo.status_counts(s)
    return ReviewSummaryOut(
        pending=n.get("pending", 0),
        needs_info=n.get("needs_info", 0),
        done=sum(n.get(st, 0) for st in DONE),
    )


async def get_for_admin(s: AsyncSession, vendor_id: uuid.UUID, today: date) -> VendorAdminDetailOut:
    pair = await repo.get_with_user(s, vendor_id)
    if pair is None:
        raise AppError(404, "not_found", "업체를 찾을 수 없어요.")
    base = (await _admin_out(s, [pair], today))[0]
    reviews = [
        ReviewOut(
            id=r.id,
            action=r.action,
            code=r.code,
            note=r.note,
            changes=r.changes,
            by_id=r.by_id,
            by_name=name,
            at=r.at,
        )
        for r, name in await repo.reviews_of(s, vendor_id)
    ]
    links = [
        ReviewLinkAdminOut(
            sort=r.sort,
            label=r.label,
            url=r.url,
            status=r.status,
            note=r.note,
            checked_at=r.checked_at,
        )
        for r in (await repo.review_links_of(s, [vendor_id]))[vendor_id]
    ]
    v = pair[0]
    return VendorAdminDetailOut(
        **base.model_dump(),
        reviews=reviews,
        review_links=links,
        suspended_at=v.suspended_at,
        suspend_scope=v.suspend_scope,
    )


async def review(
    s: AsyncSession, admin: User, vendor_id: uuid.UUID, data: ReviewIn, today: date
) -> VendorAdminDetailOut:
    """관리자 결정 저장 한 곳: 심사 중(pending)일 때만 바꾸고, 같은 트랜잭션에 이력 · 업체 알림을 남긴다."""
    if data.opened_on is not None and data.opened_on > today:
        raise AppError(422, "opened_on", "개업일은 오늘까지만 적을 수 있어요.")
    now = now_utc()
    values = {
        "status": DECIDED[data.action],
        "review_code": data.code,
        "review_note": data.note,
        "reviewed_by": admin.id,
        "reviewed_at": now,
        "approved_at": now if data.action == "approve" else None,
    }
    if data.opened_on is not None:
        values["opened_on"] = data.opened_on
    if not await repo.set_status(s, vendor_id, allowed_from=("pending",), values=values):
        if await repo.get_with_user(s, vendor_id) is None:
            raise AppError(404, "not_found", "업체를 찾을 수 없어요.")
        raise AppError(409, "invalid_state", TAKEN_DECISION)
    row = VendorReview(
        id=uuid7(),
        vendor_id=vendor_id,
        action=data.action,
        code=data.code,
        note=data.note,
        by_id=admin.id,
        at=now,
    )
    s.add(row)
    await notify_service.add(
        s,
        user_id=vendor_id,
        kind=f"vendor_{data.action}",
        body=NOTICE[data.action],
        link="/vendor",
        dedup_key=f"vendor_review:{row.id}",
    )
    await s.commit()
    return await get_for_admin(s, vendor_id, today)


async def application(s: AsyncSession, vendor: User) -> ApplicationOut:
    """업체 홈의 가입 심사 칸: 낸 내용 · 최근 결정 · 보완 요청이면 열리는 칸."""
    v = await s.get(Vendor, vendor.id, populate_existing=True)
    if v is None:
        raise AppError(404, "not_found", "업체 신청을 찾을 수 없어요.")
    trades, areas = await repo.trades_and_areas(s, [v.user_id])
    sub = (await repo.last_submits(s, [v.user_id])).get(v.user_id)
    lic = (await repo.files_by_id(s, [v.license_file_id])).get(v.license_file_id)
    last = next((r for r, _ in await repo.reviews_of(s, v.user_id) if r.action in DECISIONS), None)
    opened = OPEN_FIELDS.get(v.review_code or "", {}) if v.status == "needs_info" else {}
    return ApplicationOut(
        status=v.status,
        applied_at=sub.at if sub else None,
        approved_at=v.approved_at,
        biz_name=v.biz_name,
        owner_name=v.owner_name,
        biz_no=v.biz_no,
        phone=v.phone,
        trades=sorted(trades[v.user_id]),
        areas=_areas_out(areas[v.user_id]),
        license=_license(lic),
        decision=DecisionOut(action=last.action, code=last.code, note=last.note, at=last.at)
        if last
        else None,
        open_fields=[OpenFieldOut(field=k, required=req) for k, req in opened.items()],
    )


async def _locked(s: AsyncSession, vendor: User, status: str, message: str) -> Vendor:
    """업체 행을 잠그고(같은 업체의 다시 내기가 줄 선다) 지금 상태가 맞는지 본다."""
    await repo.lock_vendor(s, vendor.id)
    v = await s.get(Vendor, vendor.id, populate_existing=True)
    if v is None:
        raise AppError(404, "not_found", "업체 신청을 찾을 수 없어요.")
    if v.status != status:
        raise AppError(409, "invalid_state", message)
    return v


async def _new_license(
    s: AsyncSession, v: Vendor, file_id: uuid.UUID | None, changes: dict, values: dict
) -> uuid.UUID | None:
    """새 사업자등록증으로 바꿀 때: 내가 올린 license 파일인지 보고, 지울 예전 파일 id를 돌려준다."""
    if file_id is None:
        return None
    f = (await files_service.owned(s, v.user_id, [file_id])).get(file_id)
    if f is None or f.kind != "license" or file_id == v.license_file_id:
        raise AppError(422, "file", "새로 올린 사업자등록증을 다시 확인해 주세요.")
    old = (await repo.files_by_id(s, [v.license_file_id])).get(v.license_file_id)
    changes["file"] = old.filename if old else None
    values["license_file_id"] = file_id
    return v.license_file_id


async def _resubmitted(
    s: AsyncSession, v: Vendor, *, action: str, allowed_from: str, values: dict, changes: dict
) -> None:
    """심사 중으로 돌리고(조건부 UPDATE) 같은 트랜잭션에 이력을 남긴다."""
    values |= {"status": "pending", "review_code": None, "review_note": None}
    if not await repo.set_status(s, v.user_id, allowed_from=(allowed_from,), values=values):
        raise AppError(409, "invalid_state", "이미 다시 보낸 신청이에요.")
    s.add(
        VendorReview(
            vendor_id=v.user_id, action=action, by_id=v.user_id, changes=changes, at=now_utc()
        )
    )


async def _finish(s: AsyncSession, v: Vendor, old_file: uuid.UUID | None) -> None:
    key = await files_service.drop(s, v.user_id, old_file) if old_file else None
    await s.commit()
    if key:  # 예전 등록증 내용은 commit 뒤에 지운다
        await files_service.remove_stored(key)


async def resubmit(s: AsyncSession, vendor: User, data: ResubmitIn) -> ApplicationOut:
    """보완 요청 → 심사 중. 관리자가 고른 사유에 열린 칸만 받고, 바뀐 칸의 전 값을 이력에 남긴다."""
    v = await _locked(s, vendor, "needs_info", "보완 요청을 받은 신청만 다시 보낼 수 있어요.")
    opened = OPEN_FIELDS[v.review_code or "other"]
    given = data.model_dump(exclude_none=True)
    if any(k not in opened for k in given):
        raise AppError(422, "not_open", "이번에는 고칠 수 없는 칸이에요.")
    if any(req and k not in given for k, req in opened.items()):
        raise AppError(422, "required", "열린 칸을 채워 주세요.")
    changes: dict = {}
    values: dict = {}
    for k in ("biz_name", "owner_name"):
        if k in given and given[k] != getattr(v, k):
            changes[k], values[k] = getattr(v, k), given[k]
    old_file = await _new_license(s, v, data.license_file_id, changes, values)
    await _resubmitted(
        s, v, action="resubmit", allowed_from="needs_info", values=values, changes=changes
    )
    await _finish(s, v, old_file)
    return await application(s, vendor)


async def reapply(s: AsyncSession, vendor: User, data: ReapplyIn) -> ApplicationOut:
    """거절 → 심사 중. 같은 계정으로 신청서를 다시 내고, 바뀐 칸만 이력에 남긴다.
    사업자등록번호를 고치면(VS02) 가입처럼 다른 업체가 쓰는 번호인지 본다(409 biz_no_taken)."""
    v = await _locked(s, vendor, "rejected", "거절된 신청만 다시 신청할 수 있어요.")
    areas = _check_areas(data.areas)
    trades = sorted(set(data.trades))
    old_trades, old_areas = await repo.trades_and_areas(s, [v.user_id])
    changes: dict = {}
    values: dict = {}
    for k in ("biz_name", "owner_name", "phone"):
        if getattr(data, k) != getattr(v, k):
            changes[k], values[k] = getattr(v, k), getattr(data, k)
    if data.biz_no is not None and data.biz_no != v.biz_no:
        if await repo.biz_no_taken(s, data.biz_no):
            raise AppError(409, "biz_no_taken", BIZ_NO_TAKEN)
        changes["biz_no"], values["biz_no"] = v.biz_no, data.biz_no
    if trades != sorted(old_trades[v.user_id]):
        changes["trades"] = sorted(old_trades[v.user_id])
    if sorted(areas) != sorted(old_areas[v.user_id]):
        changes["areas"] = [{"sido": a, "sgg": b} for a, b in sorted(old_areas[v.user_id])]
    old_file = await _new_license(s, v, data.license_file_id, changes, values)
    try:
        await _resubmitted(
            s, v, action="reapply", allowed_from="rejected", values=values, changes=changes
        )
    except IntegrityError as exc:  # 같은 번호로 동시에 가입 · 다시 신청하면 UNIQUE가 막는다
        await s.rollback()
        raise AppError(409, "biz_no_taken", BIZ_NO_TAKEN) from exc
    await repo.replace_trades_areas(s, v.user_id, trades, areas)
    await _finish(s, v, old_file)
    return await application(s, vendor)


def _card(v: Vendor, trades: list[str], areas: list[tuple[str, str]]) -> dict:
    return {
        "vendor_id": v.user_id,
        "biz_name": v.biz_name,
        "trades": sorted(trades),
        "areas": [AreaOut(sido=a, sgg=b, label=region_label(a, b)) for a, b in sorted(areas)],
        "verified_at": v.approved_at,
        "contact_method": v.contact_method,
        "call_start": v.call_start,
        "call_end": v.call_end,
        "visit_fee": v.visit_fee,
        "visit_fee_refund": v.visit_fee_refund,
        "tax_kind": v.tax_kind,
    }


def _photo(p: VendorPortfolio) -> PortfolioOut:
    return PortfolioOut(
        id=p.id,
        file_id=p.file_id,
        title=p.title,
        space=p.space,
        pyeong=p.pyeong,
        taken_on=p.taken_on,
        area_m2=p.area_m2,
        paper=p.paper,
        area_basis=p.area_basis,
        area_text=area_text(p.area_m2, p.area_basis) if p.area_m2 is not None else None,
    )


async def _detail(s: AsyncSession, v: Vendor) -> dict:
    trades, areas = await repo.trades_and_areas(s, [v.user_id])
    faqs, links = await repo.faqs_links(s, v.user_id)
    photos = await repo.portfolio_of(s, v.user_id)
    return dict(
        **_card(v, trades[v.user_id], areas[v.user_id]),
        owner_name=v.owner_name,
        biz_no=v.biz_no,
        opened_on=v.opened_on,
        address_area=" ".join(v.address.split()[:2]) if v.address else None,
        interior_license=v.interior_license,
        insurance=v.insurance,
        faqs=[FaqIn(key=k, answer=faqs[k]) for k in FAQ_KEYS if k in faqs],
        links=[LinkIn(label=link.label, url=link.url) for link in links],
        portfolio=[_photo(p) for p in photos],
        updated_at=v.updated_at,
    )


async def get_profile(s: AsyncSession, vendor: User) -> ProfileOut:
    v = await s.get(Vendor, vendor.id, populate_existing=True)
    if v is None:
        raise AppError(404, "not_found", "업체를 찾을 수 없어요.")
    return ProfileOut(
        **await _detail(s, v),
        phone=v.phone,
        address=v.address,
        site_url=v.site_url,
        contact_url=v.contact_url,
        instant_default=v.instant_default,
        visit_capacity=v.visit_capacity,
        suspended_at=v.suspended_at,
        intro=v.intro or None,
    )


def _check_taken_on(items: list[PortfolioMeta], today: date) -> None:
    if any(p.taken_on is not None and p.taken_on > today for p in items):
        raise AppError(422, "taken_on", "시공한 때는 오늘까지만 고를 수 있어요.")


async def save_profile(s: AsyncSession, vendor: User, data: ProfileIn, today: date) -> ProfileOut:
    """통째로 저장. 화면이 읽은 updated_at과 지금 값이 다르면 409(다른 곳에서 먼저 저장)."""
    if not all(is_region(a.sido, a.sgg) for a in data.areas):
        raise AppError(422, "region", "활동 지역을 다시 골라 주세요.")
    if data.portfolio is not None:
        _check_taken_on(data.portfolio, today)
    url_needed = data.contact_method in ("kakao", "web")
    values = {
        "phone": data.phone,
        "address": data.address,
        "site_url": data.site_url,
        "contact_method": data.contact_method,
        "contact_url": data.contact_url if url_needed else None,  # 전화 · 문자는 휴대폰으로 연락
        "call_start": data.call_start,
        "call_end": data.call_end,
        "visit_fee": data.visit_fee,
        "visit_fee_refund": data.visit_fee_refund,
        "tax_kind": data.tax_kind,
        "interior_license": data.interior_license,
        "insurance": data.insurance,
        "updated_at": now_utc(),
    }
    # v3: 비우면 그대로 둔다 (옛 화면은 이 칸을 보내지 않는다)
    if data.instant_default is not None:
        values["instant_default"] = data.instant_default
    if data.visit_capacity is not None:
        values["visit_capacity"] = data.visit_capacity
    if data.intro is not None:  # 빈 글("")은 소개를 지운다
        values["intro"] = data.intro
    if not await repo.save_if_unchanged(s, vendor.id, data.updated_at, values):
        raise AppError(409, "stale", STALE)
    await repo.replace_parts(
        s,
        vendor.id,
        trades=sorted(set(data.trades)),
        areas=list(dict.fromkeys((a.sido, a.sgg) for a in data.areas)),
        faqs=[(f.key, f.answer) for f in data.faqs if f.answer],
        links=[(link.label, link.url) for link in data.links],
    )
    if data.portfolio is not None:
        rows = {p.id: p for p in await repo.portfolio_of(s, vendor.id)}
        if set(rows) != {e.id for e in data.portfolio}:
            raise AppError(409, "stale", STALE)
        for i, e in enumerate(data.portfolio):
            row = rows[e.id]
            row.title, row.space, row.pyeong, row.taken_on = e.title, e.space, e.pyeong, e.taken_on
            row.area_m2, row.paper = e.area_m2, e.paper
            # v3 (0034): 면적 기준은 보낸 때만 바꾼다(옛 화면은 안 보냄). 면적을 지우면 기준도 지운다
            if "area_basis" in e.model_fields_set:
                row.area_basis = e.area_basis
            if row.area_m2 is None:
                row.area_basis = None
            row.sort = i
    await s.commit()
    return await get_profile(s, vendor)


async def add_portfolio(
    s: AsyncSession, vendor: User, data: PortfolioIn, today: date
) -> PortfolioOut:
    _check_taken_on([data], today)
    f = (await files_service.owned(s, vendor.id, [data.file_id])).get(data.file_id)
    if f is None or f.kind != "portfolio":
        raise AppError(422, "file", "올린 시공 사진을 다시 확인해 주세요.")
    await repo.lock_vendor(s, vendor.id)
    count, sort = await repo.portfolio_count_and_next(s, vendor.id)
    if count >= PORTFOLIO_MAX:
        raise AppError(409, "portfolio_full", f"시공 사진은 {PORTFOLIO_MAX}장까지 올릴 수 있어요.")
    row = VendorPortfolio(
        vendor_id=vendor.id,
        file_id=data.file_id,
        title=data.title,
        space=data.space,
        pyeong=data.pyeong,
        taken_on=data.taken_on,
        area_m2=data.area_m2,
        paper=data.paper,
        area_basis=data.area_basis,
        sort=sort,
    )
    s.add(row)
    try:
        await s.commit()
    except IntegrityError as exc:  # 같은 사진을 동시에 두 번 더하면 file_id UNIQUE가 막는다
        await s.rollback()
        raise AppError(409, "file_used", "이미 더한 사진이에요.") from exc
    return _photo(row)


async def remove_portfolio(s: AsyncSession, vendor: User, item_id: uuid.UUID) -> None:
    """사진을 빼면 고객 화면에서도 사라진다. 파일 행 · 저장소의 내용도 지운다."""
    row = await s.get(VendorPortfolio, item_id)
    if row is None or row.vendor_id != vendor.id:
        raise AppError(404, "not_found", "사진을 찾을 수 없어요.")
    await s.delete(row)
    await s.flush()
    key = await files_service.drop(s, vendor.id, row.file_id)
    await s.commit()
    if key:
        await files_service.remove_stored(key)


async def public_profile(s: AsyncSession, vendor_id: uuid.UUID) -> PublicProfileOut:
    """고객이 보는 업체 자세히. 승인된 업체만, 휴대폰 · 채널 주소 · 상세 주소는 넣지 않는다."""
    v = await s.get(Vendor, vendor_id)
    if v is None or v.status != "approved":
        raise AppError(404, "not_found", "업체를 찾을 수 없어요.")
    return PublicProfileOut(**await _detail(s, v))


async def public_dobae_detail(s: AsyncSession, vendor_id: uuid.UUID) -> PublicDobaeDetailOut:
    """공사/지역과 독립된 도배 업체 공개 상세. 승인·활성·공종·연락 방식만 검사한다."""
    if not await repo.approved_ids(s, "dobae", None, None, vendor_id):
        raise AppError(404, "not_found", "업체를 찾을 수 없어요.")
    v = await s.get(Vendor, vendor_id)
    if v is None or v.suspended_at is not None:
        raise AppError(404, "not_found", "업체를 찾을 수 없어요.")
    return PublicDobaeDetailOut(
        **(await public_profile(s, vendor_id)).model_dump(), intro=v.intro or None
    )


async def public_dobae_mentions(s: AsyncSession, text: str) -> list[PublicDobaeDetailOut]:
    """질문에 상호가 명시된 도배 업체만 공개 정보로 반환. 지역·프로젝트로 좁히지 않는다."""
    return [
        await public_dobae_detail(s, vendor_id)
        for vendor_id in await repo.named_public_dobae_ids(s, text)
    ]


async def listed(s: AsyncSession, vendor_id: uuid.UUID, trade: str, sido: str, sgg: str) -> bool:
    """list_public에 나오는 업체인지 (승인 · 그 공정 · 그 지역 · 연락 방식을 정함)"""
    return bool(await repo.approved_ids(s, trade, sido, sgg, vendor_id))


async def portfolio_file(s: AsyncSession, vendor_id: uuid.UUID, file_id: uuid.UUID) -> File | None:
    """그 업체 시공 사진의 파일 행. 시공 사진이 아니면(근거 파일 · 등록증 등) None."""
    if await repo.portfolio_file_id(s, vendor_id, file_id) is None:
        return None
    return (await repo.files_by_id(s, [file_id])).get(file_id)


async def tax_kind_of(s: AsyncSession, vendor_id: uuid.UUID) -> str:
    """일반(general) · 간이(simplified) · 면세(exempt). 실측 견적서의 부가세 방식 · 계산에 쓴다.
    간이 · 면세는 부가세를 따로 붙이지 않는다(L02, NO_VAT)."""
    v = await s.get(Vendor, vendor_id)
    return v.tax_kind if v else "general"


async def contacts(s: AsyncSession, vendor_ids: list[uuid.UUID]) -> dict[uuid.UUID, ContactOut]:
    """업체 연락처 (대표 이름 · 휴대폰 · 연락 방식). 보여도 되는지는 visit.service가 정한다."""
    rows = await repo.vendors_by_id(s, vendor_ids) if vendor_ids else {}
    return {
        v.user_id: ContactOut(
            owner_name=v.owner_name,
            phone=v.phone,
            contact_method=v.contact_method,
            contact_url=v.contact_url,
            call_start=v.call_start,
            call_end=v.call_end,
            visit_fee=v.visit_fee,
            visit_fee_refund=v.visit_fee_refund,
        )
        for v in rows.values()
    }


async def list_public(
    s: AsyncSession, trade: str | None, sido: str | None, sgg: str | None
) -> list[PublicCardOut]:
    """그 공정 · 지역에서 일하는 승인된 업체 목록 (연락 방식을 정한 업체만)."""
    ids = await repo.approved_ids(s, trade, sido, sgg)
    if not ids:
        return []
    rows = await repo.vendors_by_id(s, ids)
    trades, areas = await repo.trades_and_areas(s, ids)
    return [PublicCardOut(**_card(rows[i], trades[i], areas[i])) for i in ids]


# ---- 가견적 단가: 업체가 근거 파일을 올리고 표준 행 단가를 직접 넣은 뒤 확인한다 ----

EXAMPLE_M2 = Decimal("105.8")  # 고객에게 보이는 모습 예: 32평 아파트(공급면적)
TRADE_NOT_MINE = "맡는 공정의 견적서만 올릴 수 있어요. 공정을 먼저 골라 주세요."
ROW_NAME = {r.key: r.name for rows in estimate.PRICE_ROWS.values() for r in rows}
NO_VAT = ("exempt", "simplified")  # 부가세를 따로 붙이지 않는 업체 (면세 · 간이, L02)
V3 = "dobae_v3"
V3_PAPER_UNIT = "벽지 평"  # v3 표준견적서 도배지 줄 단위 (v2 단가는 ㎡당이라 가견적 때 바꿔 센다)
# v3 단가표 '공급 69.4㎡ 집이면' 예시 집 (9판 C07 · D02): 실크 · 천장 포함 · 빈 집 · 전체
EXAMPLE_V3 = dobae.HomeV3(Decimal("69.4"), "supply", "basic", False)
EXAMPLE_V3_HOME = f"{area_text(EXAMPLE_V3.area_m2, EXAMPLE_V3.area_basis)} 집"  # 공급 69.4㎡ 집


@dataclass(frozen=True)
class ConfirmedPrices:
    """고객 가견적에 쓰는 확인한 단가 묶음. visit이 실측 요청 때 set_id와 합계를 고정한다."""

    set_id: uuid.UUID
    confirmed_at: datetime
    tax_kind: str
    prices: dict[str, estimate.VendorPrice]
    sources: dict[str, str]  # 단가 키 → 근거 파일 이름 (지운 파일은 없음)

    @property
    def files(self) -> list[str]:
        return sorted(set(self.sources.values()))


class _StdLine(Protocol):
    key: str
    name: str
    qty: Decimal | None
    unit: str
    state: str


@dataclass(frozen=True)
class _Std:
    key: str
    name: str
    qty: Decimal | None
    unit: str
    state: str


def _v2_lines(trade: str, std_lines: Sequence[_StdLine], grade: str | None) -> list:
    """옛 v2 단가(도배지 ㎡당)로 셀 때: v3 표준견적서의 도배지 줄(벽지 평)을 ㎡ 소요량 줄로 바꿔 넣는다."""
    if trade != "dobae":
        return list(std_lines)
    out: list = []
    for ln in std_lines:
        if ln.key == "paper" and ln.unit == V3_PAPER_UNIT:
            name = ROW_NAME.get(estimate.price_key("paper", grade), ln.name)
            qty = dobae.paper_m2_from_lines(std_lines, DOBAE_2026H2)
            ln = _Std(ln.key, name, qty, "㎡", ln.state)
        out.append(ln)
    return out


def estimate_out(
    trade: str,
    std_lines: Sequence[_StdLine],
    prices: dict[str, estimate.VendorPrice],
    *,
    grade: str | None,
    tax_kind: str,
    sources: dict[str, str],
) -> EstimateOut:
    """가견적 = 업체 단가 × 표준견적서 수량(계산 엔진). std_lines는 표준견적서 줄(저장본 또는 계산 결과).
    옛 v2 단가표 길이다(v3 단가는 estimate_v3_for). 간이 · 면세 업체는 부가세 0(L02)."""
    std_lines = _v2_lines(trade, std_lines, grade)
    by_key = {ln.key: ln for ln in std_lines}
    e = estimate.estimate(
        [estimate.StdLine(ln.key, ln.qty, ln.state) for ln in std_lines],
        prices,
        trade=trade,
        grade=grade,
        exempt=tax_kind in NO_VAT,
    )
    return EstimateOut(
        lines=[
            EstimateLineOut(
                key=x.key,
                price_key=x.price_key,
                name=by_key[x.key].name,
                unit=by_key[x.key].unit,
                qty=x.qty,
                unit_price=x.unit_price,
                included_in=x.included_in,
                included_in_name=ROW_NAME.get(x.included_in) if x.included_in else None,
                amount=x.amount,
                source=sources.get(x.price_key),
            )
            for x in e.lines
        ],
        supply=e.supply,
        vat=e.vat,
        total=e.total,
        missing=list(e.missing),
        left_out=list(e.left_out),
    )


def _example_lines(trade: str) -> list:
    """고객에게 보이는 모습 예: 32평 아파트 · 빈 집 · 도배 기본형(천장 포함) · 목공 몰딩 · 걸레받이"""
    if trade == "dobae":
        inp = dobae.DobaeInput(EXAMPLE_M2, "supply", "basic", True, False)
        return dobae.quote(inp, DOBAE_2026H2).lines
    inp = mokgong.MokgongInput(EXAMPLE_M2, "supply", ("molding", "base"), False)
    return mokgong.quote(inp, MOKGONG_2026H2).lines


def _prices_of(lines: list) -> dict[str, estimate.VendorPrice]:
    """v2 가견적 단가 (빼기 줄은 v3에만 있어 넣지 않는다)"""
    return {
        ln.key: estimate.VendorPrice(ln.unit_price, ln.included_in)
        for ln in lines
        if ln.unit_price is not None or ln.included_in is not None
    }


def _prices_v3(lines: list) -> dict[str, estimate.PriceV3]:
    return {ln.key: estimate.PriceV3(ln.mode, ln.unit_price, ln.included_in) for ln in lines}


def _warn(code: str, unit_price: int | None) -> str | None:
    """시장 기준과 10배 넘게 다르면 경고(L01, B6 기준값). 0원은 '10분의 1보다 작음'으로 본다."""
    if unit_price is None:
        return None
    if unit_price <= 0:
        return "too_low"
    w = price_warning(code, unit_price)
    return w.kind if w else None


def _estimate_v3_out(
    e: estimate.EstimateV3, ps: VendorPriceSet | None, tax_kind: str
) -> EstimateV3Out:
    return EstimateV3Out(
        rows_ver=V3,
        set_id=ps.id if ps else None,
        version=ps.version if ps else None,
        grade=e.grade,
        tax_kind=tax_kind,
        lines=[
            EstimateV3LineOut(
                code=x.row.code,
                key=x.row.key,
                name=x.row.name,
                qty=x.qty,
                unit=x.row.unit,
                unit_price=x.unit_price,
                amount=x.amount,
                mode=x.mode,
                included_in=x.included_in,
                in_standard=x.row.in_standard,
            )
            for x in e.lines
        ],
        supply=e.supply,
        vat=e.vat,
        total=e.total,
        vs_standard_pct=e.vs_standard_pct,
        paper_ok=e.paper_ok,
    )


def _std_v3(h: dobae.HomeV3) -> dobae.StandardV3:
    """v3 표준견적서 계산 (quote.service가 저장하는 값과 같은 계산 엔진 · 판)"""
    return dobae.standard_v3(
        h, DOBAE_2026H2, paper_market(h.grade), SPACE_SHARE_2026_11, HOUSE_2026H2
    )


def _v3_out(v: Vendor, ps: VendorPriceSet | None, lines: list) -> PricesV3Out:
    """업체 단가표 v3 (D02): 줄 1201~1208 + '공급 69.4㎡ 집이면' 예시 + 10배 경고"""
    ex = _std_v3(EXAMPLE_V3)
    stored = {ln.key: ln for ln in lines}
    prices = _prices_v3(lines)
    rows = []
    for r in estimate.V3_ROWS:
        ln = stored.get(r.key)
        qty, amount = estimate.row_amount(r, prices.get(r.key), ex.qty)
        base = vendor_base(r.code)
        rows.append(
            PriceRowV3Out(
                code=r.code,
                key=r.key,
                name=r.name,
                unit=r.unit,
                mode=ln.mode if ln else None,
                unit_price=ln.unit_price if ln else None,
                included_in=ln.included_in if ln else None,
                example_qty=qty,
                example_amount=amount,
                warn=_warn(r.code, ln.unit_price) if ln else None,
                warn_high=base.warn_above,
                warn_low=base.warn_below,
            )
        )
    example = None
    if ps is not None:
        e = estimate.estimate_v3(
            prices,
            ex.qty,
            grade=EXAMPLE_V3.grade,
            exempt=v.tax_kind in NO_VAT,
            standard_total=ex.quote.total,
        )
        example = _estimate_v3_out(e, ps, v.tax_kind)
    return PricesV3Out(
        rows_ver=V3,
        version=ps.version if ps else 0,
        confirmed_at=ps.confirmed_at if ps else None,
        needs_setup=ps is None,
        lines=rows,
        example=example,
        example_home=EXAMPLE_V3_HOME,
    )


def _sources_of(lines: list, names: dict[uuid.UUID, str]) -> dict[str, str]:
    return {ln.key: names[ln.source_file_id] for ln in lines if ln.source_file_id in names}


def _file_out(pf: VendorPriceFile, f) -> PriceFileOut:
    return PriceFileOut(
        file_id=pf.file_id,
        trade=pf.trade,
        filename=f.filename,
        content_type=f.content_type,
        size=f.size,
        created_at=pf.created_at,
    )


async def prices_for(s: AsyncSession, vendor: User, trade: str) -> PricesOut:
    """GET /vendor/prices: 단가를 넣는 표준 행 · 근거 파일 · 고치는 중(draft) · 확인한(confirmed) 묶음.
    v3 단가표(dobae_v3)는 v3 칸에만, 옛 칸(draft · confirmed)에는 옛 묶음만 보인다."""
    every = await repo.price_sets(s, [vendor.id], trade, ("draft", "confirmed"))
    lines = await repo.price_lines(s, [x.id for x in every])
    sets = [x for x in every if x.rows_ver != V3]
    v3_set = next((x for x in every if x.rows_ver == V3 and x.status == "confirmed"), None)
    files = await repo.price_files(s, [vendor.id], trade)
    names = {f.id: f.filename for _, f in files}
    v = await s.get(Vendor, vendor.id, populate_existing=True)
    example = _example_lines(trade)
    order = {k: i for i, k in enumerate(estimate.PRICE_KEYS[trade])}

    def out(ps: VendorPriceSet | None) -> PriceSetOut | None:
        if ps is None:
            return None
        rows = sorted(lines[ps.id], key=lambda ln: order.get(ln.key, len(order)))
        return PriceSetOut(
            id=ps.id,
            version=ps.version,
            status=ps.status,
            created_at=ps.created_at,
            confirmed_at=ps.confirmed_at,
            lines=[
                PriceLineOut(
                    key=ln.key,
                    unit_price=ln.unit_price,
                    included_in=ln.included_in,
                    source_file_id=ln.source_file_id,
                )
                for ln in rows
            ],
            example=estimate_out(
                trade,
                example,
                _prices_of(rows),
                grade="basic" if trade == "dobae" else None,
                tax_kind=v.tax_kind,
                sources=_sources_of(rows, names),
            ),
        )

    by = {x.status: x for x in sets}
    return PricesOut(
        trade=trade,
        rows=[PriceRowOut(key=r.key, name=r.name, unit=r.unit) for r in estimate.PRICE_ROWS[trade]],
        files=[_file_out(pf, f) for pf, f in files],
        draft=out(by.get("draft")),
        confirmed=out(by.get("confirmed")),
        v3=_v3_out(v, v3_set, lines[v3_set.id] if v3_set else []) if trade == "dobae" else None,
    )


async def _lock_and_check_trade(s: AsyncSession, vendor: User, trade: str) -> None:
    """같은 업체의 단가 쓰기가 줄 서게 업체 행을 잠그고, 맡는 공정인지 본다."""
    await repo.lock_vendor(s, vendor.id)
    trades, _ = await repo.trades_and_areas(s, [vendor.id])
    if trade not in trades[vendor.id]:
        raise AppError(409, "trade_not_mine", TRADE_NOT_MINE)


async def _commit(s: AsyncSession) -> None:
    try:
        await s.commit()
    except IntegrityError as exc:  # 부분 UQ(draft · confirmed 하나) · 파일 PK가 막은 동시 요청
        await s.rollback()
        raise AppError(409, "stale", STALE) from exc


async def save_prices(s: AsyncSession, vendor: User, trade: str, data: PriceSetIn) -> PricesOut:
    """고치는 중인 묶음(draft)을 통째로 저장한다. 없으면 새 버전으로 만든다. 확인 전에는 고객에게 안 쓴다."""
    await _lock_and_check_trade(s, vendor, trade)
    given = {ln.key: ln for ln in data.lines}
    if any(k not in estimate.PRICE_KEYS[trade] for k in given):
        raise AppError(422, "price_key", "표준 행을 다시 확인해 주세요.")
    if any(
        ln.included_in is not None
        and (ln.included_in not in given or given[ln.included_in].unit_price is None)
        for ln in data.lines
    ):
        raise AppError(422, "included_in", "'다른 줄에 포함'은 단가를 넣은 줄로 골라 주세요.")
    mine = {pf.file_id for pf, _ in await repo.price_files(s, [vendor.id], trade)}
    if any(ln.source_file_id is not None and ln.source_file_id not in mine for ln in data.lines):
        raise AppError(422, "source_file", "근거 파일을 다시 골라 주세요.")
    drafts = await repo.price_sets(s, [vendor.id], trade, ("draft",))
    try:
        if drafts:
            draft = drafts[0]
        else:
            version = await repo.next_price_version(s, vendor.id, trade)
            draft = VendorPriceSet(
                id=uuid7(),
                vendor_id=vendor.id,
                trade=trade,
                version=version,
                rows_ver=ROWS_VER_OF[trade],
            )
            s.add(draft)
            await s.flush()
        await repo.replace_price_lines(
            s,
            draft.id,
            [
                VendorPriceLine(
                    set_id=draft.id,
                    key=ln.key,
                    unit_price=ln.unit_price,
                    included_in=ln.included_in,
                    mode="included" if ln.included_in is not None else "price",
                    source_file_id=ln.source_file_id,
                )
                for ln in data.lines
            ],
        )
    except IntegrityError as exc:
        await s.rollback()
        raise AppError(409, "stale", STALE) from exc
    await _commit(s)
    return await prices_for(s, vendor, trade)


async def confirm_prices(s: AsyncSession, vendor: User, trade: str) -> PricesOut:
    """draft → confirmed, 이전 confirmed → retired. 이미 실측을 요청한 고객의 가견적은 그때 묶음으로 남는다."""
    await _lock_and_check_trade(s, vendor, trade)
    drafts = await repo.price_sets(s, [vendor.id], trade, ("draft",))
    if not drafts:
        raise AppError(409, "invalid_state", "확인할 단가가 없어요. 단가를 저장한 뒤 눌러 주세요.")
    draft = drafts[0]
    if not any(
        ln.unit_price is not None for ln in (await repo.price_lines(s, [draft.id]))[draft.id]
    ):
        raise AppError(422, "no_price", "단가를 하나 이상 넣어 주세요.")
    if not await repo.price_files(s, [vendor.id], trade):
        raise AppError(409, "no_file", "가견적에 쓸 견적서를 먼저 올려 주세요.")
    await repo.set_price_status(s, vendor.id, trade, to="retired", allowed_from="confirmed")
    changed = await repo.set_price_status(
        s, vendor.id, trade, to="confirmed", allowed_from="draft", at=now_utc(), set_id=draft.id
    )
    if changed != 1:
        raise AppError(409, "invalid_state", "이미 확인한 단가예요.")
    await _commit(s)
    return await prices_for(s, vendor, trade)


def _v3_line_errors(data: ConfirmPricesIn) -> list[dict]:
    """빈 칸(줄 빠짐) · 모르는 줄 · 포함 대상이 단가 줄이 아님. 칸 이름은 ["lines", 줄 키]."""
    given = {ln.key: ln for ln in data.lines}
    fields = [
        {"loc": ["lines", k], "msg": "모르는 줄이에요."}
        for k in given
        if k not in estimate.V3_BY_KEY
    ]
    fields += [
        {"loc": ["lines", r.key], "msg": "받는 법과 단가를 넣어 주세요."}
        for r in estimate.V3_ROWS
        if r.key not in given
    ]
    fields += [
        {"loc": ["lines", k], "msg": "포함할 줄은 단가를 넣은 줄로 골라 주세요."}
        for k, ln in given.items()
        if ln.included_in is not None
        and (ln.included_in not in given or given[ln.included_in].mode != "price")
    ]
    if not fields and not any(ln.mode == "price" for ln in data.lines):
        fields.append({"loc": ["lines"], "msg": "단가를 하나 이상 넣어 주세요."})
    return fields


async def confirm_prices_v3(
    s: AsyncSession, vendor: User, trade: str, data: ConfirmPricesIn
) -> PricesOut:
    """v3 '확인하고 저장' 한 번(D02): 줄 1201~1208을 모두 받아 새 확인 묶음(dobae_v3)을 만들고, 지금 확인 묶음
    (옛 v2 포함)은 retired로 둔다. 본 판(base_version)이 지금 v3 확인 묶음 판(없으면 0)이 아니면 409 stale
    (동시 두 번이면 늦은 쪽). 빈 칸 · 모르는 줄 422(fields). 10배 경고는 저장되고 응답 warn에 보인다."""
    if trade != "dobae":
        msg = "1201~1208 단가표는 도배만 있어요."
        raise app_error("invalid", msg, fields=[{"loc": ["trade"], "msg": msg}])
    fields = _v3_line_errors(data)
    if fields:
        raise app_error("invalid", fields[0]["msg"], fields=fields)
    await _lock_and_check_trade(s, vendor, trade)  # 같은 업체의 확인이 여기서 줄 선다
    confirmed = await repo.price_sets(s, [vendor.id], trade, ("confirmed",))
    now = next((x.version for x in confirmed if x.rows_ver == V3), 0)
    if data.base_version != now:
        raise app_error("stale", STALE)
    try:
        version = await repo.next_price_version(s, vendor.id, trade)
        await repo.set_price_status(s, vendor.id, trade, to="retired", allowed_from="confirmed")
        ps = VendorPriceSet(
            id=uuid7(),
            vendor_id=vendor.id,
            trade=trade,
            version=version,
            status="confirmed",
            confirmed_at=now_utc(),
            rows_ver=V3,
        )
        s.add(ps)
        await s.flush()
        s.add_all(
            [
                VendorPriceLine(
                    set_id=ps.id,
                    key=ln.key,
                    unit_price=ln.unit_price if ln.mode == "price" else None,
                    included_in=ln.included_in if ln.mode == "included" else None,
                    mode=ln.mode,
                )
                for ln in data.lines
            ]
        )
        await s.flush()
    except IntegrityError as exc:
        # (업체, 공정, 판) UNIQUE · 확인 묶음 하나 부분 UQ가 막은 동시 요청
        await s.rollback()
        raise app_error("stale", STALE) from exc
    await _commit(s)
    return await prices_for(s, vendor, trade)


async def add_price_file(s: AsyncSession, vendor: User, data: PriceFileIn) -> PriceFileOut:
    f = (await files_service.owned(s, vendor.id, [data.file_id])).get(data.file_id)
    if f is None or f.kind != "price_doc":
        raise AppError(422, "file", "올린 견적서 파일을 다시 확인해 주세요.")
    await _lock_and_check_trade(s, vendor, data.trade)
    if len(await repo.price_files(s, [vendor.id], data.trade)) >= PRICE_FILES_MAX:
        raise AppError(
            409, "too_many_files", f"견적서는 공정마다 {PRICE_FILES_MAX}개까지 올릴 수 있어요."
        )
    row = VendorPriceFile(file_id=f.id, vendor_id=vendor.id, trade=data.trade, created_at=now_utc())
    s.add(row)
    try:
        await s.commit()
    except IntegrityError as exc:  # 같은 파일을 동시에 두 번 붙이면 파일 PK가 막는다
        await s.rollback()
        raise AppError(409, "file_used", "이미 붙인 파일이에요.") from exc
    return _file_out(row, f)


async def remove_price_file(s: AsyncSession, vendor: User, file_id: uuid.UUID) -> None:
    """근거 파일 빼기. 파일도 지우고, 그 파일을 근거로 적은 단가는 남기되 출처만 비운다(FK SET NULL)."""
    await repo.lock_vendor(s, vendor.id)
    row = await s.get(VendorPriceFile, file_id)
    if row is None or row.vendor_id != vendor.id:
        raise AppError(404, "not_found", "파일을 찾을 수 없어요.")
    await s.delete(row)
    await s.flush()
    key = await files_service.drop(s, vendor.id, file_id)
    await s.commit()
    if key:
        await files_service.remove_stored(key)


async def confirmed_prices(
    s: AsyncSession, vendor_ids: list[uuid.UUID], trade: str
) -> dict[uuid.UUID, ConfirmedPrices]:
    """고객 가견적용(옛 v2 단가표 길): 승인된 업체의 확인한 단가 묶음. 없는 업체는 빠진다('가견적 없음').
    v3 단가표(dobae_v3)로 확인한 업체는 여기 없고 estimate_v3_for가 센다."""
    if not vendor_ids:
        return {}
    sets = await repo.price_sets(s, vendor_ids, trade, ("confirmed",))
    sets = [x for x in sets if x.rows_ver != V3]
    vendors = await repo.vendors_by_id(s, [x.vendor_id for x in sets])
    sets = [x for x in sets if vendors[x.vendor_id].status == "approved"]
    lines = await repo.price_lines(s, [x.id for x in sets])
    names = {f.id: f.filename for _, f in await repo.price_files(s, vendor_ids)}
    return {
        x.vendor_id: ConfirmedPrices(
            set_id=x.id,
            confirmed_at=x.confirmed_at,
            tax_kind=vendors[x.vendor_id].tax_kind,
            prices=_prices_of(lines[x.id]),
            sources=_sources_of(lines[x.id], names),
        )
        for x in sets
    }


def _home_v3(p: "ProjectOut", grade: str | None) -> dobae.HomeV3:
    """공사 → v3 도배 계산 입력 (quote.service.home_v3와 같은 규칙: v2 공사는 전체 공사)"""
    area, basis = house_area(p.size.band, p.size.m2, p.size.basis, HOUSE_2026H2)
    h = p.home
    g = grade or (h.grade if h else None) or (p.trades.dobae.grade if p.trades.dobae else None)
    g = g or "basic"
    if h is None:
        return dobae.HomeV3(area, basis, g, p.occupied, balcony_ext=p.balcony_ext)
    return dobae.HomeV3(
        area, basis, g, p.occupied, h.scope, tuple(h.spaces), tuple(h.faces), p.balcony_ext
    )


async def estimates_v3_for(
    s: AsyncSession,
    *,
    vendor_ids: list[uuid.UUID],
    project: "ProjectOut",
    trade: str,
    grade: str | None = None,
) -> dict[uuid.UUID, EstimateV3Out]:
    """고객 가견적 v3 여러 업체 (C10 · D06 목록): 승인된 업체가 확인한 v3 단가 × 이 집 계산 엔진 수량.
    확인한 v3 묶음이 없는 업체는 빠진다('가견적 준비 중', D06 A). 고른 벽지 줄이 '빼기'면 paper_ok False.
    grade를 주면 그 벽지로 센다(비교 %의 표준견적서도 그 벽지)."""
    if trade != "dobae" or not vendor_ids:
        return {}
    sets = [
        x for x in await repo.price_sets(s, vendor_ids, trade, ("confirmed",)) if x.rows_ver == V3
    ]
    vendors = await repo.vendors_by_id(s, [x.vendor_id for x in sets])
    sets = [x for x in sets if vendors[x.vendor_id].status == "approved"]
    if not sets:
        return {}
    lines = await repo.price_lines(s, [x.id for x in sets])
    h = _home_v3(project, grade)
    std = _std_v3(h)
    out: dict[uuid.UUID, EstimateV3Out] = {}
    for x in sets:
        tax = vendors[x.vendor_id].tax_kind
        e = estimate.estimate_v3(
            _prices_v3(lines[x.id]),
            std.qty,
            grade=h.grade,
            exempt=tax in NO_VAT,
            standard_total=std.quote.total,
        )
        out[x.vendor_id] = _estimate_v3_out(e, x, tax)
    return out


async def estimate_v3_for(
    s: AsyncSession,
    *,
    vendor_id: uuid.UUID,
    project: "ProjectOut",
    trade: str,
    grade: str | None = None,
) -> EstimateV3Out | None:
    """고객 가견적 v3 (C11 · D07): 업체가 확인한 v3 단가 × 계산 엔진 수량.

    확인한 v3 단가 묶음이 없으면 None('가견적 없음'). grade를 주면 그 벽지로 미리 본다
    (C11 '합지 −31만 1,850원' = grade 미리 보기 total − 고른 벽지 total).
    """
    got = await estimates_v3_for(
        s, vendor_ids=[vendor_id], project=project, trade=trade, grade=grade
    )
    return got.get(vendor_id)


# ---- 외부 리뷰 링크 (V18 · A01): 3개까지. 바뀐 링크는 '확인 중'이고 관리자가 확인해야 고객에게 보인다 ----


def _link_out(row: VendorReviewLink) -> ReviewLinkOut:
    return ReviewLinkOut(sort=row.sort, label=row.label, url=row.url, status=row.status)


async def review_links(s: AsyncSession, vendor: User) -> list[ReviewLinkOut]:
    """GET /vendor/review-links: 내 링크 (번호 순, 상태 포함)"""
    return [_link_out(r) for r in (await repo.review_links_of(s, [vendor.id]))[vendor.id]]


async def save_review_links(
    s: AsyncSession, vendor: User, data: ReviewLinksIn
) -> list[ReviewLinkOut]:
    """PUT /vendor/review-links: 통째로 저장. 같은 자리에 주소 · 이름이 그대로면 상태도 그대로 두고,
    새로 넣었거나 바뀐 링크는 '확인 중'(관리자 확인 전에는 고객에게 안 보여요). 빠진 자리는 지운다."""
    urls = [ln.url for ln in data.links]
    dup = [i for i, u in enumerate(urls) if u in urls[:i]]
    if dup:
        msg = "같은 주소를 두 번 넣었어요."
        fields = [{"loc": ["links", str(i), "url"], "msg": msg} for i in dup]  # 422 입구와 같은 꼴
        raise app_error("invalid", msg, fields=fields)
    await repo.lock_vendor(s, vendor.id)  # 같은 업체의 저장이 줄 선다
    old = {r.sort: r for r in (await repo.review_links_of(s, [vendor.id]))[vendor.id]}
    for i, ln in enumerate(data.links):
        row = old.pop(i, None)
        if row is None:
            s.add(VendorReviewLink(vendor_id=vendor.id, sort=i, label=ln.label, url=ln.url))
        elif (row.url, row.label) != (ln.url, ln.label):
            row.label, row.url = ln.label, ln.url
            row.status, row.note, row.checked_by, row.checked_at = "checking", None, None, None
    for row in old.values():
        await s.delete(row)
    await _commit(s)
    return await review_links(s, vendor)


async def shown_review_links(
    s: AsyncSession, vendor_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ReviewLinkOut]]:
    """고객 화면(업체 자세히 · 리뷰)용: 관리자가 '보여요'로 확인한 링크만"""
    rows = await repo.review_links_of(s, vendor_ids, ("shown",))
    return {vid: [_link_out(r) for r in links] for vid, links in rows.items()}


# ---- 관리자 업체 멈춤 · 외부 리뷰 링크 확인 (D26 · A01 · A02) ----
# report.service가 자기 트랜잭션 안에서 부른다: 조건부 UPDATE만 하고 commit하지 않는다.
# 처리 기록(admin_actions) · 알림 · 대화 닫기 · 404 · 409 문구는 부르는 쪽 몫이다(지금 report 동작 그대로).

SUSPEND_SCOPES = ("new", "all")  # 새 요청만 막기 · 진행 중인 것도 닫기
LINK_CHECKED = ("shown", "hidden")  # 관리자가 확인한 링크 상태


async def suspend(
    s: AsyncSession, vendor_id: uuid.UUID, scope: str, at: datetime
) -> tuple[datetime, str] | None:
    """업체 기능 멈춤 칸 바꾸기. 멈추지 않은 업체 → scope, 새 요청만(new) → 진행 중도(all)로 넓히기만 된다.
    바뀌면 (멈춘 때, 범위) — 넓힐 때 멈춘 때는 처음 그대로. 업체가 없거나 이미 같거나 더 넓으면 None."""
    if scope not in SUSPEND_SCOPES:
        raise ValueError(f"알 수 없는 멈춤 범위: {scope}")
    return await repo.suspend(s, vendor_id, scope, at)


async def unsuspend(s: AsyncSession, vendor_id: uuid.UUID) -> bool:
    """업체 기능 멈춤 풀기. 멈춘 업체만 True (없거나 멈추지 않았으면 False)."""
    return await repo.unsuspend(s, vendor_id)


async def set_review_link_status(
    s: AsyncSession,
    vendor_id: uuid.UUID,
    sort: int,
    status: str,
    *,
    by: uuid.UUID,
    at: datetime,
    note: str | None = None,
    url: str | None = None,
) -> ReviewLinkAdminOut | None:
    """외부 리뷰 링크를 보여요(shown) · 숨김(hidden)으로 바꾸고 확인한 관리자 · 때를 남긴다.
    note를 주면 관리자 메모도 바꾼다(None이면 그대로). url을 주면 그 주소일 때만 바꾼다 — 관리자가 본 뒤
    업체가 고친 주소를 보이게 하지 않으려고. 업체의 링크 저장과 줄 서게 업체 행을 잠근다.
    바뀌면 바뀐 링크, 링크가 없거나 · 이미 그 상태이거나 · 주소가 다르면 None."""
    if status not in LINK_CHECKED:
        raise ValueError(f"알 수 없는 링크 상태: {status}")
    note = (note or "").strip() or None  # 빈 메모는 '안 줌' (DB CHECK note)
    await repo.lock_vendor(s, vendor_id)
    row = await repo.set_review_link_status(
        s, vendor_id, sort, status, by=by, at=at, note=note, url=url
    )
    if row is None:
        return None
    return ReviewLinkAdminOut(
        sort=row.sort,
        label=row.label,
        url=row.url,
        status=row.status,
        note=row.note,
        checked_at=row.checked_at,
    )
