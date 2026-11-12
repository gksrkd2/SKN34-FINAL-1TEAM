"""예상 공정표 입력과 기준 자료 스냅샷.

Revision ID: 0021_schedule_basis
Revises: 0020_ingest_provenance
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0021_schedule_basis"
down_revision = "0020_ingest_provenance"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "plans",
        sa.Column("basis_snapshot", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_check_constraint("basis_snapshot", "plans", "jsonb_typeof(basis_snapshot) = 'object'")


def downgrade():
    op.drop_constraint(op.f("ck_plans_basis_snapshot"), "plans", type_="check")
    op.drop_column("plans", "basis_snapshot")
