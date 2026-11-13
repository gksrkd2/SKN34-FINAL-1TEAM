"""실제 DB: 사용자 물량·인원 + 검토한 DB 품만 저장·조회한다."""

import asyncio
import uuid
from datetime import date, timedelta
from decimal import Decimal

from tests.helpers import signup
from tests.test_project import body


def seed():
    from app.db import SessionLocal
    from app.refprice.models import RefDatasetVersion, UnitPriceRow

    async def run():
        async with SessionLocal() as s:
            version = RefDatasetVersion(
                id=uuid.UUID(int=100),
                dataset_key="02_unit_price",
                kind="unit_price",
                version=1,
                source="합성 평가 자료",
                processed_path="fixture.csv",
                processed_sha256="a" * 64,
                build_profile="test",
                historical=False,
                current_rule_usable=False,
                calculation_usable=True,
                reference_usable=False,
                provenance={},
                status="active",
            )
            s.add(version)
            await s.flush()
            s.add(
                UnitPriceRow(
                    dataset_id=version.id,
                    record_key="UP-S15-R00250",
                    record_type="component",
                    work_code="수장-17",
                    work_name="도배바름",
                    specification="합판ㆍ석고보드면",
                    component_name="도배공",
                    normalized_value=Decimal("0.024"),
                    normalized_unit="인",
                    basis_normalized_unit="m2",
                    condition="천장은 본 품의 30%를 가산한다.",
                    provenance={"sheet": "14수장", "row": "250"},
                )
            )
            await s.commit()

    asyncio.run(run())


def request_body():
    return {
        "dataset_id": str(uuid.UUID(int=100)),
        "start": (date.today() + timedelta(days=30)).isoformat(),
        "seen_version": 0,
        "trades": [
            {
                "trade": "dobae",
                "crew": 2,
                "items": [
                    {
                        "record_key": "UP-S15-R00250",
                        "quantity": "100",
                        "unit": "m2",
                        "part": "wall",
                        "accept_conditions": True,
                    }
                ],
            }
        ],
    }


def setup(api):
    signup(api)
    p = api.post(
        "/projects", json=body(trades={"dobae": {"spaces": ["a"], "grade": "basic"}})
    ).json()
    return p["id"]


def test_expected_requires_user_values_and_preserves_source_snapshot(api):
    pid = setup(api)
    assert api.get(f"/projects/{pid}/plan?kind=expected").status_code == 422
    seed()
    options = api.get(f"/projects/{pid}/expected-plan/inputs")
    assert options.status_code == 200 and options.json()[0]["record_key"] == "UP-S15-R00250"
    data = request_body()
    r = api.post(f"/projects/{pid}/expected-plan", json=data)
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["work_days"] == 2 and plan["blocks"][0]["crew"] == 2
    assert plan["formula_version"] == "image-2026.11.12-v2"
    assert all(b["kind"] == "work" for b in plan["blocks"])
    assert "wait_days" not in plan["basis"]["trades"][0]
    assert plan["basis"]["trades"][0]["items"][0]["record_key"] == "UP-S15-R00250"
    assert api.get(f"/projects/{pid}/plan?kind=expected").json() == plan
    assert api.post(f"/projects/{pid}/expected-plan", json=data).status_code == 409
    assert (
        api.post(
            f"/projects/{pid}/expected-plan", json={**data, "seen_version": plan["version"]}
        ).status_code
        == 200
    )


def test_missing_reference_is_not_replaced_with_book_defaults(api):
    pid = setup(api)
    assert api.post(f"/projects/{pid}/expected-plan", json=request_body()).status_code == 422


def test_other_project_and_invalid_crew_are_rejected(api):
    pid = setup(api)
    data = request_body()
    data["trades"][0]["crew"] = True
    assert api.post(f"/projects/{pid}/expected-plan", json=data).status_code == 422
    signup(api, "other@mitjip.test")
    assert api.post(f"/projects/{pid}/expected-plan", json=request_body()).status_code == 404


def test_concurrent_requests_save_one_version(api):
    from app.auth.models import User
    from app.db import SessionLocal
    from app.schedule.expected import ExpectedInput, create
    from app.shared.errors import AppError
    from tests.conftest import query

    pid = setup(api)
    seed()
    uid = query("select owner_id from projects where id = $1", uuid.UUID(pid))[0][0]

    async def once():
        async with SessionLocal() as s:
            user = await s.get(User, uid)
            try:
                await create(
                    s,
                    user,
                    uuid.UUID(pid),
                    ExpectedInput.model_validate(request_body()),
                    date.today(),
                )
                return 200
            except AppError as exc:
                return exc.status

    async def run():
        return await asyncio.gather(once(), once())

    assert sorted(asyncio.run(run())) == [200, 409]
    assert query("select count(*) from plans where project_id = $1", uuid.UUID(pid))[0][0] == 1


def test_reference_condition_and_unit_must_match(api):
    pid = setup(api)
    seed()
    data = request_body()
    data["trades"][0]["items"][0]["unit"] = "ea"
    assert api.post(f"/projects/{pid}/expected-plan", json=data).status_code == 422
    data["trades"][0]["items"][0]["accept_conditions"] = False
    assert api.post(f"/projects/{pid}/expected-plan", json=data).status_code == 422


def test_source_float_artifact_does_not_add_a_workday(api):
    import json

    from tests.conftest import query

    pid = setup(api)
    seed()
    query(
        "update ref_unit_price_rows set normalized_value=$1, provenance=$2::jsonb",
        Decimal("0.021000000000000001"),
        json.dumps(
            {
                "cells_json": json.dumps(
                    [{"role": "quantity", "parsed_value": "0.021", "number_format": "0.0000"}]
                )
            }
        ),
    )
    data = request_body()
    data["trades"][0]["crew"] = 7
    data["trades"][0]["items"][0]["quantity"] = "1000"
    response = api.post(f"/projects/{pid}/expected-plan", json=data)
    assert response.status_code == 200, response.text
    assert response.json()["work_days"] == 3


def test_mixed_primary_occupations_are_not_divided_by_one_crew(api):
    from tests.conftest import query

    signup(api)
    pid = api.post(
        "/projects", json=body(trades={"mokgong": {"spaces": ["a"], "items": ["molding", "base"]}})
    ).json()["id"]
    seed()
    for key, code, name, occupation in (
        ("UP-S11-R00307", "목공-22", "몰딩 설치", "건축목공"),
        ("UP-S11-R00421", "목공-30", "걸레받이 설치", "내장공"),
    ):
        query(
            "INSERT INTO ref_unit_price_rows (dataset_id,dataset_kind,record_key,record_type,work_code,work_name,component_name,normalized_value,normalized_unit,basis_normalized_unit,condition,provenance) VALUES ($1,'unit_price',$2,'component',$3,$4,$5,0.01,'인','m','합성 규격','{}'::jsonb)",
            uuid.UUID(int=100),
            key,
            code,
            name,
            occupation,
        )
    data = request_body()
    data["trades"][0]["trade"] = "mokgong"
    data["trades"][0]["items"] = [
        {
            "record_key": key,
            "quantity": "100",
            "unit": "m",
            "part": "trim",
            "accept_conditions": True,
        }
        for key in ("UP-S11-R00307", "UP-S11-R00421")
    ]
    response = api.post(f"/projects/{pid}/expected-plan", json=data)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "occupation_crew_required"


def test_expected_rejects_removed_wait_input(api):
    pid = setup(api)
    seed()
    for wait in (0, 2):
        data = request_body()
        data["trades"][0]["wait_days"] = wait
        assert api.post(f"/projects/{pid}/expected-plan", json=data).status_code == 422
