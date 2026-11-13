// 예상 공정표 두 가지 보기 (시안 WPlan): 하루 한 칸 막대 · 날짜별 할 일 표.
// 칸은 API가 준 days를 그대로 늘어놓고, 막대는 그 날의 일(kind · trade)이 같은 블록끼리 이어 붙인다. 날짜 · 기간을 새로 계산하지 않는다.
import { Fragment } from "react";
import type { ApiPlan } from "@/lib/api/schedule";
import { dayParts, md } from "@/lib/format";
import { tradeLabel } from "@/lib/labels";
import s from "./PlanViews.module.css";

type Block = ApiPlan["blocks"][number];
type Day = ApiPlan["days"][number];

const belongs = (b: Block, d: Day) => (b.kind === "work" ? d.kind === "work" && d.trade === b.trade : d.kind === b.kind);
const SUB: Record<string, string> = { dry: "창문 닫기", clean: "고객이 직접" };
const tone = (b: Block) => (b.kind !== "work" ? s.soft : b.trade === "dobae" ? s.pastel : s.strong);

/** 블록이 걸친 칸 묶음: [첫 칸, 칸 수]. 사이에 쉬는 날이 끼면 막대가 나뉜다. */
function runs(b: Block, days: Day[]) {
  const out: [number, number][] = [];
  days.forEach((d, i) => {
    if (!belongs(b, d)) return;
    const last = out[out.length - 1];
    if (last && last[0] + last[1] === i) last[1]++;
    else out.push([i, 1]);
  });
  return out;
}

export function PlanBars({ plan }: { plan: ApiPlan }) {
  const { days, blocks } = plan;
  const months = [...new Set(days.map((d) => dayParts(d.day).m))];
  const tall = `1 / ${blocks.length + 2}`;
  return (
    <div className={s.scroll} role="region" tabIndex={0} aria-label="예상 공정표 막대, 옆으로 넘겨 볼 수 있어요. 같은 내용은 날짜별 표에 있어요">
      <div
        className={`mj-num ${s.grid}`}
        aria-hidden="true"
        style={{ gridTemplateColumns: `232px repeat(${days.length}, minmax(48px, 1fr))`, gridTemplateRows: `48px repeat(${blocks.length}, 54px)`, minWidth: 232 + days.length * 48 }}
      >
        {days.map((d, i) => (d.kind === "off" ? <span key={`o${d.day}`} className={s.offCol} style={{ gridColumn: i + 2, gridRow: tall }} /> : null))}
        {days.map((d, i) => <span key={`v${d.day}`} className={s.vline} style={{ gridColumn: i + 2, gridRow: tall }} />)}
        {[...blocks, null].map((_, r) => <span key={`h${r}`} className={s.hline} style={{ gridRow: r + 1 }} />)}
        <span className={s.name} style={{ gridRow: 1 }}><span className="mj-small">작업 · {months.map((m) => `${m}월`).join(" ~ ")}</span></span>
        {days.map((d, i) => {
          const p = dayParts(d.day);
          return <span key={`d${d.day}`} className={`${s.head}${d.kind === "off" ? ` ${s.headOff}` : ""}`} style={{ gridColumn: i + 2 }}><b>{p.d === 1 && i > 0 ? `${p.m}.${p.d}` : p.d}</b>{p.w}</span>;
        })}
        {blocks.map((b, r) => (
          <Fragment key={`${b.kind}-${b.trade ?? ""}-${b.start}`}>
            <span className={s.name} style={{ gridRow: r + 2 }}><b>{b.label}</b><span className="mj-small">{b.days}일{SUB[b.kind] ? ` · ${SUB[b.kind]}` : ""}</span></span>
            {runs(b, days).map(([c, n]) => <span key={c} className={`${s.bar} ${tone(b)}`} style={{ gridRow: r + 2, gridColumn: `${c + 2} / span ${n}` }} />)}
          </Fragment>
        ))}
      </div>
    </div>
  );
}

export function PlanLegend({ plan }: { plan: ApiPlan }) {
  const has = (t: string) => plan.blocks.some((b) => b.kind === "work" && b.trade === t);
  return (
    <ul className={s.legend}>
      {has("mokgong") ? <li><span aria-hidden="true" className={s.strong} />목공</li> : null}
      {has("dobae") ? <li><span aria-hidden="true" className={s.pastel} />도배</li> : null}
      {plan.blocks.some((b) => b.kind !== "work") ? <li><span aria-hidden="true" className={s.soft} />건조 · 고객이 하는 일</li> : null}
      {plan.days.some((d) => d.kind === "off") ? <li><span aria-hidden="true" className={s.offKey} />휴일</li> : null}
    </ul>
  );
}

function dayText(d: Day) {
  if (d.kind === "off") return d.holiday ? `휴일 · ${d.holiday}` : "휴일";
  if (d.kind === "work") return `${tradeLabel(d.trade ?? "")} · ${d.names.join(", ")}`;
  return d.names.join(", ");
}

export function PlanDays({ plan }: { plan: ApiPlan }) {
  return (
    <table className={`mj-table mj-num ${s.days}`}>
      <caption className={s.cap}>날짜별 할 일 · {md(plan.start)} ~ {md(plan.end)}</caption>
      <thead><tr><th scope="col">날짜</th><th scope="col">할 일</th></tr></thead>
      <tbody>
        {plan.days.map((d) => (
          <tr key={d.day} className={d.kind === "off" ? s.offRow : undefined}>
            <th scope="row">{md(d.day)}</th>
            <td>{dayText(d)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
