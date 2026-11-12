import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.calc.schedule import JOB_MAX_DAYS
from app.db import Base
from app.shared.ids import uuid7


class Plan(Base):
    """공정표 한 버전. expected(예상: 집 정보로 계산) · final(확정: 업체 날짜를 수락한 뒤)."""

    __tablename__ = "plans"
    __table_args__ = (
        UniqueConstraint("project_id", "kind", "version", name="uq_plans_version"),
        CheckConstraint("kind in ('expected', 'final')", name="kind"),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("end_date >= start_date", name="dates"),
        CheckConstraint("jsonb_typeof(basis_snapshot) = 'object'", name="basis_snapshot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(10))
    version: Mapped[int] = mapped_column(SmallInteger)
    project_rev: Mapped[int] = mapped_column(SmallInteger)
    formula_ver: Mapped[str] = mapped_column(String(20))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    work_days: Mapped[int] = mapped_column(SmallInteger)
    weekend_work: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    basis_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")


class PlanStep(Base):
    """공정표 칸: 공정 작업 · 자연 건조 · 입주 청소"""

    __tablename__ = "plan_steps"
    __table_args__ = (
        CheckConstraint("kind in ('work', 'dry', 'clean')", name="kind"),
        CheckConstraint("trade is null or trade in ('dobae', 'mokgong')", name="trade"),
        CheckConstraint("(kind = 'work') = (trade is not null)", name="trade_kind"),
        CheckConstraint("end_date >= start_date and days >= 1", name="dates"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), index=True
    )
    trade: Mapped[str | None] = mapped_column(String(10))
    kind: Mapped[str] = mapped_column(String(10))
    label: Mapped[str] = mapped_column(String(40))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    days: Mapped[int] = mapped_column(SmallInteger)
    crew: Mapped[int | None] = mapped_column(SmallInteger)
    man_day: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    sort: Mapped[int] = mapped_column(SmallInteger)
    # 확정 공정표 칸이 어느 업체 확정의 공사인지 (예상 공정표 · 건조 · 청소는 비움)
    deal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("deals.id", ondelete="SET NULL"))


class PlanDay(Base):
    """공정표 날짜별 할 일(쉬는 날 포함)"""

    __tablename__ = "plan_days"
    __table_args__ = (CheckConstraint("kind in ('work', 'dry', 'clean', 'off')", name="kind"),)

    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    trade: Mapped[str | None] = mapped_column(String(10))
    kind: Mapped[str] = mapped_column(String(10))
    names: Mapped[list[str]] = mapped_column(ARRAY(String(60)), default=list, server_default="{}")


# 업체 확정 뒤 공사 상태: 확정(날짜 기다림) · 날짜 수락 · 공사 끝냈어요(고객 확인 기다림) · 남은 일 · 끝남 / 다시 고르기
DEAL_STATUSES = ("confirmed", "scheduled", "checking", "fixing", "done", "reselected")
OPEN_DEALS = ("confirmed", "scheduled", "checking", "fixing")  # 업체 작업함 '공사' 탭
_DEAL_STATUS_SQL = "('confirmed', 'scheduled', 'checking', 'fixing', 'done', 'reselected')"


class Deal(Base):
    """업체 확정: 고객이 고른 업체와 확정한 견적서 버전. 공정마다 살아 있는 확정은 하나다.

    확정한 견적서 버전(quote_id)은 바꾸지 않는다. 날짜 칸은 공사 날짜를 수락한 뒤 채운다.
    """

    __tablename__ = "deals"
    __table_args__ = (
        Index(
            "uq_deals_trade",
            "project_id",
            "trade",
            unique=True,
            postgresql_where=text("status <> 'reselected'"),
        ),
        CheckConstraint("trade in ('dobae', 'mokgong')", name="trade"),
        CheckConstraint(f"status in {_DEAL_STATUS_SQL}", name="status"),
        CheckConstraint("date_request_count between 0 and 2", name="date_request_count"),
        CheckConstraint(
            "(start_date is null) = (end_date is null)"
            " and (start_date is null or end_date >= start_date)",
            name="dates",
        ),
        # 날짜를 수락한 뒤(scheduled부터)에는 공사 날짜가 있다
        CheckConstraint(
            "status in ('confirmed', 'reselected') or start_date is not null", name="scheduled"
        ),
        # 공사 끝냈어요를 올린 뒤(checking · fixing · done)에만 끝낸 날이 있다
        CheckConstraint(
            "(status in ('checking', 'fixing', 'done')) = (work_done_on is not null)",
            name="work_done_on",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    trade: Mapped[str] = mapped_column(String(10))
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vendors.user_id", ondelete="SET NULL"), index=True
    )
    vendor_name: Mapped[str] = mapped_column(String(100))
    visit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("visits.id", ondelete="CASCADE"), unique=True
    )
    # 확정한 견적서 버전. 지우지 못하게 막는다(ON DELETE 없음)
    quote_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vendor_quotes.id"))
    status: Mapped[str] = mapped_column(String(10), default="confirmed", server_default="confirmed")
    dates_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 공사 날짜 낼 기한
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    work_done_on: Mapped[date | None] = mapped_column(Date)
    date_request_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DealEvent(Base):
    """업체 확정 상태 이력 (만들 때 없음 → confirmed)"""

    __tablename__ = "deal_events"
    __table_args__ = (
        CheckConstraint(
            f"from_status is null or from_status in {_DEAL_STATUS_SQL}", name="from_status"
        ),
        CheckConstraint(f"to_status in {_DEAL_STATUS_SQL}", name="to_status"),
        CheckConstraint("note is null or length(btrim(note)) > 0", name="note"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    deal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(10))
    to_status: Mapped[str] = mapped_column(String(10))
    by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(String(300))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# 공사 날짜: 업체가 낸 날짜(제안)와 고객의 다른 날짜 요청. 공정표 확정은 고객이 수락했을 때만
PROPOSAL_STATUSES = ("pending", "accepted", "superseded", "withdrawn")
REQUEST_STATUSES = ("open", "answered", "withdrawn")
CREW_MAX = 20  # 하루 작업 인원 한도
WISH_MAX = 3  # 다른 날짜 요청: 시작해도 되는 날 1~3개


class DateRequest(Base):
    """고객의 다른 날짜 요청: 시작해도 되는 날 1~3개 · 이유. 업체가 새 날짜를 내면 answered."""

    __tablename__ = "date_requests"
    __table_args__ = (
        Index(
            "uq_date_requests_open",
            "deal_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        CheckConstraint("status in ('open', 'answered', 'withdrawn')", name="status"),
        CheckConstraint(f"cardinality(wish_dates) between 1 and {WISH_MAX}", name="wish_dates"),
        CheckConstraint("reason is null or length(btrim(reason)) > 0", name="reason"),
        CheckConstraint("(status = 'open') = (closed_at is null)", name="closed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    deal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), index=True
    )
    reason: Mapped[str | None] = mapped_column(String(300))
    wish_dates: Mapped[list[date]] = mapped_column(ARRAY(Date))  # 서로 다른 날(service)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # 업체가 다시 낼 기한
    status: Mapped[str] = mapped_column(String(10), default="open", server_default="open")
    by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DateProposal(Base):
    """업체가 낸 공사 날짜. 수락 전까지 다시 낼 수 있고(앞 것은 superseded), 공정마다 대기는 하나."""

    __tablename__ = "date_proposals"
    __table_args__ = (
        Index(
            "uq_date_proposals_pending",
            "deal_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        CheckConstraint(
            "status in ('pending', 'accepted', 'superseded', 'withdrawn')", name="status"
        ),
        CheckConstraint("method in ('app', 'call')", name="method"),
        CheckConstraint("kind in ('initial', 'change')", name="kind"),
        CheckConstraint(
            f"end_date >= start_date and end_date - start_date <= {JOB_MAX_DAYS}", name="dates"
        ),
        CheckConstraint(f"crew between 1 and {CREW_MAX}", name="crew"),
        # 통화로 정했으면 통화한 날이 있다
        CheckConstraint("(method = 'call') = (call_on is not null)", name="call_on"),
        CheckConstraint("reason is null or length(btrim(reason)) > 0", name="reason"),
        # 확정한 날짜를 바꿀 때(change)는 이유가 있다. 고객 요청에 답한 것이면 요청에 이유가 있다
        CheckConstraint(
            "kind = 'initial' or reason is not null or request_id is not null",
            name="change_reason",
        ),
        CheckConstraint("plan_version is null or plan_version >= 1", name="plan_version"),
        CheckConstraint("(status = 'pending') = (decided_at is null)", name="decided"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    deal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), index=True
    )
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    crew: Mapped[int] = mapped_column(SmallInteger)
    method: Mapped[str] = mapped_column(String(10))
    call_on: Mapped[date | None] = mapped_column(Date)
    # 답한 다른 날짜 요청
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("date_requests.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(10), default="initial", server_default="initial")
    reason: Mapped[str | None] = mapped_column(String(300))
    plan_version: Mapped[int | None] = mapped_column(SmallInteger)  # 낼 때 본 예상 공정표 버전
    status: Mapped[str] = mapped_column(String(12), default="pending", server_default="pending")
    by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# 공사 중 추가 금액: 업체가 알리고 고객이 확인(ok) 또는 이야기하자(talk). 믿집은 기록 · 알림만 한다
ACKS = ("pending", "ok", "talk")
EXTRA_AMOUNT_MAX = 1_000_000_000  # 실측 견적서 금액 한도와 같다
EXTRA_PHOTOS_MAX = 5


class ExtraCharge(Base):
    """추가 금액 한 줄. 업체가 적은 금액 · 부가세 방식으로 계산 엔진(from_entered)이 공급가 · 부가세 ·
    합계를 만든다. 한 번 올리면 고치지 않는다(잘못 올렸으면 새로 올린다). 확정 금액 합계에 넣지 않는다."""

    __tablename__ = "extra_charges"
    __table_args__ = (
        CheckConstraint("length(btrim(title)) > 0", name="title"),
        CheckConstraint("length(btrim(reason)) > 0", name="reason"),
        CheckConstraint("vat_mode in ('included', 'excluded', 'exempt')", name="vat_mode"),
        CheckConstraint(f"entered_amount between 0 and {EXTRA_AMOUNT_MAX}", name="entered_amount"),
        CheckConstraint(
            "supply >= 0 and vat >= 0 and total = supply + vat"
            " and (vat_mode <> 'exempt' or vat = 0)"
            " and (case when vat_mode = 'excluded' then supply else total end) = entered_amount",
            name="amounts",
        ),
        CheckConstraint("ack in ('pending', 'ok', 'talk')", name="ack"),
        CheckConstraint("(ack = 'pending') = (acked_at is null)", name="acked"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    deal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(300))
    vat_mode: Mapped[str] = mapped_column(String(10))
    entered_amount: Mapped[int] = mapped_column(BigInteger)
    supply: Mapped[int] = mapped_column(BigInteger)
    vat: Mapped[int] = mapped_column(BigInteger)
    total: Mapped[int] = mapped_column(BigInteger)
    ack: Mapped[str] = mapped_column(String(10), default="pending", server_default="pending")
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExtraChargeFile(Base):
    """추가 금액에 붙인 공사 사진 (files.kind = work_photo). 사진 하나는 한 줄에만 붙는다."""

    __tablename__ = "extra_charge_files"
    __table_args__ = (CheckConstraint(f"sort between 0 and {EXTRA_PHOTOS_MAX - 1}", name="sort"),)

    extra_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extra_charges.id", ondelete="CASCADE"), primary_key=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), primary_key=True, unique=True
    )
    sort: Mapped[int] = mapped_column(SmallInteger)
