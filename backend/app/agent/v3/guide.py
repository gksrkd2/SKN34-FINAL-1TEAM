"""가이드: 처음 고객의 궁금증 · 시작 순서 · 서비스 · 공통 지식(계약 · 하자 · 관리사무소). 프로젝트 없이도 돈다.

- 일반 금액 · 기간은 숫자 대신 표준견적서 · 예상 공정표(계산 엔진)로 안내한다(프로젝트가 있으면 계산 상자).
- 답: 정책 글 → 서비스 카드 · 공통 카드(없으면 다른 공종 카드도) → 조각 문장 → 모름 + 단계별 다음 할 일.
"""

from app.agent.v3 import templates
from app.agent.v3.answer import KB, Part, Task, calc_box, drop_promise, knowledge, vendor_lookup
from app.agent.v3.schemas import UiCard

GENERAL_PRICE = (
    "일반 시세 숫자는 말하지 않아요. 기본 정보를 넣으면 이 집 조건으로 표준견적서를 계산해 드려요."
)


def run(task: Task, kb: KB) -> Part:
    if "vendor_lookup" in task.policy:
        return vendor_lookup(task)
    part = Part(route="guide", trade="common")
    f = task.facts
    flags = set(task.policy)
    lines: list[str] = []
    if "price" in flags:
        if f and f.standard:
            lines.append(templates.PRICE_DEEP)
            box = calc_box("standard.total", f)
            if box:
                part.calc.append(box)
        else:
            lines.append(GENERAL_PRICE)
    if "period" in flags:
        if f and f.plan:
            lines.append(templates.PERIOD_PLAN)
            box = calc_box("plan.range", f)
            if box:
                part.calc.append(box)
        else:
            lines.append(templates.PERIOD_NO_PROJECT)
    if "vendor_reco" in flags:
        lines.append(templates.VENDOR_RECO)
    if "brand_reco" in flags:
        lines.append(templates.BRAND_RECO)
    if "defect_judgement" in flags:
        lines.append(templates.DEFECT)
    if "legal_case" in flags:
        lines.append(templates.OUT["legal"])
    if "my_house" in flags and not ({"price", "period"} & flags):
        lines.append(templates.MY_HOUSE)
    if lines:
        part.add(templates.section(lines, route="guide"), "\n".join([*lines, templates.ALLOWED]))
    how = knowledge(
        part,
        kb,
        task.text,
        trade="common",
        route="guide",
        facts=f,
        policy=task.policy,
        original=task.asked,
        today=task.ctx.today,
    )
    if how == "unknown" and lines:
        part.unknown = [u for u in part.unknown if u != templates.UNKNOWN]
        drop_promise(part)
    nxt = templates.NEXT.get(f.stage, []) if f else templates.NO_PROJECT_NEXT
    if nxt:
        part.cards.append(UiCard(kind="next_step", choices=list(nxt)))
    return part
