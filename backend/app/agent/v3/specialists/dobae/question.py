"""도배 질문에서 검색과 응답에 필요한 조건을 읽는다."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from app.agent import llm
from app.knowledge import cards

if TYPE_CHECKING:
    from app.agent.v3.answer import KB

TILE_SURFACE = re.compile(r"타일(?:을 붙인|로 마감한)?\s*(?:벽|바탕|위)")
FURNITURE = re.compile(r"장롱|장농|붙박이\s*장|(?:장식|수납)장|가구|옷장")
ONE_WALL = re.compile(r"(?:한\s*쪽\s*벽|벽(?:면)?\s*(?:한\s*(?:면|쪽)|1\s*면))")
_LIVING = re.compile(r"살면서|사는\s*중|거주\s*(?:중|하면서)|생활하면서")
_CEILING_EXCLUDED = re.compile(
    r"천장\s*(?:은|을|도)?\s*(?:그대로|빼|제외|(?:아예\s*)?안\s*(?:해|하|할|건드))"
)
_CLAUSE = re.compile(r"[.!?\n,]+")
_WALLPAPER = re.compile(r"벽지|도배")
_LIFTING = re.compile(r"들뜨|들뜬|들뜸|들떠|들떴|벗겨|박리")
_DETACHED = re.compile(r"벽지(?:가|는|도)?\s*(?:벽(?:면)?에서\s*)?(?:떨어지|떨어져|떨어졌)")
_BEFORE_LIFTING = re.compile(r"(?:안|못)\s*$")
_AFTER_LIFTING = re.compile(
    r"(?:지\s*(?:않|못)|(?:는|\s*있으?)?\s*(?:다면|으면|면|경우)|도|\s*(?:은|이)?\s*없)"
)
_WATER_MARK = r"물\s*(?:자국|얼룩)"
_MOISTURE = rf"곰팡이|{_WATER_MARK}|결로|누수|젖(?:은|었|어)"
_MOISTURE_GROUP = re.compile(rf"(?:{_MOISTURE})(?:(?:이나|나|와|과|랑|하고)\s*(?:{_MOISTURE}))*")
_ABSENT = re.compile(r"\s*(?:(?:은|는|이|가|도)\s*)?(?:없|안\s*보|보이지\s*않)")


@dataclass(frozen=True)
class Conditions:
    lifting: bool | None = None
    water_mark: bool | None = None
    mold: bool | None = None
    moisture: bool = False
    furniture: str | None = None
    one_wall: bool = False
    ceiling_excluded: bool = False
    ceiling_included: bool = False
    small_room: bool = False
    tile_surface: bool = False
    uncertain: frozenset[str] = frozenset()
    lookup_text: str | None = None
    preferred_cards: tuple[str, ...] = ()

    @property
    def vendor_request(self) -> tuple[str, ...] | None:
        symptoms = (
            ("lifting", self.lifting, "벽지가 들뜬 부분의 바탕 상태를", "벽지가 들뜬 곳이 있는지"),
            ("mold", self.mold, "곰팡이가 보이는 부분의 원인을", "곰팡이 흔적이 있는지"),
            ("water_mark", self.water_mark, "물자국이 생긴 부분의 원인을", "물자국이 있는지"),
        )
        checks = [
            present if value is True else unconfirmed
            for key, value, present, unconfirmed in symptoms
            if value is True or key in self.uncertain
        ]
        if not checks:
            return () if any(value is False for _, value, _, _ in symptoms) else None
        return (
            f"실측 때 {' 그리고 '.join(checks)} 확인해 주세요. 의심되는 부분은 사진으로 보내 드릴게요.",
            "필요한 작업이 있다면 작업 범위와 비용을 견적서에 따로 적어 주세요.",
        )


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    field: Literal[
        "lifting",
        "water_mark",
        "mold",
        "moisture",
        "furniture",
        "one_wall",
        "ceiling_excluded",
        "small_room",
        "tile_surface",
    ]
    value: bool | Literal["uncertain"]
    quote: str


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    quote: str
    card_indices: list[int]


class QuestionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facts: list[Observation]
    request: Request | None


@cache
def _prompt() -> str:
    return Path(__file__).with_name("question_prompt_v1.md").read_text(encoding="utf-8")


async def interpret_question(question: str, *, context: str = "") -> Conditions:
    candidates = cards.find(cards.default(), question, trades=["dobae"], k=8)
    if candidates and candidates[0].score == 1:
        candidates = []
    examples = [(match.card.keys[0][:42], match.card.answer[0].text[:32]) for match in candidates]
    payload: dict[str, object] = {"질문": question}
    if context:
        payload["문맥"] = context
    if examples:
        payload["카드 예시"] = examples
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    parsed = await llm.structured("trade", QuestionOutput, [("system", _prompt()), ("user", data)])
    facts = {
        claim.field: claim.value
        for claim in parsed.facts
        if (quote := claim.quote.strip()) and (quote in question or quote in context)
    }
    values = {field: value for field, value in facts.items() if isinstance(value, bool)}
    uncertain = frozenset(
        field
        for field in ("lifting", "water_mark", "mold", "moisture")
        if facts.get(field) == "uncertain"
    )
    ceiling_excluded = values.get("ceiling_excluded")
    request = parsed.request
    lookup_text = None
    preferred_cards = ()
    if request and (quote := request.quote.strip()) and quote in question:
        lookup_text = " ".join(request.question.split())[:80] or None
        if lookup_text:
            preferred_cards = tuple(
                candidates[i].card.id
                for i in dict.fromkeys(request.card_indices[:3])
                if 0 <= i < len(candidates)
            )
    return Conditions(
        lifting=values.get("lifting"),
        water_mark=values.get("water_mark"),
        mold=values.get("mold"),
        moisture=any(values.get(key) is True for key in ("moisture", "mold", "water_mark")),
        furniture="가구" if values.get("furniture") else None,
        one_wall=values.get("one_wall", False),
        ceiling_excluded=ceiling_excluded is True,
        ceiling_included=ceiling_excluded is False,
        small_room=values.get("small_room", False),
        tile_surface=values.get("tile_surface", False),
        uncertain=uncertain,
        lookup_text=lookup_text,
        preferred_cards=preferred_cards,
    )


def conditions_for(kb: KB, question: str) -> Conditions:
    key = ("dobae", question)
    if key not in kb.interpretations:
        return read_conditions(question)
    parsed = kb.interpretations[key]
    return parsed if isinstance(parsed, Conditions) else Conditions()


def read_conditions(question: str) -> Conditions:
    lifting = False
    water_mark = mold = None
    moisture = False
    for clause in _CLAUSE.split(question):
        wallpaper = _WALLPAPER.search(clause)
        if wallpaper and not lifting:
            # '떨어지다'는 가구 등에도 쓰이므로 벽지가 떨어지는 문장만 받는다.
            lifting = any(
                abs(match.start() - wallpaper.start()) <= 24
                and not _BEFORE_LIFTING.search(clause[: match.start()])
                and not _AFTER_LIFTING.match(clause, match.end())
                for match in _LIFTING.finditer(clause)
            )
            detached = _DETACHED.search(clause)
            lifting = lifting or bool(detached and not _AFTER_LIFTING.match(clause, detached.end()))
        # 부정은 연결된 증상 묶음에만 적용한다. 앞 절의 들뜸은 따로 판단한다.
        for group in _MOISTURE_GROUP.finditer(clause):
            present = not _ABSENT.match(clause, group.end())
            if re.search(_WATER_MARK, group[0]):
                water_mark = present
            if "곰팡이" in group[0]:
                mold = present
            moisture |= present

    furniture = FURNITURE.search(question) if _LIVING.search(question) else None
    one_wall = ONE_WALL.search(question)
    return Conditions(
        lifting=lifting,
        water_mark=water_mark,
        mold=mold,
        moisture=moisture,
        furniture=furniture[0] if furniture else None,
        one_wall=bool(
            one_wall and re.match(r"\s*만(?!\s*(?:말고|아니라|빼고))", question[one_wall.end() :])
        ),
        ceiling_excluded=bool(_CEILING_EXCLUDED.search(question)),
        small_room=bool(re.search(r"작은\s*방", question)),
        tile_surface=bool(TILE_SURFACE.search(question)),
    )
