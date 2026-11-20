"""v3 한 턴(무료 모드 기본, LLM_LIVE=false): 카드 · 정책 글 · 계산 상자 · 견적 줄 · 실측 초안 · 오케스트레이터 ·
출력 검사 · 가림 · LLM 모드 층(스텁). 유료 호출 0회, DB 없음."""

import asyncio
import re
import uuid
from datetime import date

import pytest
from v3_kb import repo_kb

from app.agent import llm
from app.agent.v3 import check, llm_mode
from app.agent.v3.schemas import (
    AgentAnswer,
    Button,
    PlanFacts,
    ProjectFacts,
    Section,
    Sentence,
    StandardFacts,
    StandardLineFact,
    TurnContext,
    VendorRangeFact,
    VisitDraftFacts,
    VisitRecommendFact,
)
from app.agent.v3.turn import run_turn

KB = repo_kb()
CTX = TurnContext(user_id=uuid.uuid4(), today=date(2026, 11, 19))
DIGIT_MONEY = re.compile(r"\d[\d,]*\s*원")
FACTS = ProjectFacts(
    stage="standard", area_m2=69.4, area_basis="supply", trades=["dobae"], grade="basic",
    standard=StandardFacts(total=1974069, supply=1794608, vat=179461, lines=[
        StandardLineFact(key="wall", name="벽 도배 시공 풀 포함", state="included", qty=130.1,
                         unit="㎡", unit_price=5993, amount=779689),
        StandardLineFact(key="demolish", name="기존 벽지 철거", state="after_visit", qty=173.5,
                         unit="㎡", unit_price=1727, amount=299635),
    ]),
    plan=PlanFacts(start=date(2027, 1, 4), end=date(2027, 1, 5), work_days=2),
    visit_draft=VisitDraftFacts(
        wish_start=date(2026, 12, 7), wish_end=date(2026, 12, 21), basis_start=date(2027, 1, 4),
        formula="공사 시작 희망일 − 28일 ~ − 14일", vendors=["마루와벽", "결도배"],
    ),
)  # fmt: skip


def turn(text, *, facts=None, view=None, waiting=None, button=None, mode="free"):
    ctx = CTX.model_copy(update={"view": view, "mode": mode})
    return asyncio.run(run_turn(text, ctx, kb=KB, facts=facts, waiting=waiting, button=button))


def words(ans: AgentAnswer) -> str:
    return "\n".join(x.text for s in ans.sections for x in s.sentences)


def test_trade_question_gets_verified_card_with_cites_and_one_ask():
    r = turn("합지랑 실크 중에 뭐로 해야 해요?")
    a = r.answer
    assert a.trace.route == "trade" and a.trace.trades[0] == "dobae"
    card = [s for s in a.sections if s.source == "card"]
    assert card and all(x.cites for x in card[0].sentences)
    assert any(c.startswith("card:") for x in card[0].sentences for c in x.cites)
    assert a.ask is not None and a.ask.slot == "dobae.grade"
    assert r.waiting == {
        "route": "trade",
        "trade": "dobae",
        "slot": "dobae.grade",
        "q": "합지랑 실크 중에 뭐로 해야 해요?",
    }


def test_slot_answer_is_taken_by_waiting_agent_and_not_asked_again():
    first = turn("합지랑 실크 중에 뭐로 해야 해요?")
    r = turn("세 놓을 집", waiting=first.waiting,
             button=Button(route="trade", trade="dobae", slot="dobae.grade", value="세 놓을 집"))  # fmt: skip
    assert r.answer.trace.route == "trade"
    assert r.answer.ask is None and r.waiting is None
    assert r.answer.sections


@pytest.mark.parametrize(
    ("text", "route", "trades", "continued"),
    [
        ("거주 중이에요", "trade", ["dobae"], True),
        ("모르겠어요", "trade", ["dobae"], True),
        ("몰딩 이음새가 벌어졌어요", "trade", ["mokgong"], False),
        ("도배랑 목공은 뭐부터 해야 해요?", "multi", ["dobae", "mokgong"], False),
        ("로또 번호 알려줘", "out_of_scope", [], False),
    ],
)
def test_pending_dobae_keeps_short_answers_and_routes_new_topics(text, route, trades, continued):
    first = turn("합지랑 실크 중에 뭐로 해야 해요?")
    assert first.waiting
    r = turn(text, view="standard", facts=FACTS, waiting=first.waiting)
    assert r.answer.trace.route == route
    assert r.answer.trace.trades == trades
    assert (r.source == "waiting") is continued


def test_other_trade_question_is_answered_by_that_trade():
    a = turn("강마루랑 강화마루 차이가 뭐예요?").answer
    assert a.trace.route == "trade" and a.trace.trades == ["floor"]
    assert any(s.source == "card" and s.trade == "floor" for s in a.sections)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("거주 중이에요", "가구"),
        ("모르겠어요", "발생 시점"),
        ("있어요", "바탕 상태"),
        ("곰팡이는 없어요", "발생 시점"),
    ],
)
def test_lifting_asks_once_and_uses_the_follow_up(reply, expected):
    first = turn("벽지가 들떠요", view="standard", facts=FACTS)
    assert first.answer.ask and first.answer.ask.slot == "dobae.mold"
    answer = turn(reply, view="standard", facts=FACTS, waiting=first.waiting)
    assert answer.answer.trace.trades == ["dobae"]
    assert not answer.answer.ask and not answer.waiting
    assert expected in words(answer.answer)
    assert any(s.sources for s in answer.answer.sections)


@pytest.mark.parametrize(
    "question",
    [
        "곰팡이 핀 벽지 위에 덧붙여도 되나요?",
        "곰팡이 핀 벽지 위에 새 벽지를 덧붙이면 안 돼요?",
    ],
)
def test_mold_overlay_checks_the_wall_before_wallpapering(question):
    answer = turn(question, view="standard", facts=FACTS).answer
    assert answer.trace.route == "trade"
    assert "바탕 상태" in words(answer)
    assert any(c.startswith("card:dobae.defect.mold-redo@") for c in answer.trace.cards)
    assert not answer.ask


def test_preparation_uses_project_living_and_wall_scope():
    facts = FACTS.model_copy(update={"living": "living", "brief": {"dobae.ceiling": "벽만 해요"}})
    answer = turn("거주 중 도배하려면 뭘 준비해요?", view="standard", facts=facts).answer
    assert "시공할 벽 앞 가구" in words(answer)
    assert "보양" in words(answer)
    assert answer.calc and not answer.ask


def test_out_of_scope_and_knowledge_trade_calc_requests():
    a = turn("로또 번호 알려줘").answer
    assert a.trace.route == "out_of_scope" and not a.trace.cards
    b = turn("욕실 리모델링 견적 금액을 바로 뽑아 주세요").answer
    assert b.trace.route == "out_of_scope"
    assert "도배 · 목공만 금액을 계산해요" in words(b) and "욕실" in words(b)


def test_price_uses_calc_box_not_text_numbers():
    a = turn("도배 비용이 얼마나 나와요?", facts=FACTS).answer
    assert "price" in a.trace.policy
    assert [c.amount for c in a.calc if c.ref == "standard.total"] == [1974069]
    assert not DIGIT_MONEY.search(words(a))


def test_brand_recommendation_is_declined_with_criteria_only():
    a = turn("샤워수전은 어느 회사 제품이 제일 튼튼해요?").answer
    assert "brand_reco" in a.trace.policy
    assert "특정 제품 · 브랜드는 추천하지 않아요" in words(a)
    assert not any(s.source == "extract" for s in a.sections)


def test_quote_line_after_visit_uses_9pan_text_and_calc_box():
    a = turn("철거는 왜 합계에 안 들어가요?", facts=FACTS, view="standard").answer
    assert a.trace.route == "quote_explanation"
    assert "뜯어낼 벽지 양은 집을 봐야 알 수 있어서 실측 뒤 업체가 정해요." in words(a)
    box = [c for c in a.calc if c.ref == "standard.lines.demolish"]
    assert box and box[0].amount == 299635 and box[0].in_total is False


def test_visit_draft_card_before_vendor_answer():
    a = turn("실측 일정 요청 초안 만들어 줘", facts=FACTS, view="visit_request").answer
    assert a.trace.route == "schedule"
    draft = [c for c in a.cards if c.kind == "visit_draft"]
    assert draft and draft[0].draft.wish_start == date(2026, 12, 7)
    assert draft[0].choices[0].text == "고른 2곳에 요청 보내기"
    assert "보내기 · 날짜 확정은 직접 눌러 주세요" in words(a)  # AI는 초안만


def test_visit_draft_after_vendor_range_recommends_calc_day():
    vd = FACTS.visit_draft.model_copy(update={
        "ranges": [VendorRangeFact(vendor="마루와벽", start=date(2026, 12, 13), end=date(2026, 12, 18))],
        "recommend": VisitRecommendFact(vendor="마루와벽", day=date(2026, 12, 15), half="am",
                                        basis="1.4(월) − 20일 = 12.15(화)"),
    })  # fmt: skip
    facts = FACTS.model_copy(update={"visit_draft": vd})
    a = turn("마루와벽은 언제 실측 오면 좋아요?", facts=facts, view="visits").answer
    assert "마루와벽이 12.13(일)~12.18(금)에 된대요" in words(a)
    assert "12.15(화)" in words(a)
    assert any(c.kind == "open_calendar" for c in a.cards)
    assert "floor" not in a.trace.trades  # '마루와벽'은 업체 이름이라 바닥 공종이 아니다


def test_orchestrator_orders_trades_and_adds_order_sentence():
    a = turn("몰딩 바꾸고 도배도 새로 하려는데 순서가 어떻게 돼요?").answer
    assert a.trace.route == "multi"
    assert a.trace.parts == ["trade:mokgong", "trade:dobae"]
    order = next(s for s in a.sections if s.title == "[순서]")
    # 순서 문장은 계산 엔진 공정표 순서(rule:)와 지식 조각을 근거로 달고, 출처 칸에도 보인다
    assert "rule:plan-order" in order.sentences[0].cites
    assert any(x["kind"] == "rule" for x in order.sources)
    assert len(a.trace.parts) <= 3


def _part(trade, *texts, route="trade"):
    from app.agent.v3.answer import Part
    from app.agent.v3.templates import UNKNOWN

    p = Part(route=route, trade=trade)
    if texts:
        p.sections.append(
            Section(sentences=[Sentence(text=t) for t in texts], source="card", trade=trade)
        )
    else:
        p.unknown = [UNKNOWN, "실측 때 업체에 물어보시면 정확해요."]
    return p


def test_merge_orders_by_trade_and_adds_order_sentence_only_when_asked():
    from app.agent.v3.orchestrator import merge

    parts = [_part("dobae", "도배 답이에요."), _part("mokgong", "목공 답이에요.")]
    asked = merge(parts, asked="몰딩이랑 도배 뭐부터 해요?")
    assert [s.title or s.trade for s in asked.sections] == ["mokgong", "[순서]", "dobae"]
    other = merge(parts, asked="몰딩이랑 도배 중에 뭐가 더 쉬워요?")
    assert all(s.title != "[순서]" for s in other.sections)  # 묻지 않은 순서는 끼우지 않는다


def test_merge_keeps_same_text_once_and_folds_failed_trades_into_one_line():
    from app.agent.v3.orchestrator import merge
    from app.agent.v3.templates import UNKNOWN

    same = "관리사무소에 미리 알려요."
    m = merge([_part("dobae", same), _part("floor", same)], asked="도배랑 장판도 신고해요?")
    assert [x.text for s in m.sections for x in s.sentences] == [same]
    m = merge(
        [_part("dobae", "도배 답이에요."), _part("film"), _part("paint")],
        asked="도배랑 필름 칠 어때요?",
    )
    texts = [x.text for s in m.sections for x in s.sentences]
    assert texts[-1] == "필름 · 도장 쪽 자세한 내용은 믿집 자료로 확인하지 못했어요."
    assert UNKNOWN not in m.unknown
    none = merge([_part("film"), _part("paint")], asked="필름이랑 칠 어때요?")
    assert none.sections == [] and none.unknown[0] == UNKNOWN
    assert none.unknown.count(UNKNOWN) == 1  # 공종마다 되풀이하지 않는다


def test_reco_refusal_drops_promise_when_no_criteria_follow():
    from app.agent.v3 import templates
    from app.agent.v3.answer import Part, drop_promise

    p = Part()
    p.sections.append(templates.section([templates.BRAND_RECO]))
    drop_promise(p)
    assert p.sections[0].sentences[0].text == "특정 제품 · 브랜드는 추천하지 않아요."
    for q in ("현관 도어락은 어느 회사 게 튼튼해요?", "도배 잘하는 업체 한 곳만 찍어 주세요"):
        a = turn(q).answer
        if not a.trace.cards:  # 기준 카드가 없으면 '알려 드릴게요'라고 하지 않는다
            assert "알려 드릴게요" not in words(a), q


def test_low_confidence_asks_back_once_with_buttons():
    a = turn("음").answer
    assert a.trace.route == "ask_back"
    assert any(c.kind == "choices" and 2 <= len(c.choices) <= 3 for c in a.cards)


def test_phone_and_unit_numbers_are_masked_before_anything():
    a = turn("제 번호 010-1234-5678이고 101동 1203호예요. 도배 곰팡이는 왜 생겨요?").answer
    blob = words(a) + "".join(a.unknown)
    assert "010-1234-5678" not in blob and "1203호" not in blob


def test_check_drops_made_up_numbers_and_ai_actions():
    sec = Section(sentences=[
        Sentence(text="도배는 보통 3일 걸려요."),
        Sentence(text="합지는 시공이 빨라요."),
        Sentence(text="제가 실측 예약했어요."),
    ])  # fmt: skip
    kept, dropped = check.section(sec, "합지는 시공이 빨라요.", calc_text="")
    assert [x.text for x in kept.sentences] == ["합지는 시공이 빨라요."]
    assert len(dropped) == 2


def test_mask_covers_email_rrn_account_but_not_dates():
    t = check.mask("메일 a@b.kr, 900101-1234567, 국민 123456-78-901234, 날짜 2026-11-18")
    assert "a@b.kr" not in t and "900101-1234567" not in t and "123456-78-901234" not in t
    assert "2026-11-18" in t
    # 법 · 고시 번호와 지하철 노선은 동 · 호가 아니다
    for keep in ("대통령령 제36437호", "국토교통부고시 제2026-360호", "3호선"):
        assert check.mask(keep) == keep
    assert check.mask("1503호예요") == "(동호수 가림)예요"


@pytest.fixture
def llm_stub(monkeypatch):
    calls: list[list] = []

    def use(answer=None, error=False):
        async def fake(role, schema, messages):
            calls.append(messages)
            if error:
                raise llm.LLMError("stub")
            return schema.model_validate(answer)

        monkeypatch.setattr(llm, "structured", fake)
        return calls

    return use


def test_llm_mode_rewrites_extract_but_checks_numbers_and_cites(llm_stub):
    sec = Section(
        sentences=[Sentence(text="자료 글", cites=["K736"])], source="extract", route="trade"
    )
    calls = llm_stub({"sentences": [
        {"text": "합지는 종이 벽지라 시공이 빨라요.", "cites": ["K736"]},
        {"text": "보통 3일이면 끝나요.", "cites": ["K736"]},
    ]})  # fmt: skip
    new, dropped = asyncio.run(llm_mode.rewrite(
        sec, route="trade", question="합지 어때요? 010-1111-2222",
        evidence={"K736": "합지 벽지는 종이 두 겹을 붙여 만든 벽지예요. 시공이 빨라요."}, calc="",
    ))  # fmt: skip
    assert new is not None and new.source == "llm"
    assert [x.text for x in new.sentences] == ["합지는 종이 벽지라 시공이 빨라요."]
    assert dropped
    assert "010-1111-2222" not in str(calls[0])  # LLM 입력도 가린 글만


def test_llm_failure_keeps_free_answer(llm_stub):
    llm_stub(error=True)
    sec = Section(
        sentences=[Sentence(text="자료 글", cites=["K736"])], source="extract", route="trade"
    )
    new, dropped = asyncio.run(
        llm_mode.rewrite(sec, route="trade", question="q", evidence={}, calc="")
    )
    assert new is None and dropped == ["LLM 실패"]


def test_llm_prompt_leak_is_dropped(llm_stub):
    lines = llm_mode.prompt("trade").splitlines()
    leak = next(x for x in lines if len(x) >= 40 and not any(c.isdigit() for c in x))
    llm_stub({"sentences": [{"text": leak, "cites": []}]})
    sec = Section(
        sentences=[Sentence(text="자료 글", cites=["K736"])], source="extract", route="trade"
    )
    new, dropped = asyncio.run(
        llm_mode.rewrite(sec, route="trade", question="q", evidence={}, calc="")
    )
    assert new is None and "지시문 누출" in dropped


def test_free_mode_never_calls_llm(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("무료 모드에서 LLM을 불렀어요")

    monkeypatch.setattr(llm, "structured", boom)
    monkeypatch.setattr(llm, "chat_tools", boom)
    for q in (
        "합지랑 실크 차이가 뭐예요?",
        "몰딩 바꾸고 도배도 새로 하려는데 순서가 어떻게 돼요?",
        "음",
    ):
        assert turn(q).answer.trace.mode == "free"


@pytest.mark.parametrize(
    ("text", "card"),
    [
        ("개인 집 공사도 세금계산서를 받을 수 있어요?", "common.contract.tax-invoice"),
        ("업체가 세금계산서는 못 끊어 준대요, 왜 그래요?", "common.contract.tax-invoice"),
        (
            "인테리어 공사비를 현금으로 내면 현금영수증 받을 수 있어요?",
            "common.contract.cash-receipt",
        ),
    ],
)
def test_receipt_and_tax_invoice_questions_get_common_contract_cards(text, card):
    a = turn(text).answer
    assert a.trace.route == "guide"  # 범위 밖이 아니라 계약 · 비용(공통)
    assert any(c.startswith(f"card:{card}@") for c in a.trace.cards)


def test_paying_through_mitjip_is_still_out_of_scope():
    assert turn("믿집에서 카드 결제 돼요?").answer.trace.route == "out_of_scope"


def test_empty_index_does_not_publish_static_card_db_citations():
    from app.agent.v3.answer import KB as Knowledge
    from app.knowledge import bm25

    kb = Knowledge(ix=bm25.build([], {}), cards=KB.cards)
    ans = asyncio.run(run_turn("합지랑 실크 중에 뭐로 해야 해요?", CTX, kb=kb)).answer
    assert ans.sections or ans.unknown
    assert ans.trace.chunks == []
    assert not any(s.sources for s in ans.sections)
    assert not any(c.startswith("K") for s in ans.sections for x in s.sentences for c in x.cites)


def test_llm_drops_claim_with_unprovided_citation_and_uncomputed_amount(llm_stub):
    sec = Section(
        sentences=[Sentence(text="자료 글", cites=["K736"])], source="extract", route="trade"
    )
    llm_stub(
        {
            "sentences": [
                {"text": "합지는 종이 벽지예요.", "cites": ["K736"]},
                {"text": "항균 성능이 보장된대요.", "cites": ["K99999"]},
                {"text": "소음 저감 장치를 새로 설치해야 해요.", "cites": ["K736"]},
                {"text": "시공비가 50000원이에요.", "cites": ["K736"]},
            ]
        }
    )
    new, dropped = asyncio.run(
        llm_mode.rewrite(
            sec,
            route="trade",
            question="합지 어때요?",
            calc="",
            evidence={"K736": "합지는 종이 벽지예요. 50000원 예시."},
        )
    )
    assert new is not None
    assert [x.text for x in new.sentences] == ["합지는 종이 벽지예요."]
    assert len(dropped) == 3
