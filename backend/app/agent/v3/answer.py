"""담당들이 같이 쓰는 무료 모드 답 단계 (agents.md 10.2~10.4): 검증된 카드 → 조각 문장 뽑기 → 정직한 모름.

- 받아들이기: 카드 점수 ≥ T_CARD 이고 카드 근거 조각이 같은 질문의 검색 위 5개에 있음(맞물림), 또는 점수 ≥ T_HIGH.
  기준값은 E3 dev에서 골랐다(evaluation/v3/e3_answers.py, docs/v3/verification/b5.md).
- 숫자 검사 허용 글: 카드 글 + 인용 조각 원문(카드는 오프라인 검사를 통과한 글), 조각 문장은 그 조각 원문.
- 카드 · 조각 · 계산 결과 밖의 말은 하지 않는다. 못 찾으면 '확인하지 못했어요' + 확인할 곳 + 다음 할 일.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from app.agent.v3 import check, templates
from app.agent.v3.schemas import (
    AskSlot,
    CalcBox,
    Choice,
    PolicyFlag,
    ProjectFacts,
    PublicVendorFact,
    Route,
    Section,
    Sentence,
    TradeCode,
    TurnContext,
    UiCard,
)
from app.agent.v3.trades import BY_CODE, TAG_TO_CODE
from app.knowledge import bm25, cards, extract, search3

T_CARD = 0.55  # 맞물림이 있을 때 받는 점수(열쇠 점수 + 덤). E3 dev 격자에서 고름(b5.md 표)
T_HIGH = 0.62  # 맞물림이 없어도 받는 열쇠 점수
B3, B8 = 0.10, 0.05  # 카드 근거 조각이 검색 위 3개 · 위 8개에 있을 때 덤(E3 dev로 고름)
NEAR = 0.05  # 1등과 이만큼 가까운 카드는 '이것도 궁금하세요?'로
# 정해 둔 글로 답하는 정책 질문은 조각 문장을 붙이지 않는다(추천 · 금액 · 법률은 원문 한 줄이 오해를 부름)
NO_EXTRACT = frozenset({"price", "vendor_reco", "brand_reco", "legal_case"})


@dataclass
class KB:
    """한 턴이 쓰는 지식: 검색 색인(DB 적재본) + 카드(정적 파일)."""

    ix: bm25.Index
    cards: cards.CardSet
    tags: Mapping[str, str] = field(default_factory=lambda: TAG_TO_CODE)
    unavailable: bool = False


@dataclass
class Task:
    """오케스트레이터 → 담당 (또는 관문 → 담당). user · project는 ctx에만 있다."""

    text: str
    route: Route
    trades: list[TradeCode]
    policy: list[PolicyFlag]
    ctx: TurnContext
    facts: ProjectFacts | None = None
    vendors: list[PublicVendorFact] = field(default_factory=list)
    slot: str | None = None
    value: str | None = None
    asked: str = ""  # 되물은 담당이 남긴 원래 질문(칸 답일 때)


@dataclass
class Part:
    """담당 하나가 낸 몫. 오케스트레이터는 sections를 다시 쓰지 않고 이어 붙인다."""

    sections: list[Section] = field(default_factory=list)
    calc: list[CalcBox] = field(default_factory=list)
    cards: list[UiCard] = field(default_factory=list)
    ask: AskSlot | None = None
    unknown: list[str] = field(default_factory=list)
    suggest: list[str] = field(default_factory=list)
    used_cards: list[str] = field(default_factory=list)
    used_chunks: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    route: Route = "guide"
    trade: TradeCode | None = None
    scores: dict[str, float] = field(
        default_factory=dict
    )  # 받아들인 카드 tag → 점수(LLM 모드 '카드 먼저')

    @property
    def empty(self) -> bool:
        return not (self.sections or self.calc or self.cards or self.unknown)

    def add(self, sec: Section | None, allowed: str, calc_text: str | None = None) -> None:
        if sec is None:
            return
        shown = set(self.used_chunks) | set(self.used_cards)
        kept, dropped = check.section(sec, allowed, calc_text=calc_text, shown=shown or None)
        self.dropped.extend(dropped)
        if kept is not None:
            self.sections.append(kept)


def vendor_lookup(task: Task) -> Part:
    """명시한 도배 업체의 등록된 공개 정보와 상세 경로만 보여준다. 순위·평가·견적은 만들지 않는다."""
    part = Part(route=task.route, trade=next(iter(task.trades), None))
    if task.vendors:
        if len(task.vendors) > 1:
            line = "여러 업체가 확인됐어요. 확인할 업체의 상세 정보를 선택해 주세요."
            choices = [
                Choice(
                    text=f"{v.name} · {', '.join(v.areas[:2])}",
                    action=f"vendor_detail:{v.vendor_id}",
                )
                for v in task.vendors[:3]
            ]
            part.cards.append(UiCard(kind="next_step", choices=choices))
        else:
            v = task.vendors[0]
            lines = [f"{v.name}은 믿집에 등록된 도배 업체예요."]
            if v.areas:
                lines.append(f"업체가 등록한 활동 지역은 {', '.join(v.areas)}예요.")
            if v.intro:
                lines.append(f"업체 소개: {v.intro}")
            part.cards.append(
                UiCard(
                    kind="next_step",
                    choices=[
                        Choice(text=f"{v.name} 상세 보기", action=f"vendor_detail:{v.vendor_id}")
                    ],
                )
            )
            line = " ".join(lines)
    elif any(t != "dobae" for t in task.trades):
        line = "지금 업체 보기 화면에서는 도배 업체를 확인할 수 있어요."
    else:
        line = "알아볼 도배 업체 이름을 알려 주세요. 등록된 업체의 공개 정보와 상세 페이지를 확인해 드려요."
    sentences = lines if task.vendors and len(task.vendors) == 1 else [line]
    part.add(templates.section(sentences, route=part.route, trade=part.trade), line)
    return part


def accept(
    matches: list[cards.Match], top: list[str]
) -> tuple[cards.Match | None, list[cards.Match]]:
    """(받아들인 카드, 점수 순 후보). 점수 = 열쇠 점수 + 검색 맞물림 덤(인용 조각이 위 3개 B3 · 위 8개 B8).
    맞물림이 없으면 열쇠 점수 ≥ T_HIGH일 때만 받는다."""
    ranked: list[tuple[float, cards.Match]] = []
    for m in matches:
        cites = set(m.card.cites())
        bonus = B3 if cites & set(top[:3]) else (B8 if cites & set(top) else 0.0)
        ranked.append((m.score + bonus if bonus else m.score, m))
    ranked.sort(key=lambda p: -p[0])
    for s, m in ranked:
        locked = bool(set(m.card.cites()) & set(top))
        if (locked and s >= T_CARD) or m.score >= T_HIGH:
            return m, [x for _, x in ranked]
        break
    return None, [x for _, x in ranked]


def card_section(
    card: cards.AnswerCard, *, route: Route, trade: TradeCode | None, refs: set[str]
) -> Section:
    tag = card.tag

    def sent(x: cards.CardSentence | None) -> Sentence | None:
        return Sentence(text=x.text, cites=[*(r for r in x.cites if r in refs), tag]) if x else None

    ko = BY_CODE[card.trade].ko if card.trade in BY_CODE else ""
    return Section(
        title=f"[{ko}]" if ko else "",
        sentences=[sent(x) for x in card.answer],  # type: ignore[misc]
        caution=sent(card.caution),
        tip=sent(card.tip),
        source="card",
        route=route,
        trade=trade or card.trade,  # type: ignore[arg-type]
        sources=[
            s.model_dump(include={"ref", "title", "kind", "publisher"})
            for s in card.sources
            if s.ref in refs
        ],
    )


def card_allowed(kb: KB, card: cards.AnswerCard) -> str:
    texts = [x.text for x in card.answer] + [x.text for x in (card.caution, card.tip) if x]
    texts += [kb.ix.by_ref[r].chunk.text for r in card.cites() if r in kb.ix.by_ref]
    return "\n".join(texts)


def search(
    kb: KB, q: str, trade: TradeCode | None, *, original: str = "", today: date | None = None
) -> bm25.Result:
    want = [trade] if trade and trade != "common" else []
    return search3.search(
        kb.ix, q, trades=want, tags=kb.tags, k=8, policy_text=original, today=today
    )


def card_refs(card: cards.AnswerCard, kb: KB, restriction: search3.Limits) -> set[str] | None:
    """카드의 출처가 질문 제약과 다르면 제외한다. 정적 카드에서 조회하지 못한 DB 번호는 공개하지 않는다."""
    metadata = {s.ref: s for s in card.sources}
    refs = set()
    for ref in card.cites():
        if ref in cards.DOC_REFS:
            if restriction.kinds or restriction.dated:
                return None
            refs.add(ref)
            continue
        source = metadata.get(ref)
        if source and not restriction.allows_kind(source.kind):
            return None
        item = kb.ix.by_ref.get(ref)
        if kb.unavailable or item is None:
            if kb.unavailable or restriction.kinds or restriction.dated:
                return None
            continue
        scope = search3.scope(item, kb.tags)
        if not restriction.allows(item) or (
            card.trade != "common" and scope and card.trade not in scope
        ):
            return None
        # 같은 조각 번호가 다른 문서에 배정됐으면 카드 근거로 연결하지 않는다.
        if source and (source.title != item.doc.title or source.kind != item.doc.kind):
            return None
        refs.add(ref)
    return refs


def knowledge(
    part: Part,
    kb: KB,
    q: str,
    *,
    trade: TradeCode | None,
    route: Route,
    intents: tuple[str, ...] | None = None,
    facts: ProjectFacts | None = None,
    allow_extract: bool = True,
    policy: list[PolicyFlag] | tuple = (),
    card_id: str | None = None,
    original: str = "",
    today: date | None = None,
) -> str:
    """카드 → 조각 문장 → 모름 순서로 part에 담는다. 어느 길로 답했는지 돌려준다(card · extract · unknown)."""
    original = original or q
    restriction = search3.limits(original, today)
    hits = (
        bm25.Result([], False, [])
        if kb.unavailable
        else search(kb, q, trade, original=original, today=today)
    )
    top = [h.ref for h in hits.hits[:8]]
    want = [trade] if trade and trade != "common" else None
    matches = cards.find(kb.cards, q, trades=want, intents=intents, k=4)
    # 검색 맞물림 후보: 위 조각을 인용한 카드(열쇠 말이 달라도 근거가 같은 카드)
    seen = {m.card.id for m in matches}
    allow = set(want or ()) | {"common"} if want else None
    extra = [
        i for r in top for i in kb.cards.by_cite.get(r, [])
        if kb.cards.cards[i].id not in seen and (allow is None or kb.cards.cards[i].trade in allow)
        and (intents is None or kb.cards.cards[i].intent in intents)
    ]  # fmt: skip
    matches += cards.score(kb.cards, q, list(dict.fromkeys(extra)))
    matched_refs = {m.card.id: card_refs(m.card, kb, restriction) for m in matches}
    best, matches = accept([m for m in matches if matched_refs[m.card.id] is not None], top)
    if (
        card_id
        and (card := kb.cards.by_id.get(card_id)) is not None
        and (refs := card_refs(card, kb, restriction)) is not None
    ):
        matched_refs[card.id] = refs
        best = cards.Match(card=card, score=T_HIGH, key=q)
        matches = []
    if best is not None:
        c = best.card
        part.used_cards.append(c.tag)
        refs = matched_refs[c.id] or set()
        part.used_chunks.extend(r for r in c.cites() if r in refs)
        part.scores[c.tag] = best.score
        part.add(
            card_section(c, route=route, trade=trade, refs=refs),
            card_allowed(kb, c),
            calc_text="",
        )
        _card_extras(part, c, facts)
        part.suggest.extend(
            m.card.keys[0] for m in matches[1:] if best.score - m.score < NEAR and m.score >= T_CARD
        )
        return "card"
    if allow_extract and hits.enough and not (NO_EXTRACT & set(policy)):
        quotes = extract.pick(kb.ix, q, hits.hits)
        if quotes:
            # 서로 다른 문서·판의 적용 조건을 합쳐 하나의 기준처럼 제시하지 않는다.
            first = kb.ix.by_ref[quotes[0].ref].chunk
            quotes = [
                x
                for x in quotes
                if (chunk := kb.ix.by_ref[x.ref].chunk).doc == first.doc
                and chunk.effective_date == first.effective_date
            ]
            part.used_chunks.extend(x.ref for x in quotes)
            lead = (
                templates.EXTRACT_SUMMARY_LEAD
                if any(x.summary for x in quotes)
                else templates.EXTRACT_LEAD
            )
            sents = [Sentence(text=lead)]
            for x in quotes:  # 요약이 있으면 해요체 요약(2문장까지), 없으면 원문을 따옴표로
                if x.summary:
                    sents += [Sentence(text=s, cites=[x.ref]) for s in x.summary[:2]]
                else:
                    sents.append(
                        Sentence(text=f"“{x.text}” ({x.kind_ko} · {x.title[:40]})", cites=[x.ref])
                    )
            sec = Section(
                title=f"[{BY_CODE[trade].ko}]" if trade and trade in BY_CODE else "",
                sentences=sents, source="extract", route=route, trade=trade,
                sources=[{"ref": x.ref, "title": x.title, "kind": x.kind, "publisher": x.publisher} for x in quotes],
            )  # fmt: skip
            allowed = "\n".join(
                [lead, *(" ".join([x.text, x.title, *x.summary]) for x in quotes)]
                + [kb.ix.by_ref[x.ref].chunk.text for x in quotes if x.ref in kb.ix.by_ref]
            )
            n = len(part.sections)
            part.add(sec, allowed, calc_text="")
            if len(part.sections) > n:
                return "extract"
    where = BY_CODE[trade].check_where if trade and trade in BY_CODE else "실측 때 업체"
    notice = templates.KNOWLEDGE_UNAVAILABLE if kb.unavailable else templates.UNKNOWN
    part.unknown.extend([notice, templates.UNKNOWN_WHERE.format(where=where)])
    return "unknown"


def drop_promise(part: Part) -> None:
    """추천 거절 글의 '…알려 드릴게요'는 기준 카드가 붙을 때만 둔다. 답이 모름이면 거절 한 문장만 남겨요."""
    for sec in part.sections:
        if sec.source == "template":
            for x in sec.sentences:
                x.text = templates.RECO_SHORT.get(x.text, x.text)


def _card_extras(part: Part, c: cards.AnswerCard, facts: ProjectFacts | None) -> None:
    """카드의 계산 상자 · 되묻기. 숫자는 facts(계산 엔진)에 있을 때만 상자로."""
    for cc in c.calc:
        box = calc_box(cc.ref, facts, label=cc.label)
        if box is not None:
            part.calc.append(box)
    if c.ask and not slot_known(c.ask.slot, facts):
        part.ask = AskSlot(
            trade=c.trade,
            slot=c.ask.slot,
            question=c.ask.question,
            options=c.ask.options,  # type: ignore[arg-type]
        )


def slot_known(slot: str, facts: ProjectFacts | None) -> bool:
    if facts is None:
        return False
    if slot in facts.brief:
        return True
    spec = BY_CODE.get(slot.split(".")[0])
    for s in spec.slots if spec else ():
        if s.key == slot and s.fact:
            return getattr(facts, s.fact, None) is not None
    return False


def calc_box(ref: str, facts: ProjectFacts | None, label: str = "") -> CalcBox | None:
    """계산 엔진 결과를 상자로 옮긴다(값을 만들지 않음). 없는 값이면 None."""
    if facts is None:
        return None
    if ref == "standard.total" and facts.standard:
        st = facts.standard
        return CalcBox(
            label=label or "표준견적서 합계", ref=ref, amount=st.total, note="부가세 포함"
        )
    if ref.startswith("standard.lines.") and facts.standard:
        key = ref.rsplit(".", 1)[1]
        for ln in facts.standard.lines:
            if ln.key == key:
                return line_box(ln)
        return None
    if ref == "plan.range" and facts.plan:
        p = facts.plan
        return CalcBox(
            label=label or "예상 공정표", ref=ref, qty=p.work_days, unit="일",
            note=f"{templates.md(p.start)} ~ {templates.md(p.end)}",
        )  # fmt: skip
    return None


def line_box(ln) -> CalcBox:
    return CalcBox(
        label=ln.name, ref=f"standard.lines.{ln.key}", qty=ln.qty, unit=ln.unit,
        unit_price=ln.unit_price, amount=ln.amount, in_total=ln.state in ("included", "separate"),
    )  # fmt: skip
