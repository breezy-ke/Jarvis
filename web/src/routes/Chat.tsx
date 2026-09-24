import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { History, Plus } from "lucide-react";
import { useMemo, useRef, useState } from "react";

import { ChatView, type ChatLine } from "@/components/ChatView";
import { Orb } from "@/components/Orb";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { keys } from "@/lib/queries";
import type { Conversation, Message } from "@/lib/types";
import { useStream } from "@/lib/useStream";
import { cn, timeAgo } from "@/lib/utils";

const SUGGESTIONS = [
  "What can you do for me right now?",
  "Remember that I prefer Next.js for client landing pages.",
  "What's still missing from my profile?",
];

export function ChatPage() {
  const { c: conversationId } = useSearch({ from: "/app/chat" });
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [showHistory, setShowHistory] = useState(false);
  const [pending, setPending] = useState<ChatLine | null>(null);
  const sequence = useRef(0);
  const streamer = useStream();

  const conversations = useQuery({
    queryKey: keys.conversations,
    queryFn: () => api.get<Conversation[]>("/api/conversations"),
  });
  const messages = useQuery({
    queryKey: keys.messages(conversationId ?? "new"),
    queryFn: () => api.get<Message[]>(`/api/conversations/${conversationId}/messages`),
    enabled: Boolean(conversationId),
  });

  const lines: ChatLine[] = useMemo(() => {
    const base: ChatLine[] = (conversationId ? (messages.data ?? []) : []).map((m) => ({
      id: m.id,
      role: m.role,
      content: m.content,
      error: m.error,
    }));
    return pending ? [...base, pending] : base;
  }, [conversationId, messages.data, pending]);

  async function send(text: string) {
    sequence.current += 1;
    setPending({ id: `pending-${sequence.current}`, role: "user", content: text });
    let activeId = conversationId;
    await streamer.run(
      "/api/chat",
      { text, conversation_id: conversationId ?? null },
      {
        onStart: (data) => {
          const id = String(data.conversation_id);
          if (!conversationId) {
            activeId = id;
            void navigate({ to: "/chat", search: { c: id }, replace: true });
          }
        },
      },
    );
    await queryClient.invalidateQueries({ queryKey: keys.messages(activeId ?? "new") });
    await queryClient.invalidateQueries({ queryKey: keys.conversations });
    await queryClient.invalidateQueries({ queryKey: keys.status });
    setPending(null);
  }

  return (
    <div className="flex gap-6">
      <aside
        className={cn(
          "w-64 shrink-0 space-y-2",
          showHistory
            ? "fixed inset-0 z-40 w-full overflow-y-auto bg-background p-4 md:static md:w-64 md:p-0"
            : "hidden md:block",
        )}
        aria-label="Conversations"
      >
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-muted-foreground">Conversations</h2>
          <Button
            variant="ghost"
            size="sm"
            className="md:hidden"
            onClick={() => setShowHistory(false)}
          >
            Close
          </Button>
        </div>
        <Button asChild variant="outline" size="sm" className="w-full justify-start">
          <Link to="/chat" search={{}} onClick={() => setShowHistory(false)}>
            <Plus /> New conversation
          </Link>
        </Button>
        <ul className="space-y-1">
          {(conversations.data ?? []).map((conv) => (
            <li key={conv.id}>
              <Link
                to="/chat"
                search={{ c: conv.id }}
                onClick={() => setShowHistory(false)}
                className={cn(
                  "block rounded-md px-3 py-2 text-sm hover:bg-accent",
                  conv.id === conversationId && "bg-accent text-accent-foreground",
                )}
              >
                <span className="line-clamp-1">{conv.title}</span>
                <span className="text-xs text-muted-foreground">{timeAgo(conv.updated_at)}</span>
              </Link>
            </li>
          ))}
        </ul>
      </aside>

      <section className="min-w-0 flex-1">
        <div className="mb-4 flex items-center justify-between">
          <h1 className="text-xl font-semibold">Chat</h1>
          <Button
            variant="outline"
            size="sm"
            className="md:hidden"
            onClick={() => setShowHistory(true)}
          >
            <History /> History
          </Button>
        </div>
        <ChatView
          lines={lines}
          streaming={streamer.text}
          busy={streamer.busy}
          error={streamer.error}
          onSend={(text) => void send(text)}
          onStop={streamer.stop}
          empty={
            <div className="flex flex-col items-center gap-4 py-12 text-center">
              <Orb size={64} />
              <p className="text-muted-foreground">How can I help?</p>
              <div className="flex flex-wrap justify-center gap-2">
                {SUGGESTIONS.map((s) => (
                  <Button key={s} variant="outline" size="sm" onClick={() => void send(s)}>
                    {s}
                  </Button>
                ))}
              </div>
            </div>
          }
        />
      </section>
    </div>
  );
}
