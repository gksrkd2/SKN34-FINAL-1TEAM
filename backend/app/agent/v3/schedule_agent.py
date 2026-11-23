"""일정: 예상 · 확정 공정표 읽어 주기 + 실측 일정 AI 초안 카드(L22, 여섯 번째 에이전트 없음).

- 날짜 · 기간 · 추천일은 계산 엔진 결과(ProjectFacts.plan · visit_draft)만 옮긴다. AI는 날짜를 만들지 않는다.
- 초안 카드: 희망 기간 · 보낼 업체 · [고른 곳에 요청 보내기]. 보내기 · 날짜 확정은 고객이 누른다(AI는 안 함).
- 업체가 기간을 답한 뒤: 추천 반나절 + 쓴 산식(계산 엔진 basis) + [달력에서 고르기].
"""

import re

from app.agent.v3 import templates
from app.agent.v3.answer import KB, Part, Task, calc_box, knowledge
from app.agent.v3.schemas import CalcBox, Choice, DraftCard, UiCard

VISIT_Q = re.compile(r"실측|방문\s*(?:날|일정)|희망\s*기간|반나절|언제\s*와|요청\s*(?:보내|초안)")
CHANGE_Q = re.compile(r"연기|미루|미뤄|바꾸|바꿔|변경|늦춰|당겨|당길")
VISIT_VIEWS = ("visit_request", "visits", "pick")
PLAN_CHANGE = "날짜 바꾸기는 공정표 화면에서 해요. 업체가 연기를 요청하면 승인할지 직접 골라요."


def _draft(part: Part, task: Task) -> list[str]:
    f = task.facts
    d = f.visit_draft if f else None
    md = templates.md
    if d and d.recommend and d.ranges:
        rec = d.recommend
        rng = next((r for r in d.ranges if r.vendor == rec.vendor), d.ranges[0])
        wish = md(d.basis_start) if d.basis_start else ""
        text = templates.DRAFT_AFTER.format(
            vendor_ga=templates.josa(rng.vendor, "이", "가"),
            range=f"{md(rng.start)}~{md(rng.end)}",
            wish=wish,
            day=md(rec.day),
        )
        part.calc.append(CalcBox(label="추천일", ref="visit_draft.recommend", note=rec.basis))
        part.cards.append(
            UiCard(
                kind="open_calendar",
                choices=[
                    Choice(
                        text=f"달력에서 {md(rec.day)} 고르기",
                        action="calendar",
                        value=rec.day.isoformat(),
                    )
                ],
            )
        )
        return [text, templates.DRAFT_RULE]
    if d and d.wish_start and d.wish_end:
        part.calc.append(
            CalcBox(
                label="실측 희망 기간",
                ref="visit_draft.window",
                note=f"{md(d.wish_start)} ~ {md(d.wish_end)}"
                + (f" · {d.formula}" if d.formula else ""),
            )
        )
        n = len(d.vendors)
        part.cards.append(
            UiCard(
                kind="visit_draft",
                title="실측 일정 요청 · 초안",
                draft=DraftCard(
                    wish_start=d.wish_start,
                    wish_end=d.wish_end,
                    vendors=d.vendors,
                    formula=d.formula,
                ),
                choices=[
                    Choice(
                        text=f"고른 {n}곳에 요청 보내기" if n else "요청서 열기",
                        action="visits_new",
                    )
                ],
            )
        )
        return [templates.DRAFT_BEFORE, templates.DRAFT_RULE]
    part.cards.append(
        UiCard(kind="open_calendar", choices=[Choice(text="달력 열기", action="calendar")])
    )
    return [templates.DRAFT_NO_WISH]


def _plan(part: Part, task: Task) -> list[str]:
    f = task.facts
    if f is None or f.plan is None:
        part.cards.append(UiCard(kind="next_step", choices=list(templates.NO_PROJECT_NEXT)))
        return [templates.PLAN_NONE]
    box = calc_box("plan.range", f)
    if box:
        part.calc.append(box)
    for b in f.plan.blocks[:8]:
        part.calc.append(
            CalcBox(
                label=b.ko,
                ref=f"plan.blocks.{b.trade}",
                note=f"{templates.md(b.start)} ~ {templates.md(b.end)}",
            )
        )
    out = [templates.PLAN_READ]
    if CHANGE_Q.search(task.text):
        out.append(PLAN_CHANGE)
    part.cards.append(UiCard(kind="next_step", choices=[Choice(text="공정표 보기", action="plan")]))
    return out


def run(task: Task, kb: KB) -> Part:
    part = Part(route="schedule")
    visit = bool(VISIT_Q.search(task.text)) or task.ctx.view in VISIT_VIEWS
    lines = _draft(part, task) if visit else _plan(part, task)
    filled = " ".join(c.note for c in part.calc) + " " + " ".join(lines)
    part.add(
        templates.section(lines, route="schedule"),
        "\n".join([*lines, filled, templates.ALLOWED]),
        calc_text=filled,
    )
    # 순서 · 준비 질문이면 공통 카드를 덧붙인다(숫자 없음). 모름 문장은 붙이지 않는다
    how = knowledge(part, kb, task.text, trade=task.trades[0] if task.trades else None, route="schedule",
                    facts=task.facts, allow_extract=False)  # fmt: skip
    if how == "unknown":
        part.unknown = []
    return part
