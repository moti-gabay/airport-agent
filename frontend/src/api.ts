export type ToolCall = {
  name: string;
  input: Record<string, unknown>;
  ok: boolean;
  summary: string;
};

export type ChatResponse = {
  session_id: string;
  answer: string;
  trace: ToolCall[];
  rounds: number;
  stop_reason: string | null;
};

const BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export async function sendMessage(
  message: string,
  sessionId: string | null,
): Promise<ChatResponse> {
  const res = await fetch(`${BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `Request failed (${res.status})`);
  }
  return res.json();
}

export async function resetSession(sessionId: string): Promise<void> {
  await fetch(`${BASE}/api/session/${sessionId}`, { method: "DELETE" });
}
