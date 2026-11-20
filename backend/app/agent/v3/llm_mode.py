"""LLM 모드 층 (키가 있을 때만, ADR agent-06). 같은 관문 · 정책 · 카드 · 검색 · 출력 검사 위에 글쓰기 하나만 더한다.

- 부르는 곳은 `app/agent/llm.py` 한 곳(structured). LLM_LIVE=false면 이 층은 켜지지 않는다(턴이 mode=free).
- 카드 먼저: 카드 점수가 아주 높거나(answer.T_HIGH 이상) 정해 둔 글 · 계산 묶음이면 부르지 않는다.
- 받는 것: 지시문(판 번호) + 카드 · 조각 · 계산 결과(데이터 칸, 경계 문자 이스케이프) + 고객 글(가린 뒤).
  업체가 쓴 글은 넣지 않는다(LM-02). 사용자 · 프로젝트 id는 넣지 않는다(LM-03).
- 실패 · 시간 초과 · 검사 뒤 남은 문장 없음 → 그 몫은 무료 모드 묶음 그대로(가짜 설명으로 채우지 않음).
"""

import json
import re
from functools import cache
from pathlib import Path

from pydantic import BaseModel

from app.agent import llm
from app.agent.v3 import check
from app.agent.v3.schemas import Section, Sentence
from app.knowledge import bm25

PROMPTS = Path(__file__).with_name("prompts")
VERSION = {
    "guide": "guide_v1",
    "trade": "trade_v1",
    "quote_explanation": "quote_explanation_v1",
    "schedule": "schedule_v1",
}
ROLE = {
    "guide": "orchestrator",
    "trade": "trade",
    "quote_explanation": "quote_explanation",
    "schedule": "schedule",
}
_TAG = re.compile(r"</?\s*(?:자료|data|system|지시)[^>]*>", re.IGNORECASE)


class LlmSentence(BaseModel):
    text: str
    cites: list[str]


class LlmSection(BaseModel):
    sentences: list[LlmSentence]


@cache
def prompt(route: str) -> str:
    return (PROMPTS / f"{VERSION[route]}.md").read_text(encoding="utf-8")


def _data(s: str) -> str:
    """데이터 칸의 경계 문자 · 숨은 글자를 떼어 지시로 읽히지 않게 한다(LM-01)."""
    return _TAG.sub(" ", re.sub("[\u200b-\u200f\u2060\ufeff]", "", s or ""))


async def rewrite(
    sec: Section, *, route: str, question: str, evidence: dict[str, str], calc: str
) -> tuple[Section | None, list[str]]:
    """free 묶음(카드 · 조각)을 질문에 맞게 다시 쓴다. 쓸 수 없으면 (None, 이유)."""
    if sec.source not in ("card", "extract") or route not in VERSION:
        return None, []
    system = prompt(route)
    data = {
        "근거": [{"ref": k, "글": _data(v)} for k, v in evidence.items()],
        "지금 답(무료 모드)": [x.text for x in sec.sentences],
        "계산 결과": _data(calc),
    }
    messages = [
        ("system", system),
        (
            "user",
            f"<자료>{json.dumps(data, ensure_ascii=False)}</자료>\n고객 질문: {check.mask(question)}",
        ),
    ]
    try:
        out = await llm.structured(ROLE[route], LlmSection, messages)  # type: ignore[arg-type]
    except llm.LLMError:
        return None, ["LLM 실패"]
    new = Section(
        title=sec.title, source="llm", route=sec.route, trade=sec.trade, sources=sec.sources,
        sentences=[Sentence(text=x.text, cites=x.cites) for x in out.sentences][:4],
    )  # fmt: skip
    allowed = "\n".join([*evidence.values(), calc, question])
    kept, dropped = check.section(
        new,
        allowed,
        calc_text=calc,
        shown=set(evidence),
        prompt=system,
    )
    if kept is None:
        return None, dropped
    provided = {ref: set(bm25.grams(text)) for ref, text in evidence.items()}
    sentences = []
    for sent in kept.sentences:
        grams = set(bm25.grams(sent.text))
        if all(bm25.cite_matches(grams, provided[ref]) for ref in sent.cites):
            sentences.append(sent)
        else:
            dropped.append("인용 조각과 맞지 않는 문장")
    if not sentences:
        return None, dropped
    refs = {ref for sent in sentences for ref in sent.cites}
    sources = [s for s in kept.sources if s.get("ref") in refs]
    return kept.model_copy(update={"sentences": sentences, "sources": sources}), dropped
