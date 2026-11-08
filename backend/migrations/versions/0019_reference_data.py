"""전처리 기준 자료·RAG 저장 스키마

Revision ID: 0019_reference_data
Revises: 0018_v2_checks
Create Date: 2026-11-09

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql

# 기존 revision은 수정하지 않고 다음 head로 연결한다.
revision: str = "0019_reference_data"
down_revision: str | Sequence[str] | None = "0018_v2_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """확장과 빈 테이블만 생성한다. 시드·전처리 적재는 하지 않는다."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    op.create_table(
        "ref_dataset_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("processed_path", sa.Text(), nullable=False),
        sa.Column("processed_sha256", sa.Text(), nullable=False),
        sa.Column("build_profile", sa.Text(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("historical", sa.Boolean(), nullable=False),
        sa.Column("current_rule_usable", sa.Boolean(), nullable=False),
        sa.Column("calculation_usable", sa.Boolean(), nullable=False),
        sa.Column("reference_usable", sa.Boolean(), nullable=False),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("embedding_dimensions", sa.Integer(), nullable=True),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.CheckConstraint(
            "(kind = 'knowledge') = (embedding_model is not null)",
            name=op.f("ck_ref_dataset_versions_embedding_kind"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(provenance) = 'object'", name=op.f("ck_ref_dataset_versions_provenance")
        ),
        sa.CheckConstraint(
            "kind in ('floorplan', 'unit_price', 'productivity', 'wage', 'knowledge')",
            name=op.f("ck_ref_dataset_versions_kind"),
        ),
        sa.CheckConstraint(
            "processed_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_ref_dataset_versions_processed_sha256"),
        ),
        sa.CheckConstraint(
            "status in ('draft', 'approved', 'active', 'retired', 'rejected')",
            name=op.f("ck_ref_dataset_versions_status"),
        ),
        sa.CheckConstraint(
            "(embedding_model is null) = (embedding_dimensions is null)",
            name=op.f("ck_ref_dataset_versions_embedding_profile"),
        ),
        sa.CheckConstraint(
            "embedding_dimensions is null or embedding_dimensions between 1 and 16000",
            name=op.f("ck_ref_dataset_versions_embedding_dimensions"),
        ),
        sa.CheckConstraint(
            "embedding_model is null or length(btrim(embedding_model)) > 0",
            name=op.f("ck_ref_dataset_versions_embedding_model"),
        ),
        sa.CheckConstraint(
            "length(btrim(build_profile)) > 0", name=op.f("ck_ref_dataset_versions_build_profile")
        ),
        sa.CheckConstraint(
            "length(btrim(dataset_key)) > 0", name=op.f("ck_ref_dataset_versions_dataset_key")
        ),
        sa.CheckConstraint(
            "length(btrim(processed_path)) > 0", name=op.f("ck_ref_dataset_versions_processed_path")
        ),
        sa.CheckConstraint(
            "length(btrim(source)) > 0", name=op.f("ck_ref_dataset_versions_source")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_ref_dataset_versions_version")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ref_dataset_versions")),
        sa.UniqueConstraint(
            "dataset_key",
            "processed_sha256",
            "build_profile",
            "embedding_model",
            "embedding_dimensions",
            name="uq_ref_dataset_versions_content",
            postgresql_nulls_not_distinct=True,
        ),
        sa.UniqueConstraint("dataset_key", "version", name="uq_ref_dataset_versions_version"),
        sa.UniqueConstraint("id", "kind", name="uq_ref_dataset_versions_kind"),
    )
    op.create_index(
        "uq_ref_dataset_versions_active",
        "ref_dataset_versions",
        ["dataset_key"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "rag_chunks",
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_kind", sa.Text(), server_default="knowledge", nullable=False),
        sa.Column("record_key", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("page_numbers", sa.ARRAY(sa.Integer()), nullable=False),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("embedding", VECTOR(), nullable=True),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_rag_chunks_content_hash")
        ),
        sa.CheckConstraint("dataset_kind = 'knowledge'", name=op.f("ck_rag_chunks_dataset_kind")),
        sa.CheckConstraint(
            "cardinality(page_numbers) > 0 and array_ndims(page_numbers) = 1 and array_position(page_numbers, null) is null and 0 < all(page_numbers)",
            name=op.f("ck_rag_chunks_page_numbers"),
        ),
        sa.CheckConstraint(
            "embedding is null or vector_norm(embedding) > 0", name=op.f("ck_rag_chunks_embedding")
        ),
        sa.CheckConstraint("length(btrim(content)) > 0", name=op.f("ck_rag_chunks_content")),
        sa.CheckConstraint("length(btrim(record_key)) > 0", name=op.f("ck_rag_chunks_record_key")),
        sa.ForeignKeyConstraint(
            ["dataset_id", "dataset_kind"],
            ["ref_dataset_versions.id", "ref_dataset_versions.kind"],
            name="fk_rag_chunks_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("dataset_id", "record_key", name=op.f("pk_rag_chunks")),
    )
    op.create_table(
        "ref_dataset_events",
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("event_no", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "event_no <> 1 or to_status = 'draft'", name=op.f("ck_ref_dataset_events_initial_to")
        ),
        sa.CheckConstraint(
            "from_status is null or from_status in ('draft', 'approved', 'active', 'retired', 'rejected')",
            name=op.f("ck_ref_dataset_events_from_status"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(details) = 'object'", name=op.f("ck_ref_dataset_events_details")
        ),
        sa.CheckConstraint(
            "to_status in ('draft', 'approved', 'active', 'retired', 'rejected')",
            name=op.f("ck_ref_dataset_events_to_status"),
        ),
        sa.CheckConstraint(
            "(event_no = 1) = (from_status is null)",
            name=op.f("ck_ref_dataset_events_initial_from"),
        ),
        sa.CheckConstraint("event_no > 0", name=op.f("ck_ref_dataset_events_event_no")),
        sa.CheckConstraint(
            "from_status is distinct from to_status",
            name=op.f("ck_ref_dataset_events_status_change"),
        ),
        sa.CheckConstraint("length(btrim(actor)) > 0", name=op.f("ck_ref_dataset_events_actor")),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["ref_dataset_versions.id"],
            name=op.f("fk_ref_dataset_events_dataset_id_ref_dataset_versions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("dataset_id", "event_no", name=op.f("pk_ref_dataset_events")),
    )
    op.create_table(
        "ref_floorplan_rows",
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_kind", sa.Text(), server_default="floorplan", nullable=False),
        sa.Column("record_key", sa.Text(), nullable=False),
        sa.Column("parent_key", sa.Text(), nullable=True),
        sa.Column("record_type", sa.Text(), nullable=False),
        sa.Column("apartment_type", sa.Text(), nullable=True),
        sa.Column("space", sa.Text(), nullable=True),
        sa.Column("quantity_item", sa.Text(), nullable=True),
        sa.Column("original_value", sa.Text(), nullable=False),
        sa.Column("normalized_value", sa.Numeric(), nullable=True),
        sa.Column("normalized_unit", sa.Text(), nullable=True),
        sa.Column("condition", sa.Text(), nullable=True),
        sa.Column("aggregation_role", sa.Text(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "dataset_kind = 'floorplan'", name=op.f("ck_ref_floorplan_rows_dataset_kind")
        ),
        sa.CheckConstraint(
            "normalized_value is null or normalized_value not in ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name=op.f("ck_ref_floorplan_rows_normalized_finite"),
        ),
        sa.CheckConstraint(
            "record_type in ('page_context', 'summary', 'detail', 'total', 'conditional_total')",
            name=op.f("ck_ref_floorplan_rows_record_type"),
        ),
        sa.CheckConstraint(
            "length(btrim(record_key)) > 0", name=op.f("ck_ref_floorplan_rows_record_key")
        ),
        sa.CheckConstraint(
            "normalized_value is null or normalized_value >= 0",
            name=op.f("ck_ref_floorplan_rows_normalized_value"),
        ),
        sa.CheckConstraint("page > 0", name=op.f("ck_ref_floorplan_rows_page")),
        sa.CheckConstraint(
            "parent_key is null or parent_key <> record_key",
            name=op.f("ck_ref_floorplan_rows_parent_key"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id", "dataset_kind"],
            ["ref_dataset_versions.id", "ref_dataset_versions.kind"],
            name="fk_ref_floorplan_rows_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id", "parent_key"],
            ["ref_floorplan_rows.dataset_id", "ref_floorplan_rows.record_key"],
            name="fk_ref_floorplan_rows_parent",
            ondelete="RESTRICT",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("dataset_id", "record_key", name=op.f("pk_ref_floorplan_rows")),
    )
    op.create_index(
        "ix_ref_floorplan_rows_lookup",
        "ref_floorplan_rows",
        ["dataset_id", "apartment_type", "quantity_item"],
        unique=False,
    )
    op.create_index(
        "ix_ref_floorplan_rows_parent",
        "ref_floorplan_rows",
        ["dataset_id", "parent_key"],
        unique=False,
        postgresql_where=sa.text("parent_key is not null"),
    )
    op.create_table(
        "ref_productivity_rows",
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_kind", sa.Text(), server_default="productivity", nullable=False),
        sa.Column("record_key", sa.Text(), nullable=False),
        sa.Column("parent_key", sa.Text(), nullable=True),
        sa.Column("record_type", sa.Text(), nullable=False),
        sa.Column("division", sa.Text(), nullable=False),
        sa.Column("trade", sa.Text(), nullable=True),
        sa.Column("work_code", sa.Text(), nullable=True),
        sa.Column("work_name", sa.Text(), nullable=True),
        sa.Column("basis", sa.Text(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("table_row", sa.Integer(), nullable=True),
        sa.Column("table_column", sa.Integer(), nullable=True),
        sa.Column("column_context", sa.Text(), nullable=True),
        sa.Column("row_context", sa.Text(), nullable=True),
        sa.Column("normalized_value", sa.Numeric(), nullable=True),
        sa.Column("normalized_unit", sa.Text(), nullable=True),
        sa.Column("numeric_role", sa.Text(), nullable=True),
        sa.Column("semantic_status", sa.Text(), nullable=True),
        sa.Column("condition", sa.Text(), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "dataset_kind = 'productivity'", name=op.f("ck_ref_productivity_rows_dataset_kind")
        ),
        sa.CheckConstraint(
            "normalized_value is null or normalized_value not in ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name=op.f("ck_ref_productivity_rows_normalized_finite"),
        ),
        sa.CheckConstraint(
            "record_type in ('page_context', 'table', 'table_cell', 'work_heading', 'cross_reference')",
            name=op.f("ck_ref_productivity_rows_record_type"),
        ),
        sa.CheckConstraint(
            "length(btrim(record_key)) > 0", name=op.f("ck_ref_productivity_rows_record_key")
        ),
        sa.CheckConstraint("page > 0", name=op.f("ck_ref_productivity_rows_page")),
        sa.CheckConstraint(
            "parent_key is null or parent_key <> record_key",
            name=op.f("ck_ref_productivity_rows_parent_key"),
        ),
        sa.CheckConstraint(
            "table_column is null or table_column > 0",
            name=op.f("ck_ref_productivity_rows_table_column"),
        ),
        sa.CheckConstraint(
            "table_row is null or table_row > 0", name=op.f("ck_ref_productivity_rows_table_row")
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id", "dataset_kind"],
            ["ref_dataset_versions.id", "ref_dataset_versions.kind"],
            name="fk_ref_productivity_rows_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id", "parent_key"],
            ["ref_productivity_rows.dataset_id", "ref_productivity_rows.record_key"],
            name="fk_ref_productivity_rows_parent",
            ondelete="RESTRICT",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("dataset_id", "record_key", name=op.f("pk_ref_productivity_rows")),
    )
    op.create_index(
        "ix_ref_productivity_rows_page",
        "ref_productivity_rows",
        ["dataset_id", "page"],
        unique=False,
    )
    op.create_index(
        "ix_ref_productivity_rows_parent",
        "ref_productivity_rows",
        ["dataset_id", "parent_key"],
        unique=False,
        postgresql_where=sa.text("parent_key is not null"),
    )
    op.create_table(
        "ref_unit_price_rows",
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_kind", sa.Text(), server_default="unit_price", nullable=False),
        sa.Column("record_key", sa.Text(), nullable=False),
        sa.Column("parent_key", sa.Text(), nullable=True),
        sa.Column("record_type", sa.Text(), nullable=False),
        sa.Column("trade", sa.Text(), nullable=True),
        sa.Column("work_code", sa.Text(), nullable=True),
        sa.Column("work_name", sa.Text(), nullable=True),
        sa.Column("specification", sa.Text(), nullable=True),
        sa.Column("component_name", sa.Text(), nullable=True),
        sa.Column("normalized_value", sa.Numeric(), nullable=True),
        sa.Column("normalized_unit", sa.Text(), nullable=True),
        sa.Column("basis_normalized_unit", sa.Text(), nullable=True),
        sa.Column("condition", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "dataset_kind = 'unit_price'", name=op.f("ck_ref_unit_price_rows_dataset_kind")
        ),
        sa.CheckConstraint(
            "normalized_value is null or normalized_value not in ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name=op.f("ck_ref_unit_price_rows_normalized_finite"),
        ),
        sa.CheckConstraint(
            "record_type in ('auxiliary_row', 'work_header', 'layout_row', 'component', 'total', 'note')",
            name=op.f("ck_ref_unit_price_rows_record_type"),
        ),
        sa.CheckConstraint(
            "length(btrim(record_key)) > 0", name=op.f("ck_ref_unit_price_rows_record_key")
        ),
        sa.CheckConstraint(
            "parent_key is null or parent_key <> record_key",
            name=op.f("ck_ref_unit_price_rows_parent_key"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id", "dataset_kind"],
            ["ref_dataset_versions.id", "ref_dataset_versions.kind"],
            name="fk_ref_unit_price_rows_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id", "parent_key"],
            ["ref_unit_price_rows.dataset_id", "ref_unit_price_rows.record_key"],
            name="fk_ref_unit_price_rows_parent",
            ondelete="RESTRICT",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("dataset_id", "record_key", name=op.f("pk_ref_unit_price_rows")),
    )
    op.create_index(
        "ix_ref_unit_price_rows_lookup",
        "ref_unit_price_rows",
        ["dataset_id", "work_code"],
        unique=False,
    )
    op.create_index(
        "ix_ref_unit_price_rows_parent",
        "ref_unit_price_rows",
        ["dataset_id", "parent_key"],
        unique=False,
        postgresql_where=sa.text("parent_key is not null"),
    )
    op.create_table(
        "ref_wage_rows",
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_kind", sa.Text(), server_default="wage", nullable=False),
        sa.Column("record_key", sa.Text(), nullable=False),
        sa.Column("parent_key", sa.Text(), nullable=True),
        sa.Column("record_type", sa.Text(), nullable=False),
        sa.Column("occupation_code", sa.Text(), nullable=True),
        sa.Column("occupation_name", sa.Text(), nullable=True),
        sa.Column("publication_date", sa.Date(), nullable=True),
        sa.Column("basis_hours", sa.SmallInteger(), nullable=True),
        sa.Column("normalized_value", sa.BigInteger(), nullable=True),
        sa.Column("survey_flag", sa.Text(), nullable=True),
        sa.Column("flag_reference_period", sa.Text(), nullable=True),
        sa.Column("definition", sa.Text(), nullable=True),
        sa.Column("condition", sa.Text(), nullable=True),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.CheckConstraint("dataset_kind = 'wage'", name=op.f("ck_ref_wage_rows_dataset_kind")),
        sa.CheckConstraint(
            "record_type <> 'occupation_wage' or (occupation_code is not null and length(btrim(occupation_code)) > 0 and publication_date is not null and basis_hours is not null)",
            name=op.f("ck_ref_wage_rows_published_fields"),
        ),
        sa.CheckConstraint(
            "record_type in ('page_context', 'table', 'table_cell', 'occupation_wage', 'occupation_definition')",
            name=op.f("ck_ref_wage_rows_record_type"),
        ),
        sa.CheckConstraint(
            "survey_flag is null or survey_flag in ('*', '**')",
            name=op.f("ck_ref_wage_rows_survey_flag"),
        ),
        sa.CheckConstraint(
            "basis_hours is null or basis_hours between 1 and 24",
            name=op.f("ck_ref_wage_rows_basis_hours"),
        ),
        sa.CheckConstraint(
            "length(btrim(record_key)) > 0", name=op.f("ck_ref_wage_rows_record_key")
        ),
        sa.CheckConstraint(
            "normalized_value is null or normalized_value >= 0",
            name=op.f("ck_ref_wage_rows_normalized_value"),
        ),
        sa.CheckConstraint("page > 0", name=op.f("ck_ref_wage_rows_page")),
        sa.CheckConstraint(
            "parent_key is null or parent_key <> record_key",
            name=op.f("ck_ref_wage_rows_parent_key"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id", "dataset_kind"],
            ["ref_dataset_versions.id", "ref_dataset_versions.kind"],
            name="fk_ref_wage_rows_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id", "parent_key"],
            ["ref_wage_rows.dataset_id", "ref_wage_rows.record_key"],
            name="fk_ref_wage_rows_parent",
            ondelete="RESTRICT",
            initially="DEFERRED",
            deferrable=True,
        ),
        sa.PrimaryKeyConstraint("dataset_id", "record_key", name=op.f("pk_ref_wage_rows")),
    )
    op.create_index(
        "ix_ref_wage_rows_parent",
        "ref_wage_rows",
        ["dataset_id", "parent_key"],
        unique=False,
        postgresql_where=sa.text("parent_key is not null"),
    )
    op.create_index(
        "uq_ref_wage_rows_published",
        "ref_wage_rows",
        ["dataset_id", "occupation_code", "publication_date", "basis_hours"],
        unique=True,
        postgresql_where=sa.text("record_type = 'occupation_wage'"),
    )


def downgrade() -> None:
    """이번 revision의 표만 제거하고 공유 vector 확장은 유지한다."""
    op.drop_index(
        "uq_ref_wage_rows_published",
        table_name="ref_wage_rows",
        postgresql_where=sa.text("record_type = 'occupation_wage'"),
    )
    op.drop_index(
        "ix_ref_wage_rows_parent",
        table_name="ref_wage_rows",
        postgresql_where=sa.text("parent_key is not null"),
    )
    op.drop_table("ref_wage_rows")
    op.drop_index(
        "ix_ref_unit_price_rows_parent",
        table_name="ref_unit_price_rows",
        postgresql_where=sa.text("parent_key is not null"),
    )
    op.drop_index("ix_ref_unit_price_rows_lookup", table_name="ref_unit_price_rows")
    op.drop_table("ref_unit_price_rows")
    op.drop_index(
        "ix_ref_productivity_rows_parent",
        table_name="ref_productivity_rows",
        postgresql_where=sa.text("parent_key is not null"),
    )
    op.drop_index("ix_ref_productivity_rows_page", table_name="ref_productivity_rows")
    op.drop_table("ref_productivity_rows")
    op.drop_index(
        "ix_ref_floorplan_rows_parent",
        table_name="ref_floorplan_rows",
        postgresql_where=sa.text("parent_key is not null"),
    )
    op.drop_index("ix_ref_floorplan_rows_lookup", table_name="ref_floorplan_rows")
    op.drop_table("ref_floorplan_rows")
    op.drop_table("ref_dataset_events")
    op.drop_table("rag_chunks")
    op.drop_index(
        "uq_ref_dataset_versions_active",
        table_name="ref_dataset_versions",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_table("ref_dataset_versions")
