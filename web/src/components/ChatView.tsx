import { ArrowUp, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Markdown } from "@/components/Markdown";
import { Orb } from "@/components/Orb";
import { Button } from "@/components/ui/button";
import { Alert } from "@/components/ui/misc";
import { cn } from "@/lib/utils";

export interface ChatLine {
  id: string;
  role: "user" | "assistant";
  content: string;
  error?: boolean;
}

interface Props {
  lines: ChatLine[];
  streaming: string | null;
  busy: boolean;
  error: string | null;
  onSend: (text: string) => void;
  onStop?: () => void;
  placeholder?: string;
  empty?: React.ReactNode;
}

export function ChatView({
  lines,
  streaming,
  busy,
  error,
  onSend,
  onStop,
  placeholder,
  empty,
}: Props) {
  const [draft, setDraft] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [lines.length, streaming]);

  function submit() {
    const text = draft.trim();
    if (!text || busy) return;
    onSend(text);
    setDraft("");
    inputRef.current?.focus();
  }

  return (
    <div className="flex min-h-[calc(100dvh-15rem)] flex-col md:min-h-[calc(100dvh-10rem)]">
      <div className="flex-1 space-y-4" role="log" aria-live="polite" aria-relevant="additions">
        {lines.length === 0 && streaming === null ? empty : null}
        {lines.map((line) => (
          <Bubble key={line.id} role={line.role} error={line.error}>
            {line.content}
          </Bubble>
        ))}
        {streaming !== null ? (
          <Bubble role="assistant" thinking={streaming.length === 0}>
            {streaming}
          </Bubble>
        ) : null}
        {error ? <Alert tone="error">{error}</Alert> : null}
        <div ref={endRef} />
      </div>
      <form
        className="sticky bottom-20 mt-4 flex items-end gap-2 rounded-lg border bg-card p-2 shadow-sm md:bottom-4"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <label htmlFor="composer" className="sr-only">
          Message
        </label>
        <textarea
          id="composer"
          ref={inputRef}
          rows={1}
          value={draft}
          placeholder={placeholder ?? "Message Jarvis…"}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
          className="max-h-40 min-h-10 flex-1 resize-none bg-transparent px-2 py-2 text-sm outline-none placeholder:text-muted-foreground"
        />
        {busy && onStop ? (
          <Button type="button" size="icon" variant="secondary" onClick={onStop} aria-label="Stop">
            <Square />
          </Button>
        ) : (
          <Button type="submit" size="icon" disabled={!draft.trim() || busy} aria-label="Send">
            <ArrowUp />
          </Button>
        )}
      </form>
    </div>
  );
}

function Bubble({
  role,
  children,
  error,
  thinking,
}: {
  role: "user" | "assistant";
  children: string;
  error?: boolean;
  thinking?: boolean;
}) {
  if (role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-lg rounded-br-sm bg-primary px-4 py-2 text-sm text-primary-foreground">
          {children}
        </div>
      </div>
    );
  }
  return (
    <div className="flex gap-3">
      <Orb size={24} thinking={thinking} className="mt-1" />
      <div
        className={cn(
          "min-w-0 max-w-[85%] rounded-lg rounded-tl-sm border bg-card px-4 py-2 text-sm",
          error && "border-destructive/40 text-destructive",
        )}
      >
        {thinking ? (
          <span className="text-muted-foreground">Thinking…</span>
        ) : (
          <Markdown>{children}</Markdown>
        )}
      </div>
    </div>
  );
}
