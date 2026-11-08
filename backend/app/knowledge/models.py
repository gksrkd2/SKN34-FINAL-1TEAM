"""전처리 자료 저장 모델. 적재·계산·검색은 별도 기능에서 수행한다."""

import uuid

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    ARRAY,
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class RagChunk(Base):
    """RAG 본문 조각과 임베딩 저장 구조."""

    __tablename__ = "rag_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["dataset_id", "dataset_kind"],
            ["ref_dataset_versions.id", "ref_dataset_versions.kind"],
            ondelete="RESTRICT",
            name="fk_rag_chunks_version",
        ),
        CheckConstraint("dataset_kind = 'knowledge'", name="dataset_kind"),
        CheckConstraint("length(btrim(record_key)) > 0", name="record_key"),
        CheckConstraint("length(btrim(content)) > 0", name="content"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash"),
        CheckConstraint(
            "cardinality(page_numbers) > 0 and array_ndims(page_numbers) = 1 and array_position(page_numbers, null) is null and 0 < all(page_numbers)",
            name="page_numbers",
        ),
        CheckConstraint("embedding is null or vector_norm(embedding) > 0", name="embedding"),
    )

    dataset_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, primary_key=True)
    dataset_kind: Mapped[str] = mapped_column(
        Text(), nullable=False, default="knowledge", server_default="knowledge"
    )
    record_key: Mapped[str] = mapped_column(Text(), nullable=False, primary_key=True)
    section: Mapped[str] = mapped_column(Text(), nullable=False)
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    page_numbers: Mapped[list[int]] = mapped_column(ARRAY(Integer()), nullable=False)
    review_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    content_hash: Mapped[str] = mapped_column(Text(), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(VECTOR(), nullable=True)
