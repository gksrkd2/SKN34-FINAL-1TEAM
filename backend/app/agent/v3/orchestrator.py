"""오케스트레이터 (복합 · 여러 공종만, 깊이 1). agents.md 10.6절.

1. 나누기(코드): 물음 · 이음말로 조각을 나누고 조각마다 관문 2단계를 다시 돌린다(버튼 · 기다림 없이).
   조각이 하나뿐인데 공종이 둘 이상이면 같은 문장을 공종마다 하나씩(공종 칸 고정) 만든다. 최대 3조각.
2. 부르기: 조각마다 담당을 부른다(한 턴 담당 호출 3번까지, 담당끼리는 서로 부르지 않음).
3. 합치기: 하위 묶음을 다시 쓰지 않는다. 공정 순서(계산 엔진 순서 · TradeSpec.order)로 줄 세우고 더하는 것은 셋뿐이다.
   - 순서를 물었을 때(ORDER_ASK)만 앞뒤 공종 쌍의 순서 문장(ORDER_RULES)
   - 같은 글 묶음(같은 공통 카드 등)은 한 번만
   - 못 찾은 공종은 한 줄로 모아 '… 쪽 자세한 내용은 확인하지 못했어요'. 모두 못 찾으면 '확인하지 못했어요'만
"""

import itertools
import re

from app.agent.v3 import gate as gate_mod
from app.agent.v3 import guide, quote_explain, schedule_agent, templates, trade_agent
from app.agent.v3.answer import KB, Part, Task
from app.agent.v3.schemas import Route, Section, Sentence, TradeCode
from app.agent.v3.trades import BY_CODE, ORDER_CITES, ORDER_RULES, mention_spans, rules, trades_in

MAX_PARTS = 3
# 공사 순서 · 같이 하기를 묻는 말. 없으면 순서 문장을 붙이지 않아요('뭐가 더 쉬워요?'에 순서는 딴말)
ORDER_ASK = re.compile(
    r"순서|먼저|나중|뭐부터|무엇부터|어느\s*(?:것|거|걸|쪽)\s*부터|부터\s*해|전에|다음에|끝나고|끝난\s*(?:뒤|후|다음)|"
    r"뒤에|후에|이후|나서|언제|같이|함께|동시에|한꺼번에|한\s*번에|겹치"
)
AGENTS = {
    "guide": guide.run,
    "trade": trade_agent.run,
    "quote_explanation": quote_explain.run,
    "schedule": schedule_agent.run,
}


def split(task: Task) -> list[Task]:
    """조각 Task 목록. 조각의 route가 다시 multi · 되묻기면 공종이 있으면 trade, 없으면 가이드."""
    out: list[Task] = []
    has_project = task.facts is not None
    pieces = gate_mod.parts_of(task.text)
    if len(pieces) >= 2:
        for p in pieces:
            g = gate_mod.gate(p, view=task.ctx.view, has_project=has_project)
            route: Route = g.route
            tr = g.trades or trades_in(p)
            if route in ("multi", "ask_back", "out_of_scope"):
                route = "trade" if tr else "guide"
            if route == "trade" and len(tr) >= 2:
                out.extend(_per_trade(task, p, tr, g.policy))
            else:
                out.append(Task(text=p, route=route, trades=tr[:1] if route == "trade" else tr,
                                policy=g.policy, ctx=task.ctx, facts=task.facts))  # fmt: skip
    else:
        out.extend(_per_trade(task, task.text, task.trades, task.policy))
    if not out:
        out.append(Task(text=task.text, route="guide", trades=[], policy=task.policy, ctx=task.ctx,
                        facts=task.facts))  # fmt: skip
    # 같은 담당 · 같은 공종 조각은 하나로(같은 호출 되풀이 막기)
    seen: set[tuple] = set()
    uniq = []
    for t in out:
        k = (t.route, tuple(t.trades))
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq[:MAX_PARTS]


def _per_trade(task: Task, text: str, tr: list[TradeCode], policy) -> list[Task]:
    if not tr:
        return [
            Task(text=text, route="guide", trades=[], policy=policy, ctx=task.ctx, facts=task.facts)
        ]
    return [
        Task(text=focus(text, t, tr), route="trade", trades=[t], policy=policy, ctx=task.ctx,
             facts=task.facts, asked=text)
        for t in tr
    ]  # fmt: skip


def focus(text: str, trade: str, all_trades: list[str]) -> str:
    """같은 문장을 공종마다 나눌 때 그 공종 낱말이 든 마디 + 문장 끝 물음(예: '순서가 어떻게 돼요?')만 남긴다.

    '몰딩 바꾸고 도배도 새로 하려는데 순서가 어떻게 돼요?' → 목공: '몰딩 바꾸고 … 순서가 어떻게 돼요?'
    이음말로 이어 쓴 문장만 자른다(이음말이 없으면 문장 그대로).
    """
    spans = mention_spans(text, all_trades)
    cuts = [0]
    for a, b in itertools.pairwise(spans):
        m = gate_mod.JOIN.search(text, a[1], b[0])
        if m:
            cuts.append(m.end())
    if len(cuts) < 2:
        return text
    cuts.append(len(text))
    segs = [text[i:j].strip(" ,") for i, j in itertools.pairwise(cuts)]
    mine = next((s for s in segs if BY_CODE[trade].pattern.search(s)), text)
    tail = segs[-1]
    if mine == tail:
        return mine
    for other in all_trades:  # 끝 마디에서 다른 공종 낱말을 떼고 물음만 남긴다
        if other != trade:
            tail = BY_CODE[other].pattern.sub(" ", tail)
    return " ".join(f"{mine} {tail}".split())


def _order_key(i: int, p: Part) -> tuple:
    if p.route == "trade" and p.trade in BY_CODE:
        return (0, BY_CODE[p.trade].order, i)
    return (1, 0, i)


def order_section(pair: tuple[str, str], kb: KB | None) -> Section | None:
    """공종 쌍의 순서 문장. 근거는 계산 엔진 순서 규칙(rule:) · 지식 조각이고 출처 칸에도 붙인다."""
    text = ORDER_RULES.get(pair)
    if not text:
        return None
    cites = list(ORDER_CITES.get(pair, ()))
    rs = rules()
    sources = []
    for c in cites:
        if c in rs:
            sources.append({"ref": c, "title": rs[c]["title"], "kind": "rule", "publisher": "믿집"})
        elif kb is not None and c in kb.ix.by_ref:
            it = kb.ix.by_ref[c]
            sources.append(
                {"ref": c, "title": it.doc.title, "kind": it.doc.kind, "publisher": it.doc.pub}
            )
    return Section(
        title="[순서]", sentences=[Sentence(text=text, cites=cites)], source="template", route="multi",
        sources=sources,
    )  # fmt: skip


def merge(parts: list[Part], kb: KB | None = None, asked: str = "") -> Part:
    """하위 묶음을 그대로 이어 붙인다. 순서 문장(순서를 물었을 때) · 못 찾은 공종 한 줄만 더한다.

    asked: 고객 질문 전체. 비우면 순서를 물은 것으로 봐요(예전 동작).
    """
    out = Part(route="multi")
    ordered = [p for _, p in sorted(enumerate(parts), key=lambda x: _order_key(*x))]
    want_order = not asked or bool(ORDER_ASK.search(asked))
    prev: str | None = None
    seen: set[tuple[str, ...]] = set()
    failed: list[str] = []  # 못 찾은 공종 이름(공종 아닌 담당은 '')
    for p in ordered:
        if want_order and p.route == "trade" and p.trade and prev:
            sec = order_section((prev, p.trade), kb)
            if sec is not None:
                out.sections.append(sec)
        if p.route == "trade" and p.trade:
            prev = p.trade
        if p.empty or (not p.sections and not p.calc and p.unknown):
            named = p.route == "trade" and p.trade in BY_CODE and p.trade != "common"
            failed.append(BY_CODE[p.trade].ko if named else "")
            out.unknown.extend(
                x for x in p.unknown if x != templates.UNKNOWN and x not in out.unknown
            )
        for sec in p.sections:  # 같은 글 묶음(두 공종이 같은 공통 카드를 고름)은 한 번만
            key = tuple(x.text for x in sec.sentences)
            if key in seen:
                continue
            seen.add(key)
            out.sections.append(sec)
        out.calc.extend(p.calc)
        for c in p.cards:
            if c not in out.cards:
                out.cards.append(c)
        out.ask = out.ask or p.ask
        out.suggest.extend(x for x in p.suggest if x not in out.suggest)
        out.used_cards.extend(p.used_cards)
        out.used_chunks.extend(p.used_chunks)
        out.dropped.extend(p.dropped)
    if failed:
        if out.sections or out.calc:  # 답한 공종이 있으면 못 찾은 공종을 한 줄로
            names = list(dict.fromkeys(k for k in failed if k))
            line = (
                templates.PART_FAILED_WHICH.format(which=" · ".join(names))
                if names
                else templates.PART_FAILED
            )
            out.sections.append(templates.section([line], route="multi"))
        else:  # 모두 못 찾음: 공종마다 되풀이하지 않고 '확인하지 못했어요' 한 번 + 확인할 곳
            out.unknown.insert(0, templates.UNKNOWN)
    return out


def run(task: Task, kb: KB) -> tuple[Part, list[str], int]:
    """(합친 몫, 조각 담당 이름들, 실패한 조각 수)"""
    tasks = split(task)
    parts = [AGENTS[t.route](t, kb) for t in tasks]
    failed = sum(1 for p in parts if not p.sections and not p.calc)
    names = [f"{t.route}:{t.trades[0] if t.trades else '-'}" for t in tasks]
    return merge(parts, kb, asked=task.text), names, failed
