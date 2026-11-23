"""견적 설명: 계산 엔진이 낸 표준견적서 줄 · 가견적 차이 · 실측 견적서 비교를 정해 둔 글 + 계산 상자로 풀이한다.

금액을 만들지 않는다. 상자에 옮기는 값은 ProjectFacts(계산 엔진 결과) 그대로다.
업체 파일 · 메모 글은 풀이에 넣지 않는다(데이터 칸만). '파일을 열어 보세요 · 채팅으로 물어보세요'로 넘긴다.
"""

import re

from app.agent.v3 import templates
from app.agent.v3.answer import KB, Part, Task, calc_box, knowledge, line_box
from app.agent.v3.schemas import CalcBox, Choice, StandardLineFact, UiCard
from app.knowledge import bm25
from app.knowledge.suggest import COMMON

ESTIMATE_Q = re.compile(
    r"가견적|업체\s*(?:견적|금액)이?\s*(?:왜|달라|차이)|업체마다|업체끼리|어디가\s*(?:비싸|싸)"
)
VENDOR_QUOTE_Q = re.compile(
    r"실측\s*견적서?|받은\s*견적서|견적서\s*(?:두|세|2|3)\s*(?:곳|장|개)|비교"
)
VAT_Q = re.compile(r"부가세|공급가|VAT", re.IGNORECASE)
# 줄 이름에 없는 말 → 줄 이름 낱말 (화면 말 그대로)
ALIAS = {
    "뜯": "철거",
    "떼": "철거",
    "벽지값": "벽지",
    "밑지": "초배지",
    "빠데": "퍼티",
    "인건비": "시공",
    "품삯": "시공",
}


def _words(t: str) -> set[str]:
    return {g for g in bm25.grams(t) if len(g) == 2 and g not in COMMON}


def match_line(q: str, lines: list[StandardLineFact]) -> StandardLineFact | None:
    for k, v in ALIAS.items():
        if k in q:
            q = f"{q} {v}"
    qw = _words(q)
    best: tuple[float, StandardLineFact] | None = None
    for ln in lines:
        nw = _words(ln.name)
        if not nw:
            continue
        s = len(qw & nw) / len(nw)
        if s > 0 and (best is None or s > best[0]):
            best = (s, ln)
    return best[1] if best and best[0] >= 0.3 else None


def run(task: Task, kb: KB) -> Part:
    part = Part(route="quote_explanation")
    f = task.facts
    q = task.text
    if f is None or f.standard is None:
        part.add(
            templates.section([templates.NO_STANDARD], route="quote_explanation"),
            templates.NO_STANDARD,
        )
        part.cards.append(UiCard(kind="next_step", choices=list(templates.NO_PROJECT_NEXT)))
        return part
    st = f.standard
    lines: list[str] = []
    allowed: list[str] = []
    trade = task.trades[0] if task.trades else None
    if ESTIMATE_Q.search(q) and f.estimates:
        lines.append(templates.ESTIMATE_GAP)
        for e in f.estimates[:3]:
            pct = e.vs_standard
            note = (
                "" if pct is None else f"표준보다 {abs(pct)}% {'많아요' if pct >= 0 else '적어요'}"
            )
            part.calc.append(
                CalcBox(
                    label=f"{e.vendor} 가견적", ref="estimates.total", amount=e.total, note=note
                )
            )
            up = sorted((x for x in e.lines if x.diff > 0), key=lambda x: -x.diff)[:2]
            if up:
                lines.append(
                    templates.ESTIMATE_GAP_TOP.format(
                        vendor=templates.josa(e.vendor, "은", "는"),
                        lines=" · ".join(x.name for x in up),
                    )
                )
                part.calc.extend(
                    CalcBox(
                        label=f"{e.vendor} {x.name}",
                        ref=f"estimates.{x.key}",
                        amount=x.diff,
                        note="줄 차이",
                    )
                    for x in up
                )
            if e.separate:
                lines.append(
                    templates.ESTIMATE_SEPARATE.format(
                        vendor=templates.josa(e.vendor, "은", "는"),
                        lines=" · ".join(e.separate[:3]),
                    )
                )
            allowed.append(e.vendor)
        part.cards.append(
            UiCard(kind="next_step", choices=[Choice(text="업체 보기", action="vendors")])
        )
    elif VENDOR_QUOTE_Q.search(q) and "실측" in q:
        lines.append(templates.QUOTE_COMPARE)
        part.cards.append(
            UiCard(kind="next_step", choices=[Choice(text="실측 견적서 비교", action="quotes")])
        )
    else:
        ln = match_line(q, st.lines)
        if ln is not None:
            key = (ln.key, ln.state)
            text = templates.LINE_STATE_SPECIAL.get(key) or templates.LINE_STATE[ln.state].format(
                name=ln.name
            )
            lines.append(text)
            part.calc.append(line_box(ln))
            trade = trade or ln.trade
            allowed.append(ln.name)
        else:
            lines.append(templates.STANDARD_IS)
            box = calc_box("standard.total", f)
            if box:
                part.calc.append(box)
        if VAT_Q.search(q):
            part.calc.append(CalcBox(label="공급가", ref="standard.supply", amount=st.supply))
            part.calc.append(CalcBox(label="부가세", ref="standard.vat", amount=st.vat))
    part.add(
        templates.section(lines, route="quote_explanation", trade=trade),
        "\n".join([*lines, *allowed, templates.ALLOWED]),
    )
    # 줄 풀이에 이어 용어 · 견적 읽기 카드가 맞으면 붙인다(모름 문장은 붙이지 않음: 이미 계산으로 답함)
    how = knowledge(
        part, kb, q, trade=trade, route="quote_explanation", facts=f, allow_extract=False
    )
    if how == "unknown":
        part.unknown = []
    return part
