import { useEffect, useRef, useState } from "react";
import { resetSession, sendMessage } from "../api";
import { Message, type ChatMessage } from "./Message";

const EXAMPLES = [
  "Which airports in New England are strong candidates for terminal expansion?",
  "Compare LA and Santa Ana airport congestion levels.",
  "What is the percentage of long haul flights out of Anchorage airport?",
  "What is the unmet flight demand in SFO airport and why?",
];

export function Chat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  async function ask(question: string) {
    if (!question.trim() || busy) return;
    setInput("");
    setMessages((m) => [...m, { role: "user", text: question }]);
    setBusy(true);
    try {
      const res = await sendMessage(question, sessionId);
      setSessionId(res.session_id);
      setMessages((m) => [
        ...m,
        { role: "assistant", text: res.answer, trace: res.trace },
      ]);
    } catch (e) {
      setMessages((m) => [
        ...m,
        { role: "assistant", text: (e as Error).message, error: true },
      ]);
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    if (sessionId) await resetSession(sessionId);
    setSessionId(null);
    setMessages([]);
  }

  return (
    <div className="chat">
      <header>
        <div>
          <h1>Airport Investment Intelligence</h1>
          <p>
            US airport modernization candidates, scored from BTS T-100 and
            On-Time Performance data.
          </p>
        </div>
        {messages.length > 0 && (
          <button className="ghost" onClick={clear} disabled={busy}>
            New conversation
          </button>
        )}
      </header>

      <div className="messages">
        {messages.length === 0 && (
          <div className="empty">
            <p>Ask about candidates, congestion, long-haul mix or unmet demand.</p>
            <ul>
              {EXAMPLES.map((q) => (
                <li key={q}>
                  <button onClick={() => ask(q)}>{q}</button>
                </li>
              ))}
            </ul>
          </div>
        )}
        {messages.map((m, i) => (
          <Message key={i} message={m} />
        ))}
        {busy && (
          <div className="msg msg-assistant">
            <div className="bubble thinking">
              Querying data and composing the answer…
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          ask(input);
        }}
      >
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about US airports…"
          disabled={busy}
        />
        <button type="submit" disabled={busy || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}
