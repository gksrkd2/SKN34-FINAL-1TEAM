// 도배 업체 공개 상세: 프로젝트·공사 지역과 독립적인 등록 정보. 견적/담기는 기존 공사 화면에서만 처리한다.
import { notFound } from "next/navigation";
import { AiPanel } from "@/components/ai/AiPanel";
import { AppHeader } from "@/components/site/AppHeader";
import { Main, PageHead, WithAside } from "@/components/site/Frame";
import { Button } from "@/components/ui/Button";
import { SummaryRows } from "@/components/ui/Summary";
import { serverGet } from "@/lib/backend/server";
import s from "@/components/vendors/vendors3.module.css";

type PublicVendor = {
  vendor_id: string;
  biz_name: string | null;
  intro: string | null;
  trades: string[];
  areas: { label: string; sgg: string }[];
  verified_at: string | null;
  visit_fee: number | null;
  visit_fee_refund: boolean;
  opened_on: string | null;
  interior_license: string | null;
  insurance: string | null;
  faqs: { key: string; answer: string }[];
  portfolio: { id: string; title: string }[];
};

const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const TRADE: Record<string, string> = { dobae: "도배", mokgong: "목공" };

export const metadata = { title: "도배 업체 상세" };

export default async function PublicVendorPage({ params, searchParams }: {
  params: Promise<{ vid: string }>;
  searchParams: Promise<{ project?: string }>;
}) {
  const { vid } = await params;
  if (!UUID.test(vid)) notFound();
  const profile = await serverGet<PublicVendor>(`/vendors/${encodeURIComponent(vid)}`);
  if (!profile) notFound();

  const name = profile.biz_name ?? "도배 업체";
  const { project } = await searchParams;
  const hasProject = !!project && UUID.test(project);
  const back = hasProject ? `/projects/${project}/vendors` : "/my";
  const details = [
    { key: "trades", label: "시공 공종", value: profile.trades.map((t) => TRADE[t] ?? t).join(" · ") },
    { key: "areas", label: "업체 등록 활동 지역", value: profile.areas.map((a) => a.label || a.sgg).join(" · ") },
    { key: "verified", label: "사업자 승인", value: profile.verified_at ? "확인됨" : "확인 안 됨" },
    ...(profile.visit_fee != null ? [{ key: "fee", label: "등록된 실측 방문비", value: profile.visit_fee ? `${profile.visit_fee.toLocaleString("ko-KR")}원` : "무료" }] : []),
    ...(profile.interior_license ? [{ key: "license", label: "실내건축공사업 등록(업체 기재)", value: profile.interior_license }] : []),
    ...(profile.insurance ? [{ key: "insurance", label: "보험(업체 기재)", value: profile.insurance }] : []),
  ];
  return (
    <>
      <AppHeader only="phone" phone={{ title: name, back }} />
      <Main>
        <PageHead title={name} crumbs={[{ label: "내 공사", href: "/my" }, { label: "도배 업체 상세" }]} />
        <WithAside aside={<AiPanel screen="vendor" ids={[vid]} context={`${name} 업체 상세`} />}>
          <section className={s.group} aria-label="업체 소개">
            <h2>업체 소개</h2>
            <p>{profile.intro || "등록된 업체 소개가 없어요."}</p>
          </section>
          <section className={s.group} aria-label="업체 공개 정보">
            <h2>공개 정보</h2>
            <SummaryRows size="sm" items={details} />
          </section>
          {profile.faqs.length ? (
            <section className={s.group} aria-label="업체 FAQ">
              <h2>업체가 등록한 답변</h2>
              {profile.faqs.map((faq) => <p key={faq.key}>{faq.answer}</p>)}
            </section>
          ) : null}
          <Button variant="secondary" href={back}>{hasProject ? "업체 선택 화면으로 돌아가기" : "내 공사로 돌아가기"}</Button>
        </WithAside>
      </Main>
    </>
  );
}
