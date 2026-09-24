import { useInfiniteQuery, useMutation } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Alert, EmptyState, Skeleton } from "@/components/ui/misc";
import { api } from "@/lib/api";
import type { AuditEvent } from "@/lib/types";
import { cn, timeAgo } from "@/lib/utils";

const FILTERS = [
  { id: "", label: "All" },
  { id: "action.", label: "Actions" },
  { id: "memory.", label: "Memory" },
  { id: "profile.", label: "Profile" },
  { id: "llm.", label: "AI calls" },
  { id: "auth.", label: "Sign-ins" },
];

export function ActivityPage() {
  const [filter, setFilter] = useState("");
  const query = useInfiniteQuery({
    queryKey: ["audit", filter],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      api.get<AuditEvent[]>(
        `/api/audit?limit=50${pageParam ? `&before_id=${pageParam}` : ""}${filter ? `&event_type=${filter}` : ""}`,
      ),
    getNextPageParam: (last) => (last.length === 50 ? last[last.length - 1]?.id : undefined),
  });
  const verify = useMutation({
    mutationFn: () =>
      api.get<{ ok: boolean; checked: number; reason: string | null }>("/api/audit/verify"),
  });
  const events = query.data?.pages.flat() ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Activity</h1>
          <p className="text-sm text-muted-foreground">
            Every model call, tool call and action, in a tamper-evident log. Personal content is
            never written here.
          </p>
        </div>
        <Button variant="outline" onClick={() => verify.mutate()} disabled={verify.isPending}>
          <ShieldCheck /> Verify integrity
        </Button>
      </div>
      {verify.data ? (
        <Alert
          tone={verify.data.ok ? "success" : "error"}
          title={verify.data.ok ? "Log intact" : "Tampering detected"}
        >
          {verify.data.ok ? `All ${verify.data.checked} entries check out.` : verify.data.reason}
        </Alert>
      ) : null}
      <div className="flex flex-wrap gap-2" role="group" aria-label="Filter">
        {FILTERS.map((f) => (
          <Button
            key={f.id}
            size="sm"
            variant={filter === f.id ? "default" : "outline"}
            aria-pressed={filter === f.id}
            onClick={() => setFilter(f.id)}
          >
            {f.label}
          </Button>
        ))}
      </div>
      {query.isLoading ? <Skeleton className="h-60" /> : null}
      {!query.isLoading && events.length === 0 ? <EmptyState title="Nothing logged yet" /> : null}
      <Card>
        <CardContent className="divide-y p-0">
          {events.map((event) => (
            <div
              key={event.id}
              className="flex flex-wrap items-start gap-x-3 gap-y-1 px-4 py-3 text-sm"
            >
              <Badge variant="outline" className={cn("font-mono")}>
                {event.event_type}
              </Badge>
              <span className="min-w-0 flex-1">{event.summary}</span>
              <span className="text-xs text-muted-foreground" title={event.ts}>
                {event.actor} · {timeAgo(event.ts)}
              </span>
            </div>
          ))}
        </CardContent>
      </Card>
      {query.hasNextPage ? (
        <Button
          variant="outline"
          className="w-full"
          onClick={() => void query.fetchNextPage()}
          disabled={query.isFetchingNextPage}
        >
          Load more
        </Button>
      ) : null}
    </div>
  );
}
