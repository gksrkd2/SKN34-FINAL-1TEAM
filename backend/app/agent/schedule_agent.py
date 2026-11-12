"""공정표 설명. 수치·일정 생성과 저장은 하지 않고 확인한 근거만 선택한다."""

import json
import logging
from pathlib import Path
from time import perf_counter

from pydantic import BaseModel, Field, ValidationError

from app.agent import llm
from app.agent.schemas import HISTORY_LIMIT, AgentAnswer, ChatState, Section
from app.chat import tools
from app.shared.errors import AppError

PROMPT = Path(__file__).with_name("prompts").joinpath("schedule_v1.md").read_text(encoding="utf-8")
logger = logging.getLogger(__name__)


class ScheduleReply(BaseModel):
    """LLM이 새 사실을 쓰지 않고 도구에서 받은 근거를 선택한다."""

    facts: list[str] = Field(min_length=1, max_length=8)


async def schedule(state: ChatState) -> dict:
    """도구 DB 세션은 LLM 호출 전에 닫힌다. 로그에는 문장·ID·예외 본문을 남기지 않는다."""
    started = perf_counter()
    status, calls = "failed", 0
    answer = AgentAnswer(failed=True)
    try:
        ctx = state.get("ctx")
        if ctx is None or ctx.project_id is None:
            return {"answer": answer}
        context = await tools.read_schedule(ctx)
        if (
            not context
            or context.get("project_id") != str(ctx.project_id)
            or not context.get("facts")
        ):
            return {"answer": answer}
        messages = [("system", PROMPT)]
        messages.extend(
            (turn.role, turn.text) for turn in state.get("history", [])[-HISTORY_LIMIT:]
        )
        messages.append(
            (
                "user",
                json.dumps(
                    {"question": state.get("message", ""), "facts": context["facts"]},
                    ensure_ascii=False,
                ),
            )
        )
        calls = 1
        reply = await llm.structured("schedule", ScheduleReply, messages)
        facts = reply.facts
        if (
            not facts
            or len(set(facts)) != len(facts)
            or any(key not in context["facts"] for key in facts)
        ):
            return {"answer": answer}
        answer = AgentAnswer(
            sections=[Section(title="[공정표]", sentences=[context["facts"][key] for key in facts])]
        )
        status = "ok"
        return {"answer": answer}
    except (llm.LLMError, ValidationError, AppError):
        return {"answer": answer}
    finally:
        logger.info(
            "schedule_agent status=%s elapsed_ms=%.3f llm_calls=%d prompt=schedule_v1",
            status,
            (perf_counter() - started) * 1000,
            calls,
        )
