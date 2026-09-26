import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  Fingerprint,
  Inbox,
  Undo2,
  X,
  XCircle,
} from "lucide-react";
import { useState } from "react";

import { EmailPreview } from "@/components/EmailPreview";
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
import { Alert, EmptyState, Skeleton } from "@/components/ui/misc";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { keys } from "@/lib/queries";
import type { Action, Validation } from "@/lib/types";
import { useCountdown } from "@/lib/useCountdown";
import { shortHash, timeAgo } from "@/lib/utils";
import { confirmWithPasskey } from "@/lib/webauthn";

const riskVariant = {
  low: "secondary",
  medium: "default",
  high: "warning",
  critical: "destructive",
} as const;

export function ApprovalsPage() {
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Approvals</h1>
        <p className="text-sm text-muted-foreground">
          Jarvis only proposes. You decide. Each approval covers exactly the content shown, down to
          its fingerprint.
        </p>
      </div>
      <Tabs defaultValue="open">
        <TabsList>
          <TabsTrigger value="open">Waiting</TabsTrigger>
          <TabsTrigger value="history">History</TabsTrigger>
        </TabsList>
        <TabsContent value="open">
          <ActionList view="open" />
        </TabsContent>
        <TabsContent value="history">
          <ActionList view="history" />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function ActionList({ view }: { view: "open" | "history" }) {
  const { data, isLoading, error } = useQuery({
    queryKey: keys.actions(view),
    queryFn: () => api.get<Action[]>(`/api/actions?view=${view}`),
    refetchInterval: 5_000,
  });
  if (isLoading) return <Skeleton className="h-40" />;
  if (error) return <Alert tone="error">{error.message}</Alert>;
  if (!data?.length) {
    return (
      <EmptyState
        icon={<Inbox className="size-8" />}
        title={view === "open" ? "Nothing waiting" : "No history yet"}
      >
        {view === "open"
          ? "When Jarvis wants to do something on your behalf, it appears here first."
          : null}
      </EmptyState>
    );
  }
  return (
    <div className="space-y-3">
      {data.map((action) => (
        <ActionCard key={action.id} action={action} />
      ))}
    </div>
  );
}

const EMAIL_KINDS = new Set(["email.send", "email.draft"]);

function ValidationRow({ check }: { check: Validation }) {
  const icon =
    check.outcome === "pass" ? (
      <CheckCircle2 className="size-4 text-success" />
    ) : check.outcome === "warn" ? (
      <AlertTriangle className="size-4 text-warning" />
    ) : (
      <XCircle className="size-4 text-destructive" />
    );
  return (
    <li className="flex items-start gap-2 text-sm">
      <span className="mt-0.5">{icon}</span>
      <span>{check.message}</span>
    </li>
  );
}

function ActionCard({ action }: { action: Action }) {
  const queryClient = useQueryClient();
  const [message, setMessage] = useState<string | null>(null);
  const secondsLeft = useCountdown(action.status === "approved" ? action.execute_after : null);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["actions"] });
    void queryClient.invalidateQueries({ queryKey: keys.status });
  };
  const approve = useMutation({
    mutationFn: async () => {
      if (action.needs_passkey) await confirmWithPasskey();
      return api.post(`/api/actions/${action.id}/approve`, {
        payload_hash: action.payload_hash,
        use_passkey: action.needs_passkey,
      });
    },
    onSuccess: refresh,
    onError: (e: Error) => setMessage(e.message),
  });
  const reject = useMutation({
    mutationFn: () => api.post(`/api/actions/${action.id}/reject`, {}),
    onSuccess: refresh,
    onError: (e: Error) => setMessage(e.message),
  });
  const undo = useMutation({
    mutationFn: () => api.post(`/api/actions/${action.id}/undo`),
    onSuccess: refresh,
    onError: (e: Error) => setMessage(e.message),
  });

  const pending = action.status === "pending";
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={riskVariant[action.risk]}>{action.risk} risk</Badge>
          <Badge variant="outline">{action.kind}</Badge>
          <Badge
            variant={
              action.status === "executed"
                ? "success"
                : action.status === "failed"
                  ? "destructive"
                  : "secondary"
            }
          >
            {action.status.replace("_", " ")}
          </Badge>
          <span className="ml-auto text-xs text-muted-foreground">
            {timeAgo(action.created_at)}
          </span>
        </div>
        <CardTitle className="pt-1">{action.summary || action.kind}</CardTitle>
        {action.rationale ? <CardDescription>Why: {action.rationale}</CardDescription> : null}
      </CardHeader>
      <CardContent className="space-y-3">
        {action.status_reason ? (
          <Alert tone={pending ? "warning" : "info"}>{action.status_reason}</Alert>
        ) : null}
        {EMAIL_KINDS.has(action.kind) ? (
          <EmailPreview payload={action.payload} email={action.email} />
        ) : null}
        {action.validation.length ? (
          <ul className="space-y-1" aria-label="Safety checks">
            {action.validation.map((check) => (
              <ValidationRow key={check.validator} check={check} />
            ))}
          </ul>
        ) : null}
        <details className="rounded-md border bg-muted/30 p-3 text-sm">
          <summary className="cursor-pointer select-none font-medium">Exact content</summary>
          <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-words font-mono text-xs">
            {JSON.stringify(action.payload, null, 2)}
          </pre>
          <p className="mt-2 text-xs text-muted-foreground">
            Fingerprint <code>{shortHash(action.payload_hash)}</code>. If anything changes, you'll
            be asked again.
          </p>
        </details>
        {action.error ? (
          <Alert tone="error" title="Error">
            {action.error}
          </Alert>
        ) : null}
        {message ? <Alert tone="error">{message}</Alert> : null}
      </CardContent>
      {pending ? (
        <CardFooter className="flex-wrap">
          <Button onClick={() => approve.mutate()} disabled={approve.isPending}>
            {action.needs_passkey ? <Fingerprint /> : <Check />}
            {action.needs_passkey ? "Approve with passkey" : "Approve"}
          </Button>
          <Button variant="outline" onClick={() => reject.mutate()} disabled={reject.isPending}>
            <X /> Reject
          </Button>
        </CardFooter>
      ) : action.status === "approved" && secondsLeft > 0 ? (
        <CardFooter>
          <Button variant="outline" onClick={() => undo.mutate()} disabled={undo.isPending}>
            <Undo2 /> Undo ({secondsLeft}s)
          </Button>
        </CardFooter>
      ) : null}
    </Card>
  );
}
