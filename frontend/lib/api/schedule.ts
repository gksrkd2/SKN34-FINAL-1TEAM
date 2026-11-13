// 서버 쪽 읽기: 공정표 · 업체 확정 · 공사 날짜 · 한 공사 화면 · 서류.
import { serverGet, type Schemas } from "@/lib/backend/server";
import { isUuid } from "./project";

export type ApiPlan = Schemas["PlanOut"];
export async function getExpectedStatus(projectId: string) {
  return isUuid(projectId) ? serverGet<Schemas["ExpectedStatus"]>(`/projects/${projectId}/expected-plan`) : null;
}
/** 공정표(믿집 계산). 남의 것이면 null. kind를 비우면 수락한 날짜가 있을 때 확정 공정표, 없으면 예상 공정표 */
export async function getPlan(projectId: string, kind?: "expected" | "final") {
  return isUuid(projectId) ? serverGet<ApiPlan>(`/projects/${projectId}/plan${kind ? `?kind=${kind}` : ""}`) : null;
}

export type ApiDeal = Schemas["DealOut"];
/** 이 집 공정의 업체 확정(다시 고른 것은 빠짐). 남의 공사면 null */
export async function listDeals(projectId: string, trade?: "dobae" | "mokgong") {
  return isUuid(projectId) ? serverGet<ApiDeal[]>(`/projects/${projectId}/deals${trade ? `?trade=${trade}` : ""}`) : null;
}

export type ApiSchedule = Schemas["ScheduleOut"];
/** 고객: 공정별 공사 날짜(업체가 낸 날짜 · 다른 날짜 요청 · 남은 요청 수 · 기한). 남의 공사면 null */
export async function getSchedule(projectId: string) {
  return isUuid(projectId) ? serverGet<ApiSchedule>(`/projects/${projectId}/schedule`) : null;
}

export type ApiJob = Schemas["JobOut"];
/** 업체: 공사 날짜 내기 화면 값(내 확정만, 아니면 null) */
export async function getVendorJob(dealId: string) {
  return isUuid(dealId) ? serverGet<ApiJob>(`/vendor/jobs/${dealId}`) : null;
}

export type ApiOverview = Schemas["OverviewOut"];
/** 고객: 한 공사 화면(지금 할 일 · 공정별 단계 · 낼 금액 · 공사 기간 · 연락처 · 체크). 남의 공사면 null */
export async function getOverview(projectId: string) {
  return isUuid(projectId) ? serverGet<ApiOverview>(`/projects/${projectId}/overview`) : null;
}

export type ApiDocs = Schemas["DocsOut"];
/** 고객: 서류 모음(표준견적서 · 공정표 버전 · 실측 견적서 · 추가 금액). 남의 공사면 null */
export async function getDocs(projectId: string) {
  return isUuid(projectId) ? serverGet<ApiDocs>(`/projects/${projectId}/docs`) : null;
}
