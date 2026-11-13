"""사용자 입력으로 예상 공정표 생성. 읽기·LLM 대기·계산·저장 트랜잭션을 분리한다."""

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent import schedule_agent
from app.agent.schemas import TurnContext
from app.auth.models import User
from app.calc.schedule import (
    REFERENCE_FORMULA_VERSION,
    ReferenceWork,
    reference_plan,
    reference_quantity,
)
from app.db import SessionLocal
from app.project import service as projects
from app.refprice.holidays import LAST_YEAR, days_off
from app.refprice.schedule_reference import catalog
from app.schedule.models import Deal, Plan
from app.schedule.schemas import PlanOut
from app.shared.errors import AppError

TASKS = {
    "wallpaper_wall": ("dobae", "도배 벽", "도배 면적 (㎡)", "m2", {"wallpaper", "replacement"}),
    "wallpaper_ceiling": (
        "dobae",
        "도배 천장",
        "바닥=천장 (㎡)",
        "m2",
        {"wallpaper", "replacement"},
    ),
    "ceiling_frame": ("mokgong", "천장틀", "바닥=천장 (㎡)", "m2", {"ceiling_frame"}),
    "ceiling_board": ("mokgong", "천장판", "바닥=천장 (㎡)", "m2", {"ceiling_board"}),
    "base": ("mokgong", "걸레받이", "둘레 (m)", "m", {"base"}),
    "molding": ("mokgong", "천장 몰딩", "둘레 (m)", "m", {"molding"}),
    "door": ("mokgong", "문짝 교체", "문짝 수", "ea", {"door"}),
    "door_frame": ("mokgong", "문틀 교체", "문틀 수", "ea", {"door_frame"}),
    "insulation": ("mokgong", "외벽 단열 보강", "단열 면적", "m2", {"insulation"}),
}


class GenerateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seen_version: int = Field(strict=True, ge=0, le=32766)
    start: date | None = None
    mokgong_items: (
        list[Literal["flatten", "insulation", "door_frame", "door", "molding", "base"]] | None
    ) = Field(default=None, min_length=1, max_length=6)


class ExpectedStatus(BaseModel):
    version: int
    plan: PlanOut | None
    final_version: int
    has_deals: bool


async def status_for(s, user, project_id):
    from app.schedule import service

    p = await projects.get_for(s, user, project_id)
    previous = await service._latest(s, project_id, "expected")
    final = await service._latest(s, project_id, "final")
    dealt = await s.scalar(
        select(Deal.id).where(Deal.project_id == project_id, Deal.status != "reselected").limit(1)
    )
    valid = (
        previous
        and previous.project_rev == p.rev
        and previous.formula_ver.startswith(("image-", "reference-"))
    )
    return ExpectedStatus(
        version=previous.version if previous else 0,
        plan=await service._out(s, previous) if valid else None,
        final_version=final.version if final else 0,
        has_deals=dealt is not None,
    )


@dataclass
class PreparedPlan:
    project_rev: int
    result: object
    man_days: dict
    snapshot: dict


def unavailable(message="사용자 조건에 맞는 물량·표준 인원의 근거를 확인해 주세요."):
    return AppError(422, "schedule_reference_required", message)


async def owned(s, ctx):
    user = await s.get(User, ctx.user_id)
    if user is None or user.role != "customer":
        raise AppError(403, "forbidden", "고객만 예상 공정표를 만들 수 있어요.")
    return await projects.get_for(s, user, ctx.project_id)


def required_tasks(p, mokgong_items=None):
    tasks = {}
    if p.trades.dobae:
        for key in ("wallpaper_wall", "wallpaper_ceiling"):
            tasks[key] = [str(v) for v in p.trades.dobae.spaces]
    if p.trades.mokgong:
        for item in mokgong_items if mokgong_items is not None else p.trades.mokgong.items:
            for key in ("ceiling_frame", "ceiling_board") if item == "flatten" else (item,):
                if key not in TASKS:
                    raise unavailable()
                tasks[key] = [str(v) for v in p.trades.mokgong.spaces]
    return tasks


def space_kind(name):
    """공간 이름의 동의어 범위만 허용한다. 불명확한 사용자 이름을 억지 대응하지 않는다."""
    name = name.replace(" ", "").lower()
    common = {
        kind
        for kind, words in [
            ("living", ("거실",)),
            ("kitchen", ("주방", "부엌")),
            ("dining", ("식당",)),
            ("corridor", ("복도",)),
        ]
        if any(word in name for word in words)
    }
    if common:
        return "common:" + "|".join(sorted(common)), None
    for kind, words in [
        ("entry", ("현관",)),
        ("bath", ("욕실", "화장실")),
        ("balcony", ("발코니", "베란다")),
    ]:
        if any(word in name for word in words):
            return kind, None
    if "방" in name or "침실" in name or "서재" in name:
        number = re.search(r"\d+", name)
        index = int(number[0]) if number else (1 if name in ("안방", "큰방") else None)
        if "작은방" in name and index is not None:
            index += 1
        return "bedroom", index
    return name, None


def matches_space(user_name, reference_name):
    user_kind, user_index = space_kind(user_name)
    ref_kind, ref_index = space_kind(reference_name or "")
    if user_kind.startswith("common:") and ref_kind.startswith("common:"):
        return set(user_kind.removeprefix("common:").split("|")) <= set(
            ref_kind.removeprefix("common:").split("|")
        )
    return user_kind == ref_kind and (
        user_index is None or ref_index is None or user_index == ref_index
    )


async def reference_candidates(s, ctx, start, apartment_type=None):
    await owned(s, ctx)
    return await catalog(s, start, apartment_type)


async def context_for(s, ctx, start, mokgong_items=None):
    p = await owned(s, ctx)
    refs = await catalog(s, start)
    if not refs["apartments"] or not refs["rates"]:
        raise unavailable(
            "사용 가능한 평면도 물량·표준품셈이 없어요. 기준 데이터 적재 상태를 확인해 주세요."
        )
    if mokgong_items is not None and (
        not p.trades.mokgong or len(set(mokgong_items)) != len(mokgong_items)
    ):
        raise unavailable("목공 공종과 항목을 확인해 주세요.")
    tasks = required_tasks(p, mokgong_items)
    rate_tasks = {r["task"] for r in refs["rates"]}
    missing = [TASKS[key][1] for key in tasks if not TASKS[key][4] & rate_tasks]
    if missing:
        raise unavailable(
            " · ".join(missing)
            + "의 물량·주 직종 인원 기준이 부족해요. 확인되지 않은 인원을 임의로 적용하지 않아요."
        )
    # 개인정보·자재·예산·파일 원문은 기준 선택에 필요하지 않다.
    return {
        "project_rev": p.rev,
        "apartments": refs["apartments"],
        "tasks": tasks,
        "mokgong_items": mokgong_items,
        "input": {
            "size": p.size.model_dump(mode="json"),
            "balcony_ext": p.balcony_ext,
            "occupied": p.occupied,
            "built_year": p.built_year,
            "spaces": [{"id": str(v.id), "name": v.name} for v in p.spaces],
        },
    }


async def prepare(s, ctx, start, context, candidates, selection):
    p = await owned(s, ctx)
    if p.rev != context["project_rev"]:
        raise AppError(409, "stale", "집 정보가 바뀌었어요. 다시 계산해 주세요.")
    if not candidates["apartments"]:
        raise unavailable()
    fresh = await catalog(s, start, candidates["apartments"][0])
    if fresh != candidates:
        raise AppError(409, "stale", "기준 자료가 바뀌었어요. 다시 계산해 주세요.")
    quantities = {v["key"]: v for v in fresh["quantities"]}
    rates = {v["key"]: v for v in fresh["rates"]}
    tasks, covered, used, works, evidence = (
        required_tasks(p, context.get("mokgong_items")),
        {},
        set(),
        [],
        [],
    )
    user_spaces = {str(space.id): space.name for space in p.spaces}
    assignments = {}
    for item in selection.items:
        if item.task not in tasks:
            raise unavailable()
        trade, name, quantity_item, unit, rate_tasks = TASKS[item.task]
        q, rate = quantities.get(item.quantity_key), rates.get(item.rate_key)
        assumed_quantity = item.quantity_key is None
        if assumed_quantity:
            if (
                item.task not in {"door", "door_frame", "insulation"}
                or item.assumed_quantity is None
                or not item.quantity_reason
                or any(
                    v["item"] == quantity_item and v["unit"] == unit for v in quantities.values()
                )
            ):
                raise unavailable("부족한 물량의 가정값과 근거를 확인해 주세요.")
            if unit == "ea" and (
                item.assumed_quantity != item.assumed_quantity.to_integral_value()
                or item.assumed_quantity > 30
            ):
                raise unavailable("문 개수 가정값을 확인해 주세요.")
            q = {
                "item": quantity_item,
                "unit": unit,
                "value": str(item.assumed_quantity),
                "source": "llm_assumption",
                "reason": item.quantity_reason,
            }
        elif item.assumed_quantity is not None or item.quantity_reason is not None:
            raise unavailable("DB 물량과 물량 가정값을 함께 적용할 수 없어요.")
        if (
            not q
            or not rate
            or q["item"] != quantity_item
            or q["unit"] != unit
            or rate["unit"] != unit
            or rate["task"] not in rate_tasks
            or (
                item.task.startswith("wallpaper_")
                and item.task.removeprefix("wallpaper_") not in rate["parts"]
            )
            or not set(item.space_ids) <= set(tasks[item.task])
            or len(set(item.space_ids)) != len(item.space_ids)
            or (item.task, item.quantity_key) in used
            or set(item.space_ids) & covered.get(item.task, set())
        ):
            raise unavailable()
        for space_id in [] if assumed_quantity else item.space_ids:
            reference_space = space_kind(q["space"] or "")
            if not matches_space(user_spaces[space_id], q["space"]) or (
                space_id in assignments and assignments[space_id] != reference_space
            ):
                raise unavailable(
                    "선택한 공간과 참고 평면도의 공간이 맞지 않아요. 공간 이름과 범위를 확인해 주세요."
                )
            assignments[space_id] = reference_space
        # 같은 작업·공간에 서로 다른 규격의 품을 중복 적용하지 않는다.
        used.add((item.task, item.quantity_key))
        covered.setdefault(item.task, set()).update(item.space_ids)
        assumed = rate["crew"] is None
        if assumed:
            if (
                item.task not in {"molding", "door", "door_frame"}
                or item.assumed_crew is None
                or not item.assumption_reason
            ):
                raise unavailable("작업 인원 가정과 근거를 확인해 주세요.")
        elif item.assumed_crew is not None or item.assumption_reason is not None:
            raise unavailable("DB 표준 인원이 있는 작업은 가정값으로 바꿀 수 없어요.")
        try:
            quantity = (
                item.assumed_quantity
                if assumed_quantity
                else reference_quantity(
                    [Decimal(q["value"])],
                    p.size.m2 if p.size.basis == "exclusive" else None,
                    Decimal(q["nominal_area"]),
                )
            )
            output = Decimal(rate["output"])
            output_factor = Decimal(1)
            if rate["task"] == "wallpaper" and item.task == "wallpaper_ceiling":
                if "23%" not in rate["condition"] or "감" not in rate["condition"]:
                    raise ValueError("천장 조건을 확인해 주세요")
                # 감소율은 해당 DB 원문으로 확인한 조건만 적용한다.
                output_factor *= Decimal("0.77")
            if rate["task"] == "replacement" and p.occupied:
                if "15%" not in rate["condition"] or "감" not in rate["condition"]:
                    raise ValueError("거주 조건을 확인해 주세요")
                output_factor *= Decimal("0.85")
            works.append(
                ReferenceWork(
                    trade,
                    name,
                    quantity,
                    Decimal(item.assumed_crew if assumed else rate["crew"]),
                    output,
                    output_factor=output_factor,
                    man_day=Decimal(rate["man_day"]) if assumed else None,
                )
            )
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise unavailable() from exc
        evidence.append(
            {
                **item.model_dump(mode="json"),
                "quantity": str(quantity),
                "quantity_source": q,
                "rate_source": rate,
                "output_factor": str(output_factor),
            }
        )
    if any(covered.get(key) != set(spaces) for key, spaces in tasks.items()) or not works:
        raise unavailable(
            "선택한 공종·공간 중 계산 근거가 부족한 항목이 있어요. 누락된 항목을 제외하고 계산하지 않아요."
        )
    door_quantities = {
        v["task"]: (set(v["space_ids"]), v["quantity"])
        for v in evidence
        if v["task"] in {"door", "door_frame"}
    }
    if (
        "door" in door_quantities
        and "door_frame" in door_quantities
        and door_quantities["door"] != door_quantities["door_frame"]
    ):
        raise unavailable("같은 대상의 문틀과 문짝은 동일한 문 개수로 가정해야 해요.")
    try:
        result, man_days = reference_plan(
            works, start, days_off(start, start + timedelta(days=1098))
        )
    except ValueError as exc:
        raise unavailable() from exc
    if result.end.year > LAST_YEAR or any(v > Decimal("999.9") for v in man_days.values()):
        raise unavailable()
    return PreparedPlan(
        p.rev,
        result,
        man_days,
        {
            "mode": "reference",
            "input": context["input"],
            "mokgong_items": context.get("mokgong_items"),
            "requested_start": str(start),
            "versions": fresh["versions"],
            "apartment_type": fresh["apartments"][0],
            "items": evidence,
            "quantity_basis": "exclusive_scaled"
            if p.size.basis == "exclusive"
            else "sample_unscaled",
            "quantity_assumptions": [
                {
                    "task": v["task"],
                    "name": TASKS[v["task"]][1],
                    "quantity": v["quantity"],
                    "unit": TASKS[v["task"]][3],
                    "reason": v["quantity_reason"],
                }
                for v in evidence
                if v["assumed_quantity"] is not None
            ],
            "assumptions": [
                {
                    "task": v["task"],
                    "name": TASKS[v["task"]][1],
                    "crew": v["assumed_crew"],
                    "reason": v["assumption_reason"],
                }
                for v in evidence
                if v["assumed_crew"] is not None
            ],
            "notice": "물량·작업 인원에 LLM 기본 지식으로 정한 가정값이 포함될 수 있어요. 계산 근거에서 확인해 주세요. 예시 평면도 물량과 표준 작업 인원을 사용한 참고 일정이에요. 실제 실측 물량·투입 인원과 달라요.",
            "scope_notice": "선택 공간은 예시 평면도의 공간에 대응해요. 거실·주방 등 합쳐진 공간의 물량은 함께 포함돼요. 걸레받이 둘레는 문 폭을 공제하지 않은 참고량이에요.",
        },
    )


async def generate(user_id: UUID, project_id: UUID, data: GenerateInput, today: date):
    from app.schedule import service

    ctx = TurnContext(user_id=user_id, chat_id=project_id, project_id=project_id)
    async with SessionLocal() as s:
        p = await owned(s, ctx)
        start = data.start or (p.when.start if p.when.kind == "date" else None)
        if start is None:
            raise unavailable("예상 공정표의 시작일을 골라 주세요.")
        if not today < start <= today + timedelta(days=730) or start.year > LAST_YEAR:
            raise unavailable("공휴일 자료가 있는 범위에서 내일 이후 시작일을 골라 주세요.")
        prev = await service._latest(s, project_id, "expected")
        if data.seen_version != (prev.version if prev else 0):
            raise AppError(409, "stale", "공정표가 바뀌었어요. 다시 확인해 주세요.")
        context = await context_for(s, ctx, start, data.mokgong_items)
    prepared = await schedule_agent.plan_references(ctx, context, start)
    # LLM을 기다릴 때는 DB 세션·트랜잭션·락을 잡지 않는다.
    async with SessionLocal() as s:
        await projects.lock(s, project_id)
        p = await owned(s, ctx)
        prev = await service._latest(s, project_id, "expected")
        if p.rev != prepared.project_rev or data.seen_version != (prev.version if prev else 0):
            raise AppError(409, "stale", "집 정보나 공정표가 바뀌었어요. 다시 확인해 주세요.")
        if await s.scalar(
            select(Deal.id)
            .where(Deal.project_id == project_id, Deal.status != "reselected")
            .limit(1)
        ):
            raise AppError(
                409, "dates_locked", "업체 확정 후에는 공사 날짜에서 변경을 요청해 주세요."
            )
        refs = await catalog(s, start, prepared.snapshot["apartment_type"], lock=True)
        if refs["versions"] != prepared.snapshot["versions"]:
            raise AppError(409, "stale", "기준 자료가 바뀌었어요. 다시 계산해 주세요.")
        result = prepared.result
        row = Plan(
            project_id=project_id,
            kind="expected",
            version=data.seen_version + 1,
            project_rev=p.rev,
            formula_ver=REFERENCE_FORMULA_VERSION,
            start_date=result.start,
            end_date=result.end,
            work_days=result.work_days,
            weekend_work=True,
            basis_snapshot=prepared.snapshot,
        )
        s.add(row)
        try:
            await s.flush()
            service._add_parts(s, row, result, prepared.man_days)
            await s.commit()
        except IntegrityError as exc:
            await s.rollback()
            raise AppError(409, "stale", "공정표가 바뀌었어요. 다시 확인해 주세요.") from exc
        return await service._out(s, row)
