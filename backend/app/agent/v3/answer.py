"""담당들이 같이 쓰는 무료 모드 답 단계 (agents.md 10.2~10.4): 검증된 카드 → 조각 문장 뽑기 → 정직한 모름.

- 받아들이기: 카드 점수 ≥ T_CARD 이고 카드 근거 조각이 같은 질문의 검색 위 5개에 있음(맞물림), 또는 점수 ≥ T_HIGH.
  기준값은 E3 dev에서 골랐다(evaluation/v3/e3_answers.py, docs/v3/verification/b5.md).
- 숫자 검사 허용 글: 카드 글 + 인용 조각 원문(카드는 오프라인 검사를 통과한 글), 조각 문장은 그 조각 원문.
- 카드 · 조각 · 계산 결과 밖의 말은 하지 않는다. 못 찾으면 '확인하지 못했어요' + 확인할 곳 + 다음 할 일.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.agent.v3 import check, templates
from app.agent.v3.schemas import (
    AskSlot,
    CalcBox,
    CompareTable,
    PolicyFlag,
    ProjectFacts,
    Route,
    Section,
    Sentence,
    TableRow,
    TradeCode,
    TurnContext,
    UiCard,
)
from app.agent.v3.specialists import registered
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
    vector_rids: Mapping[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    interpretations: Mapping[tuple[str, str], object | None] = field(default_factory=dict)


@dataclass
class Task:
    """오케스트레이터 → 담당 (또는 관문 → 담당). user · project는 ctx에만 있다."""

    text: str
    route: Route
    trades: list[TradeCode]
    policy: list[PolicyFlag]
    ctx: TurnContext
    facts: ProjectFacts | None = None
    slot: str | None = None
    value: str | None = None
    asked: str = ""  # 되물은 담당이 남긴 원래 질문(칸 답일 때)

    @property
    def search_text(self) -> str:
        if self.slot and self.value:
            return f"{self.asked} {self.value}".strip()
        return self.text


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


def card_section(card: cards.AnswerCard, *, route: Route, trade: TradeCode | None) -> Section:
    tag = card.tag

    def sent(x: cards.CardSentence | None) -> Sentence | None:
        return Sentence(text=x.text, cites=[*x.cites, tag]) if x else None

    ko = BY_CODE[card.trade].ko if card.trade in BY_CODE else ""
    return Section(
        title=f"[{ko}]" if ko else "",
        sentences=[sent(x) for x in card.answer],  # type: ignore[misc]
        caution=sent(card.caution),
        tip=sent(card.tip),
        source="card",
        route=route,
        trade=trade or card.trade,  # type: ignore[arg-type]
        sources=[s.model_dump(include={"ref", "title", "kind", "publisher"}) for s in card.sources],
    )


def card_allowed(kb: KB, card: cards.AnswerCard) -> str:
    texts = [x.text for x in card.answer] + [x.text for x in (card.caution, card.tip) if x]
    texts += [kb.ix.by_ref[r].chunk.text for r in card.cites() if r in kb.ix.by_ref]
    return "\n".join(texts)


def _lookup_text(kb: KB, q: str, trade: TradeCode | None) -> str:
    interpreted = kb.interpretations.get((trade, q)) if trade else None
    return getattr(interpreted, "lookup_text", None) or q


def search(kb: KB, q: str, trade: TradeCode | None) -> bm25.Result:
    specialist = registered().get(trade or "")
    if specialist is not None and specialist.retrieve is not None:
        return specialist.retrieve(kb, q)
    want = [trade] if trade and trade != "common" else []
    query = _lookup_text(kb, q, trade)
    return search3.search(
        kb.ix, query, trades=want, tags=kb.tags, k=8, policy_text=q if query != q else None
    )


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
) -> str:
    """카드 → 조각 문장 → 모름 순서로 part에 담는다. 어느 길로 답했는지 돌려준다(card · extract · unknown)."""
    hits = search(kb, q, trade)
    top = [h.ref for h in hits.hits[:8]]
    want = [trade] if trade and trade != "common" else None
    matches = cards.find(kb.cards, q, trades=want, intents=intents, k=4)
    restricted = NO_EXTRACT & set(policy)
    lookup_text = q if restricted else _lookup_text(kb, q, trade)
    # 담당이 원문과 정확히 연결한 카드가 있으면 그 결정을 유지한다.
    if any(match.score == 1 for match in matches):
        lookup_text = q
    elif not restricted:
        interpreted = kb.interpretations.get((trade, q)) if trade else None
        for card_id in getattr(interpreted, "preferred_cards", ()):
            preferred = kb.cards.by_id.get(card_id)
            if preferred and _supported_card(kb, preferred, q, trade, intents):
                use_card(part, kb, preferred, route=route, trade=trade, facts=facts)
                return "card"
    if lookup_text != q:
        matches = cards.find(kb.cards, lookup_text, trades=want, intents=intents, k=4)
    # 검색 맞물림 후보: 위 조각을 인용한 카드(열쇠 말이 달라도 근거가 같은 카드)
    seen = {m.card.id for m in matches}
    allow = set(want or ()) | {"common"} if want else None
    extra = [
        i for r in top for i in kb.cards.by_cite.get(r, [])
        if kb.cards.cards[i].id not in seen and (allow is None or kb.cards.cards[i].trade in allow)
        and (intents is None or kb.cards.cards[i].intent in intents)
    ]  # fmt: skip
    matches += cards.score(kb.cards, lookup_text, list(dict.fromkeys(extra)))
    if lookup_text != q:
        matches = [m for m in matches if _supported_card(kb, m.card, q, trade, intents)]
    best, matches = accept(matches, top)
    if best is not None:
        c = best.card
        use_card(part, kb, c, route=route, trade=trade, facts=facts, score=best.score)
        part.suggest.extend(
            m.card.keys[0] for m in matches[1:] if best.score - m.score < NEAR and m.score >= T_CARD
        )
        return "card"
    if allow_extract and hits.enough and not (NO_EXTRACT & set(policy)):
        quotes = extract.pick(kb.ix, lookup_text, hits.hits)
        if quotes:
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
            part.add(sec, allowed)
            if part.sections:
                return "extract"
    where = BY_CODE[trade].check_where if trade and trade in BY_CODE else "실측 때 업체"
    part.unknown.extend([templates.UNKNOWN, templates.UNKNOWN_WHERE.format(where=where)])
    return "unknown"


def _supported_card(
    kb: KB, card: cards.AnswerCard, q: str, trade: TradeCode | None, intents: tuple[str, ...] | None
) -> bool:
    if card.trade not in (trade, "common") or (intents is not None and card.intent not in intents):
        return False
    _, excluded = bm25.policy_of(q)
    cites = card.cites()
    return bool(cites) and all(
        ref in kb.ix.by_ref and kb.ix.by_ref[ref].doc.kind not in excluded for ref in cites
    )


def use_card(
    part: Part,
    kb: KB,
    c: cards.AnswerCard,
    *,
    route: Route,
    trade: TradeCode | None,
    facts: ProjectFacts | None,
    score: float = 1.0,
) -> None:
    """카드 하나를 몫에 담는다: 묶음 문장 + 비교표 · 계산 상자 · 되묻기."""
    part.used_cards.append(c.tag)
    part.used_chunks.extend(c.cites())
    part.scores[c.tag] = score
    part.add(card_section(c, route=route, trade=trade), card_allowed(kb, c))
    _card_extras(part, c, facts)


@dataclass
class NextPick:
    """되물은 칸 답 고르기 결과: 다음 카드 하나, 또는 다시 물을 선택지(card는 None)."""

    card: cards.AnswerCard | None = None
    again: list[str] = field(default_factory=list)


# 버튼 글 바로 뒤에 붙으면 '그건 아니에요'라는 뜻("벽 · 천장까지 새로 말고 도배만 새로")
DENY_AFTER = re.compile(r"^(?:은|는|이|가|을|를)?(?:말고|아니고|아니라|빼고|대신|안해|안하|싫)")


def _flat(s: str) -> str:
    return re.sub(r"[\s·,]", "", s)


def next_card(kb: KB, slot: str, value: str) -> NextPick | None:
    """되물은 칸의 답에 이어 줄 카드(카드 ask.next). 버튼 글과 같거나 버튼 글이 답 안에 있으면 그 카드.

    글로 답할 때 버튼 글 바로 뒤에 '말고 · 아니고 · 빼고 · 안 해요'가 오면 그 선택지는 뺀 것으로 봐요.
    남은 선택지가 하나면 그 카드, 둘 이상이거나 부정만 했으면 남은 선택지로 다시 물어요(일반 검색으로 넘기지 않음).
    버튼 글이 하나도 없으면 None(원래 질문 + 답으로 다시 찾기).
    """
    v = _flat(value)
    for c in kb.cards.cards:
        if not (c.ask and c.ask.slot == slot and c.ask.next):
            continue
        keep: list[str] = []
        denied: list[str] = []
        for opt in c.ask.next:
            o = _flat(opt)
            i = v.find(o) if o else -1
            if i < 0:
                continue
            (denied if DENY_AFTER.match(v[i + len(o) :]) else keep).append(opt)
        if len(keep) == 1:
            return NextPick(card=kb.cards.by_id.get(c.ask.next[keep[0]]))
        if keep:
            return NextPick(again=keep)
        if denied:
            return NextPick(again=[o for o in c.ask.options if o not in denied])
    return None


def drop_promise(part: Part) -> None:
    """추천 거절 글의 '…알려 드릴게요'는 기준 카드가 붙을 때만 둔다. 답이 모름이면 거절 한 문장만 남겨요."""
    for sec in part.sections:
        if sec.source == "template":
            for x in sec.sentences:
                x.text = templates.RECO_SHORT.get(x.text, x.text)


def _card_extras(part: Part, c: cards.AnswerCard, facts: ProjectFacts | None) -> None:
    """카드의 비교표 · 계산 상자 · 되묻기. 숫자는 facts(계산 엔진)에 있을 때만 상자로."""
    if c.compare:
        t = c.compare
        refs = {r for row in t.rows for r in row.cites}
        part.used_chunks.extend(sorted(refs))
        titles = [s.title for s in c.sources if s.ref in refs]
        part.cards.append(
            UiCard(
                kind="compare",
                title=t.title,
                table=CompareTable(
                    cols=list(t.cols),
                    rows=[TableRow(cells=list(r.cells), cites=list(r.cites)) for r in t.rows],
                    sources=list(dict.fromkeys(titles)),
                ),
            )
        )
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
            label=label or "표준견적서 합계",
            ref=ref,
            amount=st.total,
            note="부가세 포함 · 시장 단가 기준" if st.basis == "market" else "부가세 포함",
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
