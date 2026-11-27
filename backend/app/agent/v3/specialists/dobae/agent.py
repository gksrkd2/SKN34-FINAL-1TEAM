"""도배 상담 시작점."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING

from app.agent.v3 import templates
from app.agent.v3.answer import NO_EXTRACT, drop_promise
from app.agent.v3.schemas import AskSlot
from app.agent.v3.specialists.dobae.question import Conditions, conditions_for, interpret_question
from app.agent.v3.trades import BY_CODE
from app.knowledge.cards import AnswerCard, CardSentence, CardSet, build

if TYPE_CHECKING:
    from app.agent.v3.answer import KB, Part, Task
    from app.agent.v3.specialists import TradeRunner

__all__ = ["interpret_question", "run"]

_MATERIAL_PAIR = re.compile(
    r"(?:합지(?:\s*벽지)?\s*(?:와|이?랑|하고|나|보다|,)\s*실크"
    r"|실크(?:\s*벽지)?\s*(?:와|이?랑|하고|나|보다|,)\s*합지)(?:\s*벽지)?"
)
_COMPARE = re.compile(r"차이|비교|달라|다르|어느\s*쪽|어떤\s*게|뭐가|뭘\s*골라|더\s*")
_GENERAL_COMPARISON = re.compile(
    r"\s*(?:중(?:에)?\s*(?:뭘|뭐|어느|어떤)|(?:는|가)?\s*(?:차이|비교|뭐가\s*달라))"
)
_MATERIAL_EVIDENCE = (
    (
        re.compile(r"얼룩|오염|물걸레|닦|청소|관리"),
        (
            ("실크는 PVC 코팅이 있어 얼룩을 물걸레로 닦을 수 있어요.", "K737"),
            ("합지는 종이 벽지라 물걸레로 닦으면 표면이 벗겨질 수 있어요.", "K736"),
        ),
    ),
    (
        re.compile(r"가격|비용|비싸|저렴|싼|값"),
        (("제조사 설명으로는 합지가 실크보다 저렴해요.", "K737"),),
    ),
    (
        re.compile(r"광택|질감|재질|겉면"),
        (
            ("합지는 종이 두 겹으로 만들어요.", "K736"),
            ("실크는 종이에 PVC를 코팅해 은은한 광택과 입체적인 질감이 나요.", "K737"),
        ),
    ),
    (
        re.compile(r"내구|오래|수명|튼튼"),
        (
            (
                "제조사 자료는 실크가 오래 쓰이는 편이라고 설명해요. 합지와의 구체적인 수명 차이는 확인하기 어려워요.",
                "K737",
            ),
        ),
    ),
)
_EXACT_COST = re.compile(r"견적|평당|단가|얼마|금액|총액|(?:우리|이)\s*집|\d")


def _comparison_card(card: AnswerCard, question: str, pair: re.Match[str]) -> AnswerCard | None:
    lines = [
        CardSentence(
            text=" ".join(text for text, _ in statements),
            cites=[ref for _, ref in statements],
        )
        for criterion, statements in _MATERIAL_EVIDENCE
        if criterion.search(question)
    ]
    if lines:
        refs = {ref for line in lines for ref in line.cites}
        return card.model_copy(
            update={
                "keys": [question],
                "answer": lines,
                "sources": [source for source in card.sources if source.ref in refs],
                "caution": None,
                "tip": None,
                "ask": None,
                "calc": [],
                "actions": [],
            }
        )
    if _GENERAL_COMPARISON.match(question, pair.end()):
        return card.model_copy(update={"keys": [*card.keys, question]})
    return None


def _guidance_lines(conditions: Conditions) -> list[str]:
    lines = []
    if conditions.furniture:
        furniture = "가구" if conditions.furniture == "가구" else f"{conditions.furniture} 등 가구"
        lines.append(
            f"{furniture}를 옮길 수 있는지, 제자리에서 작업한다면 "
            "가구와 바닥을 어디까지 보호해야 하는지 업체에 확인해 주세요."
        )
    if conditions.one_wall:
        wall = "작은방 벽 한 면" if conditions.small_room else "벽 한 면"
        scope = f"{wall}만 도배하는 범위"
        if conditions.ceiling_excluded:
            scope = f"{wall}만 도배하고 천장은 제외하는 범위"
        elif conditions.ceiling_included:
            scope = f"{wall}을 도배하고 천장도 포함하는 범위"
        lines.append(
            f"{scope}로 문의해 보세요. 기존 벽지와 맞닿는 경계의 마감은 실측 때 확인해 주세요."
        )
    elif conditions.ceiling_excluded:
        lines.append("시공 범위에서 천장을 제외한다고 업체에 전달해 주세요.")
    elif conditions.ceiling_included:
        lines.append("시공 범위에 천장도 포함한다고 업체에 전달해 주세요.")
    if "lifting" in conditions.uncertain:
        lines.append("벽지 이음매나 모서리가 벽에서 벌어진 곳이 있는지 눈으로 살펴보세요.")
    if conditions.uncertain & {"mold", "water_mark", "moisture"}:
        lines.append("벽지에 얼룩이나 곰팡이 흔적이 보이는지 살펴보세요.")
    if conditions.uncertain:
        lines.append("의심되는 부분은 사진으로 남겨 실측 때 업체에 보여 주세요.")
    return lines


def run(task: Task, kb: KB, run_common: TradeRunner) -> Part:
    question = task.search_text
    conditions = conditions_for(kb, question)
    tile = conditions.tile_surface
    blocked = NO_EXTRACT.intersection(task.policy)
    guidance = _guidance_lines(conditions) if not task.slot and not blocked else []
    interpreted_moisture = ("dobae", question) in kb.interpretations and conditions.moisture
    if not (
        tile
        or conditions.lifting
        or conditions.water_mark
        or interpreted_moisture
        or task.slot == "dobae.mold"
    ):
        pair = _MATERIAL_PAIR.search(question)
        comparison = pair and (
            _COMPARE.search(question) or _GENERAL_COMPARISON.match(question, pair.end())
        )
        relative_price = blocked == {"price"} and not _EXACT_COST.search(question)
        if not task.slot and pair and blocked == {"brand_reco"}:
            base_card = kb.cards.by_id.get("dobae.material.silk-vs-hapji")
            if base_card is None:
                return run_common(task, replace(kb, cards=CardSet([], kb.cards.version)))
            card = base_card.model_copy(
                update={"keys": [question], "caution": None, "ask": None, "calc": [], "actions": []}
            )
            return run_common(task, replace(kb, cards=build([card], kb.cards.version)))
        if not task.slot and comparison and (not blocked or relative_price):
            base_card = kb.cards.by_id.get("dobae.material.silk-vs-hapji")
            card = _comparison_card(base_card, question, pair) if base_card and pair else None
            if card is not None:
                if relative_price and card.ask is None:
                    task = replace(task, policy=[flag for flag in task.policy if flag != "price"])
                return run_common(task, replace(kb, cards=build([card], kb.cards.version)))
            # 지원 근거가 확인되지 않은 비교 기준은 관련 없는 카드로 답하지 않는다.
            return run_common(task, replace(kb, cards=CardSet([], kb.cards.version)))
        if guidance:
            part = run_common(task, replace(kb, cards=CardSet([], kb.cards.version)))
            part.unknown.clear()
            part.add(
                templates.section(guidance, title="[도배]", route="trade", trade="dobae"),
                "\n".join(guidance),
            )
            return part
        return run_common(task, kb)

    # 일반 카드가 특수 바탕이나 관찰 증상에 대한 근거를 대신하지 않도록 한다.
    part = run_common(task, replace(kb, cards=CardSet([], kb.cards.version)))
    if tile:
        if any(sec.source == "extract" for sec in part.sections):
            if guidance:
                part.add(
                    templates.section(guidance, route="trade", trade="dobae"), "\n".join(guidance)
                )
            return part
        lines = [
            "믿집 자료만으로는 이 타일 바탕에 바로 도배할 수 있는지 확인하기 어려워요.",
            "실측 때 타일이 단단히 붙어 있는지, 표면과 줄눈에 어떤 바탕 작업이 필요한지 확인해 주세요.",
        ]
    else:
        lines = ["벽지 상태만으로 원인을 단정하기 어려워요."]
        if (
            conditions.moisture
            or task.slot
            or (conditions.mold is False and conditions.water_mark is False)
        ):
            area = (
                "들뜬 곳이나 물자국 주변"
                if conditions.water_mark is not False
                else "들뜬 곳"
                if conditions.lifting
                else "해당 부분"
            )
            lines.append(
                f"새 벽지로 덮기 전에 {area}에 습기가 남아 있는지,"
                " 바탕이 손상됐는지 실측 때 확인해 주세요."
            )

    # 증상만 주어진 경우에는 원인 설명 발췌 대신 현장에서 확인할 조건을 전달한다.
    part.sections = [sec for sec in part.sections if sec.source != "extract"]
    part.used_chunks.clear()
    part.unknown.clear()
    drop_promise(part)
    lines.extend(guidance)
    part.add(
        templates.section(lines, title="[도배]", route="trade", trade="dobae"), "\n".join(lines)
    )

    unknown_sign = conditions.mold is None or conditions.water_mark is None
    if (
        conditions.lifting
        and unknown_sign
        and not (tile or conditions.moisture or task.slot or NO_EXTRACT.intersection(task.policy))
    ):
        slot = next(slot for slot in BY_CODE["dobae"].slots if slot.key == "dobae.mold")
        prompt = slot.question
        if conditions.mold is False:
            prompt = "벽에 물자국이 있나요?"
        elif conditions.water_mark is False:
            prompt = "벽에 곰팡이가 있나요?"
        part.ask = AskSlot(
            trade="dobae", slot=slot.key, question=prompt, options=list(slot.options)
        )
    return part
