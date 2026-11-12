"""계산 입력용 기준 자료 읽기. 활성 버전과 행의 문맥을 함께 반환한다."""

import json
import re
import uuid
from collections.abc import Collection
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.refprice.models import RefDatasetVersion, UnitPriceRow


def schedule_man_day(row: UnitPriceRow) -> Decimal:
    """원본 수량 셀의 표시 정밀도로 확인되는 Excel 표현 오차만 제거한다."""
    value = row.normalized_value
    cells = row.provenance.get("cells_json")
    if not cells:
        return value
    cells = json.loads(cells) if isinstance(cells, str) else cells
    if not isinstance(cells, list) or any(not isinstance(cell, dict) for cell in cells):
        raise ValueError("원본 수량 셀을 확인해 주세요")
    quantities = [c for c in cells if c.get("role") == "quantity"]
    if len(quantities) != 1:
        return value
    cell = quantities[0]
    precision = re.search(r"0\.(0+)", cell.get("number_format", ""))
    if precision:
        unit = Decimal(1).scaleb(-len(precision[1]))
        parsed = Decimal(cell["parsed_value"])
        if (
            parsed.is_finite()
            and parsed > 0
            and value.quantize(unit, rounding=ROUND_HALF_UP) == parsed
        ):
            return parsed
    return value


async def schedule_rows(
    s: AsyncSession,
    keys: Collection[str],
    dataset_id: uuid.UUID | None = None,
    *,
    lock: bool = False,
):
    query = (
        select(RefDatasetVersion, UnitPriceRow)
        .join(UnitPriceRow, UnitPriceRow.dataset_id == RefDatasetVersion.id)
        .where(
            RefDatasetVersion.dataset_key == "02_unit_price",
            RefDatasetVersion.status == "active",
            RefDatasetVersion.calculation_usable.is_(True),
            RefDatasetVersion.historical.is_(False),
            UnitPriceRow.record_type == "component",
            UnitPriceRow.normalized_unit == "인",
            UnitPriceRow.record_key.in_(keys),
        )
        .order_by(UnitPriceRow.record_key)
    )
    if dataset_id is not None:
        query = query.where(RefDatasetVersion.id == dataset_id)
    if lock:
        query = query.with_for_update(read=True, of=RefDatasetVersion)
    return (await s.execute(query)).all()
