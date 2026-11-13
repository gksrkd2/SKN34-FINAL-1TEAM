"""공정표 설명. 수치·일정 생성과 저장은 하지 않고 확인한 근거만 선택한다."""

import json
import logging
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent import llm
from app.agent.schemas import HISTORY_LIMIT, AgentAnswer, ChatState, Section
from app.chat import tools
from app.shared.errors import AppError

PROMPT = Path(__file__).with_name("prompts").joinpath("schedule_v1.md").read_text(encoding="utf-8")
logger = logging.getLogger(__name__)


class ReferenceSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apartment_type: str = Field(min_length=1, max_length=30)


class ReferenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=40)
    space_ids: list[str] = Field(min_length=1, max_length=30)
    quantity_key: str | None = Field(default=None, min_length=1, max_length=100)
    assumed_quantity: Decimal | None = Field(
        default=None, gt=0, le=1000, max_digits=7, decimal_places=2
    )
    quantity_reason: str | None = Field(default=None, min_length=1, max_length=300)
    rate_key: str = Field(min_length=1, max_length=100)
    assumed_crew: int | None = Field(default=None, strict=True, ge=1, le=10)
    assumption_reason: str | None = Field(default=None, min_length=1, max_length=300)


class ReferenceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ReferenceItem] = Field(max_length=120)


async def plan_references(ctx, context, start):
    """LLM은 조회 조건과 근거 ID만 고른다. 계산과 저장 권한은 갖지 않는다."""
    prompt = (
        Path(__file__)
        .with_name("prompts")
        .joinpath("schedule_reference_v1.md")
        .read_text(encoding="utf-8")
    )
    started = perf_counter()
    status = "failed"
    try:
        search = await llm.structured(
            "schedule",
            ReferenceSearch,
            [
                ("system", prompt),
                ("user", json.dumps({"step": "search", **context}, ensure_ascii=False)),
            ],
        )
        if search.apartment_type not in context["apartments"]:
            raise AppError(
                422, "schedule_reference_required", "해당 면적의 참고 평면도를 확인해 주세요."
            )
        candidates = await tools.search_schedule_references(ctx, start, search.apartment_type)
        selection = await llm.structured(
            "schedule",
            ReferenceSelection,
            [
                ("system", prompt),
                (
                    "user",
                    json.dumps(
                        {
                            "step": "select",
                            "input": context["input"],
                            "tasks": context["tasks"],
                            "candidates": candidates,
                        },
                        ensure_ascii=False,
                    ),
                ),
            ],
        )
        prepared = await tools.calculate_schedule_references(
            ctx, start, context, candidates, selection
        )
        status = "ok"
        return prepared
    except (llm.LLMError, ValidationError) as exc:
        raise AppError(
            503,
            "schedule_agent_unavailable",
            "공정표 기준 자료를 선택하지 못했어요. 잠시 뒤 다시 시도해 주세요.",
        ) from exc
    finally:
        logger.info(
            "schedule_reference_agent status=%s elapsed_ms=%.3f prompt=schedule_reference_v1",
            status,
            (perf_counter() - started) * 1000,
        )


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
