import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearch } from "@tanstack/react-router";
import { CheckCircle2, Headphones, ListChecks, Newspaper, RefreshCw } from "lucide-react";
import { useEffect } from "react";

import { BriefStory, type Vote } from "@/components/BriefStory";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, EmptyState, Skeleton } from "@/components/ui/misc";
import { ApiError, api } from "@/lib/api";
import { clockTime, deliveryNotes, longDay, SECTIONS } from "@/lib/brief";
import { keys, useBriefStatus } from "@/lib/queries";
import type { Brief, BriefHistoryItem, BriefSectionId, BriefStatus } from "@/lib/types";
import { timeAgo } from "@/lib/utils";

const VOTABLE: BriefSectionId[] = ["top", "africa", "quick"];

function Arrival({ brief, status }: { brief: Brief; status: BriefStatus }) {
  const tz = status.timezone;
  if (brief.status === "delivered" && brief.delivered_at) {
    return (
      <span className="flex flex-wrap items-center gap-2">
        Arrived at {clockTime(brief.delivered_at, tz)}
        {brief.on_time ? (
          <Badge variant="success">On time</Badge>
        ) : (
          <Badge variant="warning">Late</Badge>
        )}
      </span>
    );
  }
  if (brief.status === "ready" && brief.scheduled_for) {
    return <span>Ready. It goes out at {clockTime(brief.scheduled_for, tz)}.</span>;
  }
  return <span>Skipped: its day passed before Jarvis could send it.</span>;
}

function useVote(day: string) {
  const queryClient = useQueryClient();
  const key = keys.briefDay(day);
  return useMutation({
    mutationFn: ({ id, vote }: { id: string; vote: Vote }) =>
      api.post(`/api/brief/entries/${id}/vote`, { vote }),
    onMutate: async ({ id, vote }) => {
      await queryClient.cancelQueries({ queryKey: key });
      const before = queryClient.getQueryData<Brief>(key);
      if (before) {
        const sections = Object.fromEntries(
          Object.entries(before.sections).map(([name, entries]) => [
            name,
            entries.map((e) => (e.id === id ? { ...e, vote } : e)),
          ]),
        ) as Brief["sections"];
        queryClient.setQueryData<Brief>(key, { ...before, sections });
      }
      return { before };
    },
    onError: (_error, _vars, context) => {
      if (context?.before) queryClient.setQueryData(key, context.before);
    },
  });
}

function BriefBody({ brief, status }: { brief: Brief; status: BriefStatus }) {
  const vote = useVote(brief.day);
  const notes = deliveryNotes(brief.deliveries);
  return (
    <div className="space-y-6">
      <div className="text-sm text-muted-foreground">
        <Arrival brief={brief} status={status} />
      </div>
      {notes.map((note) => (
        <Alert key={note.channel} tone={note.failed ? "warning" : "info"} title={note.channel}>
          {note.text}
        </Alert>
      ))}
      {brief.audio_url ? (
        <section aria-labelledby="listen" className="space-y-2">
          <h2 id="listen" className="flex items-center gap-2 text-sm font-medium">
            <Headphones className="size-4" /> Listen
            {brief.audio_seconds ? (
              <span className="font-normal text-muted-foreground">
                about {Math.max(1, Math.round(brief.audio_seconds / 60))} min
              </span>
            ) : null}
          </h2>
          <audio controls preload="none" src={brief.audio_url} className="w-full">
            Your browser can't play the audio version.
          </audio>
        </section>
      ) : null}
      {brief.do_today ? (
        <Card className="border-primary/40">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <ListChecks className="size-4" /> One thing to do today
            </CardTitle>
            <CardDescription className="text-foreground">{brief.do_today}</CardDescription>
          </CardHeader>
        </Card>
      ) : null}
      {SECTIONS.map(({ id, title }) => {
        const entries = brief.sections[id];
        if (!entries.length && id !== "security") return null;
        return (
          <section key={id} aria-labelledby={`section-${id}`} className="space-y-3">
            <h2 id={`section-${id}`} className="text-lg font-semibold">
              {title}
            </h2>
            {entries.length ? (
              <div className="space-y-3">
                {entries.map((entry) => (
                  <BriefStory
                    key={entry.id}
                    entry={entry}
                    voting={vote.isPending && vote.variables.id === entry.id}
                    onVote={
                      VOTABLE.includes(id)
                        ? (value) => vote.mutate({ id: entry.id, vote: value })
                        : undefined
                    }
                  />
                ))}
              </div>
            ) : (
              <p className="flex items-center gap-2 text-sm text-muted-foreground">
                <CheckCircle2 className="size-4 text-success" /> Nothing urgent today.
              </p>
            )}
          </section>
        );
      })}
      {vote.isError ? (
        <Alert tone="error" title="Your vote didn't save">
          {vote.error.message}
        </Alert>
      ) : null}
    </div>
  );
}

function PastBriefs({ current }: { current: string | undefined }) {
  const history = useQuery({
    queryKey: keys.briefHistory,
    queryFn: () => api.get<BriefHistoryItem[]>("/api/brief/history"),
  });
  if (!history.data?.length) return null;
  return (
    <nav aria-label="Past briefs">
      <h2 className="mb-2 text-sm font-medium">Past briefs</h2>
      <ul className="space-y-1">
        {history.data.map((item) => (
          <li key={item.day}>
            <Link
              to="/brief"
              search={{ day: item.day }}
              aria-current={item.day === current ? "page" : undefined}
              className="flex items-center justify-between gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-accent aria-[current=page]:bg-accent"
            >
              <span className="min-w-0">
                <span className="block font-medium">{longDay(item.day)}</span>
                <span className="block truncate text-xs text-muted-foreground">
                  {item.headline ?? "A quiet morning"}
                </span>
              </span>
              {item.status === "skipped" ? (
                <Badge variant="outline">Skipped</Badge>
              ) : item.on_time === false ? (
                <Badge variant="warning">Late</Badge>
              ) : null}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function Sources({ status }: { status: BriefStatus }) {
  const sources = status.sources ?? [];
  if (!sources.length) return null;
  const failing = sources.filter((s) => s.enabled && s.failures > 0);
  return (
    <details className="text-sm">
      <summary className="cursor-pointer select-none font-medium">
        Sources: {sources.length - failing.length} of {sources.length} fine
      </summary>
      <ul className="mt-2 space-y-1">
        {sources.map((source) => (
          <li key={source.id} className="flex flex-wrap items-baseline justify-between gap-x-2">
            <span>{source.name}</span>
            <span className="text-xs text-muted-foreground">
              {source.failures > 0
                ? `failing (${source.last_error ?? "unknown"})`
                : `read ${timeAgo(source.last_ok_at)}`}
            </span>
          </li>
        ))}
      </ul>
    </details>
  );
}

export function BriefPage() {
  const { day } = useSearch({ from: "/app/brief" });
  const queryClient = useQueryClient();
  const status = useBriefStatus();
  const today = status.data?.today?.day;
  const shown = day ?? today;
  const making = status.data?.making === true;
  const brief = useQuery({
    queryKey: keys.briefDay(shown ?? "today"),
    queryFn: () => api.get<Brief>(`/api/brief?day=${shown ?? ""}`),
    enabled: status.data?.enabled === true && shown !== undefined,
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 2,
  });
  const make = useMutation({
    mutationFn: () => api.post("/api/brief/make"),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.briefStatus }),
  });

  useEffect(() => {
    if (!making) void queryClient.invalidateQueries({ queryKey: keys.brief });
  }, [making, queryClient]);

  if (status.isLoading || !status.data) return <Skeleton className="h-60" />;
  const s = status.data;
  if (!s.enabled) {
    return (
      <div className="space-y-4">
        <h1 className="text-xl font-semibold">Tech brief</h1>
        <Alert tone="error" title="The brief is off">
          {s.problem ?? "Its settings couldn't be read."} Fix it, then restart Jarvis (`make
          restart`); `make doctor` explains.
        </Alert>
      </div>
    );
  }
  const missing = brief.error instanceof ApiError && brief.error.status === 404;
  const isToday = shown === today;
  const next = s.next_delivery ? clockTime(s.next_delivery, s.timezone) : s.time;

  return (
    <div className="grid gap-8 lg:grid-cols-[1fr_16rem]">
      <div className="min-w-0 space-y-6">
        <header className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-xl font-semibold">
              <Newspaper className="size-5" /> Tech brief
              {shown ? (
                <span className="font-normal text-muted-foreground">· {longDay(shown)}</span>
              ) : null}
            </h1>
            <p className="text-sm text-muted-foreground">
              Every morning at {s.time}, with links to every source.
              {s.streak ? ` On time ${s.streak} day${s.streak === 1 ? "" : "s"} in a row.` : ""}
            </p>
          </div>
          {isToday && (missing || making) ? (
            <Button onClick={() => make.mutate()} disabled={making || make.isPending}>
              <RefreshCw className={making ? "animate-spin" : undefined} />
              {making ? "Making it…" : "Make it now"}
            </Button>
          ) : null}
        </header>
        {s.last_error ? (
          <Alert tone="warning" title="Something went wrong last time">
            {s.last_error}
          </Alert>
        ) : null}
        {make.isError ? (
          <Alert tone="error" title="Couldn't start it">
            {make.error.message}
          </Alert>
        ) : null}
        {brief.isLoading ? <Skeleton className="h-60" /> : null}
        {missing ? (
          <EmptyState icon={<Newspaper className="size-8" />} title="No brief here yet">
            {isToday
              ? making
                ? "Jarvis is reading the news and putting it together. It takes a minute or two."
                : `Today's brief arrives at ${next}. Want a first look now? Tap “Make it now”.`
              : "There's no brief for that day."}
          </EmptyState>
        ) : null}
        {brief.data ? <BriefBody brief={brief.data} status={s} /> : null}
      </div>
      <aside className="space-y-6 lg:pt-12">
        <PastBriefs current={shown} />
        <Sources status={s} />
      </aside>
    </div>
  );
}
