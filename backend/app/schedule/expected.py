"""예상 공정표: 사용자 입력과 검토한 DB 품. 팀 가정값·예시 평면도는 사용하지 않는다."""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.calc.schedule import EXPECTED_FORMULA_VERSION, ExpectedTrade, WorkItem, expected_plan
from app.project import service as project_service
from app.refprice import service as refprice_service
from app.refprice.holidays import LAST_YEAR, days_off
from app.schedule.models import CREW_MAX, Deal, Plan
from app.shared.errors import AppError

# 원본 규격·직종을 확인한 행만 허용한다. DB 수치는 여기 복사하지 않는다.
# record_key: (공정, 작업 코드, 주 직종, 기준 단위)
REVIEWED_ROWS = {
    "UP-S15-R00250": ("dobae", "수장-17", "도배공", "m2"),
    "UP-S15-R00265": ("dobae", "수장-18", "도배공", "m2"),
    "UP-S10-R00206": ("mokgong", "단열-15", "내장공", "m2"),
    "UP-S11-R00307": ("mokgong", "목공-22", "건축목공", "m"),
    "UP-S11-R00421": ("mokgong", "목공-30", "내장공", "m"),
    "UP-S13-R00134": ("mokgong", "창호-13", "창호공", "ea"),
    "UP-S13-R00263": ("mokgong", "창호-22", "창호공", "ea"),
    "UP-S16-R00013": ("mokgong", "경량-01", "내장공", "m2"),
    "UP-S16-R00263": ("mokgong", "경량-13", "내장공", "m2"),
}
REVIEWED_PARTS = {
    "UP-S15-R00250": {"wall", "ceiling"},
    "UP-S15-R00265": {"wall", "ceiling"},
    "UP-S10-R00206": {"wall"},
    "UP-S11-R00307": {"trim"},
    "UP-S11-R00421": {"trim"},
    "UP-S13-R00134": {"leaf"},
    "UP-S13-R00263": {"frame"},
    "UP-S16-R00013": {"ceiling"},
    "UP-S16-R00263": {"ceiling"},
}


class MeasuredItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_key: str = Field(min_length=1, max_length=80)
    quantity: Decimal = Field(gt=0, le=100000, allow_inf_nan=False)
    unit: Literal["m2", "m", "ea"]
    part: Literal["wall", "ceiling", "floor", "frame", "leaf", "trim"]
    accept_conditions: Literal[True]

    @field_validator("accept_conditions", mode="before")
    @classmethod
    def conditions_are_accepted(cls, value):
        if value is not True:
            raise ValueError("기준 자료의 적용 조건을 확인해 주세요")
        return value


class MeasuredTrade(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trade: Literal["dobae", "mokgong"]
    crew: int = Field(strict=True, ge=1, le=CREW_MAX)
    wait_days: int = Field(strict=True, ge=0, le=120)
    items: list[MeasuredItem] = Field(min_length=1, max_length=30)


class ExpectedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date
    dataset_id: uuid.UUID
    seen_version: int = Field(strict=True, ge=0)
    trades: list[MeasuredTrade] = Field(min_length=1, max_length=2)


class ReferenceOption(BaseModel):
    dataset_id: uuid.UUID
    dataset_version: int
    source: str
    effective_date: date | None
    trade: str
    record_key: str
    name: str
    specification: str | None
    condition: str
    unit: str
    man_day: Decimal
    parts: list[str]


async def options(s: AsyncSession, user: User, project_id: uuid.UUID) -> list[ReferenceOption]:
    await project_service.get_for(s, user, project_id)
    rows = await refprice_service.schedule_rows(s, REVIEWED_ROWS)
    return [
        ReferenceOption(
            dataset_id=v.id,
            dataset_version=v.version,
            source=v.source,
            effective_date=v.effective_date,
            trade=REVIEWED_ROWS[r.record_key][0],
            record_key=r.record_key,
            name=r.work_name,
            specification=r.specification,
            condition=r.condition,
            unit=r.basis_normalized_unit,
            man_day=refprice_service.schedule_man_day(r),
            parts=sorted(REVIEWED_PARTS[r.record_key]),
        )
        for v, r in rows
        if r.condition
        and r.work_name
        and r.normalized_value is not None
        and r.normalized_value > 0
        and (
            REVIEWED_ROWS[r.record_key][1:]
            == (r.work_code, r.component_name, r.basis_normalized_unit)
        )
    ]


def missing():
    return AppError(
        422, "schedule_input_required", "물량·주 직종 인원·시작일과 기준 자료 조건을 확인해 주세요."
    )


async def read(s: AsyncSession, user: User, project_id: uuid.UUID):
    from app.schedule import service

    p = await project_service.get_for(s, user, project_id)
    row = await service._latest(s, project_id, "expected")
    if row is None or row.project_rev != p.rev:
        raise missing()
    # 이미 저장된 구형 버전은 조회만 허용한다. 새 계산이나 에이전트 근거로 재사용하지 않는다.
    return await service._out(s, row)


async def agent_context(s: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID) -> dict | None:
    """저장된 본인 예상 공정표만 읽는다. 조회 중 새 버전을 만들지 않는다."""
    from app.agent.trades import TRADES

    user = await s.get(User, user_id)
    if user is None or user.role != "customer":
        raise AppError(403, "forbidden", "고객만 공정표를 상담할 수 있어요.")
    plan = await read(s, user, project_id)
    if plan.formula_version != EXPECTED_FORMULA_VERSION:
        raise missing()
    trades = {b.trade for b in plan.blocks if b.kind == "work"}
    facts = {
        "sunday": "일요일에는 작업하지 않아요.",
        "saturday": "일반 토요일에는 작업할 수 있어요.",
        "reference": "사용자가 입력한 물량과 주 직종 인원, 기준 자료의 품으로 만든 참고 공정표예요.",
        "dates": "작업 기간과 시작일·종료일은 저장된 공정표에서 확인해 주세요.",
        "change": "공사 날짜의 변경과 확정은 공사 날짜 화면에서 직접 요청해 주세요.",
        "scope": "이 공정표는 선택한 목공과 도배만 다뤄요.",
    }
    if trades == {"mokgong", "dobae"}:
        facts["order"] = "목공이 끝난 뒤 도배해요."
    for trade in sorted(trades):
        facts[trade + "_caution"] = " ".join(TRADES[trade].cautions)
        facts[trade + "_holiday"] = (
            "목공은 공휴일에 쉬어요."
            if trade == "mokgong"
            else "도배는 일요일을 제외한 공휴일에 작업할 수 있어요."
        )
    if any(b.kind == "dry" for b in plan.blocks):
        facts["wait"] = "비시공 대기는 실제 작업일에 포함하지 않아요."
    return {"project_id": str(project_id), "facts": facts, "plan_version": plan.version}


async def create(
    s: AsyncSession, user: User, project_id: uuid.UUID, data: ExpectedInput, today: date
):
    from app.schedule import service

    p = await project_service.get_for(s, user, project_id)
    if user.role != "customer":
        raise AppError(403, "forbidden", "고객만 예상 공정표를 만들 수 있어요.")
    if not today < data.start <= today + timedelta(days=730):
        raise missing()
    if data.start.year > LAST_YEAR:
        raise missing()
    # 프로젝트 수정·업체 확정과 같은 행 잠금으로 입력 스냅샷과 선택 공정을 고정한다.
    await project_service.lock(s, project_id)
    p = await project_service.get_for(s, user, project_id)
    if await s.scalar(
        select(Deal.id).where(Deal.project_id == project_id, Deal.status != "reselected").limit(1)
    ):
        raise AppError(409, "dates_locked", "업체 확정 후에는 공사 날짜에서 변경을 요청해 주세요.")
    previous = await service._latest(s, project_id, "expected")
    if data.seen_version != (previous.version if previous else 0):
        raise AppError(409, "stale", "공정표가 바뀌었어요. 다시 확인해 주세요.")
    chosen = {t for t in ("mokgong", "dobae") if getattr(p.trades, t) is not None}
    if {t.trade for t in data.trades} != chosen or len(chosen) != len(data.trades):
        raise missing()
    rows = await refprice_service.schedule_rows(s, REVIEWED_ROWS, data.dataset_id, lock=True)
    if not rows:
        raise missing()
    version = rows[0][0]
    by_key = {row.record_key: row for _, row in rows}
    if version.effective_date and version.effective_date > data.start:
        raise missing()
    trades, snapshot, man_days = [], [], {}
    for t in data.trades:
        items, evidence, seen, occupations = [], [], set(), set()
        for item in t.items:
            mapping = REVIEWED_ROWS.get(item.record_key)
            row = by_key.get(item.record_key)
            if (
                mapping is None
                or row is None
                or mapping != (t.trade, row.work_code, row.component_name, item.unit)
                or row.record_type != "component"
                or row.normalized_unit != "인"
                or row.basis_normalized_unit != item.unit
                or not row.condition
                or row.normalized_value is None
                or row.normalized_value <= 0
                or not row.work_name
                or item.part not in REVIEWED_PARTS[item.record_key]
            ):
                raise missing()
            key = (item.record_key, item.part)
            if key in seen:
                raise missing()
            seen.add(key)
            occupations.add(row.component_name)
            if len(occupations) > 1:
                raise AppError(
                    422,
                    "occupation_crew_required",
                    "주 직종이 다른 작업은 인원 하나로 합산할 수 없어요. 직종별 인원을 확인해 주세요.",
                )
            try:
                man_day = refprice_service.schedule_man_day(row)
            except (ValueError, ArithmeticError, TypeError, KeyError) as exc:
                raise missing() from exc
            factor = Decimal(1)
            if t.trade == "dobae" and item.part == "ceiling":
                if "천장은 본 품의 30%를 가산" not in row.condition:
                    raise missing()
                factor = Decimal("1.3")
            items.append(WorkItem(row.work_name, item.quantity, man_day, factor))
            evidence.append(
                {
                    **item.model_dump(mode="json"),
                    "man_day": str(man_day),
                    "raw_man_day": str(row.normalized_value),
                    "factor": str(factor),
                    "occupation": row.component_name,
                    "work_code": row.work_code,
                    "specification": row.specification,
                    "condition": row.condition,
                    "source_location": {
                        k: row.provenance.get(k) for k in ("sheet", "row", "cell_range")
                    },
                }
            )
        trades.append(ExpectedTrade(t.trade, items, t.crew, t.wait_days))
        man_days[t.trade] = sum((i.quantity * i.man_day * i.factor for i in items), Decimal(0))
        snapshot.append(
            {"trade": t.trade, "crew": t.crew, "wait_days": t.wait_days, "items": evidence}
        )
    try:
        result = expected_plan(
            trades, data.start, days_off(data.start, data.start + timedelta(days=1098))
        )
    except ValueError as exc:
        raise missing() from exc
    if result.end.year > LAST_YEAR:
        raise missing()
    # 기존 Numeric(4,1) 상한을 지킨다. 미반올림 품은 스냅샷에 보존한다.
    if any(v > Decimal("999.9") for v in man_days.values()):
        raise missing()
    row = Plan(
        project_id=project_id,
        kind="expected",
        version=data.seen_version + 1,
        project_rev=p.rev,
        formula_ver=EXPECTED_FORMULA_VERSION,
        start_date=result.start,
        end_date=result.end,
        work_days=result.work_days,
        weekend_work=True,
        basis_snapshot={
            "trades": snapshot,
            "requested_start": str(data.start),
            "dataset_id": str(version.id),
            "dataset_version": version.version,
            "source": version.source,
            "effective_date": str(version.effective_date),
            "processed_sha256": version.processed_sha256,
            "current_rule_usable": version.current_rule_usable,
        },
    )
    s.add(row)
    try:
        await s.flush()
        service._add_parts(s, row, result, man_days)
        await s.commit()
    except IntegrityError as exc:
        await s.rollback()
        raise AppError(409, "stale", "공정표가 바뀌었어요. 다시 확인해 주세요.") from exc
    return await service._out(s, row)
