"""시연 데이터. 로컬 개발 · 테스트(APP_ENV=local · test)에서만 만든다(staging · production은 멈춤).

v3 (기본, B6): 이야기 J1 ~ J24를 화면에서 바로 누를 수 있게 단계마다 고객 한 명 · 공사 하나, 9판 업체 6곳,
관리자, 업체 별점을 만드는 지난 공사를 만든다(아래 'v3 시연 데이터'). 계정 목록은 docs/v3/verification/b6.md.
  돌리기: uv run python -m app.demo            (DATABASE_URL · FILES_DIR은 API와 같게)
  비밀번호: DEMO_PASSWORD 환경변수, 없으면 이 파일의 DEMO_PASSWORD(로컬 시험 값). 화면 · 로그에 찍지 않는다.
  다시 돌리면 이미 있는 계정은 건너뛴다.

v2 (`--v2`): 시연 고객 1명과 시연 업체 6곳(도배 3 · 목공 3, 승인 · 확인한 단가까지)을 만들고,
고객 흐름 단계마다 프로젝트를 하나씩 만든다. 업체 쪽 일(견적서 보내기 · 공사 날짜 내기)은 미리 해 둔다.
  업체 고르기(pick) → 실측 요청(visit_requested) → 견적서 옴(quotes_arrived)
  → 공사 날짜 옴(dates_received) → 확정 공정표(before_work)
다시 돌리면 계정은 그대로 두고 단계별 프로젝트를 한 벌 더 만든다. 시연 안내 화면(/demo)은 단계마다 가장 최근 것을 쓴다.
업체 승인자로 남길 관리자 계정이 먼저 있어야 한다. 이미 있는 계정의 비밀번호는 바꾸지 않는다.
  돌리기: docker compose exec -T -e DEMO_PASSWORD api python -m app.demo --v2

DEMO_AUTO=true면 worker가 몇 초마다 auto_reply로 v2 시연 업체 일을 대신 한다(아래 '자동 응답').
"""

import asyncio
import io
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager, nullcontext, suppress
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import and_, event, or_, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session
from starlette.datastructures import Headers, UploadFile

from app import queue
from app.auth import repository as auth_repo
from app.auth import service as auth_service
from app.auth.models import User
from app.calc.schedule import WEEKEND, is_off, workdays
from app.config import settings
from app.db import SessionLocal, engine
from app.files import service as files_service
from app.notify import service as notify_service
from app.project import service as project_service
from app.project.schemas import HomeInfoIn, ProjectCreate
from app.quote.schemas import QuoteDraftIn
from app.refprice.holidays import days_off
from app.review import service as review_service
from app.review.schemas import ReplyIn, ReviewWriteIn
from app.schedule import service as schedule_service
from app.schedule.models import DateProposal, DateRequest, Deal
from app.schedule.schemas import AcceptIn, CheckIn, ConfirmIn, DatesIn, DoneIn
from app.seed import LICENSE, sample_pdf
from app.shared import clock
from app.shared.clock import SEOUL, now_utc, today_seoul
from app.shared.errors import AppError
from app.thread import service as thread_service
from app.thread.schemas import MessageIn
from app.vendor import repository as vendor_repo
from app.vendor import service as vendor_service
from app.vendor.models import Vendor, VendorArea, VendorFaq, VendorReview, VendorTrade
from app.vendor.schemas import (
    AreaIn,
    ConfirmPricesIn,
    FaqIn,
    PortfolioIn,
    PriceFileIn,
    PriceLineIn,
    PriceLineV3In,
    PriceSetIn,
    ProfileIn,
    ReviewIn,
    ReviewLinkIn,
    ReviewLinksIn,
    VendorSignupIn,
)
from app.visit import flow as visit_flow
from app.visit import service as visit_service
from app.visit.models import Visit
from app.visit.schemas import (
    ShortlistIn,
    SlotIn,
    VisitCreateIn,
    VisitedIn,
    VisitPhotoIn,
    WindowIn,
)

log = logging.getLogger(__name__)

CUSTOMER = ("demo@mitjip.test", "시연", "01000000000")  # 실제로 없는 번호
# 이메일 → (담당자, 상호, 대표, 사업자번호, 전화, 공정, 확인한 단가). 단가는 원 · 부가세 빼고
VENDORS = {
    "demo-dobae1@mitjip.test": (
        "김시연",
        "시연도배",
        "김시연",
        "9990000901",
        "01000000901",
        "dobae",
        {"wall": 6650, "ceiling": 6800, "paper_basic": 3400, "paper_economy": 2600},
    ),
    "demo-dobae2@mitjip.test": (
        "이시연",
        "시연벽지",
        "이시연",
        "9990000902",
        "01000000902",
        "dobae",
        {
            "wall": 7000,
            "ceiling": 7200,
            "paper_basic": 3600,
            "paper_economy": 2800,
            "primer": 600,
        },
    ),
    "demo-mok1@mitjip.test": (
        "박시연",
        "시연목공",
        "박시연",
        "9990000903",
        "01000000903",
        "mokgong",
        {
            "frame": 18000,
            "board": 12000,
            "ceiling_mat": 9000,
            "door": 90000,
            "casing": 4500,
            "door_mat": 180000,
            "molding": 3500,
            "molding_mat": 2500,
            "base": 3000,
            "base_mat": 2000,
        },
    ),
    "demo-dobae3@mitjip.test": (
        "최시연",
        "시연한지",
        "최시연",
        "9990000904",
        "01000000904",
        "dobae",
        {"wall": 6900, "ceiling": 7000, "paper_basic": 3300, "paper_economy": 2500},
    ),
    "demo-mok2@mitjip.test": (
        "정시연",
        "시연목수",
        "정시연",
        "9990000905",
        "01000000905",
        "mokgong",
        {
            "frame": 19000,
            "board": 12500,
            "ceiling_mat": 9500,
            "door": 95000,
            "casing": 4800,
            "door_mat": 175000,
            "molding": 3800,
            "molding_mat": 2400,
            "base": 3200,
            "base_mat": 2100,
        },
    ),
    "demo-mok3@mitjip.test": (
        "강시연",
        "시연우드",
        "강시연",
        "9990000906",
        "01000000906",
        "mokgong",
        {
            "frame": 17500,
            "board": 11500,
            "ceiling_mat": 9000,
            "door": 88000,
            "casing": 4300,
            "door_mat": 190000,
            "molding": 3300,
            "molding_mat": 2700,
            "base": 2900,
            "base_mat": 2200,
        },
    ),
}
D1, D2, MOK = list(VENDORS)[:3]
AREA = ("seoul", "노원구")
# 업체마다 보내는 실측 견적서: demo_assets 이미지 · 문서에 적힌 낼 금액 · 부가세 방식 (공사 기간 3일)
ASSETS = Path(__file__).parent / "demo_assets"
QUOTE_DOCS = {
    "demo-dobae1@mitjip.test": ("quote-dobae1.jpg", 3_080_000, "excluded"),
    "demo-dobae2@mitjip.test": ("quote-dobae2.jpg", 3_300_000, "included"),
    "demo-dobae3@mitjip.test": ("quote-dobae3.jpg", 2_980_000, "excluded"),
    "demo-mok1@mitjip.test": ("quote-mok1.jpg", 1_450_000, "excluded"),
    "demo-mok2@mitjip.test": ("quote-mok2.jpg", 1_540_000, "included"),
    "demo-mok3@mitjip.test": ("quote-mok3.jpg", 1_390_000, "excluded"),
}
# 시연 업체가 적은 자주 묻는 5가지 (업체가 쓴 글이라 금액은 넣지 않는다)
FAQS = {
    "dobae": {
        "process": "전화로 방문 날을 잡고 벽 상태와 면적을 재요. 실측 견적서를 보내 드리고, "
        "확정하면 공사 날짜를 올려요. 공사는 기존 벽지 제거 → 초배 → 정배 순서예요.",
        "pricing": "실측한 벽 · 천장 면적과 고르신 벽지 유형으로 계산해요. "
        "자재비와 인건비를 나눠 견적서에 적어 드려요.",
        "extra_cost": "곰팡이 · 들뜸이 심해 벽 보수가 필요하거나, 실측 뒤 벽지를 바꾸시면 "
        "금액이 달라질 수 있어요. 생기면 공사 전에 먼저 알려 드려요.",
        "duration": "빈 집 기준 보통 2~3일이에요. 짐이 있거나 천장까지 하면 하루 더 걸릴 수 있어요.",
        "as_refund": "공사 뒤 1년 안에 들뜸 · 이음새 벌어짐이 생기면 다시 붙여 드려요. "
        "자재를 주문하기 전에 취소하시면 받은 돈을 모두 돌려 드려요.",
    },
    "mokgong": {
        "process": "방문해 몰딩 · 걸레받이 길이와 벽 상태를 재요. 실측 견적서를 보내 드리고, "
        "확정하면 공사 날짜를 올려요. 목공은 도배 전에 끝내요.",
        "pricing": "실측한 길이(m)와 고르신 자재로 계산해요. 자재비와 인건비를 나눠 적어 드려요.",
        "extra_cost": "벽이 많이 휘었거나 기존 몰딩을 떼다 벽이 상하면 보수비가 더 들 수 있어요. "
        "생기면 먼저 알려 드려요.",
        "duration": "몰딩 · 걸레받이는 보통 1~2일이에요.",
        "as_refund": "공사 뒤 1년 안에 들뜸 · 벌어짐은 고쳐 드려요. "
        "자재를 주문하기 전에 취소하시면 받은 돈을 모두 돌려 드려요.",
    },
}
QUOTE_DAYS = 3
QUOTE_VALID_DAYS = 14
QUOTE_PDF = sample_pdf(b"SAMPLE QUOTE - demo data only")
PRICE_PDF = sample_pdf(b"SAMPLE PRICE LIST - demo data only")


def _project(today: date, both: bool) -> ProjectCreate:
    """노원구 32평(공급 105.8㎡) 빈 집. both면 목공(몰딩 · 걸레받이)도 한다."""
    trades = {"dobae": {"spaces": ["a", "b", "c"], "grade": "basic"}}
    if both:
        trades["mokgong"] = {"spaces": ["a"], "items": ["molding", "base"]}
    return ProjectCreate.model_validate(
        {
            "sido": AREA[0],
            "sgg": AREA[1],
            "built_year": 2009,
            "size": {"basis": "supply", "m2": "105.8"},
            "balcony_ext": False,
            "when": {
                "kind": "date",
                "start": (today + timedelta(days=28)).isoformat(),
                "end": (today + timedelta(days=42)).isoformat(),
            },
            "occupied": False,
            "budget": "500-700",
            "spaces": [
                {"key": "a", "name": "거실"},
                {"key": "b", "name": "안방"},
                {"key": "c", "name": "작은방"},
            ],
            "trades": trades,
        }
    )


async def _user(s, email: str, name: str, password: str, role: str, phone=None) -> User:
    user = await auth_repo.user_by_email(s, email)
    if user is None:
        user = await auth_service.create_user(
            s, email=email, name=name, password=password, role=role, phone=phone
        )
    return user


async def _vendor(s, email: str, password: str, admin: User, today: date) -> User:
    """승인 · 업체 정보 · 확인한 단가까지 갖춘 시연 업체. 이미 있으면 빈 것만 채운다."""
    name, biz, owner, biz_no, phone, trade, prices = VENDORS[email]
    user = await _user(s, email, name, password, "vendor")
    if await s.get(Vendor, user.id) is None:
        lic = files_service.Prepared(
            kind="license", data=LICENSE, content_type="application/pdf", filename="시연용.pdf"
        )
        row = await files_service.persist(s, user.id, lic)
        now = now_utc()
        try:
            s.add(
                Vendor(
                    user_id=user.id,
                    license_file_id=row.id,
                    status="approved",
                    reviewed_by=admin.id,
                    reviewed_at=now,
                    approved_at=now,
                    biz_name=biz,
                    owner_name=owner,
                    biz_no=biz_no,
                    phone=phone,
                    address="서울 노원구 (시연용 주소)",
                    contact_method="phone",
                    call_start=time(9),
                    call_end=time(18),
                    visit_fee=0,
                )
            )
            await s.flush()
            s.add_all(
                [
                    VendorReview(vendor_id=user.id, action="apply", by_id=user.id, at=now),
                    VendorReview(vendor_id=user.id, action="approve", by_id=admin.id, at=now),
                    VendorTrade(vendor_id=user.id, trade=trade),
                    VendorArea(vendor_id=user.id, sido=AREA[0], sgg=AREA[1]),
                ]
            )
            await s.commit()
        except Exception:
            await s.rollback()
            files_service.discard(row)  # 업체 행을 못 만들었으면 등록증 파일도 남기지 않는다
            raise
    else:
        await s.commit()
    if await s.scalar(select(VendorFaq.key).where(VendorFaq.vendor_id == user.id).limit(1)) is None:
        s.add_all(VendorFaq(vendor_id=user.id, key=k, answer=a) for k, a in FAQS[trade].items())
        await s.commit()
    if not await vendor_repo.price_sets(s, [user.id], trade, ("confirmed",)):
        if not await vendor_repo.price_files(
            s, [user.id], trade
        ):  # 단가 확인에 꼭 필요한 근거 파일
            doc = files_service.Prepared(
                kind="price_doc",
                data=PRICE_PDF,
                content_type="application/pdf",
                filename="단가표.pdf",
            )
            row = await files_service.persist(s, user.id, doc)
            await s.commit()
            await vendor_service.add_price_file(s, user, PriceFileIn(file_id=row.id, trade=trade))
        lines = [PriceLineIn(key=k, unit_price=v) for k, v in prices.items()]
        if trade == "dobae" and "primer" not in prices:
            lines.append(PriceLineIn(key="primer", included_in="paper_basic"))
        await vendor_service.save_prices(s, user, trade, PriceSetIn(lines=lines))
        await vendor_service.confirm_prices(s, user, trade)
    return user


async def _ask(s, customer: User, pid, trade: str, vendors: list[User], today: date) -> list:
    """실측 요청(연락처 보냄). 업체 순서대로 요청 id"""
    ids = [v.id for v in vendors]
    data = VisitCreateIn(
        trade=trade,
        vendor_ids=ids,
        share_contact_ids=ids,
        apt_name="시연아파트",
        dong="101",
        ho="1203",
        consent_ver=visit_service.CONSENT_VER,
    )
    out = await visit_service.request_visits(s, customer, pid, data, today)
    if out.failed:
        raise RuntimeError("시연 업체에 실측을 요청하지 못했어요.")
    by_vendor = {v.vendor_id: v.id for v in out.created}
    return [by_vendor[i] for i in ids]


async def _quote(s, vendor: User, visit_id, today: date) -> None:
    """업체가 실측 견적서를 보낸다: 그 업체의 견적서 이미지(없으면 빈 PDF)와 문서에 적힌 금액 그대로."""
    name, amount, vat_mode = QUOTE_DOCS[vendor.email]
    path = ASSETS / name
    if path.exists():
        doc = (await asyncio.to_thread(path.read_bytes), "image/jpeg", "실측견적서.jpg")
    else:
        doc = (QUOTE_PDF, "application/pdf", "실측견적서.pdf")
    f = files_service.Prepared(kind="quote_doc", data=doc[0], content_type=doc[1], filename=doc[2])
    row = await files_service.persist(s, vendor.id, f)
    await s.commit()
    data = QuoteDraftIn(
        file_ids=[row.id],
        measured_on=today,
        entered_amount=amount,
        vat_mode=vat_mode,
        days=QUOTE_DAYS,
        valid_until=today + timedelta(days=QUOTE_VALID_DAYS),
    )
    await visit_service.save_quote(s, vendor, visit_id, data, today)
    await visit_service.send_quote(s, vendor, visit_id, today)


async def _dates(s, customer: User, pid, visit_id, vendor: User, days: list[date], today: date):
    """확정 → 업체가 공사 날짜 냄. (확정 id, 낸 날짜 id)"""
    deal = (
        await schedule_service.confirm(s, customer, pid, visit_id, ConfirmIn(seen_ver=1), today)
    ).deal
    dates = DatesIn(start=days[0], end=days[-1], crew=2, method="app")
    job = await schedule_service.propose_dates(s, vendor, deal.id, dates, today)
    return deal.id, job.proposal.id


LOCK = "demo:auto_reply"  # worker 자동 응답이 한 번 도는 동안 · 시연 데이터를 만드는 동안 잡는다


@asynccontextmanager
async def _paused():
    """시연 데이터를 만드는 동안 자동 응답을 멈춘다 (업체 일을 build가 직접 한다). Redis가 없으면 그냥 간다."""
    r = Redis.from_url(settings.redis_url, socket_timeout=1, socket_connect_timeout=1)
    held = False
    try:
        for _ in range(30):  # 자동 응답이 도는 중이면 끝날 때까지 기다린다
            if held := bool(await r.set(LOCK, "build", nx=True, ex=600)):
                break
            await asyncio.sleep(1)
    except RedisError:
        pass
    try:
        yield
    finally:
        if held:
            with suppress(RedisError):
                await r.delete(LOCK)
        await r.aclose()


async def build(password: str, today: date) -> dict[str, str]:
    """단계 코드 → 새로 만든 프로젝트 번호(MJ-…)"""
    if settings.app_env not in DEMO_ENVS:  # staging · production에서는 시연 계정을 만들지 않는다
        raise SystemExit("시연 데이터는 로컬 개발(APP_ENV=local · test)에서만 만들어요.")
    try:
        async with _paused(), SessionLocal() as s:
            first_admin = select(User).where(User.role == "admin").order_by(User.created_at)
            admin = await s.scalar(first_admin.limit(1))
            if admin is None:
                raise RuntimeError("업체 승인자로 남길 관리자 계정이 먼저 있어야 해요.")
            email, name, phone = CUSTOMER
            customer = await _user(s, email, name, password, "customer", phone)
            await s.commit()
            vendors = {e: await _vendor(s, e, password, admin, today) for e in VENDORS}
            d1, d2, mok = vendors[D1], vendors[D2], vendors[MOK]
            off = days_off(today, today + timedelta(days=400))
            made = {}

            async def new(stage: str, both: bool = False):
                p = await project_service.create(s, customer, _project(today, both), today)
                made[stage] = p.code
                return p.id

            await new("pick", both=True)

            pid = await new("visit_requested")
            for vendor, visit_id in zip(
                (d1, d2), await _ask(s, customer, pid, "dobae", [d1, d2], today), strict=True
            ):  # 업체가 열어 본 요청은 자동 응답이 건드리지 않는다 (이 단계를 그대로 보여 준다)
                await visit_service.mark_seen(s, vendor, visit_id)

            pid = await new("quotes_arrived")
            va, vb = await _ask(s, customer, pid, "dobae", [d1, d2], today)
            await _quote(s, d1, va, today)
            await _quote(s, d2, vb, today)

            pid = await new("dates_received")
            (va,) = await _ask(s, customer, pid, "dobae", [d1], today)
            await _quote(s, d1, va, today)
            await _dates(
                s, customer, pid, va, d1, workdays(today + timedelta(days=28), 3, off), today
            )

            pid = await new("before_work", both=True)
            (vm,) = await _ask(s, customer, pid, "mokgong", [mok], today)
            (vd,) = await _ask(s, customer, pid, "dobae", [d1], today)
            await _quote(s, mok, vm, today)
            await _quote(s, d1, vd, today)
            mok_days = workdays(today + timedelta(days=28), 2, off)
            dobae_days = workdays(mok_days[-1] + timedelta(days=3), 3, off)
            seen = 0
            for visit_id, vendor, days in ((vm, mok, mok_days), (vd, d1, dobae_days)):
                deal_id, prop_id = await _dates(s, customer, pid, visit_id, vendor, days, today)
                out = await schedule_service.accept_dates(
                    s, customer, pid, deal_id, prop_id, AcceptIn(seen_version=seen), today
                )
                seen = out.final_version
            # 미리 만든 공사의 알림은 읽음으로 둔다 (시연을 시작할 때 종 숫자가 0이고, 이번 시연 알림만 새로 쌓인다)
            for user in (customer, *vendors.values()):
                await notify_service.mark_all_read(s, user.id)
    finally:
        await engine.dispose()  # 한 번 돌고 끝나는 스크립트 · 시험이 이벤트 루프를 새로 만든다
    return made


# ---- 자동 응답: 시연 업체 3곳이 할 일을 업체가 누르는 것과 같은 service 함수로 대신 한다 ----
# 실측 요청(업체가 아직 안 열어 봄) → 그 업체의 견적서 이미지 · 문서 금액으로 실측 견적서 보내기
# 확정 → 예상 공정표 자리에 공사 날짜 내기, 고객이 다른 날짜 · 바꾸기를 요청 → 고른 첫 시작일로 다시 내기
# 날짜는 계산 엔진, 금액은 업체 문서에 적힌 값(QUOTE_DOCS)을 그대로 쓰고, 여기서 셈하지 않는다.


def _span(first: date, days: int, weekend: tuple[int, ...]) -> tuple[date, date]:
    """first부터 일하는 날 days개의 첫날 · 끝날"""
    got = workdays(first, max(days, 1), days_off(first, first + timedelta(days=400)), weekend)
    return got[0], got[-1]


async def _reply_quote(s, vendor: User, visit_id, today: date) -> None:
    await _quote(s, vendor, visit_id, today)


async def _reply_dates(s, vendor: User, deal_id, today: date) -> None:
    job = await schedule_service.job_for(s, vendor, deal_id, today)
    weekend = () if job.weekend_work else WEEKEND
    tomorrow = today + timedelta(days=1)
    if job.dates_state == "waiting":
        crew = job.need.crew
        start, end = _span(max(job.plan_start or tomorrow, tomorrow), job.need.days, weekend)
        fit = (
            await schedule_service.preview_dates(s, vendor, deal_id, start, end, crew, today)
        ).fit
        if fit.conflicts and fit.start_from and fit.start_from > start:  # 앞 공정이 끝난 뒤로
            start, end = _span(fit.start_from, job.need.days, weekend)
        data = DatesIn(start=start, end=end, crew=crew, method="app")
    elif job.dates_state == "requested" and job.request and job.proposal:
        start, end = _span(min(job.request.wish_dates), job.proposal.workdays, weekend)
        data = DatesIn(
            start=start, end=end, crew=job.proposal.crew, method="app", request_id=job.request.id
        )
    elif job.dates_state == "change_requested" and job.request and job.request_options:
        o = job.request_options[0]
        data = DatesIn(
            start=o.start,
            end=o.end,
            crew=job.accepted_crew or job.need.crew,
            method="app",
            kind="change",
            request_id=job.request.id,
        )
    else:
        return
    await schedule_service.propose_dates(s, vendor, deal_id, data, today)


async def _todo(s, vendor_ids: list) -> list[tuple]:
    """할 일 (종류, id, 업체 id, 열린 날짜 요청 id): 안 열어 본 실측 요청 · 날짜를 낼 확정"""
    visits = select(Visit.id, Visit.vendor_id).where(
        Visit.vendor_id.in_(vendor_ids),
        Visit.status == "requested",
        Visit.vendor_seen_at.is_(None),
    )
    pending = (
        select(DateProposal.id)
        .where(DateProposal.deal_id == Deal.id, DateProposal.status == "pending")
        .exists()
    )
    asked = (
        select(DateRequest.id)
        .where(DateRequest.deal_id == Deal.id, DateRequest.status == "open")
        .limit(1)
        .scalar_subquery()
    )
    deals = select(Deal.id, Deal.vendor_id, asked).where(
        Deal.vendor_id.in_(vendor_ids),
        or_(
            and_(Deal.status == "confirmed", ~pending, asked.is_(None)),
            and_(Deal.status.in_(("confirmed", "scheduled")), asked.is_not(None)),
        ),
    )
    out = [("quote", i, vid, None) for i, vid in (await s.execute(visits)).all()]
    return out + [("dates", i, vid, r) for i, vid, r in (await s.execute(deals)).all()]


REPLY = {"quote": _reply_quote, "dates": _reply_dates}


async def auto_reply(today: date, skip: set) -> int:
    """한 번 돌면서 한 일의 수. 실패한 건은 skip에 넣어 같은 상태로는 다시 시도하지 않는다."""
    done = 0
    async with SessionLocal() as s:
        q = select(User).where(User.email.in_(list(VENDORS)), User.role == "vendor")
        vendors = {u.id: u for u in (await s.scalars(q)).all()}
        if not vendors:
            return 0
        for kind, row_id, vendor_id, req in await _todo(s, list(vendors)):
            key = (kind, row_id, req)
            if key in skip:
                continue
            try:
                await REPLY[kind](s, vendors[vendor_id], row_id, today)
                done += 1
            except AppError as exc:  # 그사이 고객이 바꿨거나 낼 수 없는 날짜 (로그에는 코드만)
                await s.rollback()
                skip.add(key)
                log.warning("시연 자동 응답을 건너뛰었어요: %s %s %s", kind, row_id, exc.code)
            except Exception:
                await s.rollback()
                skip.add(key)
                log.exception("시연 자동 응답이 실패했어요: %s %s", kind, row_id)
    return done


# 시연 계정 비밀번호: 로컬 개발 앱(localhost) · 테스트 전용 시험 값(사람의 진짜 비밀번호가 아니에요).
# DEMO_PASSWORD 환경변수가 있으면 그 값을 써요. 화면 · 로그 · 문서에는 이 값을 다시 적지 않아요(위치만 적어요).
DEMO_PASSWORD = "mjv3-4xf9hsnrhv"
DEMO_ENVS = ("local", "test")  # 시연 계정을 만들어도 되는 환경 (staging · production은 멈춤)


def demo_password() -> str:
    """DEMO_PASSWORD 환경변수 → 없으면 위 시험 값. 로컬 · 테스트가 아니면 멈춘다."""
    if settings.app_env not in DEMO_ENVS:
        raise SystemExit("시연 데이터는 로컬 개발(APP_ENV=local · test)에서만 만들어요.")
    return os.environ.get("DEMO_PASSWORD") or DEMO_PASSWORD


# ===================== v3 시연 데이터 (B6) =====================
# 이야기 J1 · J3 · J5 · J7 · J10 · J22(+ 대부분의 J)를 화면에서 바로 누를 수 있게 만든다:
#   단계마다 고객 한 명 · 공사 하나(기본 정보만 → 업체 담음 → 실측 요청 → 업체 기간 답함 → 날짜 확정 → 실측 끝
#   → 견적서 옴 → 업체 확정 → 공사 중 → 공사 끝 확인 기다림 → 리뷰), 9판 업체 6곳(승인 3 · 심사 기다림 · 보완 ·
#   거절), 관리자, 업체 별점을 만드는 지난 공사(이력 고객 7명).
# 날짜: 9판(요청 11.17 21:40 · 희망 12.7~12.21 · 업체 기간 12.13~12.18 · 실측 12.15 오전 · 공사 1.4~1.5)의
#   간격을 그대로 두고, 단계마다 '지금'이 그 단계가 되게 요청한 날(R)을 오늘에서 거꾸로 잡는다.
# 넣는 법: 화면이 부르는 service 함수(visit.flow · schedule · review · vendor · thread)를 지난 시각(now · today)으로
#   불러 상태 전이 · 계산 엔진 금액 · 알림 · 기한 일이 진짜와 같다. 사진 · 파일은 files.service로 올린다(직접 INSERT 없음).
#   마지막에 기한이 이미 지난 일(jobs)을 worker.sweep으로 거둬 둔다(worker를 켜도 상태가 갑자기 바뀌지 않게).
# 다시 돌리면: 이미 있는 계정은 건너뛴다(그 계정의 공사도 다시 만들지 않는다). 며칠 지나 기한(48시간 등)이 지나면
#   DB를 새로 만들어 다시 넣는다.

V3_STAGES = (  # (열쇠, 이메일 앞, 이름, 단계, 이야기)
    ("info", "c01", "김처음", "기본 정보만", "J1 · J3 · J18"),
    ("shortlist", "c02", "이담기", "업체 담음(2곳)", "J3 · J4"),
    ("requested", "c03", "박요청", "실측 요청 보냄(48시간 답 기다림)", "J5 · J6 · J10 끝 · J8"),
    ("offered", "c04", "최기간", "업체 기간 답함(날짜 고르기)", "J5 · J19"),
    ("booked", "c05", "정확정", "실측 날짜 확정 · 채팅", "J5 · J8 · J12 · J14 · J17"),
    ("visited", "c06", "강실측", "실측 끝(견적서 기다림)", "J7 앞"),
    ("quoted", "c07", "조견적", "실측 견적서 2곳 옴", "J7 · J20"),
    ("dealt", "c08", "윤확정", "업체 확정 · 공사 날짜 옴", "J7 · J15"),
    ("working", "c09", "장공사", "공사 중", "J22"),
    ("finished", "c10", "임끝남", "공사 끝 확인 기다림", "J22 끝"),
    ("reviewed", "c11", "한리뷰", "리뷰 씀(답글 기다림)", "J23 · J24"),
)
# 업체 별점(9판 C10: 마루와벽 ★4.8 (5) · 공사 5건, 결도배 ★4.7 (3) · 3건)을 만드는 지난 공사: (업체, 별점, 답글)
V3_HISTORY = (
    ("maru", 5, True),
    ("maru", 5, False),
    ("maru", 5, True),
    ("maru", 4, False),
    ("gyeol", 5, True),
    ("gyeol", 5, False),
    ("gyeol", 4, False),
)
V3_REVIEWS = {
    5: ("이음매가 거의 안 보여요. 약속한 날에 끝내 주셨어요.", ["seam", "on_time"]),
    4: ("깔끔했어요. 첫날 조금 늦게 오셨지만 미리 연락 주셨어요.", ["seam", "kind"]),
}
V3_HISTORY_NAMES = ("서하늘", "오준서", "남궁민", "배수아", "문지호", "권예린", "황도현")
V3_REPLY = "좋게 봐 주셔서 고마워요. 들뜨는 곳이 생기면 1년 안에 다시 붙여 드려요."
V3_HOUSE = {"sido": "seoul", "sgg": "노원구", "emd": "상계동", "area": Decimal("69.4")}
V3_QUOTE_PDF = sample_pdf(b"SAMPLE QUOTE - mitjip v3 demo data only")
V3_GAP = 48  # 9판: 요청(11.17) → 공사 시작(1.4)


def _mail(local: str) -> str:
    return f"v3-{local}@mitjip.test"


@dataclass(frozen=True)
class V3Vendor:
    key: str
    email: str
    biz: str
    owner: str
    biz_no: str  # 시험용 번호(실제로 없는 10자리)
    phone: str  # 시험용 번호
    trades: tuple[str, ...]
    areas: tuple[str, ...]  # 서울 구
    state: str  # approved · pending · needs_info · rejected
    code: str | None = None  # 보완 · 거절 사유
    note: str | None = None
    instant: bool = False  # 바로 확정
    prices: tuple[
        tuple[str, str, int | None, str | None], ...
    ] = ()  # (줄, 받는 법, 단가, 포함 대상)
    cases: tuple[tuple[str, str, int, str], ...] = ()  # 시공 사례 (제목, 공간, 평, 벽지)
    links: tuple[tuple[str, str], ...] = ()  # 외부 리뷰 링크 (이름, 주소)
    intro: str = ""


# 9판 업체: 마루와벽(C11 단가 · 바로 확정) · 결도배(D02 단가, 합지 '빼기') · 한결인테리어(단가 준비 중) ·
# 바른벽(A01 보완 요청) + 심사 기다림 · 거절 업체. 단가는 원 · 부가세 빼고(1201~1208)
V3_VENDORS = (
    V3Vendor(
        "maru", _mail("maru"), "마루와벽", "이마루", "9990003001", "01000003001", ("dobae",),
        ("노원구", "도봉구", "강북구", "성북구", "구로구"), "approved", instant=True,
        prices=(
            ("putty", "price", 20_000, None), ("paper_hapji", "price", 5_000, None),
            ("paper_silk", "price", 9_500, None), ("material", "price", 13_000, None),
            ("labor", "price", 300_000, None), ("meal", "price", 11_000, None),
            ("truck", "price", 50_000, None),
        ),
        cases=(
            ("상계동 32평 거실", "거실", 32, "basic"), ("상계동 32평 안방", "안방", 32, "basic"),
            ("중계동 24평 작은방", "작은방", 24, "economy"), ("창동 25평 거실", "거실", 25, "basic"),
            ("수유동 33평 안방", "안방", 33, "basic"), ("월계동 21평 작은방", "작은방", 21, "economy"),
        ),
        links=(("지도 리뷰(시연)", "https://example.com/maru/place"),
               ("블로그 후기(시연)", "https://example.com/maru/blog")),
        intro="노원 · 도봉에서 도배만 12년 했어요. 실측부터 마감까지 대표가 직접 와요.",
    ),
    V3Vendor(
        "gyeol", _mail("gyeol"), "결도배", "김결", "9990003002", "01000003002", ("dobae",),
        ("노원구", "도봉구", "구로구"), "approved",
        prices=(
            ("putty", "price", 15_000, None), ("paper_hapji", "excluded", None, None),
            ("paper_silk", "price", 9_000, None), ("material", "price", 12_000, None),
            ("labor", "price", 270_000, None), ("meal", "included", None, "labor"),
            ("truck", "price", 38_000, None),
        ),
        cases=(
            ("공릉동 34평 거실", "거실", 34, "basic"), ("하계동 25평 안방", "안방", 25, "basic"),
            ("상계동 24평 작은방", "작은방", 24, "basic"),
        ),
        intro="부부 둘이 하는 도배팀이에요. 실크 도배만 해요.",
    ),
    V3Vendor(
        "hangyeol", _mail("hangyeol"), "한결인테리어", "박한결", "9990003003", "01000003003",
        ("dobae", "mokgong"), ("노원구", "구로구"), "approved",
        cases=(("공릉동 28평 거실", "거실", 28, "basic"),),
        intro="도배와 목공을 같이 해요. 도배 단가표는 아직 준비 중이에요.",
    ),
    V3Vendor(
        "pending", _mail("daon"), "다온도배", "정다온", "9990003004", "01000003004", ("dobae",),
        ("노원구", "중랑구"), "pending",
        links=(("지도 리뷰(시연)", "https://example.com/daon/place"),),
    ),
    V3Vendor(
        "needs_info", _mail("bareun"), "바른벽", "최바른", "9990003005", "01000003005", ("dobae",),
        ("강북구",), "needs_info", code="blurry",
        note="사업자등록증 사진이 흐려서 글자가 안 보여요. 다시 올려 주세요.",
    ),
    V3Vendor(
        "rejected", _mail("neulbom"), "늘봄벽지", "한늘봄", "9990003006", "01000003006", ("dobae",),
        ("성북구",), "rejected", code="info_mismatch",
        note="신청한 상호가 사업자등록증과 달라요. 맞게 고쳐 다시 신청해 주세요.",
    ),
)  # fmt: skip
V3_ACTION = {"approved": "approve", "needs_info": "needs_info", "rejected": "reject"}


@dataclass
class V3Row:
    """시연 계정 한 줄 (verification/b6.md 표). urls는 화면 주소"""

    email: str
    role: str
    name: str
    stage: str
    stories: str
    urls: dict[str, str] = field(default_factory=dict)
    made: bool = True


def svc_today(now: datetime) -> date:
    return now.astimezone(SEOUL).date()


def _kst(day: date, hh: int = 9, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), SEOUL)


def _at(when: datetime):
    """앱 시계를 그 단계 시각으로 옮긴다(지금보다 뒤면 지금 그대로). 서비스는 그대로 부른다"""
    return clock.moved_to(when) if when < now_utc() else nullcontext()


_NOW_COLS: dict[type, tuple[str, ...]] = {}


def _now_cols(cls: type) -> tuple[str, ...]:
    """DB 기본값이 now()인 시각 칸(ORM 속성 이름)"""
    if cls not in _NOW_COLS:
        _NOW_COLS[cls] = tuple(
            p.key
            for p in sa_inspect(cls).column_attrs
            if "now()" in str(getattr(p.columns[0].server_default, "arg", ""))
        )
    return _NOW_COLS[cls]


def _fill_now(session, flush_context, instances) -> None:
    """시계를 옮긴 동안 ORM으로 넣는 행: DB 기본값 now() 칸을 앱 시계 값으로 채운다(시연 넣기 동안만 붙임).
    insert 문으로 넣는 알림 · 대화방 · 기한 일은 DB 시각 그대로다(b6.md 목록)."""
    if not clock.moved():
        return
    at = now_utc()
    for obj in session.new:
        for key in _now_cols(type(obj)):
            if getattr(obj, key, None) is None:
                setattr(obj, key, at)


def _upload(name: str, data: bytes, ctype: str) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data), filename=name, headers=Headers({"content-type": ctype})
    )


def _jpeg(seed: int, size: tuple[int, int] = (1200, 900)) -> bytes:
    """시연용 그림(벽지 줄무늬 + 가구 자리). 실제 집 사진이 아니다"""
    from PIL import Image, ImageDraw

    tones = ((236, 230, 220), (220, 228, 236), (232, 222, 230), (226, 236, 224), (240, 234, 214))
    base = tones[seed % len(tones)]
    img = Image.new("RGB", size, base)
    draw = ImageDraw.Draw(img)
    w, h = size
    line = tuple(c - 14 for c in base)
    for x in range(0, w, 48 + seed % 5 * 8):
        draw.line([(x, 0), (x, h)], fill=line, width=2)
    draw.rectangle([w // 5, h * 3 // 5, w * 4 // 5, h], fill=tuple(c - 50 for c in base))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


async def _put(s, owner: User, kind: str, data: bytes, ctype: str, name: str) -> uuid.UUID:
    """파일 올리기와 같은 길(files.service.prepare: 형식 검사 · 사진 위치 정보 지우기 → persist)"""
    prepared = await files_service.prepare(kind, _upload(name, data, ctype))
    row = await files_service.persist(s, owner.id, prepared)
    try:
        await s.commit()
    except Exception:
        files_service.discard(row)
        raise
    return row.id


def _workday_on_or_before(day: date, off: frozenset[date]) -> date:
    while is_off(day, off, WEEKEND):
        day -= timedelta(days=1)
    return day


def _workday_before(day: date, off: frozenset[date]) -> date:
    return _workday_on_or_before(day - timedelta(days=1), off)


async def _v3_vendor(s, spec: V3Vendor, password: str, admin: User, n: int, today: date):
    """가입 신청(vendor.signup) → 관리자 결정(vendor.review) → 승인이면 업체 정보 · 사례 · v3 단가 · 링크.
    이미 있는 계정이면 손대지 않는다. 돌려주는 값: (회원, 새로 만들었는지)"""
    user = await auth_repo.user_by_email(s, spec.email)
    if user is not None:
        return user, False
    data = VendorSignupIn(
        biz_name=spec.biz,
        owner_name=spec.owner,
        phone=spec.phone,
        trades=list(spec.trades),
        areas=[AreaIn(sido="seoul", sgg=g) for g in spec.areas],
        name=spec.owner,
        email=spec.email,
        password=password,
        biz_no=spec.biz_no,
    )
    lic = _upload("시연용-사업자등록증.pdf", LICENSE, "application/pdf")
    # 가입 요청 한도는 접속 주소마다 센다: 문서용 주소(TEST-NET-1)를 업체마다 따로 쓴다
    user, _ = await vendor_service.signup(s, data, lic, f"192.0.2.{10 + n}")
    if spec.links:
        links = [ReviewLinkIn(label=label, url=url) for label, url in spec.links]
        await vendor_service.save_review_links(s, user, ReviewLinksIn(links=links))
    if spec.state in V3_ACTION:
        decision = ReviewIn(action=V3_ACTION[spec.state], code=spec.code, note=spec.note)
        await vendor_service.review(s, admin, user.id, decision, today)
    if spec.state != "approved":
        return user, True
    prof = await vendor_service.get_profile(s, user)
    await vendor_service.save_profile(
        s,
        user,
        ProfileIn(
            updated_at=prof.updated_at,
            phone=spec.phone,
            address="서울 노원구 (시연용 주소)",
            trades=list(spec.trades),
            areas=[AreaIn(sido="seoul", sgg=g) for g in spec.areas],
            contact_method="phone",
            call_start=time(9),
            call_end=time(18),
            visit_fee=0,
            tax_kind="general",
            instant_default=spec.instant,
            visit_capacity=2,
            intro=spec.intro or None,
            faqs=[FaqIn(key=k, answer=a) for k, a in FAQS["dobae"].items()],
        ),
        today,
    )
    for i, (title, space, pyeong, paper) in enumerate(spec.cases):
        fid = await _put(s, user, "portfolio", _jpeg(n * 7 + i), "image/jpeg", f"사례-{i + 1}.jpg")
        case = PortfolioIn(
            title=title,
            space=space,
            pyeong=pyeong,
            paper=paper,
            taken_on=today - timedelta(days=40 * (i + 1)),
            file_id=fid,
        )
        await vendor_service.add_portfolio(s, user, case, today)
    if spec.prices:
        lines = [
            PriceLineV3In(key=k, mode=m, unit_price=p, included_in=inc)
            for k, m, p, inc in spec.prices
        ]
        await vendor_service.confirm_prices_v3(
            s, user, "dobae", ConfirmPricesIn(base_version=0, lines=lines)
        )
    for sort in range(len(spec.links)):  # 관리자가 열어 보고 '보여요'로 확인한 링크
        await vendor_service.set_review_link_status(
            s, user.id, sort, "shown", by=admin.id, at=now_utc()
        )
    await s.commit()
    return user, True


async def build_dobae_vendor_samples(password: str) -> list[tuple[str, uuid.UUID, bool]]:
    """1-4 업체 이름 조회 시험용: 기존 V3 승인 도배 업체 3곳만 등록. 로컬/테스트 전용·재실행 안전."""
    if settings.app_env not in DEMO_ENVS:
        raise SystemExit("시연 데이터는 로컬 개발(APP_ENV=local · test)에서만 만들어요.")
    await _fresh_redis()
    try:
        async with SessionLocal() as s:
            admin = await auth_repo.user_by_email(s, _mail("admin"))
            if admin is None:
                admin = await auth_service.create_user(
                    s, email=_mail("admin"), name="믿집 관리자", password=password, role="admin"
                )
                await s.commit()
            rows = []
            for n, spec in enumerate(v for v in V3_VENDORS if v.state == "approved"):
                vendor, made = await _v3_vendor(s, spec, password, admin, n, today_seoul())
                # 이미 만든 시연 업체도 새 활동 지역만 추가한다. 기존 지역/공종/프로필은 그대로 둔다.
                if not made:
                    _, registered = await vendor_repo.trades_and_areas(s, [vendor.id])
                    existing = set(registered[vendor.id])
                    for sgg in spec.areas:
                        if ("seoul", sgg) not in existing:
                            s.add(VendorArea(vendor_id=vendor.id, sido="seoul", sgg=sgg))
                    await s.commit()
                rows.append((spec.biz, vendor.id, made))
            return rows
    finally:
        await engine.dispose()
        await _fresh_redis()


@dataclass
class _Trip:
    """고객 한 명의 공사 하나. R = 9판 요청 날(11.17)에 맞춘 날"""

    s: object
    cust: User
    pid: uuid.UUID
    R: date
    vendors: dict[str, User]
    off: frozenset[date]
    visits: dict[str, uuid.UUID] = field(default_factory=dict)
    threads: dict[str, uuid.UUID] = field(default_factory=dict)
    deal: uuid.UUID | None = None
    review: uuid.UUID | None = None

    def day(self, k: int) -> date:
        return self.R + timedelta(days=k)


async def _v3_customer(s, local: str, name: str, password: str) -> tuple[User, bool]:
    email = _mail(local)
    user = await auth_repo.user_by_email(s, email)
    if user is not None:
        return user, False
    user = await auth_service.create_user(
        s, email=email, name=name, password=password, role="customer"
    )
    await s.commit()
    return user, True


async def _v3_project(s, cust: User, made_on: date, start: date) -> uuid.UUID:
    """9판 집: 노원구 상계동 · 공급 69.4㎡ · 빈 집 · 실크 · 공사 시작 희망일"""
    data = HomeInfoIn(
        sido=V3_HOUSE["sido"],
        sgg=V3_HOUSE["sgg"],
        emd=V3_HOUSE["emd"],
        area_m2=V3_HOUSE["area"],
        area_basis="supply",
        occupied=False,
        grade="basic",
        start_date=start,
    )
    return (await project_service.create(s, cust, data, made_on)).id


async def _trip(s, cust, R: date, vendors, off, keys: tuple[str, ...]) -> _Trip:
    """공사 만들기(R - 1일 저녁) → 업체 담기"""
    with _at(_kst(R - timedelta(days=1), 20)):
        pid = await _v3_project(s, cust, R - timedelta(days=1), R + timedelta(days=V3_GAP))
        t = _Trip(s=s, cust=cust, pid=pid, R=R, vendors=vendors, off=off)
        for k in keys:
            await visit_flow.add_shortlist(
                s, cust, pid, ShortlistIn(trade="dobae", vendor_id=vendors[k].id)
            )
    return t


async def _ask_v3(t: _Trip, keys: tuple[str, ...], at: datetime, photos: bool = False) -> None:
    """실측 요청 보내기 (희망 기간 R+20 ~ R+34, 9판 12.7 ~ 12.21)"""
    with _at(at):
        shots = []
        if photos:  # 9판 V03 '사진 2장'
            for i, label in enumerate(("거실 벽", "작은방 창가")):
                fid = await _put(
                    t.s, t.cust, "photo", _jpeg(20 + i), "image/jpeg", f"사진-{i + 1}.jpg"
                )
                shots.append(VisitPhotoIn(file_id=fid, label=label))
        data = VisitCreateIn(
            trade="dobae",
            vendor_ids=[t.vendors[k].id for k in keys],
            wish_start=t.day(20),
            wish_end=t.day(34),
            message="빈 집이에요. 작은방 창가에 곰팡이 자국이 조금 있어요." if photos else None,
            photos=shots,
        )
        out = await visit_flow.request(t.s, t.cust, t.pid, data, at)
    if out.failed:
        raise RuntimeError("시연 업체에 실측을 요청하지 못했어요.")
    by = {v.vendor_id: v.id for v in out.created}
    t.visits.update({k: by[t.vendors[k].id] for k in keys})


async def _answer(t: _Trip, key: str, at: datetime) -> None:
    """업체 기간 답하기: 마루와벽 R+26 ~ R+31(바로 확정) · 결도배 R+27 ~ R+32(수락)"""
    first = 26 if key == "maru" else 27
    data = WindowIn(start=t.day(first), end=t.day(first + 5), instant=key == "maru")
    with _at(at):
        await visit_flow.answer_window(t.s, t.vendors[key], t.visits[key], data, at)


async def _pick_slot(t: _Trip, key: str, at: datetime, day: int | None = None) -> None:
    """고객 반나절 고르기: 마루와벽 R+28 오전(9판 12.15 오전) · 결도배 R+29 오후(12.16 오후)"""
    if key == "maru":
        slot = SlotIn(day=t.day(28 if day is None else day), half="am")
    else:
        slot = SlotIn(day=t.day(29 if day is None else day), half="pm")
    with _at(at):
        out = await visit_flow.pick(t.s, t.cust, t.pid, t.visits[key], slot, at)
    if out.thread_id:
        t.threads[key] = out.thread_id


async def _accept_slot(t: _Trip, key: str, at: datetime) -> None:
    with _at(at):
        out = await visit_flow.accept(t.s, t.vendors[key], t.visits[key], at)
    if out.thread_id:
        t.threads[key] = out.thread_id


async def _visited(t: _Trip, key: str) -> None:
    at = _kst(t.day(28), 13, 30) if key == "maru" else _kst(t.day(29), 18, 30)
    with _at(at):
        await visit_flow.visited(t.s, t.cust, t.pid, t.visits[key], VisitedIn(result="visited"), at)


async def _quote_v3(t: _Trip, key: str) -> None:
    """실측 견적서 (9판 C33): 마루와벽 R+29 받음 · 326만 7,000원 · 2일 · 철거 · 폐기물 포함 · 하자 1년,
    결도배 R+30 받음 · 286만 원 · 3일 · 하자 6개월. 금액은 업체가 적은 그대로, 합계 · 부가세는 계산 엔진."""
    vendor = t.vendors[key]
    maru = key == "maru"
    name = "마루와벽_상계동_견적서.pdf" if maru else "결도배_견적서.pdf"
    today = t.day(29 if maru else 30)
    with _at(_kst(today, 10 if maru else 11)):
        fid = await _put(t.s, vendor, "quote_doc", V3_QUOTE_PDF, "application/pdf", name)
        data = QuoteDraftIn(
            file_ids=[fid],
            measured_on=t.day(28 if maru else 29),
            entered_amount=3_267_000 if maru else 2_860_000,
            vat_mode="included",
            days=2 if maru else 3,
            valid_until=t.day(44),
            incl_removal=maru,
            incl_waste=maru,
            paper_spec="LX 베스띠" if maru else None,
            warranty_months=12 if maru else 6,
        )
        await visit_service.save_quote(t.s, vendor, t.visits[key], data, today)
        await visit_service.send_quote(t.s, vendor, t.visits[key], today)


async def _deal_v3(t: _Trip, key: str, days: list[date]) -> None:
    """업체 확정(R+30 오후, 9판 12.17) → 업체가 공사 날짜 내기(같은 날 저녁)"""
    today = t.day(30)
    with _at(_kst(today, 14)):
        out = await schedule_service.confirm(
            t.s, t.cust, t.pid, t.visits[key], ConfirmIn(seen_ver=1), today
        )
    t.deal = out.deal.id
    dates = DatesIn(days=days, crew=2, method="app")
    with _at(_kst(today, 16)):
        await schedule_service.propose_dates(t.s, t.vendors[key], t.deal, dates, today)


async def _accept_work(t: _Trip, key: str) -> None:
    today = t.day(31)
    with _at(_kst(today, 9)):
        job = await schedule_service.job_for(t.s, t.vendors[key], t.deal, today)
        await schedule_service.accept_dates(
            t.s, t.cust, t.pid, t.deal, job.proposal.id, AcceptIn(seen_version=0), today
        )


async def _finish_v3(t: _Trip, key: str, last: date, today: date, photos: bool = True) -> None:
    """업체 '공사 끝냈어요'(V13, 그날 저녁): 끝낸 날 · 남길 말 · 공사 끝 사진 2장"""
    vendor = t.vendors[key]
    with _at(_kst(today, 18)):
        shots = [
            await _put(t.s, vendor, "done_photo", _jpeg(30 + i), "image/jpeg", f"끝-{i + 1}.jpg")
            for i in range(2 if photos else 0)
        ]
        note = "방 3곳 · 거실 다 끝냈어요. 남은 벽지 1롤은 베란다에 뒀어요." if photos else None
        data = DoneIn(done_on=last, note=note, photo_file_ids=shots)
        await schedule_service.finish(t.s, vendor, t.deal, today, data)


async def _done_review(t: _Trip, check_on: date, rating: int) -> None:
    """고객이 끝남 확인(그날 오전) → 바로 리뷰"""
    with _at(_kst(check_on, 10)):
        await schedule_service.check_work(
            t.s, t.cust, t.pid, t.deal, CheckIn(result="done"), check_on
        )
    body, tags = V3_REVIEWS[rating]
    with _at(_kst(check_on, 10, 5)):
        out = await review_service.write(
            t.s, t.cust, t.pid, t.deal, ReviewWriteIn(rating=rating, tags=tags, body=body)
        )
    t.review = out.id


async def _say(s, user: User, thread_id: uuid.UUID, body: str, at: datetime) -> None:
    with _at(at):
        await thread_service.post(
            s, user, thread_id, MessageIn(client_key=uuid.uuid4(), kind="text", body=body)
        )


def _work_days(t: _Trip, n: int) -> list[date]:
    """9판 공사 1.4 ~: R+48부터 일하는 날 n일 (계산 엔진)"""
    return workdays(t.day(V3_GAP), n, t.off)


async def _v3_history(
    s, i: int, key: str, rating: int, reply: bool, vendors, off, today, password
) -> bool:
    """업체 별점을 만드는 지난 공사 하나(요청 → 실측 → 견적서 → 확정 → 공사 → 끝 → 리뷰).
    돌려주는 값: 새로 만들었는지"""
    cust, made = await _v3_customer(s, f"h{i + 1:02d}", V3_HISTORY_NAMES[i], password)
    if not made:
        return False
    R = today - timedelta(days=120 + 24 * i)
    t = await _trip(s, cust, R, vendors, off, (key,))
    await _ask_v3(t, (key,), _kst(R, 21, 40))
    await _answer(t, key, _kst(t.day(1), 8, 30))
    await _pick_slot(t, key, _kst(t.day(1), 12))
    if key != "maru":
        await _accept_slot(t, key, _kst(t.day(1), 18))
    await _visited(t, key)
    await _quote_v3(t, key)
    days = _work_days(t, 2 if key == "maru" else 3)
    await _deal_v3(t, key, days)
    await _accept_work(t, key)
    await _finish_v3(t, key, days[-1], days[-1], photos=False)
    await _done_review(t, days[-1] + timedelta(days=1), rating)
    if reply:  # 리뷰 다음 날 아침 답글
        with _at(_kst(days[-1] + timedelta(days=2), 9)):
            await review_service.reply(s, vendors[key], t.review, ReplyIn(body=V3_REPLY))
    return True


async def _v3_stage(s, key: str, cust: User, vendors, off, now: datetime) -> dict[str, str]:
    """단계 하나를 만든다. 돌려주는 값: 바로 갈 화면 주소"""
    today = svc_today(now)
    both = ("maru", "gyeol")
    if key == "info":
        pid = await _v3_project(s, cust, today, today + timedelta(days=V3_GAP))
        return {"고객": f"/projects/{pid}/standard"}
    if key == "shortlist":
        t = await _trip(s, cust, today + timedelta(days=1), vendors, off, both)
        return {"고객": f"/projects/{t.pid}/vendors"}
    if key == "requested":
        t = await _trip(s, cust, today, vendors, off, both)
        await _ask_v3(t, both, now - timedelta(minutes=3), photos=True)
        return {"고객": f"/projects/{t.pid}/visits",
                "마루와벽": f"/vendor/requests/{t.visits['maru']}",
                "결도배": f"/vendor/requests/{t.visits['gyeol']}"}  # fmt: skip
    if key == "offered":
        R = today - timedelta(days=1)
        t = await _trip(s, cust, R, vendors, off, both)
        await _ask_v3(t, both, _kst(R, 21, 40))
        await _answer(t, "maru", min(_kst(today, 8, 30), now - timedelta(minutes=2)))
        return {"고객": f"/projects/{t.pid}/visits/{t.visits['maru']}",
                "결도배": f"/vendor/requests/{t.visits['gyeol']}"}  # fmt: skip
    R = {
        "booked": today - timedelta(days=2),
        "visited": today - timedelta(days=29),
        "quoted": today - timedelta(days=31),
        "dealt": today - timedelta(days=31),
    }.get(key)
    if key in ("working", "finished", "reviewed"):
        last = {
            "working": _workday_on_or_before(today, off),
            "finished": _workday_before(today, off),
            "reviewed": _workday_before(today - timedelta(days=2), off),
        }[key]
        first = _workday_before(last, off)
        R = first - timedelta(days=V3_GAP)
    keys = ("maru",) if key in ("working", "finished", "reviewed") else both
    t = await _trip(s, cust, R, vendors, off, keys)
    await _ask_v3(t, keys, _kst(R, 21, 40))
    for k in keys:
        await _answer(t, k, _kst(t.day(1), 8, 30 if k == "maru" else 50))
    for k in keys:  # 실측 끝 단계: 결도배 실측은 내일 오후(전날 알림이 제때 가게)
        later = 30 if key == "visited" and k == "gyeol" else None
        await _pick_slot(t, k, _kst(t.day(1), 12, 0 if k == "maru" else 5), later)
    if "gyeol" in keys and key != "booked":
        await _accept_slot(t, "gyeol", _kst(t.day(1), 18))
    if key == "booked":  # 결도배는 고객이 고른 날을 수락하기 전(J12). 대화는 날짜 확정 30분 뒤
        await _say(
            s,
            vendors["maru"],
            t.threads["maru"],
            "실측 날 오전 9시쯤 도착해요. 출발할 때 연락드릴게요.",
            _kst(t.day(1), 12, 30),
        )
        await _say(
            s,
            cust,
            t.threads["maru"],
            "네, 관리사무소에는 미리 말해 둘게요.",
            _kst(t.day(1), 12, 40),
        )
        return {"고객": f"/projects/{t.pid}/visits/{t.visits['maru']}",
                "채팅": f"/chat/{t.threads['maru']}",
                "결도배": f"/vendor/requests/{t.visits['gyeol']}"}  # fmt: skip
    await _visited(t, "maru")
    if key == "visited":  # 결도배 실측은 내일 오후
        return {"고객": f"/projects/{t.pid}/visits",
                "마루와벽": f"/vendor/requests/{t.visits['maru']}/quote"}  # fmt: skip
    if "gyeol" in keys:
        await _visited(t, "gyeol")
    await _quote_v3(t, "maru")
    if "gyeol" in keys:
        await _quote_v3(t, "gyeol")
    if key == "quoted":
        return {"고객": f"/projects/{t.pid}/quotes"}
    days = (
        _work_days(t, 2)
        if key == "dealt"
        else [first, last]  # 공사 중 · 끝: 오늘(또는 지난 일하는 날)까지
    )
    await _deal_v3(t, "maru", days)
    if key == "dealt":
        return {"고객": f"/projects/{t.pid}/schedule",
                "마루와벽": f"/vendor/jobs/{t.deal}"}  # fmt: skip
    await _accept_work(t, "maru")
    if key == "working":
        return {"고객": f"/projects/{t.pid}/schedule",
                "마루와벽": f"/vendor/jobs/{t.deal}"}  # fmt: skip
    if key == "finished":  # 마지막 공사일 저녁에 끝냈다고 올림
        await _finish_v3(t, "maru", last, last)
        return {"고객": f"/projects/{t.pid}/schedule",
                "마루와벽": f"/vendor/jobs/{t.deal}"}  # fmt: skip
    await _finish_v3(t, "maru", last, last)
    await _done_review(t, last + timedelta(days=1), 5)
    return {"고객": f"/projects/{t.pid}/review", "마루와벽": "/vendor/reviews"}


async def build_v3(password: str, now: datetime | None = None) -> list[V3Row]:
    """v3 시연 계정 · 공사를 만든다. 이미 있는 계정은 건너뛴다. 돌려주는 값: verification 표 줄"""
    from app import worker  # 조립 층: 기한 일 처리 함수 표

    if settings.app_env not in DEMO_ENVS:  # staging · production에서는 시연 계정을 만들지 않는다
        raise SystemExit("시연 데이터는 로컬 개발(APP_ENV=local · test)에서만 만들어요.")
    now = now or now_utc()
    today = svc_today(now)
    off = days_off(today - timedelta(days=400), today + timedelta(days=400))
    rows: list[V3Row] = []
    await _fresh_redis()  # 앞선 이벤트 루프에서 연 연결을 이 루프에서 쓰지 않게
    event.listen(Session, "before_flush", _fill_now)  # 넣는 동안만: 옮긴 시계로 DB 기본 시각 채우기
    try:
        async with SessionLocal() as s:
            admin = await auth_repo.user_by_email(s, _mail("admin"))
            made = admin is None
            if admin is None:
                admin = await auth_service.create_user(
                    s, email=_mail("admin"), name="믿집 관리자", password=password, role="admin"
                )
                await s.commit()
            rows.append(V3Row(_mail("admin"), "관리자", "믿집 관리자", "업체 심사 3건(심사 기다림 · 보완 · 거절)",
                              "J10 · J13 · J21", {"관리자": "/admin/vendors"}, made))  # fmt: skip
            vendors: dict[str, User] = {}
            for n, spec in enumerate(V3_VENDORS):
                user, made = await _v3_vendor(s, spec, password, admin, n, today)
                vendors[spec.key] = user
                rows.append(V3Row(spec.email, "업체", spec.biz, V3_VENDOR_STAGE[spec.key],
                                  V3_VENDOR_STORY[spec.key], {"업체": V3_VENDOR_URL[spec.key]}, made))  # fmt: skip
            fresh = any(r.made for r in rows)
            for i, (key, rating, reply) in enumerate(V3_HISTORY):
                fresh |= await _v3_history(s, i, key, rating, reply, vendors, off, today, password)
            # 지난 일 알림은 읽음으로 둔다(시연을 시작할 때 업체 종 숫자는 단계 공사의 알림만).
            # 이번에 만든 것이 있을 때만: 다시 돌리면 아무것도 바꾸지 않는다
            if fresh:
                for user in [admin, vendors["maru"], vendors["gyeol"]]:  # 심사 · 승인 알림은 남긴다
                    await notify_service.mark_all_read(s, user.id)
                for i in range(len(V3_HISTORY)):
                    h = await auth_repo.user_by_email(s, _mail(f"h{i + 1:02d}"))
                    if h is not None:
                        await notify_service.mark_all_read(s, h.id)
            got: dict[str, V3Row] = {}
            last = [x for x in V3_STAGES if x[0] == "requested"]
            for key, local, name, stage, stories in [
                *(x for x in V3_STAGES if x not in last),
                *last,
            ]:
                cust, made = await _v3_customer(s, local, name, password)
                if key == "requested" and made:  # 업체 종 숫자는 이 단계의 새 요청 알림만 남긴다
                    for user in (vendors["maru"], vendors["gyeol"]):
                        await notify_service.mark_all_read(s, user.id)
                urls = await _v3_stage(s, key, cust, vendors, off, now) if made else {}
                got[key] = V3Row(_mail(local), "고객", name, stage, stories, urls, made)
            rows += [got[x[0]] for x in V3_STAGES]
        # 기한이 이미 지난 일을 거둔다(지난 단계의 전날 알림 · 기한 등). 만든 것이 없으면 건너뛴다
        for _ in range(MAX_SWEEP if any(r.made for r in rows) else 0):
            if not await worker.sweep(now_utc()):
                break
    finally:  # 한 번 돌고 끝나는 스크립트 · 시험이 이벤트 루프를 새로 만든다: 이 루프에서 연 연결을 닫는다
        event.remove(Session, "before_flush", _fill_now)
        await engine.dispose()
        await _fresh_redis()
    return rows


async def _fresh_redis() -> None:
    """요청 제한 · 화면 신호가 쓰는 공용 Redis 연결을 비운다(다른 이벤트 루프에서 연 연결을 버린다)"""
    pool = queue.redis.connection_pool
    with suppress(Exception):
        await pool.disconnect()
    pool.reset()


MAX_SWEEP = 50
V3_VENDOR_STAGE = {
    "maru": "승인 · 단가 확인(바로 확정) · 사례 6 · 리뷰 5",
    "gyeol": "승인 · 단가 확인(합지 빼기, 수락 방식) · 사례 3 · 리뷰 3",
    "hangyeol": "승인 · 단가 준비 중(가견적 없음)",
    "pending": "심사 기다림 · 외부 리뷰 링크 1",
    "needs_info": "보완 요청(등록증이 흐려요)",
    "rejected": "거절(상호가 등록증과 달라요)",
}
V3_VENDOR_STORY = {
    "maru": "J5 · J7 · J11 · J17 · J22 · J23",
    "gyeol": "J5 · J7 · J12 · J20",
    "hangyeol": "J10(승인 뒤 처음 할 일 · 단가 넣기)",
    "pending": "J10 · J13(관리자 심사)",
    "needs_info": "J10(보완 → 다시 보내기)",
    "rejected": "J21(거절 → 다시 신청)",
}
V3_VENDOR_URL = {
    "maru": "/vendor",
    "gyeol": "/vendor",
    "hangyeol": "/vendor/prices/dobae",
    "pending": "/vendor",
    "needs_info": "/vendor",
    "rejected": "/vendor",
}

LABEL = {
    "pick": "업체 고르기",
    "visit_requested": "실측 요청",
    "quotes_arrived": "견적서 옴",
    "dates_received": "공사 날짜 옴",
    "before_work": "확정 공정표",
}


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    password = demo_password()  # 로컬 · 테스트가 아니면 여기서 멈춘다
    if len(password) < 8:
        raise SystemExit("DEMO_PASSWORD는 8자 이상이어야 해요.")
    if "--vendors-only" in argv:
        for name, vendor_id, made in asyncio.run(build_dobae_vendor_samples(password)):
            print(f"{'추가' if made else '기존'}: {name} · /vendors/{vendor_id}")
        return
    if "--v2" in argv:
        made = asyncio.run(build(password, today_seoul()))
        for stage, label in LABEL.items():
            print(f"만들었어요: {label} {made[stage]}")
        return
    for row in asyncio.run(build_v3(password)):
        state = "만들었어요" if row.made else "이미 있어요"
        go = " · ".join(f"{k} {v}" for k, v in row.urls.items())
        print(
            f"{state}: {row.role} {row.email} ({row.name}) · {row.stage}"
            + (f" · {go}" if go else "")
        )


if __name__ == "__main__":
    main()
