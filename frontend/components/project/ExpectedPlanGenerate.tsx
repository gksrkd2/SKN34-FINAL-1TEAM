"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { DateCalendar } from "@/components/start/DateCalendar";
import { call, errorText, type Schemas } from "@/lib/backend/client";

const MOKGONG_ITEMS = [
  ["flatten", "천장 평탄화"], ["insulation", "외벽 단열 보강"],
  ["door_frame", "문틀 교체"], ["door", "문짝 교체"],
  ["molding", "천장 몰딩"], ["base", "걸레받이"],
] as const;
import s from "./ExpectedPlanGenerate.module.css";

export function ExpectedPlanGenerate({ id, version, start, today, locked = false, mokgongItems }: {
  id: string; version: number; start: string; today: string; locked?: boolean; mokgongItems?: string[];
}) {
  const router = useRouter();
  const [items, setItems] = useState(mokgongItems ?? []);
  const [day, setDay] = useState(start);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function generate(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (mokgongItems && !items.length) { setError("목공 항목을 골라 주세요."); return; }
    if (!day) { setError("시작일을 골라 주세요."); return; }
    setBusy(true);
    setError("");
    try {
      await call<Schemas["PlanOut"]>("POST", `/projects/${id}/expected-plan/generate`, { seen_version: version, start: day, ...(mokgongItems ? { mokgong_items: items } : {}) });
      router.refresh();
    } catch (exception) {
      setError(errorText(exception));
    } finally {
      setBusy(false);
    }
  }

  if (locked) return <Link className="mj-btn mj-btn-sec" href={`/projects/${id}/schedule`}>공사 날짜 보기</Link>;
  return <form onSubmit={generate} aria-label="예상 공정표 생성">
    <h2 className="mj-h3">{version ? "예상 공정표 다시 만들기" : "예상 공정표 만들기"}</h2>
    <p className="mj-small">입력한 면적·공간·공종에 맞는 DB 기준을 찾아 목공·도배 기간을 계산해요. 부족한 물량·인원은 LLM이 추정하고 가정값으로 표시해요.</p>
    {mokgongItems ? <fieldset><legend>계산할 목공 항목</legend>
      <p className="mj-small">예상 공정표에서 시험할 항목을 골라요. 집 정보에 저장된 선택은 바뀌지 않아요.</p>
      <div className={s.options}>{MOKGONG_ITEMS.map(([key, label]) => <label key={key}>
        <input type="checkbox" checked={items.includes(key)} disabled={busy} onChange={(event) => setItems(event.target.checked ? [...items, key] : items.filter((item) => item !== key))} />{label}
      </label>)}</div>
    </fieldset> : null}
    <DateCalendar id="expected-start" value={day} onChange={setDay} today={today} notToday label="공사 시작일" />
    <p><button type="submit" className="mj-btn mj-btn-pri" disabled={busy}>{busy ? "기준 자료를 찾아 계산하고 있어요…" : "예상 공정표 계산하기"}</button></p>
    <p className="mj-small" role="status">{busy ? "잠시 기다려 주세요." : error}</p>
  </form>;
}
