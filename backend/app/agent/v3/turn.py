"""한 턴 (무료 · LLM 모드 공통). agents.md 2.1 · 10.1절.

가림 · 길이 → 입구 관문 → (범위 밖 · 되묻기 · 담당 하나 · 오케스트레이터) → [LLM 모드면 글 다시 쓰기] → 출력 검사.
DB · HTTP를 모른다: 프로젝트 사실(ProjectFacts)과 지식(KB)은 chat.service가 짧은 DB 세션으로 읽어 넘긴다.
돌려주는 waiting은 되물은 담당의 칸(다음 턴의 짧은 답을 그 담당이 받게). 저장은 chat.service가 한다.
"""

import re
from dataclasses import dataclass

from app.agent.v3 import check, llm_mode, orchestrator, policy, templates
from app.agent.v3 import gate as gate_mod
from app.agent.v3.answer import KB, T_HIGH, Part, Task
from app.agent.v3.schemas import (
    TEXT_MAX,
    AgentAnswer,
    Button,
    ProjectFacts,
    Trace,
    TurnContext,
    UiCard,
)
from app.agent.v3.trades import BY_CODE, trades_in
from app.knowledge import extract

CHITCHAT = re.compile(r"로또|주식|코인|집값|영화|점심|저녁\s*뭐|강아지|번역|날씨|심심|노래|게임")


@dataclass
class TurnResult:
    answer: AgentAnswer
    waiting: dict | None  # {route, trade, slot, q} — chat.service가 짧게 보관
    label: str | None  # 분류기 라벨(대화 표 intent_label 기록용)
    confidence: float
    source: str


def ignore_words(facts: ProjectFacts | None) -> list[str]:
    """업체 이름(예: '마루와벽')을 공종 낱말로 읽지 않게."""
    if facts is None:
        return []
    names = list(facts.visit_draft.vendors) if facts.visit_draft else []
    if facts.visit_draft:
        names += [r.vendor for r in facts.visit_draft.ranges]
    names += [e.vendor for e in facts.estimates]
    return sorted({n for n in names if n}, key=len, reverse=True)


def _out(text: str, flags: list[str], facts: ProjectFacts | None) -> Part:
    part = Part(route="out_of_scope")
    found = trades_in(text)
    if "legal_case" in flags:
        line = templates.OUT["legal"]
    elif policy.calc_request(text) and found and not BY_CODE[found[0]].calc:
        line = templates.OUT["calc"].format(ko=BY_CODE[found[0]].ko)
    elif CHITCHAT.search(text):
        line = templates.OUT["chat"]
    else:
        line = templates.OUT["service"]
    part.add(templates.section([line], route="out_of_scope"), f"{line}\n{templates.ALLOWED}")
    nxt = templates.NEXT.get(facts.stage, []) if facts else templates.NO_PROJECT_NEXT
    part.cards.append(UiCard(kind="next_step", choices=list(nxt)))
    return part


async def _llm(part: Part, kb: KB, question: str) -> tuple[Part, list[str]]:
    """카드 · 조각 묶음만 다시 쓴다. 점수 높은 카드 · 정해 둔 글 · 계산 묶음은 그대로(카드 먼저)."""
    used: list[str] = []
    calc = " ".join(
        " ".join(
            [
                c.label,
                f"{c.qty}{c.unit}" if c.qty is not None else "",
                f"{c.amount}원" if c.amount is not None else "",
                f"{c.unit_price}원" if c.unit_price is not None else "",
                c.note,
            ]
        )
        for c in part.calc
    )
    out = []
    for sec in part.sections:
        tag = next((c for x in sec.sentences for c in x.cites if c.startswith("card:")), None)
        if sec.source == "card" and tag and part.scores.get(tag, 0) >= T_HIGH:
            out.append(sec)
            continue
        refs = {c for x in sec.sentences for c in x.cites if c.startswith("K")}
        evidence = {r: kb.ix.by_ref[r].chunk.text for r in sorted(refs) if r in kb.ix.by_ref}
        if not evidence or any(
            extract.display_of(kb.ix.by_ref[r].doc.kind) == "none" for r in evidence
        ):
            out.append(sec)
            continue
        new, dropped = await llm_mode.rewrite(
            sec, route=sec.route, question=question, evidence=evidence, calc=calc
        )
        part.dropped.extend(dropped)
        if new is not None and sec.route in llm_mode.VERSION:
            used.append(sec.route)
        out.append(new or sec)
    part.sections = out
    return part, list(dict.fromkeys(used))


async def run_turn(
    text: str,
    ctx: TurnContext,
    *,
    kb: KB,
    facts: ProjectFacts | None = None,
    waiting: dict | None = None,
    button: Button | None = None,
) -> TurnResult:
    masked = check.mask(text or "").strip()[:TEXT_MAX]
    w_route = waiting.get("route") if waiting else None
    g = gate_mod.gate(
        masked, view=ctx.view, waiting=w_route, waiting_trade=waiting.get("trade") if waiting else None,
        button=button, has_project=facts is not None,
        ignore=ignore_words(facts),
    )  # fmt: skip
    slot = value = None
    asked = ""
    waiting_slot = waiting.get("slot") if waiting else None
    if (
        waiting
        and waiting_slot
        and (
            g.source == "waiting"
            or (
                button is not None
                and button.slot == waiting_slot
                and button.trade in (None, waiting.get("trade"))
            )
        )
    ):
        slot = waiting_slot
        value = (button.value if button and button.value else masked)[:40]
        asked = waiting.get("q", "")
        if waiting.get("trade"):
            g = g.model_copy(update={"route": "trade", "trades": [waiting["trade"]]})
    task = Task(text=masked, route=g.route, trades=g.trades, policy=g.policy, ctx=ctx, facts=facts,
                slot=slot, value=value, asked=asked)  # fmt: skip
    names: list[str] = []
    failed_parts = 0
    if g.route == "out_of_scope":
        part = _out(masked, g.policy, facts)
    elif g.route == "ask_back":
        part = Part(route="ask_back")
        part.add(templates.section([templates.ASK_BACK], route="ask_back"), templates.ASK_BACK)
        part.cards.append(UiCard(kind="choices", title="궁금한 것", choices=g.candidates))
    elif g.route == "multi":
        part, names, failed_parts = orchestrator.run(task, kb)
    else:
        part = orchestrator.AGENTS[g.route](task, kb)
    routes: list[str] = []
    if ctx.mode == "llm" and g.route in ("guide", "trade", "multi"):
        part, routes = await _llm(part, kb, masked)
    prompts = [llm_mode.VERSION[r] for r in routes]
    ans = AgentAnswer(
        sections=part.sections, calc=part.calc, cards=part.cards, ask=part.ask,
        unknown=part.unknown, suggest=part.suggest[:3],
        trace=Trace(
            route=g.route, source=g.source, confidence=g.confidence, trades=g.trades, policy=g.policy,
            mode=ctx.mode, cards=list(dict.fromkeys(part.used_cards)),
            chunks=list(dict.fromkeys(part.used_chunks)), prompts=prompts, card_set=kb.cards.version,
            parts=names, failed_parts=failed_parts, dropped=part.dropped,
        ),
    )  # fmt: skip
    ans = check.final(ans, prompt="\n".join(llm_mode.prompt(r) for r in routes) or None)
    new_wait = None
    if ans.ask is not None:
        new_wait = {"route": "trade", "trade": ans.ask.trade, "slot": ans.ask.slot, "q": masked}
    return TurnResult(ans, new_wait, g.label, g.confidence, g.source)
