"""도배 상담의 적용 범위와 후속 응답 검증."""

import asyncio
import uuid
from dataclasses import replace
from datetime import date

import pytest
from v3_kb import repo_kb

from app.agent import llm
from app.agent.v3 import llm_mode
from app.agent.v3.schemas import Button, TurnContext
from app.agent.v3.specialists.dobae.retrieval import retrieve
from app.agent.v3.turn import run_turn
from app.knowledge import bm25

CTX = TurnContext(user_id=uuid.UUID(int=0), today=date(2026, 11, 26))


def test_tile_wall_search_keeps_direct_evidence_only():
    index = bm25.build(
        [
            bm25.Chunk(
                "K1", "spec", "타일 바탕에 도배할 때는 표면 상태를 확인한다.", trades=("도배",)
            ),
            bm25.Chunk(
                "K2",
                "spec",
                "바닥 타일에 접한 벽면은 도배 후 오염을 막아야 한다.",
                trades=("도배",),
            ),
        ],
        {"spec": bm25.Doc(title="시방서", kind="kcs")},
    )
    result = retrieve(replace(repo_kb(), ix=index), "타일 바탕에 도배할 때 뭘 확인해요?")

    assert [hit.ref for hit in result.hits] == ["K1"]


def test_tile_wall_requires_evidence_for_the_surface():
    result = asyncio.run(
        run_turn(
            "타일 위에 실크벽지를 바를 수 있나요?",
            CTX,
            kb=repo_kb(),
        )
    )

    dobae = [sec for sec in result.answer.sections if sec.trade == "dobae"]
    assert result.answer.trace.trades[0] == "dobae"
    assert "타일 바탕에 바로 도배할 수 있는지" in result.answer.plain()
    assert dobae
    assert all(not sec.sources for sec in dobae)


def test_tile_and_wallpaper_work_are_still_routed_as_separate_trades():
    result = asyncio.run(run_turn("타일이랑 도배를 같이 하면 순서가 어때요?", CTX, kb=repo_kb()))

    assert result.answer.trace.route == "multi"
    assert result.answer.trace.trades == ["tile", "dobae"]


def test_wallpaper_choice_uses_existing_card_without_search_hits():
    kb = replace(repo_kb(), ix=bm25.build([], {}))
    result = asyncio.run(run_turn("합지와 실크 중 뭘 골라야 해요?", CTX, kb=kb))

    assert result.answer.trace.cards == ["card:dobae.material.silk-vs-hapji@1"]
    assert result.answer.ask is not None
    assert result.answer.ask.slot == "dobae.grade"


def test_lifted_wallpaper_asks_about_moisture_once():
    kb = repo_kb()
    first = asyncio.run(run_turn("도배한 벽지가 들떠요.", CTX, kb=kb))

    assert first.answer.ask is not None
    assert first.answer.ask.slot == "dobae.mold"
    second = asyncio.run(
        run_turn(
            "",
            CTX,
            kb=kb,
            waiting=first.waiting,
            button=Button(route="trade", trade="dobae", slot="dobae.mold", value="있어요"),
        )
    )

    assert second.answer.ask is None
    assert "덮기 전에" in second.answer.plain()


def test_water_stain_gets_inspection_guidance_without_a_cause():
    result = asyncio.run(
        run_turn("벽지가 들뜨고 물자국이 있는데, 바로 도배해도 돼요?", CTX, kb=repo_kb())
    )

    assert result.answer.ask is None
    assert "원인을 단정하기 어려워요" in result.answer.plain()
    assert "덮기 전에" in result.answer.plain()


@pytest.mark.parametrize(
    "question",
    [
        "벽지가 한쪽만 들떴어요. 처음엔 괜찮았는데 요즘 더 심해져요.",
        "들뜬 벽지가 있어요.",
        "벽지가 벽에서 떨어져요.",
    ],
)
def test_lifting_expressions_open_the_same_slot(question):
    result = asyncio.run(run_turn(question, CTX, kb=repo_kb()))

    assert result.answer.ask is not None
    assert result.answer.ask.slot == "dobae.mold"


@pytest.mark.parametrize(
    "question",
    [
        "벽지는 안 들떴어요.",
        "벽지는 들뜨지 않았어요.",
        "나중에 벽지가 들뜨면 어떡해요?",
        "나중에 벽지가 들뜨는 경우 어떻게 해요?",
        "벽지가 들떠 있으면 어떻게 해요?",
        "벽지를 도배하고 액자가 떨어졌어요.",
    ],
)
def test_unobserved_lifting_does_not_open_the_moisture_slot(question):
    result = asyncio.run(run_turn(question, CTX, kb=repo_kb()))

    assert result.answer.ask is None or result.answer.ask.slot != "dobae.mold"


def test_only_asks_about_the_unreported_moisture_sign():
    result = asyncio.run(run_turn("벽지가 들떴는데 물자국은 없어요.", CTX, kb=repo_kb()))

    assert result.answer.ask is not None
    assert result.answer.ask.slot == "dobae.mold"
    assert "곰팡이" in result.answer.ask.question
    assert "물자국" not in result.answer.ask.question


def test_brand_refusal_also_explains_the_material_choices():
    result = asyncio.run(
        run_turn(
            "합지와 실크 중에서 특정 브랜드 하나가 무조건 최고라고 추천해줘요.", CTX, kb=repo_kb()
        )
    )

    assert "추천하지 않아요" in result.answer.plain()
    assert "물걸레" in result.answer.plain()
    assert [section.title for section in result.answer.sections] == ["[도배]"]
    assert {"K736", "K737"} <= {source["ref"] for source in result.answer.sections[0].sources}
    assert "card:dobae.material.silk-vs-hapji@1" in result.answer.trace.cards
    assert result.answer.ask is None


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("이사 안 가고 살면서 도배할 건데 큰 장롱은 어떻게 해요?", ("장롱", "옮길", "보호")),
        ("거주 중에 도배하려는데 붙박이장이 있어요.", ("가구", "옮길", "보호")),
        (
            "작은방 한쪽 벽만 도배할 수 있어요? 천장은 그대로 둘 거예요.",
            ("작은방 벽 한 면", "천장은 제외", "경계"),
        ),
        ("천장은 빼고 벽 한 면만 도배할 수 있나요?", ("벽 한 면", "천장은 제외", "경계")),
    ],
)
def test_customer_scope_is_preserved_without_search_evidence(question, expected):
    kb = replace(repo_kb(), ix=bm25.build([], {}))
    result = asyncio.run(run_turn(question, CTX, kb=kb))

    assert all(text in result.answer.plain() for text in expected)
    assert all(not section.sources for section in result.answer.sections)


def test_furniture_search_keeps_direct_dobae_preparation_evidence():
    doc = bm25.Doc(title="시공 안내", kind="official")
    index = bm25.build(
        [
            bm25.Chunk(
                "K1", "guide", "거주 중 도배할 때 대형 가구를 옮기고 보양한다.", trades=("도배",)
            ),
            bm25.Chunk(
                "K2", "guide", "도배 전 수도 배관에 누수가 없는지 점검한다.", trades=("도배",)
            ),
        ],
        {"guide": doc},
    )
    result = retrieve(replace(repo_kb(), ix=index), "살면서 도배할 건데 큰 장롱은 어떻게 해요?")

    assert [hit.ref for hit in result.hits] == ["K1"]


def test_one_wall_mentioned_as_part_of_a_larger_scope_stays_a_larger_scope():
    result = asyncio.run(run_turn("벽 한 면도 하고 나머지 벽도 도배할 거예요.", CTX, kb=repo_kb()))

    assert "벽 한 면만 도배하는 범위" not in result.answer.plain()


def test_live_question_understands_scope_alongside_a_symptom(monkeypatch):
    question = (
        "종이가 벽과 벌어졌어요. 작은방 벽 한 면을 손보고, 천장은 그대로 두지 말고 같이 도배해요."
    )
    calls = []
    searches = []

    async def structured(role, schema, messages):
        calls.append(role)
        assert question in messages[-1][1]
        return schema.model_validate(
            {
                "facts": [
                    {"field": "lifting", "value": True, "quote": "종이가 벽과 벌어졌어요"},
                    {"field": "one_wall", "value": True, "quote": "작은방 벽 한 면을 손보고"},
                    {"field": "small_room", "value": True, "quote": "작은방 벽 한 면을"},
                    {
                        "field": "ceiling_excluded",
                        "value": False,
                        "quote": "천장은 그대로 두지 말고 같이 도배해요",
                    },
                    {"field": "mold", "value": True, "quote": "곰팡이가 피었어요"},
                ],
                "request": None,
            }
        )

    async def rewrite(sec, **kwargs):
        pytest.fail("도배 답변에 재작성 호출이 이어졌어요")

    from app.knowledge import search3

    search = search3.search

    def record(index, query, **kwargs):
        searches.append((query, kwargs.get("policy_text")))
        return search(index, query, **kwargs)

    monkeypatch.setattr(llm, "structured", structured)
    monkeypatch.setattr(llm_mode, "rewrite", rewrite)
    monkeypatch.setattr(search3, "search", record)
    result = asyncio.run(run_turn(question, CTX.model_copy(update={"mode": "llm"}), kb=repo_kb()))

    answer = result.answer
    assert calls == ["trade"]
    assert answer.ask and answer.ask.slot == "dobae.mold"
    assert "작은방 벽 한 면" in answer.plain()
    assert "천장도 포함" in answer.plain()
    assert any(
        "벽지 들뜸 박리" in query and "경계 마감" in query and source == question
        for query, source in searches
    )


@pytest.mark.parametrize(
    ("question", "claims", "expected", "ask"),
    [
        (
            "벽지가 한쪽만 들떴어요. 처음엔 괜찮았는데 요즘 더 심해져요.",
            {"lifting": (True, "벽지가 한쪽만 들떴어요")},
            (),
            True,
        ),
        (
            "벽지가 뜬 것 같지만 아직 확인 못 했어요. 도배 시공 상태를 어떻게 확인해요?",
            {"lifting": ("uncertain", "벽지가 뜬 것 같지만 아직 확인 못 했어요")},
            ("이음매나 모서리", "사진"),
            False,
        ),
        (
            "도배한 벽지가 들뜬 곳은 없고 곰팡이가 있는지도 모르겠어요.",
            {
                "lifting": (False, "들뜬 곳은 없고"),
                "mold": ("uncertain", "곰팡이가 있는지도 모르겠어요"),
            },
            ("얼룩이나 곰팡이 흔적", "사진"),
            False,
        ),
        (
            "합지와 실크 중에서 특정 브랜드 하나가 무조건 최고라고 추천해줘요.",
            {},
            ("추천하지 않아요", "물걸레"),
            False,
        ),
        (
            "이사 안 가고 살면서 도배할 건데 큰 장롱은 어떻게 해요?",
            {"furniture": (True, "살면서 도배할 건데 큰 장롱")},
            ("가구를 옮길", "보호"),
            False,
        ),
        (
            "작은방 한쪽 벽만 도배할 수 있어요? 천장은 그대로 둘 거예요.",
            {
                "one_wall": (True, "작은방 한쪽 벽만 도배"),
                "small_room": (True, "작은방 한쪽 벽만"),
                "ceiling_excluded": (True, "천장은 그대로 둘 거예요"),
            },
            ("작은방 벽 한 면", "천장은 제외", "경계"),
            False,
        ),
    ],
)
def test_live_question_uses_validated_conditions(monkeypatch, question, claims, expected, ask):
    calls = []

    async def structured(role, schema, messages):
        calls.append(role)
        return schema.model_validate(
            {
                "facts": [
                    {"field": field, "value": value, "quote": quote}
                    for field, (value, quote) in claims.items()
                ],
                "request": {
                    "question": "도배 상담에 필요한 조건을 확인해요.",
                    "quote": question,
                    "card_indices": [],
                },
            }
        )

    monkeypatch.setattr(llm, "structured", structured)
    result = asyncio.run(run_turn(question, CTX.model_copy(update={"mode": "llm"}), kb=repo_kb()))
    assert calls == ["trade"]
    assert all(line in result.answer.plain() for line in expected)
    assert (result.answer.ask is not None and result.answer.ask.slot == "dobae.mold") == ask
    if claims.get("lifting", (None, ""))[0] is False:
        assert "이음매나 모서리" not in result.answer.plain()


@pytest.mark.parametrize(
    ("question", "quote", "lookup", "expected_card"),
    [
        (
            "벽지에 곰팡이는 없어요. 도배 전에 확인할 게 있나요?",
            "도배 전에 확인할 게 있나요?",
            "도배 시공 전에 벽지 바탕에서 확인할 사항은 무엇인가요?",
            "dobae.prep.before-check",
        ),
        (
            "벽지를 바르기 전에 벽 상태에서 뭘 점검해야 하나요?",
            "벽지를 바르기 전에 벽 상태에서 뭘 점검해야 하나요?",
            "도배 시공 전에 벽 바탕 상태에서 무엇을 점검해야 하나요?",
            "dobae.prep.before-check",
        ),
        (
            "곰팡이는 없고, 바를 때 벽을 왜 먼저 손보는 건지가 궁금해요. 도배 얘기예요.",
            "바를 때 벽을 왜 먼저 손보는 건지가 궁금해요",
            "도배 전에 벽 정리는 왜 해요?",
            "dobae.method.wall-prep",
        ),
    ],
)
def test_search_and_cards_use_the_request_not_the_denied_condition(
    monkeypatch, question, quote, lookup, expected_card
):
    from app.knowledge import cards, search3

    calls = []
    searches = []
    search = search3.search
    kb = repo_kb()
    candidates = cards.find(kb.cards, question, trades=["dobae"], k=8)
    card_index = next(
        (index for index, match in enumerate(candidates) if match.card.id == expected_card), None
    )

    async def structured(role, schema, messages):
        calls.append(role)
        request = {
            "question": lookup,
            "quote": quote,
            "card_indices": [] if card_index is None else [card_index],
        }
        facts = []
        if "곰팡이는 없" in question:
            facts.append({"field": "mold", "value": False, "quote": "곰팡이는 없"})
        return schema.model_validate({"facts": facts, "request": request})

    def record(ix, q, **kwargs):
        searches.append((q, kwargs.get("policy_text")))
        return search(ix, q, **kwargs)

    monkeypatch.setattr(search3, "search", record)
    monkeypatch.setattr(llm, "structured", structured)
    result = asyncio.run(run_turn(question, CTX.model_copy(update={"mode": "llm"}), kb=kb))

    assert calls == ["trade"]
    assert searches == [(lookup, question)]
    assert result.answer.trace.cards[0].startswith(f"card:{expected_card}@")
    assert "곰팡이" not in " ".join(result.answer.vendor)
    if expected_card == "dobae.prep.before-check":
        assert card_index is not None
        assert "K2069" in result.answer.trace.chunks
        assert "K751" not in result.answer.trace.chunks
        no_sources = replace(kb, ix=bm25.build([], {}))
        without_evidence = asyncio.run(
            run_turn(question, CTX.model_copy(update={"mode": "llm"}), kb=no_sources)
        )
        assert not without_evidence.answer.trace.cards


@pytest.mark.parametrize(
    ("question", "claims", "expected", "unexpected"),
    [
        (
            "도배한 벽지가 들뜬 곳은 없고 곰팡이가 있는지도 모르겠어요.",
            {
                "lifting": (False, "들뜬 곳은 없고"),
                "mold": ("uncertain", "곰팡이가 있는지도 모르겠어요"),
            },
            "곰팡이 흔적이 있는지",
            "왜 그런지",
        ),
        (
            "도배한 벽지가 들떴어요. 곰팡이도 물자국도 없어요.",
            {
                "lifting": (True, "벽지가 들떴어요"),
                "mold": (False, "곰팡이도 물자국도 없어요"),
                "water_mark": (False, "곰팡이도 물자국도 없어요"),
            },
            "벽지가 들뜬 부분의 바탕 상태를 확인해 주세요",
            "곰팡이",
        ),
        (
            "벽지에 곰팡이는 없어요. 도배 전에 확인할 게 있나요?",
            {"mold": (False, "곰팡이는 없어요")},
            "",
            "곰팡이",
        ),
    ],
)
def test_vendor_request_respects_dobae_observations(
    monkeypatch, question, claims, expected, unexpected
):
    async def structured(role, schema, messages):
        return schema.model_validate(
            {
                "facts": [
                    {"field": field, "value": value, "quote": quote}
                    for field, (value, quote) in claims.items()
                ],
                "request": None,
            }
        )

    monkeypatch.setattr(llm, "structured", structured)
    ctx = CTX.model_copy(update={"mode": "llm"})
    result = asyncio.run(run_turn(question, ctx, kb=repo_kb()))

    vendor = " ".join(result.answer.vendor)
    assert expected in vendor if expected else not vendor
    assert unexpected not in vendor
    if claims.get("water_mark", (None, ""))[0] is False:
        assert "물자국" not in result.answer.plain()


def test_live_question_failure_skips_rewrite_and_keeps_button_available(monkeypatch):
    calls = []

    async def fail(role, schema, messages):
        calls.append(role)
        raise llm.LLMError("test")

    monkeypatch.setattr(llm, "structured", fail)
    ctx = CTX.model_copy(update={"mode": "llm"})
    question = "벽지가 들떴어요."
    kb = repo_kb()
    failed = asyncio.run(run_turn(question, ctx, kb=kb))
    assert "설명을 만들지 못했어요" in failed.answer.plain()
    assert failed.answer.ask is None
    assert calls == ["trade"]

    waiting = {"route": "trade", "trade": "dobae", "slot": "dobae.mold", "q": question}
    button = Button(route="trade", trade="dobae", slot="dobae.mold", value="있어요")
    followup = asyncio.run(run_turn("있어요", ctx, kb=kb, waiting=waiting, button=button))
    assert "습기" in followup.answer.plain()
    assert followup.answer.ask is None
    assert calls == ["trade"]
