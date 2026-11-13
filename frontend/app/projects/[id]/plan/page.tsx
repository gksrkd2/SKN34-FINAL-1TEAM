import Link from "next/link";
import { notFound } from "next/navigation";
import { FinalPlan } from "@/components/plan/FinalPlan";
import type { PlanContact } from "@/components/plan/PlanContacts";
import { ExpectedPlanGenerate } from "@/components/project/ExpectedPlanGenerate";
import { PlanTabs } from "@/components/project/PlanTabs";
import { PlanBars, PlanDays, PlanLegend } from "@/components/project/PlanViews";
import { ProjectCrumb } from "@/components/project/ProjectCrumb";
import { getMyProject } from "@/lib/api/project";
import { getExpectedStatus, getOverview, getPlan, getSchedule, type ApiSchedule } from "@/lib/api/schedule";
import { listProjectVisits } from "@/lib/api/visit";
import { projectTitle, TRADE_ORDER } from "@/lib/labels";
import { dateSeoul, dotDate, md, todaySeoul } from "@/lib/format";
import s from "./page.module.css";

export const metadata = { title: "공정표" };

/** 확정한 업체 연락처: 공정마다 확정한 요청(chosen)이 공개 규칙대로 준 것만 */
async function contactsOf(id: string, sch: ApiSchedule | null): Promise<PlanContact[]> {
  const rows = sch?.trades ?? [];
  const lists = await Promise.all(rows.map((t) => listProjectVisits(id, t.deal.trade === "dobae" ? "dobae" : "mokgong")));
  return rows.flatMap((t, i) => {
    const v = lists[i]?.visits.find((x) => x.id === t.deal.visit_id);
    return v?.contact_visible && v.contact ? [{ trade: t.deal.trade, vendorName: t.deal.vendor_name, ct: v.contact }] : [];
  });
}

// 공정표: 공사 날짜를 하나라도 수락했으면 확정 공정표(시안 WPlanDone, components/plan), 아니면 예상 공정표(시안 WPlan).
// ?kind=expected면 수락한 뒤에도 예상 공정표를 본다. 날짜 · 기간 · 품 · 인원은 모두 API(계산 엔진) 값이고 화면에서 나누거나 올리지 않는다.
export default async function PlanPage({ params, searchParams }: { params: Promise<{ id: string }>; searchParams: Promise<{ kind?: string }> }) {
  const { id } = await params;
  const { kind } = await searchParams;
  const want = kind === "expected" ? "expected" : undefined;
  const [p, expected] = await Promise.all([getMyProject(id), getExpectedStatus(id)]);
  if (!p || !expected) notFound();
  const plan = want !== "expected" && expected.final_version > 0 ? await getPlan(id, "final") : expected.plan;
  const title = projectTitle(p.region, TRADE_ORDER.filter((t) => p.trades[t]));
  if (!plan) return <main id="main">
    <ProjectCrumb id={id} title={title} here="예상 공정표" />
    <section className="mj-wrap">
      <h1 className="mj-h1">예상 공정표</h1>
      <ExpectedPlanGenerate id={id} version={expected.version} start={p.when.kind === "date" ? p.when.start ?? "" : ""} today={todaySeoul()} locked={expected.has_deals} mokgongItems={p.trades.mokgong?.items} />
    </section>
  </main>;
  if (plan.kind === "final") {
    // 공사 전에 할 일: 체크 목록 · 시작일은 한 공사 화면 API 값 (시작일은 그 공정표가 확정일 때만)
    const [sch, ov] = await Promise.all([getSchedule(id), getOverview(id)]);
    const start = ov?.period.kind === "final" ? ov.period.start : null;
    return <FinalPlan id={id} title={title} plan={plan} rows={sch?.trades ?? []} contacts={await contactsOf(id, sch)} checks={ov?.checks ?? []} start={start} />;
  }
  // 예상 공정표를 골라 봤는데 수락한 날짜가 있으면 확정 공정표로 돌아가는 링크를 둔다.
  // 업체를 확정한 공정이 있으면(다시 고른 것 빼고) 날짜 바꿔 보기를 막는다(서버도 409로 막는다).
  const hasFinal = want === "expected" && expected.final_version > 0;
  const dealt = expected.has_deals;
  const works = plan.blocks.filter((b) => b.kind === "work");
  const workEnd = works.at(-1)?.end ?? plan.end;
  const basis = plan.basis ?? {};

  return (
    <main id="main">
      <ProjectCrumb id={id} title={title} here="예상 공정표" />
      {hasFinal ? (
        <p className={`mj-wrap ${s.final}`}><Link className="mj-lnk" href={`/projects/${id}/plan`}>확정 공정표 보기</Link><span className="mj-small">수락한 공사 날짜로 만든 공정표예요.</span></p>
      ) : null}
      <div className={`mj-wrap ${s.top}`}>
        <div className={s.topL}>
          <div className={s.h}>
            <h1 className="mj-h1">예상 공정표</h1>
            <p className="mj-small mj-num">{dotDate(dateSeoul(plan.computed_at))} 계산</p>
          </div>
          <p className={`mj-num ${s.line}`}>
            <span>일하는 날 <b>{plan.work_days}일</b> · <b>{md(plan.start)} ~ {md(workEnd)}</b></span>
          </p>
          <p className="mj-small">{works.map((b) => `${b.label} 약 ${b.days}일`).join(" → ")}</p>
        </div>
        <Link className="mj-btn mj-btn-pri" href={`/projects/${id}/vendors`}>업체 고르기</Link>
      </div>

      <section className={`mj-wrap ${s.views}`} aria-labelledby="pl-t">
        <h2 id="pl-t" className="mj-sr">공정표</h2>
        <PlanTabs
          bars={<><PlanBars plan={plan} /><PlanLegend plan={plan} /></>}
          list={<PlanDays plan={plan} />}
          extra={<Link className="mj-lnk" href={`/projects/${id}/standard`}>표준견적서 보기</Link>}
        />
      </section>

      <div className={`mj-wrap ${s.bottom}`}>
        <div className={s.left}>
          <p className="mj-small">
            토요일에는 작업하고 일요일에는 쉬어요. 공휴일에는 목공만 쉬어요.
          </p>
          <p className="mj-small">예상 일정이에요. 업체를 확정하고 업체가 공사 날짜를 올리면 확정 공정표로 바뀌어요.</p>
          {basis.mode === "reference" ? <>
            <p className="mj-small">참고 평면도: {String(basis.apartment_type)} · 예시 물량을 사용한 참고 일정이에요.</p>
            <p className="mj-small">{String(basis.notice)}</p>
            {Array.isArray(basis.assumptions) ? basis.assumptions.map((value, index) => {
              const assumption = value as { task?: string; name?: string; crew?: number; reason?: string };
              return <p className="mj-small" key={index}>{assumption.name ?? "작업"} 인원 가정값: {assumption.crew}명 · {assumption.reason}</p>;
            }) : null}
            {Array.isArray(basis.quantity_assumptions) ? basis.quantity_assumptions.map((value, index) => {
              const assumption = value as { name?: string; quantity?: string; unit?: string; reason?: string };
              return <p className="mj-small" key={index}>{assumption.name} 물량 가정값: {assumption.quantity}{assumption.unit === "ea" ? "개" : "㎡"} · {assumption.reason}</p>;
            }) : null}
            <p className="mj-small">{String(basis.scope_notice)}</p>
            <p className="mj-small">{basis.quantity_basis === "exclusive_scaled" ? "입력한 전용면적 비율로 참고 물량을 환산했어요." : "공급면적·평형을 전용면적으로 임의 환산하지 않고 선택한 예시 물량을 사용했어요."}</p>
          </> : null}
        </div>
        <div className={s.right}>
          <ExpectedPlanGenerate id={id} version={plan.version} start={plan.start} today={todaySeoul()} locked={dealt} mokgongItems={Array.isArray(basis.mokgong_items) ? basis.mokgong_items as string[] : p.trades.mokgong?.items} />
        </div>
      </div>
    </main>
  );
}
