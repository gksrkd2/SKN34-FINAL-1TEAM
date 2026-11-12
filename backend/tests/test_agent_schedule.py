"""공정표 에이전트: 근거 선택·실패 처리·LangGraph 호출 계약."""

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from app.agent import llm, schedule_agent
from app.agent.schemas import TurnContext
from app.chat import tools


def test_schedule_selects_only_supplied_facts(monkeypatch):
    ctx = TurnContext(
        user_id=uuid.UUID(int=1), chat_id=uuid.UUID(int=2), project_id=uuid.UUID(int=3)
    )
    assert hasattr(schedule_agent, "ScheduleReply"), "공정표 출력 계약이 필요해요"
    monkeypatch.setattr(
        tools,
        "read_schedule",
        AsyncMock(
            return_value={
                "project_id": str(ctx.project_id),
                "facts": {"order": "목공이 끝난 뒤 도배해요."},
            }
        ),
    )
    llm.set_stub("schedule", schedule_agent.ScheduleReply(facts=["order"]))
    try:
        answer = asyncio.run(schedule_agent.schedule({"ctx": ctx, "message": "순서를 알려줘요"}))[
            "answer"
        ]
        assert not answer.failed and answer.sections[0].sentences == ["목공이 끝난 뒤 도배해요."]
    finally:
        llm.clear_stubs()


@pytest.mark.parametrize("facts", [[], ["invented"], ["order", "order"]])
def test_bad_llm_output_fails_without_fake_explanation(monkeypatch, facts):
    assert hasattr(schedule_agent, "ScheduleReply"), "공정표 출력 계약이 필요해요"
    ctx = TurnContext(
        user_id=uuid.UUID(int=1), chat_id=uuid.UUID(int=2), project_id=uuid.UUID(int=3)
    )
    monkeypatch.setattr(
        tools,
        "read_schedule",
        AsyncMock(
            return_value={
                "project_id": str(ctx.project_id),
                "facts": {"order": "목공이 끝난 뒤 도배해요."},
            }
        ),
    )
    llm.set_stub("schedule", schedule_agent.ScheduleReply.model_construct(facts=facts))
    try:
        assert asyncio.run(schedule_agent.schedule({"ctx": ctx, "message": "순서"}))[
            "answer"
        ].failed
    finally:
        llm.clear_stubs()


def test_wrong_project_does_not_call_llm(monkeypatch):
    assert hasattr(tools, "read_schedule"), "본인 프로젝트 조회 도구가 필요해요"
    ctx = TurnContext(
        user_id=uuid.UUID(int=1), chat_id=uuid.UUID(int=2), project_id=uuid.UUID(int=3)
    )
    monkeypatch.setattr(
        tools,
        "read_schedule",
        AsyncMock(
            return_value={"project_id": str(uuid.UUID(int=4)), "facts": {"order": "목공 먼저"}}
        ),
    )
    call = AsyncMock(side_effect=AssertionError("다른 프로젝트를 LLM에 넘기면 안 돼요"))
    monkeypatch.setattr(llm, "structured", call)
    assert asyncio.run(schedule_agent.schedule({"ctx": ctx, "message": "순서"}))["answer"].failed
    call.assert_not_called()


def test_graph_and_direct_call_match(monkeypatch):
    from app.agent.graph import build_graph, default_nodes
    from app.agent.schemas import Intent

    assert hasattr(schedule_agent, "ScheduleReply"), "공정표 출력 계약이 필요해요"
    ctx = TurnContext(
        user_id=uuid.UUID(int=1), chat_id=uuid.UUID(int=2), project_id=uuid.UUID(int=3)
    )
    monkeypatch.setattr(
        tools,
        "read_schedule",
        AsyncMock(
            return_value={
                "project_id": str(ctx.project_id),
                "facts": {"order": "목공이 끝난 뒤 도배해요."},
            }
        ),
    )
    llm.set_stub("schedule", schedule_agent.ScheduleReply(facts=["order"]))
    state = {
        "ctx": ctx,
        "message": "순서",
        "intent": Intent(label="schedule", confidence=1, source="button"),
    }
    try:
        direct = asyncio.run(schedule_agent.schedule(state))
        graph = asyncio.run(build_graph(default_nodes()).ainvoke(state))
        assert graph["answer"] == direct["answer"] and not graph["answer"].failed
    finally:
        llm.clear_stubs()
