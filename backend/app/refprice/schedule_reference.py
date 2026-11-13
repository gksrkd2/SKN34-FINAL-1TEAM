"""예상 공정표의 읽기 후보. 검산한 표 구조에서만 표준 인원과 일당 시공량을 읽는다."""

import json
from decimal import Decimal

from sqlalchemy import select

from app.refprice.models import FloorplanRow, ProductivityRow, RefDatasetVersion, UnitPriceRow

# 표 키: (작업, 단위, 인원 행/열, 시공량 행/열, 주 직종). 수치 자체는 DB에서 읽는다.
TABLES = {
    "SP-P0653-T05": ("wallpaper", "m2", 2, 2, 2, (3, 4), "도배공"),
    "SP-P0975-T03": ("replacement", "m2", 2, 2, 2, (3, 4), "내장공"),
    "SP-P0676-T02": ("ceiling_frame", "m2", 1, 2, 1, (4,), "내장공"),
    "SP-P0651-T02": ("ceiling_board", "m2", 2, 2, 2, (3, 4, 5), "내장공"),
    "SP-P0655-T01": ("insulation", "m2", 2, 2, 2, (4,), "내장공"),
    "SP-P0653-T02": ("base", "m", 1, 3, 1, (4,), "내장공"),
}


def daily_options(key: str, source: str, condition: str) -> list[dict]:
    task, unit, cr, cc, pr, columns, occupation = TABLES[key]
    try:
        table = json.loads(source)
        compact = lambda value: "".join(str(value).replace("\\n", " ").split())
        if "시공량" not in compact(table[0]) or occupation not in compact(table[cr]):
            raise ValueError("표 구조가 달라요")
        options = []
        rows = range(1, len(table)) if task == "base" else [pr]
        for row in rows:
            crew_row = row if task == "base" else cr
            if occupation not in compact(table[crew_row]):
                raise ValueError("주 직종을 확인해 주세요")
            crew = Decimal(str(table[crew_row][cc]).replace("\\n", "\n").splitlines()[0])
            if not crew.is_finite() or crew != crew.to_integral_value() or not 1 <= crew <= 30:
                raise ValueError("주 직종 인원이 아니에요")
            for column in columns:
                outputs = str(table[row][column]).replace("\\n", "\n").splitlines()
                for index, value in enumerate(outputs):
                    output = Decimal(value)
                    if not output.is_finite() or output <= 0:
                        raise ValueError("일당 시공량이 아니에요")
                    label = " / ".join(str(x) for x in table[row][:column])
                    options.append(
                        {
                            "key": f"{key}:{row}:{column}:{index}",
                            "task": task,
                            "unit": unit,
                            "crew": int(crew),
                            "output": str(output),
                            "occupation": occupation,
                            "specification": label,
                            "condition": condition,
                            "table": table,
                            "output_cell": [row + 1, column + 1, index],
                            "crew_cell": [crew_row + 1, cc + 1],
                            "parts": (["wall"] if column == 3 else ["ceiling"])
                            if task == "replacement"
                            else ["wall", "ceiling"],
                        }
                    )
        return options
    except (IndexError, TypeError, ArithmeticError, json.JSONDecodeError) as exc:
        raise ValueError("표준 인원·시공량 표를 확인해 주세요") from exc


async def catalog(s, start, apartment_type=None, *, lock=False):
    """원문·출처·버전이 있는 활성 자료만 조회한다. SQL은 LLM에서 받지 않는다."""
    versions = (
        select(RefDatasetVersion)
        .where(
            RefDatasetVersion.status == "active",
            RefDatasetVersion.calculation_usable.is_(True),
            RefDatasetVersion.historical.is_(False),
            RefDatasetVersion.dataset_key.in_(
                ["01_floorplan_quantities", "03_standard_productivity", "02_unit_price"]
            ),
            (
                RefDatasetVersion.effective_date.is_(None)
                | (RefDatasetVersion.effective_date <= start)
            ),
        )
        .order_by(RefDatasetVersion.dataset_key)
    )
    if lock:
        versions = versions.with_for_update(read=True)
    available = (await s.scalars(versions)).all()
    by_kind = {v.kind: v for v in available}
    if "floorplan" not in by_kind or "productivity" not in by_kind:
        return {"apartments": [], "quantities": [], "rates": [], "versions": []}
    query = (
        select(FloorplanRow)
        .where(
            FloorplanRow.dataset_id == by_kind["floorplan"].id,
            FloorplanRow.record_type == "detail",
            FloorplanRow.aggregation_role == "detail",
            FloorplanRow.normalized_value > 0,
        )
        .order_by(FloorplanRow.record_key)
    )
    if apartment_type:
        query = query.where(FloorplanRow.apartment_type == apartment_type)
    quantities = (await s.scalars(query)).all()
    rates = []
    for row in await s.scalars(
        select(ProductivityRow)
        .where(
            ProductivityRow.dataset_id == by_kind["productivity"].id,
            ProductivityRow.record_type == "table",
            ProductivityRow.record_key.in_(TABLES),
        )
        .order_by(ProductivityRow.record_key)
    ):
        if row.basis != "(일당)" or not row.condition:
            continue
        try:
            for option in daily_options(row.record_key, row.source_text, row.condition):
                rates.append(
                    {
                        **option,
                        "record_key": row.record_key,
                        "dataset_id": str(row.dataset_id),
                        "page": row.page,
                        "name": row.work_name,
                        "basis": row.basis,
                    }
                )
        except ValueError:
            continue
    # 품만 있는 표는 표준 인원으로 취급하지 않고 LLM의 가정을 따로 받는다.
    molding = await s.get(ProductivityRow, (by_kind["productivity"].id, "SP-P0674-T03"))
    if molding is not None:
        table = json.loads(molding.source_text)
        coefficient = Decimal(table[1][3])
        if coefficient.is_finite() and coefficient > 0:
            rates.append(
                {
                    "key": molding.record_key,
                    "task": "molding",
                    "unit": "m",
                    "crew": None,
                    "man_day": str(coefficient),
                    "output": "1",
                    "occupation": "내장공",
                    "condition": molding.condition,
                    "record_key": molding.record_key,
                    "page": molding.page,
                    "dataset_id": str(molding.dataset_id),
                    "table": table,
                }
            )
    if "unit_price" in by_kind:
        for key, task in [("UP-S13-R00134", "door"), ("UP-S13-R00263", "door_frame")]:
            row = await s.get(UnitPriceRow, (by_kind["unit_price"].id, key))
            if (
                row is not None
                and row.normalized_unit == "인"
                and row.basis_normalized_unit == "ea"
                and row.normalized_value is not None
                and row.normalized_value > 0
            ):
                rates.append(
                    {
                        "key": key,
                        "task": task,
                        "unit": "ea",
                        "crew": None,
                        "man_day": str(row.normalized_value),
                        "output": "1",
                        "occupation": row.component_name,
                        "condition": row.condition,
                        "specification": row.specification,
                        "record_key": key,
                        "dataset_id": str(row.dataset_id),
                    }
                )
    return {
        "apartments": sorted({r.apartment_type for r in quantities if r.apartment_type}),
        "quantities": [
            {
                "key": r.record_key,
                "apartment_type": r.apartment_type,
                "space": r.space,
                "item": r.quantity_item,
                "value": str(r.normalized_value),
                "unit": r.normalized_unit,
                "condition": r.condition,
                "page": r.page,
                "nominal_area": r.provenance.get("nominal_area_m2"),
                "dataset_id": str(r.dataset_id),
            }
            for r in quantities
        ],
        "rates": rates,
        "versions": [
            {
                "id": str(v.id),
                "version": v.version,
                "sha256": v.processed_sha256,
                "source": v.source,
                "effective_date": str(v.effective_date),
            }
            for v in available
        ],
    }
