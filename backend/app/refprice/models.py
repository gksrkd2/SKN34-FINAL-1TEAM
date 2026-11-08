"""전처리 자료 저장 모델. 적재·계산·검색은 별도 기능에서 수행한다."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.shared.ids import uuid7


class RefDatasetVersion(Base):
    """기준 자료·문서의 출처와 저장 버전."""

    __tablename__ = "ref_dataset_versions"
    __table_args__ = (
        CheckConstraint(
            "kind in ('floorplan', 'unit_price', 'productivity', 'wage', 'knowledge')", name="kind"
        ),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("length(btrim(dataset_key)) > 0", name="dataset_key"),
        CheckConstraint("length(btrim(source)) > 0", name="source"),
        CheckConstraint("length(btrim(processed_path)) > 0", name="processed_path"),
        CheckConstraint("length(btrim(build_profile)) > 0", name="build_profile"),
        CheckConstraint("processed_sha256 ~ '^[0-9a-f]{64}$'", name="processed_sha256"),
        CheckConstraint("jsonb_typeof(provenance) = 'object'", name="provenance"),
        CheckConstraint(
            "status in ('draft', 'approved', 'active', 'retired', 'rejected')", name="status"
        ),
        CheckConstraint(
            "(embedding_model is null) = (embedding_dimensions is null)", name="embedding_profile"
        ),
        CheckConstraint(
            "(kind = 'knowledge') = (embedding_model is not null)", name="embedding_kind"
        ),
        CheckConstraint(
            "embedding_model is null or length(btrim(embedding_model)) > 0", name="embedding_model"
        ),
        CheckConstraint(
            "embedding_dimensions is null or embedding_dimensions between 1 and 16000",
            name="embedding_dimensions",
        ),
        UniqueConstraint("id", "kind", name="uq_ref_dataset_versions_kind"),
        UniqueConstraint("dataset_key", "version", name="uq_ref_dataset_versions_version"),
        UniqueConstraint(
            "dataset_key",
            "processed_sha256",
            "build_profile",
            "embedding_model",
            "embedding_dimensions",
            name="uq_ref_dataset_versions_content",
            postgresql_nulls_not_distinct=True,
        ),
        Index(
            "uq_ref_dataset_versions_active",
            "dataset_key",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, primary_key=True, default=uuid7)
    dataset_key: Mapped[str] = mapped_column(Text(), nullable=False)
    kind: Mapped[str] = mapped_column(Text(), nullable=False)
    version: Mapped[int] = mapped_column(Integer(), nullable=False)
    source: Mapped[str] = mapped_column(Text(), nullable=False)
    processed_path: Mapped[str] = mapped_column(Text(), nullable=False)
    processed_sha256: Mapped[str] = mapped_column(Text(), nullable=False)
    build_profile: Mapped[str] = mapped_column(Text(), nullable=False)
    effective_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    historical: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    current_rule_usable: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    calculation_usable: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    reference_usable: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    embedding_model: Mapped[str | None] = mapped_column(Text(), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(
        Text(), nullable=False, default="draft", server_default="draft"
    )


class FloorplanRow(Base):
    """예시 평면도 물량과 조건."""

    __tablename__ = "ref_floorplan_rows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dataset_id", "dataset_kind"],
            ["ref_dataset_versions.id", "ref_dataset_versions.kind"],
            ondelete="RESTRICT",
            name="fk_ref_floorplan_rows_version",
        ),
        CheckConstraint("dataset_kind = 'floorplan'", name="dataset_kind"),
        CheckConstraint("length(btrim(record_key)) > 0", name="record_key"),
        CheckConstraint(
            "record_type in ('page_context', 'summary', 'detail', 'total', 'conditional_total')",
            name="record_type",
        ),
        CheckConstraint("page > 0", name="page"),
        CheckConstraint(
            "normalized_value is null or normalized_value not in ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="normalized_finite",
        ),
        CheckConstraint(
            "normalized_value is null or normalized_value >= 0", name="normalized_value"
        ),
        Index("ix_ref_floorplan_rows_lookup", "dataset_id", "apartment_type", "quantity_item"),
        ForeignKeyConstraint(
            ["dataset_id", "parent_key"],
            ["ref_floorplan_rows.dataset_id", "ref_floorplan_rows.record_key"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_ref_floorplan_rows_parent",
        ),
        CheckConstraint("parent_key is null or parent_key <> record_key", name="parent_key"),
        Index(
            "ix_ref_floorplan_rows_parent",
            "dataset_id",
            "parent_key",
            postgresql_where=text("parent_key is not null"),
        ),
    )

    dataset_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, primary_key=True)
    dataset_kind: Mapped[str] = mapped_column(
        Text(), nullable=False, default="floorplan", server_default="floorplan"
    )
    record_key: Mapped[str] = mapped_column(Text(), nullable=False, primary_key=True)
    parent_key: Mapped[str | None] = mapped_column(Text(), nullable=True)
    record_type: Mapped[str] = mapped_column(Text(), nullable=False)
    apartment_type: Mapped[str | None] = mapped_column(Text(), nullable=True)
    space: Mapped[str | None] = mapped_column(Text(), nullable=True)
    quantity_item: Mapped[str | None] = mapped_column(Text(), nullable=True)
    original_value: Mapped[str] = mapped_column(Text(), nullable=False)
    normalized_value: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    normalized_unit: Mapped[str | None] = mapped_column(Text(), nullable=True)
    condition: Mapped[str | None] = mapped_column(Text(), nullable=True)
    aggregation_role: Mapped[str] = mapped_column(Text(), nullable=False)
    page: Mapped[int] = mapped_column(Integer(), nullable=False)


class UnitPriceRow(Base):
    """일위대가 작업·구성 수량."""

    __tablename__ = "ref_unit_price_rows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dataset_id", "dataset_kind"],
            ["ref_dataset_versions.id", "ref_dataset_versions.kind"],
            ondelete="RESTRICT",
            name="fk_ref_unit_price_rows_version",
        ),
        CheckConstraint("dataset_kind = 'unit_price'", name="dataset_kind"),
        CheckConstraint("length(btrim(record_key)) > 0", name="record_key"),
        CheckConstraint(
            "record_type in ('auxiliary_row', 'work_header', 'layout_row', 'component', 'total', 'note')",
            name="record_type",
        ),
        CheckConstraint(
            "normalized_value is null or normalized_value not in ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="normalized_finite",
        ),
        Index("ix_ref_unit_price_rows_lookup", "dataset_id", "work_code"),
        ForeignKeyConstraint(
            ["dataset_id", "parent_key"],
            ["ref_unit_price_rows.dataset_id", "ref_unit_price_rows.record_key"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_ref_unit_price_rows_parent",
        ),
        CheckConstraint("parent_key is null or parent_key <> record_key", name="parent_key"),
        Index(
            "ix_ref_unit_price_rows_parent",
            "dataset_id",
            "parent_key",
            postgresql_where=text("parent_key is not null"),
        ),
    )

    dataset_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, primary_key=True)
    dataset_kind: Mapped[str] = mapped_column(
        Text(), nullable=False, default="unit_price", server_default="unit_price"
    )
    record_key: Mapped[str] = mapped_column(Text(), nullable=False, primary_key=True)
    parent_key: Mapped[str | None] = mapped_column(Text(), nullable=True)
    record_type: Mapped[str] = mapped_column(Text(), nullable=False)
    trade: Mapped[str | None] = mapped_column(Text(), nullable=True)
    work_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    work_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    specification: Mapped[str | None] = mapped_column(Text(), nullable=True)
    component_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    normalized_value: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    normalized_unit: Mapped[str | None] = mapped_column(Text(), nullable=True)
    basis_normalized_unit: Mapped[str | None] = mapped_column(Text(), nullable=True)
    condition: Mapped[str | None] = mapped_column(Text(), nullable=True)


class ProductivityRow(Base):
    """품셈의 수치·단위·표 문맥."""

    __tablename__ = "ref_productivity_rows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dataset_id", "dataset_kind"],
            ["ref_dataset_versions.id", "ref_dataset_versions.kind"],
            ondelete="RESTRICT",
            name="fk_ref_productivity_rows_version",
        ),
        CheckConstraint("dataset_kind = 'productivity'", name="dataset_kind"),
        CheckConstraint("length(btrim(record_key)) > 0", name="record_key"),
        CheckConstraint(
            "record_type in ('page_context', 'table', 'table_cell', 'work_heading', 'cross_reference')",
            name="record_type",
        ),
        CheckConstraint("page > 0", name="page"),
        CheckConstraint("table_row is null or table_row > 0", name="table_row"),
        CheckConstraint("table_column is null or table_column > 0", name="table_column"),
        CheckConstraint(
            "normalized_value is null or normalized_value not in ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="normalized_finite",
        ),
        Index("ix_ref_productivity_rows_page", "dataset_id", "page"),
        ForeignKeyConstraint(
            ["dataset_id", "parent_key"],
            ["ref_productivity_rows.dataset_id", "ref_productivity_rows.record_key"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_ref_productivity_rows_parent",
        ),
        CheckConstraint("parent_key is null or parent_key <> record_key", name="parent_key"),
        Index(
            "ix_ref_productivity_rows_parent",
            "dataset_id",
            "parent_key",
            postgresql_where=text("parent_key is not null"),
        ),
    )

    dataset_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, primary_key=True)
    dataset_kind: Mapped[str] = mapped_column(
        Text(), nullable=False, default="productivity", server_default="productivity"
    )
    record_key: Mapped[str] = mapped_column(Text(), nullable=False, primary_key=True)
    parent_key: Mapped[str | None] = mapped_column(Text(), nullable=True)
    record_type: Mapped[str] = mapped_column(Text(), nullable=False)
    division: Mapped[str] = mapped_column(Text(), nullable=False)
    trade: Mapped[str | None] = mapped_column(Text(), nullable=True)
    work_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    work_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    basis: Mapped[str | None] = mapped_column(Text(), nullable=True)
    page: Mapped[int] = mapped_column(Integer(), nullable=False)
    table_row: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    table_column: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    column_context: Mapped[str | None] = mapped_column(Text(), nullable=True)
    row_context: Mapped[str | None] = mapped_column(Text(), nullable=True)
    normalized_value: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    normalized_unit: Mapped[str | None] = mapped_column(Text(), nullable=True)
    numeric_role: Mapped[str | None] = mapped_column(Text(), nullable=True)
    semantic_status: Mapped[str | None] = mapped_column(Text(), nullable=True)
    condition: Mapped[str | None] = mapped_column(Text(), nullable=True)
    source_text: Mapped[str | None] = mapped_column(Text(), nullable=True)


class WageRow(Base):
    """직종별 공표 임금과 원본 문맥."""

    __tablename__ = "ref_wage_rows"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dataset_id", "dataset_kind"],
            ["ref_dataset_versions.id", "ref_dataset_versions.kind"],
            ondelete="RESTRICT",
            name="fk_ref_wage_rows_version",
        ),
        CheckConstraint("dataset_kind = 'wage'", name="dataset_kind"),
        CheckConstraint("length(btrim(record_key)) > 0", name="record_key"),
        CheckConstraint(
            "record_type in ('page_context', 'table', 'table_cell', 'occupation_wage', 'occupation_definition')",
            name="record_type",
        ),
        CheckConstraint("page > 0", name="page"),
        CheckConstraint("basis_hours is null or basis_hours between 1 and 24", name="basis_hours"),
        CheckConstraint(
            "normalized_value is null or normalized_value >= 0", name="normalized_value"
        ),
        CheckConstraint("survey_flag is null or survey_flag in ('*', '**')", name="survey_flag"),
        CheckConstraint(
            "record_type <> 'occupation_wage' or (occupation_code is not null and length(btrim(occupation_code)) > 0 and publication_date is not null and basis_hours is not null)",
            name="published_fields",
        ),
        Index(
            "uq_ref_wage_rows_published",
            "dataset_id",
            "occupation_code",
            "publication_date",
            "basis_hours",
            unique=True,
            postgresql_where=text("record_type = 'occupation_wage'"),
        ),
        ForeignKeyConstraint(
            ["dataset_id", "parent_key"],
            ["ref_wage_rows.dataset_id", "ref_wage_rows.record_key"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_ref_wage_rows_parent",
        ),
        CheckConstraint("parent_key is null or parent_key <> record_key", name="parent_key"),
        Index(
            "ix_ref_wage_rows_parent",
            "dataset_id",
            "parent_key",
            postgresql_where=text("parent_key is not null"),
        ),
    )

    dataset_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, primary_key=True)
    dataset_kind: Mapped[str] = mapped_column(
        Text(), nullable=False, default="wage", server_default="wage"
    )
    record_key: Mapped[str] = mapped_column(Text(), nullable=False, primary_key=True)
    parent_key: Mapped[str | None] = mapped_column(Text(), nullable=True)
    record_type: Mapped[str] = mapped_column(Text(), nullable=False)
    occupation_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    occupation_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    publication_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    basis_hours: Mapped[int | None] = mapped_column(SmallInteger(), nullable=True)
    normalized_value: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    survey_flag: Mapped[str | None] = mapped_column(Text(), nullable=True)
    flag_reference_period: Mapped[str | None] = mapped_column(Text(), nullable=True)
    definition: Mapped[str | None] = mapped_column(Text(), nullable=True)
    condition: Mapped[str | None] = mapped_column(Text(), nullable=True)
    page: Mapped[int] = mapped_column(Integer(), nullable=False)
    source_text: Mapped[str] = mapped_column(Text(), nullable=False)


class RefDatasetEvent(Base):
    """버전 등록과 상태 변경 이력."""

    __tablename__ = "ref_dataset_events"
    __table_args__ = (
        ForeignKeyConstraint(["dataset_id"], ["ref_dataset_versions.id"], ondelete="RESTRICT"),
        CheckConstraint("event_no > 0", name="event_no"),
        CheckConstraint(
            "from_status is null or from_status in ('draft', 'approved', 'active', 'retired', 'rejected')",
            name="from_status",
        ),
        CheckConstraint(
            "to_status in ('draft', 'approved', 'active', 'retired', 'rejected')", name="to_status"
        ),
        CheckConstraint("from_status is distinct from to_status", name="status_change"),
        CheckConstraint("(event_no = 1) = (from_status is null)", name="initial_from"),
        CheckConstraint("event_no <> 1 or to_status = 'draft'", name="initial_to"),
        CheckConstraint("length(btrim(actor)) > 0", name="actor"),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="details"),
    )

    dataset_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, primary_key=True)
    event_no: Mapped[int] = mapped_column(Integer(), nullable=False, primary_key=True)
    from_status: Mapped[str | None] = mapped_column(Text(), nullable=True)
    to_status: Mapped[str] = mapped_column(Text(), nullable=False)
    actor: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False)
