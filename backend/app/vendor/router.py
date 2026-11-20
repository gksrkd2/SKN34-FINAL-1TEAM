import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service as auth_service
from app.auth.deps import require_role, set_session_cookie
from app.auth.models import User
from app.auth.schemas import MeOut
from app.db import get_session
from app.shared.clock import today_seoul
from app.shared.ratelimit import client_ip
from app.vendor import service
from app.vendor.deps import approved_vendor
from app.vendor.schemas import (
    ApplicationOut,
    ConfirmPricesIn,
    PortfolioIn,
    PortfolioOut,
    PriceFileIn,
    PriceFileOut,
    PriceSetIn,
    PricesOut,
    ProfileIn,
    ProfileOut,
    PublicDobaeDetailOut,
    ReapplyIn,
    ResubmitIn,
    ReviewIn,
    ReviewLinkOut,
    ReviewLinksIn,
    ReviewSummaryOut,
    VendorAdminDetailOut,
    VendorAdminOut,
    VendorSignupIn,
)

router = APIRouter(tags=["업체"])
admin_only = require_role("admin")
vendor_only = require_role("vendor")


@router.get(
    "/vendors/{vendor_id}",
    response_model=PublicDobaeDetailOut,
    summary="도배 업체 공개 상세 (지역·프로젝트 무관, 고객)",
)
async def public_dobae_vendor(
    vendor_id: uuid.UUID,
    _user: User = Depends(require_role("customer")),
    s: AsyncSession = Depends(get_session),
) -> PublicDobaeDetailOut:
    return await service.public_dobae_detail(s, vendor_id)


@router.post(
    "/auth/vendor-signup",
    status_code=201,
    response_model=MeOut,
    summary="업체 가입 신청 · 신청서 + 사업자등록증, 심사 중으로 시작",
)
async def vendor_signup(
    request: Request,
    response: Response,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    biz_name: Annotated[str, Form(description="상호")],
    owner_name: Annotated[str, Form(description="대표자 이름")],
    biz_no: Annotated[str, Form(description="사업자등록번호 10자리")],
    phone: Annotated[str, Form(description="휴대폰")],
    trades: Annotated[list[str], Form(description="하는 공정 (dobae · mokgong, 여러 번)")],
    areas: Annotated[list[str], Form(description="활동 지역 'seoul/노원구' (여러 번)")],
    license_file: Annotated[UploadFile, File(description="사업자등록증 (jpg · png · pdf)")],
    name: Annotated[str | None, Form(description="담당자 이름 (비우면 대표자)")] = None,
    s: AsyncSession = Depends(get_session),
) -> MeOut:
    try:
        body = VendorSignupIn(
            name=name,
            email=email,
            password=password,
            biz_name=biz_name,
            owner_name=owner_name,
            biz_no=biz_no,
            phone=phone,
            trades=trades,
            areas=areas,
        )
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False)
        raise RequestValidationError([{**e, "loc": ("body", *e["loc"])} for e in errors]) from exc
    user, token = await service.signup(s, body, license_file, client_ip(request))
    set_session_cookie(response, token)
    return await auth_service.me(s, user)


@router.get(
    "/admin/vendors",
    response_model=list[VendorAdminOut],
    summary="업체 심사 목록 · 기다림 · 보완 요청 · 처리함, 상호 · 사업자번호로 찾기",
)
async def list_vendors(
    status: Literal["pending", "needs_info", "done"] | None = None,
    q: Annotated[str | None, Query(max_length=50)] = None,
    _: User = Depends(admin_only),
    s: AsyncSession = Depends(get_session),
) -> list[VendorAdminOut]:
    return await service.list_for_admin(s, status, q, today_seoul())


@router.get("/admin/vendors/summary", response_model=ReviewSummaryOut, summary="업체 심사 칸별 수")
async def vendor_summary(
    _: User = Depends(admin_only), s: AsyncSession = Depends(get_session)
) -> ReviewSummaryOut:
    return await service.review_summary(s)


@router.get(
    "/admin/vendors/{vendor_id}",
    response_model=VendorAdminDetailOut,
    summary="업체 심사 자세히 · 처리 기록",
)
async def get_vendor(
    vendor_id: uuid.UUID, _: User = Depends(admin_only), s: AsyncSession = Depends(get_session)
) -> VendorAdminDetailOut:
    return await service.get_for_admin(s, vendor_id, today_seoul())


@router.post(
    "/admin/vendors/{vendor_id}/review",
    response_model=VendorAdminDetailOut,
    summary="업체 심사 결정 저장 · 승인 · 보완 요청 · 거절 (심사 중일 때만, 먼저 정했으면 409)",
)
async def review_vendor(
    vendor_id: uuid.UUID,
    data: ReviewIn,
    admin: User = Depends(admin_only),
    s: AsyncSession = Depends(get_session),
) -> VendorAdminDetailOut:
    return await service.review(s, admin, vendor_id, data, today_seoul())


@router.get(
    "/vendor/application",
    response_model=ApplicationOut,
    summary="내 가입 심사 (승인 전에도) · 낸 내용 · 최근 결정 · 열린 칸",
)
async def my_application(
    vendor: User = Depends(vendor_only), s: AsyncSession = Depends(get_session)
) -> ApplicationOut:
    return await service.application(s, vendor)


@router.post(
    "/vendor/application/resubmit",
    response_model=ApplicationOut,
    summary="보완해서 다시 보내기 (보완 요청 → 심사 중, 열린 칸만)",
)
async def resubmit(
    data: ResubmitIn,
    vendor: User = Depends(vendor_only),
    s: AsyncSession = Depends(get_session),
) -> ApplicationOut:
    return await service.resubmit(s, vendor, data)


@router.post(
    "/vendor/application/reapply",
    response_model=ApplicationOut,
    summary="거절 뒤 다시 신청 (같은 계정, 사업자등록번호도 고칠 수 있음 · 다른 업체 번호면 409)",
)
async def reapply(
    data: ReapplyIn,
    vendor: User = Depends(vendor_only),
    s: AsyncSession = Depends(get_session),
) -> ApplicationOut:
    return await service.reapply(s, vendor, data)


@router.get("/vendor/profile", response_model=ProfileOut, summary="업체 정보 (승인된 업체 본인)")
async def profile(
    vendor: User = Depends(approved_vendor), s: AsyncSession = Depends(get_session)
) -> ProfileOut:
    return await service.get_profile(s, vendor)


@router.put(
    "/vendor/profile",
    response_model=ProfileOut,
    summary="업체 정보 저장 · 통째로 (상호 · 대표자 · 사업자번호는 못 바꿈, updated_at이 다르면 409)",
)
async def save_profile(
    data: ProfileIn,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> ProfileOut:
    return await service.save_profile(s, vendor, data, today_seoul())


@router.post(
    "/vendor/portfolio",
    status_code=201,
    response_model=PortfolioOut,
    summary="시공 사진 더하기 (먼저 POST /files kind=portfolio)",
)
async def add_portfolio(
    data: PortfolioIn,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> PortfolioOut:
    return await service.add_portfolio(s, vendor, data, today_seoul())


@router.delete("/vendor/portfolio/{item_id}", status_code=204, summary="시공 사진 빼기")
async def remove_portfolio(
    item_id: uuid.UUID,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> Response:
    await service.remove_portfolio(s, vendor, item_id)
    return Response(status_code=204)


TradeParam = Literal["dobae", "mokgong"]


@router.get(
    "/vendor/prices",
    response_model=PricesOut,
    summary="가견적 단가 · 근거 파일 (고치는 중 · 확인한 묶음, 고객에게 보이는 모습 예)",
)
async def prices(
    trade: TradeParam,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> PricesOut:
    return await service.prices_for(s, vendor, trade)


@router.put(
    "/vendor/prices/{trade}",
    response_model=PricesOut,
    summary="가견적 단가 저장 (고치는 중인 묶음, 확인 전에는 고객에게 안 씀)",
)
async def save_prices(
    trade: TradeParam,
    data: PriceSetIn,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> PricesOut:
    return await service.save_prices(s, vendor, trade, data)


@router.post(
    "/vendor/prices/{trade}/confirm",
    response_model=PricesOut,
    summary="가견적 단가 확인했어요 (몸통 없으면 고치는 중 → 확인, 몸통 있으면 v3 '확인하고 저장')",
)
async def confirm_prices(
    trade: TradeParam,
    data: ConfirmPricesIn | None = None,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> PricesOut:
    """v3: 몸통(본 판 + 줄 1201~1208)이면 '확인하고 저장' 한 번(D02, 본 판이 아니면 409 stale).
    몸통이 없으면 지금 길(고치는 중인 묶음을 확인)."""
    if data is not None:
        return await service.confirm_prices_v3(s, vendor, trade, data)
    return await service.confirm_prices(s, vendor, trade)


@router.post(
    "/vendor/price-files",
    status_code=201,
    response_model=PriceFileOut,
    summary="가견적 근거 파일 붙이기 (먼저 POST /files kind=price_doc)",
)
async def add_price_file(
    data: PriceFileIn,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> PriceFileOut:
    return await service.add_price_file(s, vendor, data)


@router.delete("/vendor/price-files/{file_id}", status_code=204, summary="가견적 근거 파일 빼기")
async def remove_price_file(
    file_id: uuid.UUID,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> Response:
    await service.remove_price_file(s, vendor, file_id)
    return Response(status_code=204)


# ---- v3: 외부 리뷰 링크 (V18 · A01). 관리자 링크 확인은 아직 0단계 뼈대 ----
# 업체 멈춤 · 풀기(POST /admin/vendors/{vendor_id}/suspend · /unsuspend)는 report/router.py가 맡는다(D26)


@router.get(
    "/vendor/review-links",
    response_model=list[ReviewLinkOut],
    summary="내 외부 리뷰 링크 (3개까지, 상태 포함)",
)
async def review_links(
    vendor: User = Depends(approved_vendor), s: AsyncSession = Depends(get_session)
) -> list[ReviewLinkOut]:
    return await service.review_links(s, vendor)


@router.put(
    "/vendor/review-links",
    response_model=list[ReviewLinkOut],
    summary="외부 리뷰 링크 통째로 저장 (https만 · 3개까지, 바뀐 링크는 확인 중)",
)
async def save_review_links(
    data: ReviewLinksIn,
    vendor: User = Depends(approved_vendor),
    s: AsyncSession = Depends(get_session),
) -> list[ReviewLinkOut]:
    return await service.save_review_links(s, vendor, data)
