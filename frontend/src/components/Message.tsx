import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ToolCall } from "../api";
import { ToolTrace } from "./ToolTrace";

export type ChatMessage = {
  role: "user" | "assistant";
  text: string;
  trace?: ToolCall[];
  error?: boolean;
};

export function Message({ message }: { message: ChatMessage }) {
  if (message.role === "user") {
    return (
      <div className="msg msg-user">
        <div className="bubble">{message.text}</div>
      </div>
    );
  }
  return (
    <div className="msg msg-assistant">
      {message.trace && <ToolTrace trace={message.trace} />}
      <div className={message.error ? "bubble bubble-error" : "bubble"}>
        <Markdown remarkPlugins={[remarkGfm]}>{message.text}</Markdown>
      </div>
    </div>
  );
}
