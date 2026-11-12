"""에이전트에 제공하는 읽기 도구. 사용자·프로젝트는 서버가 정한 ctx만 받는다."""

from app.agent.schemas import TurnContext
from app.db import SessionLocal


async def read_schedule(ctx: TurnContext) -> dict | None:
    from app.schedule.expected import agent_context

    if ctx.project_id is None:
        return None
    async with SessionLocal() as session:
        return await agent_context(session, ctx.user_id, ctx.project_id)
