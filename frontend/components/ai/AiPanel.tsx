// 옆 AI 칸 · 폰 전체 화면 AI (C09 · D01 · D03 · D06~D08 · D15~D19). 자리는 B4의 WithAside · AsidePanel이고, 내용은 여기(B5).
// - 사용자 · 프로젝트는 서버가 세션으로 정해요. 화면은 보던 화면(screen)과 프로젝트 id만 보내요.
// - 처음 열 때 AI 알림 한 번(AI 기본법 · 9판 C09). 닫은 것은 이 기기에만 기억해요(실패해도 다시 보여 줄 뿐).
// - 같은 공사 화면 사이를 옮겨도 layout이 이 조각을 그대로 두면 대화가 이어져요(F7 A).
"use client";

import { useEffect, useState } from "react";
import { AsidePanel } from "@/components/site/AsideSlot";
import { Button } from "@/components/ui/Button";
import { errorText } from "@/lib/backend/client";
import { askAi, newClientKey, type AiButton, type AiScreen, type AiTurnOut } from "@/lib/api/chat";
import { AiAnswer } from "./AiAnswer";
import s from "./ai.module.css";

const NOTICE = "AI가 답해요. 금액 · 날짜는 믿집 계산을 그대로 보여 주고, 저장 · 확정은 하지 않아요.";
const NOTICE_KEY = "mj-ai-notice-seen";
const MAX = 1000;
const CHIPS: Partial<Record<AiScreen, string[]>> = {
  standard: ["철거는 왜 합계에 안 들어가요?", "합지랑 실크는 뭐가 달라요?", "며칠 걸려요?"],
  vendors: ["가견적은 어떻게 나와요?", "업체 견적은 왜 달라요?"],
  vendor: ["가견적은 어떻게 나와요?", "실측은 돈이 들어요?"],
  visits: ["실측 날짜는 어떻게 정해요?", "업체가 언제 답해 줘요?"],
  visit_request: ["실측 일정 요청 초안 만들어 줘", "실측 날짜는 어떻게 정해요?"],
  quotes: ["실측 견적서 두 곳은 어떻게 비교해요?"],
  plan: ["공사 순서가 어떻게 돼요?"],
};
const PLACEHOLDER: Partial<Record<AiScreen, string>> = {
  standard: "이 견적서에 대해 물어보세요", visits: "일정에 대해 물어보세요", visit_request: "일정에 대해 물어보세요",
};

type Msg = { role: "user"; text: string } | { role: "ai"; out: AiTurnOut };

function seenNotice(): boolean {
  try {
    return window.localStorage.getItem(NOTICE_KEY) === "1";
  } catch {
    return false;
  }
}

export function AiPanel({ screen, projectId, ids, embed = false }: {
  screen: AiScreen;
  projectId?: string;
  /** 보던 것(실측 · 업체 id 등, 5개까지). 서버가 본인 것인지 다시 봐요 */
  ids?: string[];
  /** 예전 '보는 중' 줄 글. 11-19부터 보이지 않아요(부르는 쪽 호환으로만 받아요) */
  context?: string;
  /** 다른 옆 칸 안에 끼울 때(예: 확정 공정표 '공사 전 할 일' 칸 아래). 칸 틀(AsidePanel) 없이 대화 · 입력만 그려요 */
  embed?: boolean;
}) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [chatId, setChatId] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState(false); // 서버 그림과 같게 시작하고, 열린 뒤 이 기기 기록을 봐요
  useEffect(() => {
    if (!seenNotice()) setNotice(true);
  }, []);

  function closeNotice() {
    setNotice(false);
    try {
      window.localStorage.setItem(NOTICE_KEY, "1");
    } catch {
      /* 저장이 막혀도 다음에 한 번 더 보일 뿐이에요 */
    }
  }

  async function send(q: string, button?: AiButton) {
    const t = q.trim().slice(0, MAX);
    if (!t || busy) return;
    setBusy(true);
    setError("");
    setMsgs((m) => [...m, { role: "user", text: t }]);
    setText("");
    try {
      const out = await askAi({
        client_key: newClientKey(), text: t, chat_id: chatId,
        context: { screen, project_id: projectId ?? null, ids: ids?.slice(0, 5) ?? [] }, button: button ?? null,
      });
      if (out.chat_id) setChatId(out.chat_id);
      setMsgs((m) => [...m, { role: "ai", out }]);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const chips = msgs.length ? [] : CHIPS[screen] ?? ["처음이라 순서를 알고 싶어요", "표준견적서는 업체 견적이에요?"];
  const foot = (
    <>
      {chips.length ? (
        <div className={s.choices}>
          {chips.map((c) => <Button key={c} variant="weak" size="sm" onClick={() => send(c)}>{c}</Button>)}
        </div>
      ) : null}
      {error ? <p className={s.error} role="alert">{error}</p> : null}
      <form className={s.composer} onSubmit={(e) => { e.preventDefault(); void send(text); }}>
        <label className="mj-sr" htmlFor="ai-input">AI에게 물을 말</label>
        <textarea
          id="ai-input" className={s.input} rows={1} maxLength={MAX} value={text}
          placeholder={PLACEHOLDER[screen] ?? "궁금한 걸 물어보세요"} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(text); } }}
        />
        <Button type="submit" busy={busy} disabled={!text.trim()}>{busy ? "답 기다리는 중" : "보내기"}</Button>
      </form>
    </>
  );

  const body = (
      <div className={s.msgs} aria-live="polite">
        {notice ? (
          <div className={s.notice1}>
            <p>{NOTICE}</p>
            <div><Button variant="secondary" size="sm" onClick={closeNotice}>알겠어요</Button></div>
          </div>
        ) : null}
        {msgs.map((m, i) =>
          m.role === "user" ? (
            <p key={i} className={s.user}>{m.text}</p>
          ) : (
            <AiAnswer key={i} answer={m.out.answer} turn={m.out} projectId={projectId} onPick={(q, b) => void send(q, b)} />
          ),
        )}
      </div>
  );
  if (embed) {
    return (
      <div className={s.embed}>
        {body}
        {foot}
      </div>
    );
  }
  return (
    <AsidePanel title="AI 채팅" listLabel="AI 대화" foot={foot}>
      {body}
    </AsidePanel>
  );
}
