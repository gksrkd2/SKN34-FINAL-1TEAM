// AI 답 한 개를 그린다: '자동 답변' · 묶음 문장 · 주의 · 팁 · 출처 · 믿집 계산 상자 · 화면 조각(버튼 · 초안 카드).
// 숫자는 계산 엔진 값(calc)만 상자로 보이고, 이 조각은 계산하지 않아요. 링크는 믿집 화면 주소만 만들어요.
"use client";

import { Button } from "@/components/ui/Button";
import { CalcBox, type CalcLine } from "@/components/ui/CalcBox";
import { md } from "@/lib/format";
import type { AiAnswer as Answer, AiButton, AiCalc, AiCard, AiChoice, AiRoute, AiTurnOut } from "@/lib/api/chat";
import s from "./ai.module.css";

const ACTION_PATH: Record<string, (id?: string) => string> = {
  start: () => "/start",
  login: () => "/login",
  standard: (id) => (id ? `/projects/${id}/standard` : "/start/standard"),
  plan: (id) => (id ? `/projects/${id}/plan` : "/start"),
  vendors: (id) => (id ? `/projects/${id}/vendors` : "/start"),
  visits_new: (id) => (id ? `/projects/${id}/visits/new` : "/start"),
  visits: (id) => (id ? `/projects/${id}/visits` : "/start"),
  calendar: (id) => (id ? `/projects/${id}/visits` : "/start"),
  quotes: (id) => (id ? `/projects/${id}/quotes` : "/start"),
};

function calcLine(c: AiCalc): CalcLine | null {
  if (c.amount != null && c.qty != null && c.unit_price != null) {
    return {
      terms: [{ n: c.qty, unit: (c.unit as "㎡") || "" }, "×", { n: c.unit_price, unit: "원" }],
      result: { n: c.amount, unit: "원", paren: !c.in_total },
      note: c.in_total ? c.label : `${c.label} · 합계 밖`,
    };
  }
  if (c.amount != null) return { terms: [], result: { n: c.amount, unit: "원", label: c.label }, note: c.note || undefined };
  return null;
}

function Choices({ choices, projectId, onPick }: { choices: AiChoice[]; projectId?: string; onPick?: (text: string, b?: AiButton) => void }) {
  return (
    <div className={s.choices}>
      {choices.map((c, i) => {
        if (c.action && ACTION_PATH[c.action]) {
          return <Button key={i} variant="secondary" size="sm" href={ACTION_PATH[c.action](projectId)}>{c.text}</Button>;
        }
        return (
          <Button key={i} variant="weak" size="sm" onClick={() => onPick?.(c.text, c.route ? { route: c.route, trade: c.trade, slot: c.slot, value: c.value } : undefined)}>
            {c.text}
          </Button>
        );
      })}
    </div>
  );
}

function UiCard({ card, projectId, onPick }: { card: AiCard; projectId?: string; onPick?: (text: string, b?: AiButton) => void }) {
  if (card.kind === "visit_draft" && card.draft) {
    const d = card.draft;
    return (
      <div className={s.draft}>
        <p className={s.draftTitle}>{card.title || "실측 일정 요청 · 초안"}</p>
        {d.wish_start && d.wish_end ? (
          <p><span className={s.mute}>희망 기간</span> {md(d.wish_start)} ~ {md(d.wish_end)}</p>
        ) : null}
        {d.vendors.length ? <p><span className={s.mute}>업체</span> {d.vendors.join(" · ")}</p> : null}
        <p className={s.mute}>AI는 초안만 만들어요. 보내기는 직접 눌러요.</p>
        <Choices choices={card.choices} projectId={projectId} onPick={onPick} />
      </div>
    );
  }
  return <Choices choices={card.choices} projectId={projectId} onPick={onPick} />;
}

const ROUTE_LABEL: Record<AiRoute, string> = {
  guide: "일반 안내", trade: "공정 상담", quote_explanation: "견적 설명", schedule: "공정표",
  multi: "복합 상담", out_of_scope: "범위 밖 안내", ask_back: "질문 확인",
};
const TRADE_LABEL: Record<string, string> = {
  dobae: "도배", mokgong: "목공", floor: "바닥", tile: "타일", bath: "욕실",
  kitchen: "주방", paint: "도장", film: "필름", electric: "전기", window: "창호",
  demolition: "철거", plumbing: "설비", common: "공통",
};
const CLASSIFICATION_LABEL = { button: "선택지", waiting: "이전 상담 이어가기", rule: "규칙", model: "분류 모델" };
const SOURCE_LABEL: Record<Answer["sections"][number]["source"], string> = {
  card: "답 카드", extract: "자료 발췌", llm: "모델 재작성", template: "정해진 안내", calc: "계산 결과",
};

function QuestionAnalysis({ turn }: { turn: AiTurnOut }) {
  const { analysis, route, answer, mode } = turn;
  if (!analysis) return null;
  const topic = ROUTE_LABEL[route];
  const trades = analysis.trades.map((trade) => TRADE_LABEL[trade]).filter(Boolean).join(" · ");
  const participants = analysis.participants.map((part) => {
    const [role, trade] = part.split(":");
    return [ROUTE_LABEL[role as AiRoute], TRADE_LABEL[trade]].filter(Boolean).join(" · ");
  }).filter(Boolean);
  const sources = new Set(answer.sections.map((section) => SOURCE_LABEL[section.source]));
  if (answer.calc.length) sources.add("믿집 계산");
  if (answer.unknown.length) sources.add("확인 안내");
  const refs = [...new Set(answer.sections.flatMap((section) => section.sources.map((source) => source.ref)))];
  const classification = CLASSIFICATION_LABEL[analysis.classification];
  const answerBy = route === "multi" ? "오케스트레이터" : topic;
  const rows = [
    ["분류 방식", classification],
    ["적용 공정", trades || "없어요"],
    ["답변 주체", answerBy],
    ["참여 에이전트", participants.join(" / ") || "없어요"],
    ["답변 모드", mode === "llm" ? "모델 사용 모드" : "무료 모드"],
    ["적용 프롬프트", analysis.prompts.join(" · ") || "없어요"],
    ["답변 구성", [...sources].join(" · ") || "없어요"],
    ["표시된 출처 번호", refs.join(" · ") || "없어요"],
    ["처리 경로", [classification, answerBy, "출력 검사"].join(" → ")],
  ];

  return (
    <details className={s.analysis}>
      <summary>질문 분석 · {topic}{trades ? ` · ${trades}` : ""}</summary>
      <dl>
        {rows.map(([label, value]) => (
          <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
        ))}
      </dl>
    </details>
  );
}

export function AiAnswer({ answer, turn, projectId, onPick }: { answer: Answer; turn?: AiTurnOut; projectId?: string; onPick?: (text: string, b?: AiButton) => void }) {
  const many = answer.sections.length > 1;
  const lines = answer.calc.map(calcLine).filter((x): x is CalcLine => x !== null);
  const notes = answer.calc.filter((c) => calcLine(c) === null && (c.note || c.qty != null));
  return (
    <div className={s.answer}>
      <p className={s.notice}>{answer.notice}</p>
      {answer.sections.map((sec, i) => (
        <div key={i} className={s.section}>
          {many && sec.title ? <p className={s.secTitle}>{sec.title}</p> : null}
          {sec.sentences.map((t, j) => <p key={j}>{t}</p>)}
          {sec.caution ? <p className={s.caution}>주의: {sec.caution}</p> : null}
          {sec.tip ? <p className={s.tip}>팁: {sec.tip}</p> : null}
          {sec.sources.length ? (
            <p className={s.sources}>출처 · {Array.from(new Set(sec.sources.map((x) => x.title))).slice(0, 3).join(" · ")}</p>
          ) : null}
        </div>
      ))}
      {answer.unknown.map((t, i) => <p key={`u${i}`} className={s.unknown}>{t}</p>)}
      {lines.length ? <CalcBox lines={lines} /> : null}
      {notes.map((c, i) => (
        <p key={`n${i}`} className={`${s.calcNote} mj-num`}>믿집 계산 · {c.label} {c.qty != null ? `${c.qty}${c.unit} · ` : ""}{c.note}</p>
      ))}
      {answer.ask ? (
        <div className={s.ask}>
          <p>{answer.ask.question}</p>
          <div className={s.choices}>
            {answer.ask.options.map((o) => (
              <Button key={o} variant="weak" size="sm" onClick={() => onPick?.(o, { route: "trade", trade: answer.ask!.trade, slot: answer.ask!.slot, value: o })}>{o}</Button>
            ))}
          </div>
        </div>
      ) : null}
      {answer.cards.map((c, i) => <UiCard key={`c${i}`} card={c} projectId={projectId} onPick={onPick} />)}
      {answer.suggest.length ? (
        <div className={s.choices} aria-label="이어 물을 질문">
          {answer.suggest.map((q) => <Button key={q} variant="text" size="sm" onClick={() => onPick?.(q)}>{q}</Button>)}
        </div>
      ) : null}
      {turn ? <QuestionAnalysis turn={turn} /> : null}
    </div>
  );
}
