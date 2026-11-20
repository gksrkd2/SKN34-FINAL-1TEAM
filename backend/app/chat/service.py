"""대화 저장 · 복원 (#86 1단계). 한 턴 순서는 협약 8절.

열기(get_for) → 최근 대화(recent) → [에이전트 실행: 여기서 하지 않음] → 저장(save_turn).
동시 요청은 기한 잠금 대신 번호표(rev)로 막는다: 시작할 때 본 rev와 저장할 때 rev가 다르면 409.
"""

import hashlib
import json
import uuid
from datetime import date

from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schemas import HISTORY_LIMIT, Intent, IntentLabel, Turn
from app.agent.v3.check import mask
from app.chat import repository
from app.chat.models import Chat
from app.chat.schemas import (
    AiAnswer,
    AiFaqOut,
    AiMessageOut,
    AiSource,
    AiThreadDetailOut,
    AiThreadOut,
    AiTurnAnalysis,
    AiTurnIn,
    AiTurnOut,
    section_out,
)
from app.shared.errors import AppError

NOT_FOUND = "대화를 찾을 수 없어요."
CONFLICT = "다른 질문이 먼저 저장됐어요. 다시 물어봐 주세요."


async def create(s: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    """새 대화방. 만든 대화 id를 돌려준다."""
    chat = await repository.insert_chat(s, user_id)
    await s.commit()
    return chat.id


async def get_for(s: AsyncSession, user_id: uuid.UUID, chat_id: uuid.UUID) -> Chat:
    """내 대화를 연다. 남의 대화 · 없는 대화는 똑같이 404 (있는지 없는지 알려 주지 않는다)."""
    chat = await repository.get_owned(s, user_id, chat_id)
    if chat is None:
        raise AppError(404, "not_found", NOT_FOUND)
    return chat


async def recent(s: AsyncSession, chat: Chat) -> list[Turn]:
    """에이전트에 넘길 지난 메시지. 오래된 것부터 HISTORY_LIMIT줄 (ChatState.history).

    chat은 get_for()가 돌려준 것만 넘긴다. 주인 확인을 건너뛰고 id로 남의 대화를 읽지 못하게.
    """
    rows = await repository.recent_messages(s, chat.id, HISTORY_LIMIT)
    return [Turn(role=m.role, text=m.text) for m in rows]


async def save_turn(
    s: AsyncSession,
    *,
    user_id: uuid.UUID,
    chat_id: uuid.UUID,
    rev: int,
    question: str,
    answer: str,
    intent: Intent | None,
    waiting: IntentLabel | None,
) -> int:
    """한 턴을 저장하고 새 rev를 돌려준다.

    rev: get_for() 때 본 번호표. 그사이 다른 질문이 먼저 저장했으면 아무것도 쓰지 않고 409.
    순서: 번호표 확인 · 올리기(조건부 UPDATE) → 질문 · 답 두 줄 → commit. 한 트랜잭션이다.
    """
    if not await repository.bump_rev(s, user_id, chat_id, rev, waiting):
        await s.rollback()
        raise AppError(409, "conflict", CONFLICT)
    repository.add_turn(s, chat_id, rev, question, answer, intent.model_dump() if intent else None)
    await s.commit()
    return rev + 1


# ---- v3 AI 칸 (/ai/*). 한 턴: 짧은 세션으로 열기 · 사실 읽기 → 에이전트(세션 없음) → 새 세션으로 저장 ----
AI_PER_HOUR = 60  # 사용자별 시간당 질문 수(APP_ENV=local이면 세지 않음, v2와 같음)
FAQ_PER_MIN = 60  # 로그인 전 자주 묻는 질문: IP 해시별 분당
TOO_MANY = "AI에게 너무 많이 물었어요. 잠시 뒤에 다시 해 주세요."
WAIT_TTL = 1800  # 되물은 칸은 30분 뒤 비운다(agents.md 5절)
REPLAY_TTL = 600  # 같은 client_key는 10분 동안 처음 답을 그대로 돌려준다
THREAD_MESSAGES = 30
# 대화 표 CHECK(0022)에 맞춘 라벨 · 출처. v3 가이드 · 복합은 v2처럼 orchestrator로 남긴다
LABEL = {
    "guide": "orchestrator",
    "multi": "orchestrator",
    "trade": "trade",
    "quote_explanation": "quote_explanation",
    "schedule": "schedule",
    "out_of_scope": "out_of_scope",
}
SOURCE = {"button": "button", "waiting": "rule", "rule": "rule", "model": "model"}


def mode() -> str:
    """키가 있고 LLM_LIVE=true일 때만 LLM 모드. 아니면 무료 모드(기본)."""
    from app.config import settings

    return "llm" if settings.llm_live and settings.openai_api_key.get_secret_value() else "free"


def _key(*parts: str) -> str:
    return "ai:" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


async def _get_json(key: str) -> dict | None:
    from app.queue import redis

    try:
        raw = await redis.get(key)
    except RedisError:
        return None
    return json.loads(raw) if raw else None


async def _set_json(key: str, value: dict | None, ttl: int) -> None:
    from app.queue import redis

    try:
        if value is None:
            await redis.delete(key)
        else:
            await redis.set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
    except RedisError:
        return  # 지워져도 되는 값(되묻기 칸 · 같은 요청 되돌림)이라 Redis가 멈춰도 답은 낸다


def _public(ans) -> AiAnswer:
    return AiAnswer(
        sections=[section_out(x) for x in ans.sections],
        calc=ans.calc,
        cards=ans.cards,
        ask=ans.ask,
        unknown=ans.unknown,
        suggest=ans.suggest,
        failed=ans.failed,
    )


async def ai_turn(user, body: AiTurnIn, today: date) -> AiTurnOut:
    """POST /ai/turns. 남의 대화 · 남의 프로젝트는 404, 같은 대화 동시 저장은 409."""
    from app.agent.v3.answer import KB
    from app.agent.v3.schemas import TurnContext
    from app.agent.v3.turn import run_turn
    from app.chat import tools
    from app.config import settings
    from app.db import SessionLocal
    from app.knowledge import cards
    from app.knowledge import service as knowledge
    from app.shared import ratelimit

    if settings.app_env != "local":
        await ratelimit.hit(ratelimit.key("ai-turn", str(user.id)), AI_PER_HOUR, 3600, TOO_MANY)
    replay_key = _key("ck", str(user.id), body.client_key)
    old = await _get_json(replay_key)
    if old is not None:
        return AiTurnOut.model_validate(old)
    rev, waiting_label = 1, None
    facts = None
    async with SessionLocal() as s:  # 짧게 열고 닫는다. 답을 만드는 동안 세션을 쥐지 않는다
        if body.chat_id is not None:
            chat = await get_for(s, user.id, body.chat_id)
            rev, waiting_label = chat.rev, chat.waiting
        if body.context.project_id is not None:
            facts = await tools.project_facts(
                s, user, body.context.project_id, today, body.context.ids
            )
        kb = KB(ix=await knowledge.index(s), cards=cards.default())
    wait_key = _key("wait", str(body.chat_id)) if body.chat_id else None
    waiting = await _get_json(wait_key) if wait_key and waiting_label else None
    if waiting is None and waiting_label:
        waiting = {"route": waiting_label}
    ctx = TurnContext(
        user_id=user.id,
        chat_id=body.chat_id,
        project_id=body.context.project_id,
        view=body.context.screen,
        mode=mode(),  # type: ignore[arg-type]
        today=today,
    )
    res = await run_turn(body.text, ctx, kb=kb, facts=facts, waiting=waiting, button=body.button)
    ans = res.answer
    chat_id, new_rev = body.chat_id, None
    if not ans.failed:
        label = LABEL.get(ans.trace.route)
        intent = (
            {"label": label, "confidence": res.confidence, "source": SOURCE[res.source]}
            if label
            else None
        )
        # 공사 id는 위에서 project.service.get_for(세션 사용자 기준, 남의 것 404)로 확인한 것만 적는다
        pid = body.context.project_id if facts is not None else None
        async with SessionLocal() as s:
            if chat_id is None:  # 새 대화: 대화방 · 첫 턴을 한 트랜잭션으로
                chat_id = (await repository.insert_chat(s, user.id, project_id=pid)).id
            elif pid is not None:
                await repository.set_project(s, user.id, chat_id, pid)
            if not await repository.bump_rev(
                s, user.id, chat_id, rev, "trade" if res.waiting else None
            ):
                await s.rollback()
                raise AppError(409, "conflict", CONFLICT)
            repository.add_turn(s, chat_id, rev, mask(body.text), ans.plain(), intent)
            await s.commit()
        new_rev = rev + 1
        await _set_json(_key("wait", str(chat_id)), res.waiting, WAIT_TTL)
    trace = ans.trace
    out = AiTurnOut(
        chat_id=chat_id,
        rev=new_rev,
        mode=ctx.mode,
        route=trace.route,
        answer=_public(ans),
        analysis=AiTurnAnalysis(
            classification=trace.source, trades=trace.trades,
            participants=trace.parts, prompts=trace.prompts,
        ),
    )  # fmt: skip
    await _set_json(replay_key, out.model_dump(mode="json"), REPLAY_TTL)
    return out


async def ai_threads(s: AsyncSession, user, project_id: uuid.UUID | None) -> list[AiThreadOut]:
    """GET /ai/threads. project_id를 주면 내 프로젝트인지 먼저 보고(남의 것 404) 그 공사 대화만 돌려준다."""
    if project_id is not None:
        from app.project import service as project_service

        await project_service.get_for(s, user, project_id)
    chats = await repository.list_chats(s, user.id, project_id=project_id)
    last = await repository.last_questions(s, [c.id for c in chats])
    return [
        AiThreadOut(chat_id=c.id, updated_at=c.updated_at, preview=last.get(c.id, "")[:40])
        for c in chats
    ]


async def ai_thread(s: AsyncSession, user, chat_id: uuid.UUID) -> AiThreadDetailOut:
    chat = await get_for(s, user.id, chat_id)
    rows = await repository.recent_messages(s, chat.id, THREAD_MESSAGES)
    return AiThreadDetailOut(
        chat_id=chat.id,
        rev=chat.rev,
        messages=[AiMessageOut(role=m.role, text=m.text, turn=m.turn) for m in rows],
    )


async def faq(ip: str) -> list[AiFaqOut]:
    """GET /ai/faq (로그인 없음). 검증된 카드 파일만 읽는다."""
    from app.knowledge import cards
    from app.shared import ratelimit

    await ratelimit.hit(ratelimit.key("ai-faq", ip), FAQ_PER_MIN, 60, TOO_MANY)
    out = []
    for c in cards.guest_cards(cards.default()):
        out.append(
            AiFaqOut(
                id=c.id,
                question=c.keys[0],
                sentences=[x.text for x in c.answer],
                tip=c.tip.text if c.tip else "",
                sources=[AiSource(ref=x.ref, title=x.title, kind=x.kind) for x in c.sources],
                actions=c.actions,
            )
        )
    return out
