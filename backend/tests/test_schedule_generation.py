"""사용자 입력 → DB 근거 선택 → 계산. LLM은 계산값을 제출할 수 없다."""

from datetime import date
from decimal import Decimal

import pytest


def seed_references():
    """저장소 실제 전처리 자료를 테스트 전용 DB에만 넣는다."""
    import asyncio
    import csv
    from pathlib import Path

    from app.db import SessionLocal
    from app.refprice.ingest import MODELS, convert
    from app.refprice.models import RefDatasetVersion
    from app.refprice.schedule_reference import TABLES

    async def run():
        async with SessionLocal() as s:
            for kind, filename in [
                ("floorplan", "01_floorplan_quantities"),
                ("productivity", "03_standard_productivity"),
                ("unit_price", "02_unit_price"),
            ]:
                version = RefDatasetVersion(
                    dataset_key=filename,
                    kind=kind,
                    version=1,
                    source=filename,
                    processed_path=filename + ".csv",
                    processed_sha256="a" * 64,
                    build_profile="test",
                    historical=False,
                    calculation_usable=True,
                    current_rule_usable=False,
                    reference_usable=False,
                    status="active",
                    provenance={},
                )
                s.add(version)
                await s.flush()
                path = (
                    Path(__file__).resolve().parents[2] / "data" / "processed" / (filename + ".csv")
                )
                with path.open(encoding="utf-8-sig") as stream:
                    for raw in csv.DictReader(stream):
                        if kind == "productivity" and raw["id"] not in {*TABLES, "SP-P0674-T03"}:
                            continue
                        if kind == "unit_price" and raw["id"] not in {
                            "UP-S13-R00134",
                            "UP-S13-R00263",
                        }:
                            continue
                        values = convert(kind, raw)
                        # 이 fixture는 개별 기준 표만 적재한다. 원본 parent_id는 provenance에 남긴다.
                        values["parent_key"] = None
                        s.add(MODELS[kind](dataset_id=version.id, **values))
            await s.commit()

    asyncio.run(run())


def project(api, *, items=("flatten",), grade="basic"):
    from tests.helpers import signup
    from tests.test_project import body

    signup(api)
    trades = {"dobae": {"spaces": ["a"], "grade": grade}}
    if items:
        trades["mokgong"] = {"spaces": ["a"], "items": list(items)}
    return api.post(
        "/projects",
        json=body(
            size={"basis": "exclusive", "m2": "59.0"},
            occupied=False,
            spaces=[{"key": "a", "name": "거실"}],
            trades=trades,
        ),
    ).json()


def choose_references(monkeypatch, transform=None):
    import json

    from app.agent import llm, schedule_agent

    async def choose(role, schema, messages):
        request = json.loads(messages[-1][1])
        if schema is schedule_agent.ReferenceSearch:
            return schema(apartment_type="59A")
        candidates = request["candidates"]
        items = []
        for task, spaces in request["tasks"].items():
            quantity_name = (
                "도배 면적 (㎡)"
                if task == "wallpaper_wall"
                else "둘레 (m)"
                if task in {"base", "molding"}
                else "바닥=천장 (㎡)"
            )
            q = next(
                (
                    q
                    for q in candidates["quantities"]
                    if q["space"] == "거실·주방·식당·복도" and q["item"] == quantity_name
                ),
                None,
            )
            if task in {"door", "door_frame", "insulation"}:
                q = None
            rate_task = "replacement" if task.startswith("wallpaper_") else task
            rates = [r for r in candidates["rates"] if r["task"] == rate_task]
            if task.startswith("wallpaper_"):
                rates = [r for r in rates if task.removeprefix("wallpaper_") in r["parts"]]
            rate = rates[0]
            items.append(
                {
                    "task": task,
                    "space_ids": spaces,
                    "quantity_key": q["key"] if q else None,
                    "assumed_quantity": None if q else 2 if task in {"door", "door_frame"} else 20,
                    "quantity_reason": None if q else "LLM 기본 지식으로 추정한 물량 가정값",
                    "rate_key": rate["key"],
                    "assumed_crew": 2 if rate["crew"] is None else None,
                    "assumption_reason": "LLM 기본 지식에 따른 가정값"
                    if rate["crew"] is None
                    else None,
                }
            )
        if transform:
            items = transform(items)
        return schema(items=items)

    monkeypatch.setattr(llm, "structured", choose)


@pytest.mark.parametrize(
    "items, carpentry_days, evidence_count", [(("flatten",), 2, 4), (("base",), 1, 3)]
)
def test_user_input_to_db_to_saved_trade_schedule(
    api, monkeypatch, items, carpentry_days, evidence_count
):
    from tests.conftest import query

    p = project(api, items=items)
    seed_references()
    choose_references(monkeypatch)
    response = api.post(f"/projects/{p['id']}/expected-plan/generate", json={"seen_version": 0})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert [(b["trade"], b["kind"], b["days"]) for b in plan["blocks"]] == [
        ("mokgong", "work", carpentry_days),
        ("dobae", "work", 2),
    ]
    assert plan["basis"]["mode"] == "reference"
    assert len(plan["basis"]["items"]) == evidence_count
    assert plan["basis"]["items"][0]["rate_source"]["crew"] == 2
    assert len(query("select id from plans")) == 1
    # 읽기가 구형 산식으로 덮어쓰지 않는다.
    assert api.get(f"/projects/{p['id']}/plan").json()["version"] == plan["version"]
    assert (
        api.post(
            f"/projects/{p['id']}/expected-plan/generate", json={"seen_version": 0}
        ).status_code
        == 409
    )
    from uuid import UUID

    query("update projects set rev=rev+1 where id=$1 returning rev", UUID(p["id"]))
    status = api.get(f"/projects/{p['id']}/expected-plan").json()
    assert status["version"] == 1 and status["plan"] is None
    assert len(query("select id from plans")) == 1
    regenerated = api.post(f"/projects/{p['id']}/expected-plan/generate", json={"seen_version": 1})
    assert regenerated.status_code == 200 and regenerated.json()["version"] == 2


@pytest.mark.parametrize("bad", ["unknown", "omit", "duplicate", "wrong_part", "wrong_space"])
def test_invalid_llm_selection_does_not_save_partial_schedule(api, monkeypatch, bad):
    from tests.conftest import query

    p = project(api, items=())
    seed_references()

    def change(items):
        if bad == "omit":
            return items[:1]
        if bad == "duplicate":
            return items + items[:1]
        if bad == "wrong_part":
            items[0]["rate_key"] = items[1]["rate_key"]
        elif bad == "wrong_space":
            items[0]["quantity_key"] = "FP-P07-L053-C4-59A"
        else:
            items[0]["quantity_key"] = "invented"
        return items

    choose_references(monkeypatch, change)
    response = api.post(f"/projects/{p['id']}/expected-plan/generate", json={"seen_version": 0})
    assert response.status_code == 422, response.text
    assert not query("select id from plans")


def test_missing_llm_or_foreign_project_never_saves_estimate(api, monkeypatch):
    from app.agent import llm
    from tests.conftest import query
    from tests.helpers import signup

    p = project(api, items=())
    seed_references()

    async def fail(*args):
        raise llm.LLMError("실패")

    monkeypatch.setattr(llm, "structured", fail)
    assert (
        api.post(
            f"/projects/{p['id']}/expected-plan/generate", json={"seen_version": 0}
        ).status_code
        == 503
    )
    assert not query("select id from plans")
    signup(api, "different@example.test")
    assert (
        api.post(
            f"/projects/{p['id']}/expected-plan/generate", json={"seen_version": 0}
        ).status_code
        == 404
    )


def test_trade_rounds_once_after_different_standard_crews():
    from app.calc.schedule import ReferenceWork, reference_plan

    result, _ = reference_plan(
        [
            ReferenceWork("mokgong", "천장틀", Decimal("64.715"), Decimal(2), Decimal(62)),
            ReferenceWork("mokgong", "석고판", Decimal("64.715"), Decimal(3), Decimal(45)),
        ],
        date(2026, 11, 14),
    )
    assert result.work_days == 3
    assert len(result.blocks) == 1 and result.blocks[0].crew is None
    assert all(b.kind == "work" for b in result.blocks)


def test_fractional_standard_days_do_not_add_a_day_at_integer_boundary():
    from app.calc.schedule import ReferenceWork, reference_plan

    work = ReferenceWork("mokgong", "검산", Decimal(2), Decimal(2), Decimal(3))
    plan, _ = reference_plan([work, work, work], date(2026, 11, 14))
    assert plan.work_days == 2
    plan, _ = reference_plan(
        [
            ReferenceWork(
                "dobae",
                "천장",
                Decimal("65.45"),
                Decimal(2),
                Decimal(85),
                output_factor=Decimal("0.77"),
            )
        ],
        date(2026, 11, 14),
    )
    assert plan.work_days == 1


def test_concurrent_generation_saves_only_one_version(api, monkeypatch):
    import asyncio
    from uuid import UUID

    from app.schedule.generation import GenerateInput, generate
    from app.shared.errors import AppError
    from tests.conftest import query

    p = project(api, items=())
    seed_references()
    choose_references(monkeypatch)
    owner_id = query("select owner_id from projects where id=$1", UUID(p["id"]))[0][0]

    async def once():
        try:
            await generate(owner_id, UUID(p["id"]), GenerateInput(seen_version=0), date.today())
            return 200
        except AppError as error:
            return error.status

    async def run():
        return await asyncio.gather(once(), once())

    assert sorted(asyncio.run(run())) == [200, 409]
    assert len(query("select id from plans")) == 1


def test_generation_status_does_not_create_legacy_estimate(api):
    from tests.conftest import query

    p = project(api, items=())
    response = api.get(f"/projects/{p['id']}/expected-plan")
    assert response.status_code == 200 and response.json() == {
        "version": 0,
        "plan": None,
        "final_version": 0,
        "has_deals": False,
    }
    assert not query("select id from plans")


def test_unsupported_work_does_not_get_an_invented_crew(api, monkeypatch):
    from app.agent import schedule_agent
    from tests.conftest import query

    p = project(api, items=("door",))
    seed_references()
    query("delete from ref_unit_price_rows returning record_key")
    monkeypatch.setattr(schedule_agent, "plan_references", None)
    response = api.post(f"/projects/{p['id']}/expected-plan/generate", json={"seen_version": 0})
    assert response.status_code == 422, response.text
    assert "문짝 교체" in response.json()["error"]["message"]
    assert not query("select id from plans")


def test_daily_standard_is_parsed_from_source_not_llm_numbers():
    from app.refprice.schedule_reference import daily_options

    options = daily_options(
        "SP-P0653-T05",
        '[["구 분","단 위","수 량","시공량(㎡)",null],'
        '[null,null,null,"합판･석고보드면","콘크리트･모르타르면"],'
        '["도 배 공\\n보 통 인 부","인\\n인","2\\n1","85","95"]]',
        "천장은 시공량의 23%를 감하여 적용한다.",
    )
    assert options[0]["crew"] == 2
    assert options[0]["output"] == "85"
    assert options[0]["occupation"] == "도배공"
    with pytest.raises(ValueError):
        daily_options("SP-P0653-T05", '[["임의 값", "85"]]', "")


def test_selection_cannot_add_numbers_or_raw_sql():
    from pydantic import ValidationError

    from app.agent.schedule_agent import ReferenceSearch, ReferenceSelection

    with pytest.raises(ValidationError):
        ReferenceSearch.model_validate({"apartment_type": "59A", "sql": "select * from users"})
    with pytest.raises(ValidationError):
        ReferenceSelection.model_validate({"items": [], "work_days": 2})


def test_separate_living_and_kitchen_quantities_are_not_interchangeable():
    from app.schedule.generation import matches_space, space_kind

    assert matches_space("거실", "거실·복도")
    assert not matches_space("거실", "주방/식당")
    assert matches_space("주방", "거실·주방·식당·복도")
    assert not matches_space("거실·주방", "거실·복도")
    assert space_kind("거실·복도") != space_kind("주방/식당")


def test_molding_uses_explicit_assumed_crew(api, monkeypatch):
    p = project(api, items=("molding",))
    seed_references()
    choose_references(monkeypatch)
    response = api.post(f"/projects/{p['id']}/expected-plan/generate", json={"seen_version": 0})
    assert response.status_code == 200, response.text
    basis = response.json()["basis"]
    assert basis["assumptions"][0]["crew"] == 2
    assert basis["assumptions"][0]["task"] == "molding"
    assert "가정값" in basis["notice"]
    evidence = next(v for v in basis["items"] if v["task"] == "molding")
    assert evidence["rate_source"]["crew"] is None
    assert evidence["rate_source"]["man_day"] == "0.035"


def test_molding_rejects_missing_assumption(api, monkeypatch):
    p = project(api, items=("molding",))
    seed_references()

    def missing(items):
        for item in items:
            item["assumed_crew"] = None
            item["assumption_reason"] = None
        return items

    choose_references(monkeypatch, missing)
    response = api.post(f"/projects/{p['id']}/expected-plan/generate", json={"seen_version": 0})
    assert response.status_code == 422


def test_assumed_crew_divides_db_man_days():
    from app.calc.schedule import ReferenceWork, reference_plan

    work = ReferenceWork(
        "mokgong", "몰딩", Decimal(100), Decimal(2), Decimal(1), man_day=Decimal("0.035")
    )
    result, man_days = reference_plan([work], date(2026, 11, 16))
    assert result.work_days == 2
    assert man_days["mokgong"] == Decimal("3.5")


def test_all_six_items_with_labeled_assumptions(api, monkeypatch):
    p = project(api)
    seed_references()
    choose_references(monkeypatch)
    items = ["flatten", "insulation", "door_frame", "door", "molding", "base"]
    response = api.post(
        f"/projects/{p['id']}/expected-plan/generate",
        json={"seen_version": 0, "mokgong_items": items},
    )
    assert response.status_code == 200, response.text
    basis = response.json()["basis"]
    assert basis["mokgong_items"] == items
    assert len(basis["items"]) == 9
    assert {v["task"] for v in basis["quantity_assumptions"]} == {
        "door",
        "door_frame",
        "insulation",
    }
    assert {v["task"] for v in basis["assumptions"]} == {"door", "door_frame", "molding"}
    assert api.get(f"/projects/{p['id']}").json()["trades"]["mokgong"]["items"] == ["flatten"]


@pytest.mark.parametrize(
    "bad", ["fractional_count", "db_override", "missing_reason", "mismatched_doors"]
)
def test_invalid_quantity_assumptions_are_rejected(api, monkeypatch, bad):
    p = project(api)
    seed_references()

    def change(items):
        for item in items:
            if item["task"] == "door":
                if bad == "mismatched_doors":
                    item["assumed_quantity"] = "3"
                if bad == "fractional_count":
                    item["assumed_quantity"] = "1.5"
                if bad == "missing_reason":
                    item["quantity_reason"] = None
            if item["task"] == "wallpaper_wall" and bad == "db_override":
                item["assumed_quantity"] = "20"
                item["quantity_reason"] = "가정"
        return items

    choose_references(monkeypatch, change)
    response = api.post(
        f"/projects/{p['id']}/expected-plan/generate",
        json={"seen_version": 0, "mokgong_items": ["door", "door_frame"]},
    )
    assert response.status_code == 422
