"""AI 칸 API 모양 (`/ai/*`, api.md 16절). 사용자 · 프로젝트는 서버가 세션으로 고정하고 LLM 출력으로 받지 않는다.

- 질문 분석은 분류·공종·담당·프롬프트 버전만 내보낸다.
- 출처는 이름 · 종류만 보낸다(주소 없음: `repo:` 같은 저장소 경로가 링크로 보이지 않게).
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.v3.schemas import (
    TEXT_MAX,
    AskSlot,
    Button,
    CalcBox,
    GateSource,
    Route,
    Section,
    TradeCode,
    UiCard,
    View,
)

NOTICE = "자동 답변"  # 9판 C09 · D01 화면 글(답 위 표시)
FIRST_NOTICE = "AI가 답해요. 금액 · 날짜는 믿집 계산을 그대로 보여 주고, 저장 · 확정은 하지 않아요."


class AiContext(BaseModel):
    screen: View | None = None
    project_id: uuid.UUID | None = None
    ids: list[str] = Field(default=[], max_length=5)


class AiTurnIn(BaseModel):
    client_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    text: str = Field(min_length=1, max_length=TEXT_MAX)
    chat_id: uuid.UUID | None = None
    context: AiContext = AiContext()
    button: Button | None = None


class AiSource(BaseModel):
    ref: str
    title: str
    kind: str = ""


class AiSection(BaseModel):
    title: str = ""
    sentences: list[str]
    caution: str = ""
    tip: str = ""
    source: Literal["card", "extract", "llm", "template", "calc"]
    route: Route
    trade: str | None = None
    sources: list[AiSource] = []


class AiAnswer(BaseModel):
    notice: str = NOTICE
    sections: list[AiSection] = []
    calc: list[CalcBox] = []
    cards: list[UiCard] = []
    ask: AskSlot | None = None
    unknown: list[str] = []
    suggest: list[str] = []
    failed: bool = False


class AiTurnAnalysis(BaseModel):
    classification: GateSource
    trades: list[TradeCode] = []
    participants: list[str] = []
    prompts: list[str] = []


class AiTurnOut(BaseModel):
    """SSE `done` 이벤트 몸통."""

    chat_id: uuid.UUID | None
    rev: int | None
    mode: Literal["free", "llm"]
    route: Route
    answer: AiAnswer
    analysis: AiTurnAnalysis | None = None  # 이전 요청의 Redis 재응답과 호환


class AiThreadOut(BaseModel):
    chat_id: uuid.UUID
    updated_at: datetime
    preview: str  # 마지막 질문 앞 40자


class AiMessageOut(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    turn: int


class AiThreadDetailOut(BaseModel):
    chat_id: uuid.UUID
    rev: int
    messages: list[AiMessageOut]


class AiFaqOut(BaseModel):
    """로그인 전 자주 묻는 질문 칩(카드 파일만 읽음, 서버 계산 없음)."""

    id: str
    question: str
    sentences: list[str]
    tip: str = ""
    sources: list[AiSource] = []
    actions: list[str] = []


def section_out(s: Section) -> AiSection:
    src = []
    for x in s.sources:
        ref = str(x.get("ref") or "")
        if ref:
            src.append(
                AiSource(
                    ref=ref, title=str(x.get("title") or "")[:80], kind=str(x.get("kind") or "")
                )
            )
    return AiSection(
        title=s.title,
        sentences=[x.text for x in s.sentences],
        caution=s.caution.text if s.caution else "",
        tip=s.tip.text if s.tip else "",
        source=s.source,
        route=s.route,
        trade=s.trade,
        sources=src,
    )
