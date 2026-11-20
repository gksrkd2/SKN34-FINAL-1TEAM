"""AI 칸 API (`/ai/*`): 로그인 · 역할 · 남의 것 404 · SSE · 저장 · 같은 키 · 동시 저장 409 · 되묻기 칸 ·
로그인 전 자주 묻는 질문 · 입력 한도. LLM_LIVE=false(무료 모드), 유료 호출 0회.
"""

import json
import uuid
from dataclasses import replace

import asyncpg
import pytest
from conftest import TEST_DSN, query
from helpers import make_admin, signup, upload, use
from v3_kb import repo_index

from tests.test_project import body as project_body

Q = "합지랑 실크 중에 뭐로 해야 해요?"


@pytest.fixture(autouse=True)
def repo_knowledge(monkeypatch):
    """시험 DB에는 지식 자료를 넣지 않으니, 운영과 같은 CSV로 만든 색인을 넘긴다(DB 적재본과 같은 규칙)."""
    from app.knowledge import service as knowledge

    async def index(_s):
        return replace(repo_index(), version="")

    monkeypatch.setattr(knowledge, "index", index)


def events(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def ask(api, text=Q, key=None, **over):
    body = {"client_key": key or uuid.uuid4().hex, "text": text, **over}
    return api.post("/ai/turns", json=body)


def done(r) -> dict:
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    ev = events(r.text)
    assert ev[-1][0] == "done"
    return ev[-1][1]


def test_login_and_customer_role_are_required(api):
    assert ask(api).status_code == 401
    use(api, make_admin(api))
    assert ask(api).status_code == 403


def test_turn_streams_sentences_then_done_and_saves_masked_question(api):
    use(api, signup(api))
    r = ask(api, text="제 번호 010-1234-5678이에요. " + Q)
    ev = events(r.text)
    names = [e for e, _ in ev]
    assert names[0] == "token" and names[-1] == "done"
    out = ev[-1][1]
    assert out["mode"] == "free" and out["route"] == "trade"
    assert out["rev"] == 2 and out["chat_id"]
    sec = out["answer"]["sections"]
    assert sec and sec[0]["source"] == "card"
    assert all("url" not in s for x in sec for s in x["sources"])  # 출처는 이름 · 종류만
    assert "trace" not in out["answer"]
    analysis = out["analysis"]
    assert analysis["classification"] in ("rule", "model")
    assert analysis["trades"] == ["dobae"]
    assert set(analysis) == {"classification", "trades", "participants", "prompts"}
    rows = query("select role, text from chat_messages order by role desc")
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert "010-1234-5678" not in rows[0]["text"]


def test_same_client_key_returns_first_answer_without_saving_twice(api):
    use(api, signup(api))
    a = done(ask(api, key="same-key-123"))
    b = done(ask(api, key="same-key-123"))
    assert a == b
    assert query("select count(*) as n from chat_messages")[0]["n"] == 2


def test_follow_up_and_slot_answer_clear_waiting(api):
    use(api, signup(api))
    first = done(ask(api))
    assert first["answer"]["ask"]["slot"] == "dobae.grade"
    cid = first["chat_id"]
    assert query("select waiting from chats where id = $1", uuid.UUID(cid))[0]["waiting"] == "trade"
    btn = {"route": "trade", "trade": "dobae", "slot": "dobae.grade", "value": "세 놓을 집"}
    second = done(ask(api, text="세 놓을 집", chat_id=cid, button=btn))
    assert second["rev"] == 3 and second["answer"]["ask"] is None
    assert second["analysis"]["classification"] == "button"
    assert second["analysis"]["trades"] == ["dobae"]
    assert query("select waiting from chats where id = $1", uuid.UUID(cid))[0]["waiting"] is None


def test_someone_elses_chat_and_missing_project_are_404(api):
    use(api, signup(api, "a@mitjip.test"))
    cid = done(ask(api))["chat_id"]
    use(api, signup(api, "b@mitjip.test"))
    assert ask(api, chat_id=cid).status_code == 404
    assert api.get(f"/ai/threads/{cid}").status_code == 404
    r = ask(api, context={"screen": "standard", "project_id": str(uuid.uuid4())})
    assert r.status_code == 404


def test_stale_rev_is_409_and_saves_nothing(api, monkeypatch):
    use(api, signup(api))
    cid = done(ask(api))["chat_id"]
    from app.agent.v3 import turn as turn_mod

    real = turn_mod.run_turn

    async def racing(*a, **k):
        out = await real(*a, **k)
        conn = await asyncpg.connect(TEST_DSN)  # 다른 질문이 먼저 저장됨
        try:
            await conn.execute("update chats set rev = rev + 1 where id = $1", uuid.UUID(cid))
        finally:
            await conn.close()
        return out

    monkeypatch.setattr(turn_mod, "run_turn", racing)
    r = ask(api, text="도배 곰팡이는 왜 생겨요?", chat_id=cid)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"
    assert query("select count(*) as n from chat_messages")[0]["n"] == 2


@pytest.mark.parametrize("text", ["", "가" * 1001])
def test_text_length_is_limited(api, text):
    use(api, signup(api))
    assert ask(api, text=text).status_code == 422


def test_threads_list_and_detail(api):
    use(api, signup(api))
    cid = done(ask(api))["chat_id"]
    rows = api.get("/ai/threads").json()
    assert rows[0]["chat_id"] == cid and rows[0]["preview"].startswith("합지랑")
    d = api.get(f"/ai/threads/{cid}").json()
    assert [m["role"] for m in d["messages"]] == ["user", "assistant"] and d["rev"] == 2
    assert api.get(f"/ai/threads?project_id={uuid.uuid4()}").status_code == 404


def test_guest_faq_needs_no_login_and_reads_cards_only(api):
    api.cookies.clear()
    r = api.get("/ai/faq")
    assert r.status_code == 200
    chips = r.json()
    assert len(chips) >= 3 and all(c["sentences"] for c in chips)
    assert "표준견적서는 업체 견적이에요?" in [c["question"] for c in chips]


def _project(api) -> str:
    photo = upload(api).json()["id"]
    r = api.post("/projects", json=project_body([photo]))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_project_screen_chat_is_tied_to_that_project(api):
    use(api, signup(api, "p@mitjip.test"))
    pid = _project(api)
    cid = done(ask(api, context={"screen": "standard", "project_id": pid}))["chat_id"]
    assert (
        str(query("select project_id from chats where id = $1", uuid.UUID(cid))[0]["project_id"])
        == pid
    )
    other = done(ask(api))["chat_id"]  # 공사 밖에서 연 대화는 공사 칸이 비어요
    assert (
        query("select project_id from chats where id = $1", uuid.UUID(other))[0]["project_id"]
        is None
    )
    assert [r["chat_id"] for r in api.get(f"/ai/threads?project_id={pid}").json()] == [cid]
    assert {r["chat_id"] for r in api.get("/ai/threads").json()} == {cid, other}
    # 공사 없이 시작한 대화를 공사 화면에서 이어 물으면 그 공사가 적혀요
    done(
        ask(
            api,
            text="도배 곰팡이는 왜 생겨요?",
            chat_id=other,
            context={"screen": "standard", "project_id": pid},
        )
    )
    assert len(api.get(f"/ai/threads?project_id={pid}").json()) == 2


def test_standard_chat_changes_trade_while_waiting_for_dobae(api):
    use(api, signup(api))
    pid = _project(api)
    # 도배 등급을 아직 정하지 않은 상담에서 되묻는 칸을 만든다.
    pending = done(ask(api))
    assert pending["answer"]["ask"]["trade"] == "dobae"
    out = done(
        ask(
            api,
            text="몰딩 이음새가 벌어졌어요",
            chat_id=pending["chat_id"],
            context={"screen": "standard", "project_id": pid},
        )
    )
    assert out["route"] == "trade"
    assert out["analysis"]["trades"] == ["mokgong"]
    assert out["analysis"]["classification"] != "waiting"
    assert out["answer"]["ask"] is None
    assert (
        query("select waiting from chats where id = $1", uuid.UUID(out["chat_id"]))[0]["waiting"]
        is None
    )


def test_standard_chat_uses_dobae_project_conditions_and_mold_choice(api):
    from tests.test_v3_project_home import PART

    use(api, signup(api))
    project = api.post("/projects", json={**PART, "occupied": True})
    assert project.status_code == 201, project.text
    context = {"screen": "standard", "project_id": project.json()["id"]}
    prep = done(ask(api, text="거주 중 도배하려면 뭘 준비해요?", context=context))["answer"]
    assert any("시공할 벽 앞 가구" in x for s in prep["sections"] for x in s["sentences"])
    assert prep["calc"] and prep["ask"] is None

    first = done(ask(api, text="벽지가 들떠요", context=context))
    assert first["answer"]["ask"]["slot"] == "dobae.mold"
    button = {"route": "trade", "trade": "dobae", "slot": "dobae.mold", "value": "있어요"}
    second = done(
        ask(api, text="있어요", chat_id=first["chat_id"], context=context, button=button)
    )["answer"]
    assert second["ask"] is None
    assert any("바탕 상태" in x for s in second["sections"] if s["sources"] for x in s["sentences"])


def test_someone_elses_project_is_404_and_writes_no_chat(api):
    use(api, signup(api, "owner@mitjip.test"))
    pid = _project(api)
    use(api, signup(api, "other@mitjip.test"))
    before = query("select count(*) as n from chats")[0]["n"]
    assert ask(api, context={"screen": "standard", "project_id": pid}).status_code == 404
    assert query("select count(*) as n from chats")[0]["n"] == before
    assert api.get(f"/ai/threads?project_id={pid}").status_code == 404


def test_vendor_named_lookup_ignores_region_and_uses_only_public_details(api):
    from tests.helpers import vendor_signup
    from tests.test_v3_visit_kit import customer
    from tests.test_visit_request import listed

    other_area = listed(
        api,
        "far@mitjip.test",
        "먼지역도배",
        areas=[{"sido": "seoul", "sgg": "강남구"}],
    )
    assert vendor_signup(api, email="pending@mitjip.test").status_code == 201
    query(
        "update vendors set contact_method = 'phone' where user_id = "
        "(select id from users where email = 'pending@mitjip.test')"
    )
    other_trade = listed(api, "wood@mitjip.test", "목공전용", trades=["mokgong"])
    stopped = listed(api, "stopped@mitjip.test", "중지된도배")
    query(
        "update vendors set suspended_at = now(), suspend_scope = 'new' where user_id = $1",
        uuid.UUID(stopped),
    )
    _, pid = customer(api, sgg="강북구")
    answer = done(
        ask(api, "먼지역도배 업체에 대해 알려줘", context={"screen": "vendors", "project_id": pid})
    )
    text = json.dumps(answer["answer"], ensure_ascii=False)
    assert "먼지역도배" in text and "강남구" in text
    assert answer["answer"]["cards"][0]["choices"][0]["action"] == f"vendor_detail:{other_area}"
    profile = api.get(f"/vendors/{other_area}")
    assert profile.status_code == 200, profile.text
    assert profile.json()["biz_name"] == "먼지역도배"
    assert {"phone", "contact_url", "address", "site_url", "license_file_id"}.isdisjoint(
        profile.json()
    )
    assert "010" not in text
    assert query("select count(*) as n from visits")[0]["n"] == 0
    for vid in (other_trade, stopped):
        assert api.get(f"/vendors/{vid}").status_code == 404
    stopped_answer = done(ask(api, "중지된도배 알려줘"))["answer"]
    assert "중지된도배" not in json.dumps(stopped_answer, ensure_ascii=False)


def test_vendor_named_lookup_without_project_or_region(api):
    from tests.test_visit_request import listed

    vendor_id = listed(api, "near@mitjip.test", "인접도배")
    token = signup(api)
    result = done(ask(api, "인접도배 어때요?"))
    assert result["answer"]["cards"][0]["choices"][0]["action"] == f"vendor_detail:{vendor_id}"
    details = done(ask(api, "이 업체 소개해줘", context={"screen": "vendor", "ids": [vendor_id]}))
    assert "인접도배" in json.dumps(details["answer"], ensure_ascii=False)
    use(api, token)
    assert api.get(f"/vendors/{vendor_id}").status_code == 200


def test_vendor_lookup_without_name_requests_name_not_project(api):
    use(api, signup(api))
    out = done(ask(api, "도배 업체 있나요?"))
    assert "업체 이름을 알려 주세요" in json.dumps(out["answer"], ensure_ascii=False)
    assert not any(
        choice["action"] == "start" for card in out["answer"]["cards"] for choice in card["choices"]
    )


def test_three_demo_dobae_vendors_can_be_asked_by_name(api):
    import asyncio

    from app import demo

    rows = asyncio.run(demo.build_dobae_vendor_samples(demo.demo_password()))
    assert {name for name, _, _ in rows} == {"마루와벽", "결도배", "한결인테리어"}
    token = signup(api)
    for name, vendor_id, made in rows:
        assert made
        answer = done(ask(api, f"{name} 업체 정보 알려줘"))
        assert name in json.dumps(answer["answer"], ensure_ascii=False)
        assert answer["answer"]["cards"][0]["choices"][0]["action"] == f"vendor_detail:{vendor_id}"
        assert api.get(f"/vendors/{vendor_id}").status_code == 200
    use(api, token)


def test_public_dobae_detail_requires_customer_login(api):
    from tests.test_visit_request import listed

    vendor_id = listed(api, "vendor-info@mitjip.test", "공개도배")
    api.cookies.clear()
    assert api.get(f"/vendors/{vendor_id}").status_code == 401
    use(api, make_admin(api))
    assert api.get(f"/vendors/{vendor_id}").status_code == 403
    use(api, signup(api))
    assert api.get(f"/vendors/{vendor_id}").status_code == 200


def test_knowledge_index_failure_returns_safe_answer(api, monkeypatch):
    from app.knowledge import service as knowledge

    async def broken(_s):
        raise OSError("index read failed")

    monkeypatch.setattr(knowledge, "index", broken)
    use(api, signup(api))
    out = done(ask(api))
    assert out["rev"] == 2
    assert out["answer"]["unknown"]
    assert not any(s["sources"] for s in out["answer"]["sections"])


def test_answer_drops_old_evidence_if_active_version_changes_during_turn(api, monkeypatch):
    from sqlalchemy import update

    from app.agent.v3 import turn
    from app.db import SessionLocal
    from app.knowledge import service as knowledge
    from app.refprice.models import RefDatasetVersion

    key = "wallpaper-test"
    sql = """
        insert into ref_dataset_versions
        (id, dataset_key, kind, version, source, processed_path, processed_sha256,
         build_profile, embedding_model, embedding_dimensions, provenance, status)
        values ($1, $2, 'knowledge', $3, 'test', 'test', $4, 'test', 'test', 3, '{}'::jsonb, $5)
    """
    old, new = uuid.uuid4(), uuid.uuid4()
    query(sql, old, key, 1, "1" * 64, True)
    query(sql, new, key, 2, "2" * 64, False)

    async def starting_index(_s):
        return replace(repo_index(), version=str(old))

    run = turn.run_turn

    async def switch_after_answer(*args, **kwargs):
        result = await run(*args, **kwargs)
        if result.answer.trace.chunks:
            async with SessionLocal() as s:
                await s.execute(
                    update(RefDatasetVersion)
                    .where(RefDatasetVersion.id == old)
                    .values(status=False)
                )
                await s.execute(
                    update(RefDatasetVersion).where(RefDatasetVersion.id == new).values(status=True)
                )
                await s.commit()
        return result

    monkeypatch.setattr(knowledge, "index", starting_index)
    monkeypatch.setattr(turn, "run_turn", switch_after_answer)
    use(api, signup(api))
    out = done(ask(api))
    assert out["rev"] == 2
    assert out["answer"]["unknown"] and out["answer"]["ask"] is None
    assert not any(s["sources"] for s in out["answer"]["sections"])
    saved = query("select text from chat_messages where role = 'assistant'")
    assert "물걸레" not in saved[0]["text"]


def test_output_check_failure_keeps_unchecked_answer_out_of_response_and_history(api, monkeypatch):
    from app.agent.v3 import check

    def broken(*args, **kwargs):
        raise RuntimeError("check failed")

    monkeypatch.setattr(check, "final", broken)
    use(api, signup(api))
    out = done(ask(api))
    assert out["answer"]["unknown"] and out["answer"]["sections"] == []
    saved = query("select text from chat_messages where role = 'assistant'")
    assert saved[0]["text"] == out["answer"]["unknown"][0]
