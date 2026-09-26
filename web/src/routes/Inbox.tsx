import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import {
  AlertTriangle,
  ArrowLeft,
  CalendarPlus,
  Check,
  Inbox as InboxIcon,
  Paperclip,
  RefreshCw,
  Send,
  ShieldAlert,
  Sparkles,
  Trash2,
  Undo2,
} from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input, Label, Textarea } from "@/components/ui/input";
import { Alert, EmptyState, Skeleton } from "@/components/ui/misc";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { keys, useMailStatus } from "@/lib/queries";
import type {
  DraftFields,
  InboxTab,
  MailCategory,
  MailDate,
  MailDraft,
  MailSignal,
  ThreadItem,
  ThreadView,
} from "@/lib/types";
import { CATEGORY_LABELS, CATEGORY_VARIANT, INBOX_TABS, splitAddresses } from "@/lib/mail";
import { useCountdown } from "@/lib/useCountdown";
import { cn, timeAgo } from "@/lib/utils";

function CategoryBadge({ category }: { category: MailCategory | null }) {
  if (!category) return <Badge variant="outline">not sorted yet</Badge>;
  return <Badge variant={CATEGORY_VARIANT[category]}>{CATEGORY_LABELS[category]}</Badge>;
}

function Signals({ signals }: { signals: MailSignal[] }) {
  if (!signals.length) return null;
  return (
    <ul className="flex flex-wrap gap-1" aria-label="What Jarvis checked">
      {signals.map((signal) => (
        <li key={signal.id}>
          <Badge variant={signal.hard ? "destructive" : "outline"} className="font-normal">
            {signal.hard ? <ShieldAlert className="size-3" /> : null}
            {signal.label}
          </Badge>
        </li>
      ))}
    </ul>
  );
}

// --- The list ---------------------------------------------------------------------------------

export function InboxPage() {
  const { tab = "attention" } = useSearch({ from: "/app/inbox" });
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { data: status, isLoading } = useMailStatus();
  const threads = useQuery({
    queryKey: keys.threads(tab),
    queryFn: () => api.get<ThreadItem[]>(`/api/mail/threads?tab=${tab}`),
    enabled: status?.access !== undefined && status.access !== "none",
    refetchInterval: 30_000,
  });
  const checkNow = useMutation({
    mutationFn: () => api.post("/api/mail/sync"),
    onSuccess: () => {
      window.setTimeout(() => void queryClient.invalidateQueries({ queryKey: keys.mail }), 3_000);
    },
  });

  if (isLoading || !status) return <Skeleton className="h-60" />;
  if (status.access === "none") {
    return (
      <div className="space-y-4">
        <h1 className="text-xl font-semibold">Inbox</h1>
        <EmptyState icon={<InboxIcon className="size-8" />} title="Your inbox isn't connected">
          Connect Google in <Link to="/sources">Sources</Link> and I'll sort your mail, summarise it
          and draft replies for you to approve.
        </EmptyState>
      </div>
    );
  }
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Inbox</h1>
          <p className="text-sm text-muted-foreground">
            {status.account} · checked {timeAgo(status.sync.last_sync_at)}. Replies go out only
            after you approve them.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => checkNow.mutate()}
          disabled={checkNow.isPending}
        >
          <RefreshCw /> Check now
        </Button>
      </div>
      {status.sync.status === "reconnect" ? (
        <Alert tone="warning" title="Google needs reconnecting">
          {status.sync.error} <Link to="/sources">Reconnect in Sources</Link>.
        </Alert>
      ) : status.sync.status === "error" ? (
        <Alert tone="warning">Couldn't reach Gmail last time; I'll keep trying.</Alert>
      ) : null}
      {status.access === "read" ? (
        <Alert tone="info">
          I can read and sort your mail, but not draft in Gmail or send.{" "}
          <Link to="/sources">Give me your inbox</Link> to change that.
        </Alert>
      ) : null}
      <Tabs
        value={tab}
        onValueChange={(value) =>
          void navigate({ to: "/inbox", search: { tab: value as InboxTab } })
        }
      >
        <TabsList className="h-auto flex-wrap justify-start">
          {INBOX_TABS.map((item) => (
            <TabsTrigger key={item.id} value={item.id}>
              {item.label}
              {status.counts[item.id] ? (
                <span className="ml-1.5 text-xs text-muted-foreground">
                  {status.counts[item.id]}
                </span>
              ) : null}
            </TabsTrigger>
          ))}
        </TabsList>
        {INBOX_TABS.map((item) => (
          <TabsContent key={item.id} value={item.id} className="space-y-2">
            {item.id !== tab ? null : threads.isLoading ? (
              <Skeleton className="h-40" />
            ) : threads.error ? (
              <Alert tone="error">{threads.error.message}</Alert>
            ) : !threads.data?.length ? (
              <EmptyState icon={<InboxIcon className="size-8" />} title="Nothing here">
                {tab === "attention" ? "Nothing needs you right now." : null}
              </EmptyState>
            ) : (
              <ul className="space-y-2" aria-label="Conversations">
                {threads.data.map((thread) => (
                  <li key={thread.id}>
                    <ThreadRow thread={thread} />
                  </li>
                ))}
              </ul>
            )}
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}

function ThreadRow({ thread }: { thread: ThreadItem }) {
  return (
    <Link
      to="/inbox/$threadId"
      params={{ threadId: thread.id }}
      className="block rounded-lg border bg-card p-3 transition-colors hover:bg-accent"
    >
      <div className="flex items-center gap-2">
        {thread.unread ? (
          <span className="size-2 shrink-0 rounded-full bg-primary" aria-label="Unread" />
        ) : null}
        <span className={cn("truncate", thread.unread && "font-semibold")}>{thread.sender}</span>
        <span className="ml-auto shrink-0 text-xs text-muted-foreground">
          {timeAgo(thread.last_message_at)}
        </span>
      </div>
      <p className={cn("truncate text-sm", thread.unread && "font-medium")}>{thread.subject}</p>
      {thread.summary ? (
        <p className="line-clamp-2 text-sm text-muted-foreground">{thread.summary}</p>
      ) : null}
      <div className="mt-2 flex flex-wrap items-center gap-1">
        <CategoryBadge category={thread.category} />
        {thread.priority && thread.priority >= 4 ? (
          <Badge variant="outline">priority {thread.priority}</Badge>
        ) : null}
        <Signals signals={thread.signals.filter((s) => s.hard || s.id === "first_time_sender")} />
      </div>
    </Link>
  );
}

// --- One conversation -------------------------------------------------------------------------

export function ThreadPage() {
  const { threadId } = useParams({ from: "/app/inbox/$threadId" });
  const { data: status } = useMailStatus();
  const query = useQuery({
    queryKey: keys.thread(threadId),
    queryFn: () => api.get<ThreadView>(`/api/mail/threads/${threadId}`),
    // While a reply is on its way out, keep an eye on it.
    refetchInterval: (q) =>
      ["approved", "executing"].includes(q.state.data?.draft?.proposal?.status ?? "")
        ? 2_000
        : false,
  });
  if (query.isLoading) return <Skeleton className="h-80" />;
  if (query.error || !query.data) {
    return <Alert tone="error">{query.error?.message ?? "Not found."}</Alert>;
  }
  const thread = query.data;
  return (
    <div className="space-y-4">
      <Link to="/inbox" className="inline-flex items-center gap-1 text-sm text-muted-foreground">
        <ArrowLeft className="size-4" /> Inbox
      </Link>
      <div className="space-y-2">
        <h1 className="break-words text-xl font-semibold">{thread.subject}</h1>
        <div className="flex flex-wrap items-center gap-2">
          <CategoryBadge category={thread.category} />
          {thread.priority ? <Badge variant="outline">priority {thread.priority}</Badge> : null}
          <Signals signals={thread.signals} />
        </div>
      </div>
      {thread.signals.some((s) => s.hard) ? (
        <Alert tone="warning" title="Be careful with this one">
          It failed checks that emails from real senders pass. Don't click links or send anything it
          asks for until you've confirmed it another way.
        </Alert>
      ) : null}
      <SummaryCard thread={thread} canHold={status?.can_add_holds ?? false} />
      <section aria-label="Messages" className="space-y-3">
        {thread.messages.map((message, index) => {
          const recent = index >= thread.messages.length - 3;
          const header = (
            <>
              <span className="font-medium">
                {message.direction === "out" ? "You" : message.sender}
              </span>{" "}
              <span className="text-muted-foreground">
                {message.direction === "out" ? "" : `<${message.sender_address}>`} ·{" "}
                {new Date(message.date).toLocaleString()}
              </span>
            </>
          );
          const content = (
            <>
              <p className="text-xs text-muted-foreground">To {message.to.join(", ")}</p>
              <div className="mt-2 whitespace-pre-wrap break-words text-sm">{message.body}</div>
              {message.attachments.length ? (
                <p className="mt-2 flex items-center gap-1 text-xs text-muted-foreground">
                  <Paperclip className="size-3" /> {message.attachments.join(", ")} (open in Gmail)
                </p>
              ) : null}
            </>
          );
          return (
            <Card key={message.id}>
              <CardContent className="pt-4 text-sm">
                {recent ? (
                  <>
                    <p>{header}</p>
                    {content}
                  </>
                ) : (
                  <details>
                    <summary className="cursor-pointer">{header}</summary>
                    {content}
                  </details>
                )}
              </CardContent>
            </Card>
          );
        })}
      </section>
      <ReplyCard thread={thread} canSend={status?.access === "full"} />
    </div>
  );
}

function SummaryCard({ thread, canHold }: { thread: ThreadView; canHold: boolean }) {
  const queryClient = useQueryClient();
  const [category, setCategory] = useState<MailCategory | "">(thread.category ?? "");
  const [held, setHeld] = useState<Record<string, string>>({});
  const verdict = useMutation({
    mutationFn: (value: MailCategory) =>
      api.post<ThreadView>(`/api/mail/threads/${thread.id}/category`, { category: value }),
    onSuccess: (view) => {
      queryClient.setQueryData(keys.thread(thread.id), view);
      void queryClient.invalidateQueries({ queryKey: ["mail", "threads"] });
      void queryClient.invalidateQueries({ queryKey: keys.mailStatus });
    },
  });
  const hold = useMutation({
    mutationFn: (date: MailDate) =>
      api.post("/api/mail/holds", {
        title: date.what,
        start: date.start,
        end: date.end,
        all_day: date.all_day,
        thread_id: thread.id,
      }),
    onSuccess: (_, date) => setHeld((h) => ({ ...h, [date.start]: "Held in your calendar." })),
    onError: (error: Error, date) => setHeld((h) => ({ ...h, [date.start]: error.message })),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Sparkles className="size-4" /> Jarvis's summary
        </CardTitle>
        <CardDescription>{thread.summary || "Not summarised yet."}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {thread.tasks.length ? (
          <div>
            <p className="font-medium">To do</p>
            <ul className="list-inside list-disc text-muted-foreground">
              {thread.tasks.map((task) => (
                <li key={task}>{task}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {thread.dates.length ? (
          <ul className="space-y-2" aria-label="Dates in this email">
            {thread.dates.map((date) => (
              <li key={date.start} className="flex flex-wrap items-center gap-2">
                <span>
                  <span className="font-medium">{date.what}</span>:{" "}
                  {date.all_day
                    ? new Date(date.start).toLocaleDateString()
                    : new Date(date.start).toLocaleString()}
                </span>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => hold.mutate(date)}
                  disabled={!canHold || hold.isPending || Boolean(held[date.start])}
                  title={canHold ? undefined : "Reconnect Google and allow calendar events first"}
                >
                  <CalendarPlus /> Add to calendar
                </Button>
                {held[date.start] ? (
                  <span className="text-xs text-muted-foreground">{held[date.start]}</span>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
        <form
          className="flex flex-wrap items-end gap-2 border-t pt-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (category) verdict.mutate(category);
          }}
        >
          <div className="space-y-1">
            <Label htmlFor="category">Is this right? Sorted as</Label>
            <select
              id="category"
              className="h-9 rounded-md border bg-background px-2 text-sm"
              value={category}
              onChange={(event) => setCategory(event.target.value as MailCategory)}
            >
              <option value="" disabled>
                Choose…
              </option>
              {(Object.keys(CATEGORY_LABELS) as MailCategory[]).map((value) => (
                <option key={value} value={value}>
                  {CATEGORY_LABELS[value]}
                </option>
              ))}
            </select>
          </div>
          <Button
            type="submit"
            size="sm"
            variant="outline"
            disabled={!category || verdict.isPending}
          >
            <Check /> {category === thread.category ? "Yes, that's right" : "Correct it"}
          </Button>
          {thread.checked ? (
            <span className="text-xs text-muted-foreground">
              Thanks: noted for accuracy checks.
            </span>
          ) : null}
          {verdict.error ? <Alert tone="error">{verdict.error.message}</Alert> : null}
        </form>
      </CardContent>
    </Card>
  );
}

// --- Replying -----------------------------------------------------------------------------------

function ReplyCard({ thread, canSend }: { thread: ThreadView; canSend: boolean }) {
  const queryClient = useQueryClient();
  const [instructions, setInstructions] = useState("");
  const [replyAll, setReplyAll] = useState(false);
  const [sent, setSent] = useState(false);
  const refresh = (view?: MailDraft) => {
    if (view) {
      queryClient.setQueryData<ThreadView>(keys.thread(thread.id), (old) =>
        old ? { ...old, draft: view.status === "pending" ? view : null } : old,
      );
    }
    void queryClient.invalidateQueries({ queryKey: keys.thread(thread.id) });
    void queryClient.invalidateQueries({ queryKey: ["actions"] });
    void queryClient.invalidateQueries({ queryKey: keys.status });
  };
  const draft = useMutation({
    mutationFn: (write: boolean) =>
      api.post<MailDraft>(`/api/mail/threads/${thread.id}/draft`, {
        instructions: instructions.trim() || null,
        reply_all: replyAll,
        write,
      }),
    onSuccess: (view) => {
      setSent(false);
      refresh(view);
    },
  });

  if (!thread.draft) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Reply</CardTitle>
          <CardDescription>
            I draft it in your voice and address it from the conversation. Nothing is sent until you
            tap Send, and you get 60 seconds to undo.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {sent ? (
            <Alert tone="success">Sent. It will show above after the next check.</Alert>
          ) : null}
          <div className="space-y-1">
            <Label htmlFor="instructions">What should it say? (optional)</Label>
            <Input
              id="instructions"
              value={instructions}
              maxLength={1000}
              placeholder="e.g. say Tuesday at 10 works"
              onChange={(event) => setInstructions(event.target.value)}
            />
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={replyAll}
              onChange={(event) => setReplyAll(event.target.checked)}
            />
            Reply to everyone on the email
          </label>
          {draft.error ? <Alert tone="error">{draft.error.message}</Alert> : null}
        </CardContent>
        <CardFooter className="flex-wrap">
          <Button onClick={() => draft.mutate(true)} disabled={draft.isPending}>
            <Sparkles /> {draft.isPending ? "Drafting…" : "Draft a reply"}
          </Button>
          <Button variant="outline" onClick={() => draft.mutate(false)} disabled={draft.isPending}>
            Write it myself
          </Button>
        </CardFooter>
      </Card>
    );
  }
  return (
    <DraftEditor
      key={thread.draft.proposal?.id ?? thread.draft.id}
      draft={thread.draft}
      canSend={canSend}
      onChange={refresh}
      onSending={setSent}
      onRedraft={() => draft.mutate(true)}
      redrafting={draft.isPending}
      instructions={instructions}
      setInstructions={setInstructions}
    />
  );
}

function DraftEditor({
  draft,
  canSend,
  onChange,
  onSending,
  onRedraft,
  redrafting,
  instructions,
  setInstructions,
}: {
  draft: MailDraft;
  canSend: boolean;
  onChange: (view?: MailDraft) => void;
  onSending: (sending: boolean) => void;
  onRedraft: () => void;
  redrafting: boolean;
  instructions: string;
  setInstructions: (value: string) => void;
}) {
  const initial = draft.fields;
  const [to, setTo] = useState(initial.to.join(", "));
  const [cc, setCc] = useState(initial.cc.join(", "));
  const [subject, setSubject] = useState(initial.subject);
  const [body, setBody] = useState(initial.body);
  const proposal = draft.proposal;
  const secondsLeft = useCountdown(proposal?.status === "approved" ? proposal.execute_after : null);
  const fields: Partial<DraftFields> = {
    to: splitAddresses(to),
    cc: splitAddresses(cc),
    subject,
    body,
  };
  const dirty =
    fields.to?.join(",") !== initial.to.join(",") ||
    fields.cc?.join(",") !== initial.cc.join(",") ||
    subject !== initial.subject ||
    body.trim() !== initial.body.trim();

  const save = useMutation({
    mutationFn: () => api.patch<MailDraft>(`/api/mail/drafts/${draft.id}`, fields),
    onSuccess: onChange,
  });
  const send = useMutation({
    mutationFn: () =>
      api.post<MailDraft>(`/api/mail/drafts/${draft.id}/send`, {
        payload_hash: proposal?.payload_hash,
      }),
    onSuccess: (view) => {
      onSending(true);
      onChange(view);
    },
  });
  const undo = useMutation({
    mutationFn: () => api.post<MailDraft>(`/api/mail/drafts/${draft.id}/undo`),
    onSuccess: (view) => {
      onSending(false);
      onChange(view);
    },
  });
  const discard = useMutation({
    mutationFn: () => api.del(`/api/mail/drafts/${draft.id}`),
    onSuccess: () => onChange(),
  });
  const error = save.error ?? send.error ?? undo.error ?? discard.error;
  const sending = proposal?.status === "approved" && secondsLeft > 0;
  const locked = proposal?.status === "approved" || proposal?.status === "executing";
  const warnings = (proposal?.validation ?? []).filter((c) => c.outcome !== "pass");

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Your reply</CardTitle>
        <CardDescription>
          {draft.origin === "auto"
            ? "Drafted by Jarvis because this looked like it needed you. Edit anything."
            : "Edit anything, then send."}
          {draft.in_gmail ? " A copy is in your Gmail drafts." : ""}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <Label htmlFor="to">To</Label>
            <Input id="to" value={to} disabled={locked} onChange={(e) => setTo(e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label htmlFor="cc">Cc</Label>
            <Input id="cc" value={cc} disabled={locked} onChange={(e) => setCc(e.target.value)} />
          </div>
        </div>
        <div className="space-y-1">
          <Label htmlFor="subject">Subject</Label>
          <Input
            id="subject"
            value={subject}
            disabled={locked}
            onChange={(e) => setSubject(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="body">Message</Label>
          <Textarea
            id="body"
            rows={10}
            value={body}
            disabled={locked}
            onChange={(e) => setBody(e.target.value)}
          />
        </div>
        {warnings.length ? (
          <ul className="space-y-1 text-sm" aria-label="Check before sending">
            {warnings.map((check) => (
              <li key={check.validator} className="flex items-start gap-2">
                <AlertTriangle
                  className={cn(
                    "mt-0.5 size-4 shrink-0",
                    check.outcome === "block" ? "text-destructive" : "text-warning",
                  )}
                />
                {check.message}
              </li>
            ))}
          </ul>
        ) : null}
        {proposal?.status === "cancelled" ? (
          <Alert tone="info">Undone: nothing was sent.</Alert>
        ) : null}
        {proposal?.status === "failed" ? (
          <Alert tone="error" title="Not sent">
            {proposal.error}
          </Alert>
        ) : null}
        {proposal?.status === "unknown_outcome" ? (
          <Alert tone="warning" title="Did it go out?">
            Gmail didn't confirm this send. I'm checking your Sent mail; I won't send it again on my
            own.
          </Alert>
        ) : null}
        {!canSend ? (
          <Alert tone="info">
            I can only read your Gmail. <Link to="/sources">Give me your inbox</Link> to send.
          </Alert>
        ) : null}
        {error ? <Alert tone="error">{error.message}</Alert> : null}
        {!locked ? (
          <div className="space-y-1 border-t pt-3">
            <Label htmlFor="redraft">Redraft with instructions</Label>
            <div className="flex gap-2">
              <Input
                id="redraft"
                value={instructions}
                maxLength={1000}
                placeholder="e.g. shorter, and offer Wednesday instead"
                onChange={(e) => setInstructions(e.target.value)}
              />
              <Button variant="outline" onClick={onRedraft} disabled={redrafting}>
                <Sparkles /> Redraft
              </Button>
            </div>
          </div>
        ) : null}
      </CardContent>
      <CardFooter className="flex-wrap">
        {sending ? (
          <>
            <span className="text-sm" role="status">
              Sending in {secondsLeft}s…
            </span>
            <Button variant="outline" onClick={() => undo.mutate()} disabled={undo.isPending}>
              <Undo2 /> Undo
            </Button>
          </>
        ) : locked ? (
          <span className="text-sm" role="status">
            Sending…
          </span>
        ) : dirty ? (
          <Button onClick={() => save.mutate()} disabled={save.isPending || !body.trim()}>
            <Check /> Save changes
          </Button>
        ) : (
          <Button
            onClick={() => send.mutate()}
            disabled={!canSend || !proposal || send.isPending || proposal.status === "refused"}
          >
            <Send /> Send
          </Button>
        )}
        {!locked ? (
          <Button variant="ghost" onClick={() => discard.mutate()} disabled={discard.isPending}>
            <Trash2 /> Discard
          </Button>
        ) : null}
      </CardFooter>
    </Card>
  );
}
