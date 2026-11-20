// AI 칸(`/ai/*`) 브라우저 쪽. 묻기는 SSE(token · card · done), 나머지는 GET.
// 타입은 백엔드 app/chat/schemas.py와 같은 모양이에요. main.py ROUTERS에 AI router가 들어가고 `pnpm gen:api`를
// 다시 돌리면 schema.d.ts의 AiTurnOut 등으로 바꿔요(b5.md '다른 묶음에 부탁').
import { ApiError, call } from "@/lib/backend/client";

export type AiRoute = "guide" | "trade" | "quote_explanation" | "schedule" | "multi" | "out_of_scope" | "ask_back";
export type AiScreen =
  | "home" | "start" | "standard" | "standard_guest" | "vendors" | "vendor" | "visits" | "visit_request"
  | "pick" | "quotes" | "plan" | "schedule" | "thread";

export type AiChoice = { text: string; route?: AiRoute | null; trade?: string | null; slot?: string | null; value?: string | null; action?: string | null };
export type AiCalc = { label: string; ref: string; qty?: number | null; unit: string; unit_price?: number | null; amount?: number | null; in_total: boolean; note: string };
export type AiDraft = { wish_start?: string | null; wish_end?: string | null; vendors: string[]; formula: string; recommend?: { vendor: string; day: string; half?: "am" | "pm" | null; basis: string } | null };
export type AiCard = { kind: "calc" | "visit_draft" | "open_calendar" | "grade_preview" | "faq" | "choices" | "next_step"; title: string; text: string; choices: AiChoice[]; calc: AiCalc[]; draft?: AiDraft | null };
export type AiSource = { ref: string; title: string; kind: string };
export type AiSection = { title: string; sentences: string[]; caution: string; tip: string; source: "card" | "extract" | "llm" | "template" | "calc"; route: AiRoute; trade?: string | null; sources: AiSource[] };
export type AiAsk = { trade: string; slot: string; question: string; options: string[] };
export type AiAnswer = { notice: string; sections: AiSection[]; calc: AiCalc[]; cards: AiCard[]; ask?: AiAsk | null; unknown: string[]; suggest: string[]; failed: boolean };
export type AiTurnAnalysis = {
  classification: "button" | "waiting" | "rule" | "model";
  trades: string[];
  participants: string[];
  prompts: string[];
};
export type AiTurnOut = { chat_id: string | null; rev: number | null; mode: "free" | "llm"; route: AiRoute; answer: AiAnswer; analysis?: AiTurnAnalysis | null };
export type AiButton = { route: AiRoute; trade?: string | null; slot?: string | null; value?: string | null };
export type AiTurnIn = { client_key: string; text: string; chat_id?: string | null; context: { screen?: AiScreen | null; project_id?: string | null; ids?: string[] }; button?: AiButton | null };
export type AiFaq = { id: string; question: string; sentences: string[]; tip: string; sources: AiSource[]; actions: string[] };
export type AiThread = { chat_id: string; updated_at: string; preview: string };
export type AiThreadDetail = { chat_id: string; rev: number; messages: { role: "user" | "assistant"; text: string; turn: number }[] };

/** 같은 질문을 두 번 눌러도 한 번만 저장되게(서버가 같은 키면 처음 답을 돌려줘요) */
export function newClientKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID().replace(/-/g, "") : `${Date.now()}${Math.random().toString(36).slice(2)}`;
}

type Handlers = { onToken?: (text: string, section: number | null) => void; onCard?: (card: AiCard) => void };

/** POST /ai/turns. 오류(401 · 404 · 409 · 422 · 429)는 스트림을 열기 전에 ApiError로 와요. */
export async function askAi(body: AiTurnIn, on: Handlers = {}): Promise<AiTurnOut> {
  let res: Response;
  try {
    res = await fetch("/api/ai/turns", {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json", accept: "text/event-stream" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "network", "서버에 연결하지 못했어요. 잠시 뒤 다시 해 주세요.");
  }
  if (!res.ok || !res.body) {
    const data = await res.json().catch(() => null);
    const e = data?.error;
    throw new ApiError(res.status, e?.code ?? "unknown", e?.message ?? "답을 받지 못했어요. 다시 물어봐 주세요.", e?.fields);
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let done: AiTurnOut | null = null;
  for (;;) {
    const { value, done: end } = await reader.read();
    buf += dec.decode(value ?? new Uint8Array(), { stream: !end });
    let cut = buf.indexOf("\n\n");
    while (cut >= 0) {
      const block = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      const name = /^event: (.*)$/m.exec(block)?.[1];
      const raw = /^data: (.*)$/m.exec(block)?.[1];
      if (name && raw) {
        const data = JSON.parse(raw);
        if (name === "token") on.onToken?.(data.text, data.section);
        else if (name === "card") on.onCard?.(data as AiCard);
        else if (name === "done") done = data as AiTurnOut;
        else if (name === "error") throw new ApiError(500, data.code ?? "unknown", data.message ?? "답을 만들지 못했어요.");
      }
      cut = buf.indexOf("\n\n");
    }
    if (end) break;
  }
  if (!done) throw new ApiError(500, "unknown", "답을 끝까지 받지 못했어요. 다시 물어봐 주세요.");
  return done;
}

export const getAiFaq = () => call<AiFaq[]>("GET", "/ai/faq");
export const getAiThreads = (projectId?: string) =>
  call<AiThread[]>("GET", projectId ? `/ai/threads?project_id=${encodeURIComponent(projectId)}` : "/ai/threads");
export const getAiThread = (chatId: string) => call<AiThreadDetail>("GET", `/ai/threads/${encodeURIComponent(chatId)}`);
