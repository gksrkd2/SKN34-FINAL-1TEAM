"""공종 에이전트: 코드 한 벌 + 공종 설정(TradeSpec) 13개 (ADR agent-03).

- 정책 표시가 있으면 정해 둔 글 · 계산 상자를 먼저: 가격 · 기간(도배 · 목공은 계산 엔진, 10공종은 '아직 계산 안 함'),
  업체 · 제품 추천(기준만), 하자 판정(안 함 + 확인할 곳), 셀프(위험 공종은 전문 업체), 우리 집 상태(실측 때 확인).
- 그다음 카드 → 조각 문장 → 모름 (answer.knowledge).
- 도배 · 목공: 카드가 되묻는 칸(ask)이 아직 비어 있으면 하나만 되묻고, 칸 답이 오면 원래 질문 + 답으로 다시 찾는다.
- 하지 않는 일: 하자 판정, 브랜드 · 업체 추천, 10공종 금액, 다른 공종 부르기(필요하면 관문이 multi로 정함).
"""

import re

from app.agent.v3 import templates
from app.agent.v3.answer import KB, Part, Task, calc_box, drop_promise, knowledge, vendor_lookup
from app.agent.v3.schemas import Choice, UiCard
from app.agent.v3.trades import BY_CODE

LIFTING = "dobae.defect.lifting"
MOLD_REDO = "dobae.defect.mold-redo"
LIVING_PREP = "dobae.price.living-in"
LIFTED = re.compile(r"들뜨|들떠|들뜸|들뜬|들떴")
WET = re.compile(
    r"곰팡이\s*(?:핀|[이가은는]?\s*(?:펴|폈|피었|있|보여))|"
    r"(?:물자국|결로)[이가은는]?\s*(?:있|보여|생겨)|물이\s*맺|젖어"
)
DRY = re.compile(r"(?:곰팡이|물자국|결로)[이가은는]?\s*(?:없|안\s*보여)")
OVERLAY = re.compile(r"덧방|덧\s*붙|(?:위에|겹쳐).{0,20}(?:붙|바르|발라|덮)")
LIVING = re.compile(r"거주\s*중|살면서|거주하면서|살고\s*있|이사\s*안\s*나가")
VACANT = re.compile(r"빈\s*집|공실|짐[이은]?\s*없")
MOLD_YES = re.compile(r"(?:네|예|있어요|있습니다|있어)[.!\s]*")
MOLD_NO = re.compile(r"(?:아니요|아뇨|없어요|없습니다|없어)[.!\s]*")
MOLD_UNSURE = re.compile(r"(?:잘\s*)?(?:모르겠어요|모르겠습니다|몰라요|모름)|아직\s*몰라요")


def primary(task: Task) -> str:
    return task.trades[0] if task.trades else "common"


def run(task: Task, kb: KB) -> Part:
    if "vendor_lookup" in task.policy:
        return vendor_lookup(task)
    code = primary(task)
    spec = BY_CODE[code]
    part = Part(route="trade", trade=code)  # type: ignore[arg-type]
    facts = task.facts
    flags = set(task.policy)
    lines: list[str] = []
    ko = spec.ko

    if "price" in flags:
        if spec.calc and facts and facts.standard:
            lines.append(templates.PRICE_DEEP)
            box = calc_box("standard.total", facts)
            if box:
                part.calc.append(box)
        elif spec.calc:
            lines.append(templates.PRICE_DEEP_NO_PROJECT)
            part.cards.append(UiCard(kind="next_step", choices=list(templates.NO_PROJECT_NEXT)))
        elif code != "common":
            lines.append(templates.PRICE_KNOWLEDGE.format(ko=ko))
    if "period" in flags:
        if spec.calc and facts and facts.plan:
            lines.append(templates.PERIOD_PLAN)
            box = calc_box("plan.range", facts)
            if box:
                part.calc.append(box)
        elif spec.calc:
            lines.append(templates.PERIOD_NO_PROJECT)
        elif code != "common":
            lines.append(templates.PERIOD_KNOWLEDGE.format(ko=ko))
    if "vendor_reco" in flags:
        lines.append(templates.VENDOR_RECO)
        if spec.calc:
            part.cards.append(
                UiCard(kind="next_step", choices=[Choice(text="업체 보기", action="vendors")])
            )
    if "brand_reco" in flags:
        lines.append(templates.BRAND_RECO)
    if "defect_judgement" in flags:
        lines.append(templates.DEFECT)
    if "legal_case" in flags:
        lines.append(templates.OUT["legal"])
    if "diy" in flags and spec.risky_diy:
        lines.append(templates.DIY_RISKY.format(ko=ko))
    if "my_house" in flags and not ({"price", "period"} & flags):
        lines.append(templates.MY_HOUSE)
    if lines:
        part.add(
            templates.section(lines, title=f"[{ko}]", route="trade", trade=code),
            "\n".join([*lines, templates.ALLOWED]),
        )

    # 곰팡이 여부에 대한 짧은 후속 답은 최초 들뜸 설명을 되풀이하지 않는다.
    # 확답할 수 없는 상태에는 원인 추정 대신 확인할 행동만 안내한다.
    if code == "dobae" and task.slot == "dobae.mold":
        reply = (task.value or "").strip()
        if MOLD_UNSURE.fullmatch(reply) or MOLD_NO.fullmatch(reply) or DRY.search(reply):
            if MOLD_UNSURE.fullmatch(reply):
                first = "곰팡이나 물자국이 있는지 확실하지 않다면, 들뜬 곳 주변의 얼룩이나 습기를 확인해 보세요."
            else:
                first = "곰팡이나 물자국이 보이지 않는다면, 들뜬 곳의 상태를 계속 살펴보세요."
            lines_follow = [
                first,
                "들뜬 위치와 발생 시점을 기록하고 사진을 남겨 두면 실측이나 보수 요청 때 상태를 설명하기 쉬워요.",
            ]
            part.add(
                templates.section(lines_follow, title="[도배]", route="trade", trade="dobae"),
                "\n".join(lines_follow),
            )
            return part

    q = task.text
    if task.slot and task.value:  # 되물은 칸의 답: 원래 질문 + 고른 값으로 다시 찾는다
        q = f"{task.asked} {task.value}".strip()
    card_id = None
    wet = dry = False
    if code == "dobae":
        wet, dry = bool(WET.search(q)), bool(DRY.search(q))
        lifting = LIFTED.search(task.asked or task.text)
        if wet and (lifting or OVERLAY.search(q)):
            card_id = MOLD_REDO
        elif task.slot == "dobae.mold" and lifting:
            card_id = MOLD_REDO if MOLD_YES.fullmatch(task.value or "") else LIFTING

    how = knowledge(
        part,
        kb,
        q,
        trade=code,
        route="trade",
        facts=facts,
        policy=task.policy,
        card_id=card_id,
        original=q if task.slot else task.asked or q,
        today=task.ctx.today,
    )  # type: ignore[arg-type]
    if code == "dobae":
        selected = {tag.split("@")[0] for tag in part.used_cards}
        if part.ask and (
            (part.ask.slot == "dobae.mold" and (wet or dry))
            or (part.ask.slot == "dobae.living" and (LIVING.search(q) or VACANT.search(q)))
        ):
            part.ask = None
        if (
            f"card:{LIVING_PREP}" in selected or (task.slot == "dobae.mold" and LIVING.search(q))
        ) and (LIVING.search(q) or (facts and facts.living == "living" and not VACANT.search(q))):
            wall_only = facts and facts.brief.get("dobae.ceiling") == "벽만 해요"
            place = "시공할 벽 앞" if wall_only else "도배할 공간의"
            prep = f"{place} 가구를 어디로 옮길지 업체와 미리 정해요."
            part.add(
                templates.section(
                    [prep], title="[거주 중 도배 준비]", route="trade", trade="dobae"
                ),
                prep,
            )
    if task.slot:
        part.ask = None  # 한 칸은 한 번만 묻는다(되묻기 반복 막기)
    if how == "unknown" and lines:
        part.unknown = [
            u for u in part.unknown if u != templates.UNKNOWN
        ]  # 정책 글로 이미 답했으면 '모름'은 확인할 곳만
        drop_promise(part)
    if spec.calc and not any(c.kind == "next_step" for c in part.cards):
        stage = facts.stage if facts else None
        nxt = templates.NEXT.get(stage or "", []) if facts else templates.NO_PROJECT_NEXT
        if nxt:
            part.cards.append(UiCard(kind="next_step", choices=list(nxt)))
    return part
