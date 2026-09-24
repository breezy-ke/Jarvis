import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import {
  ArrowLeft,
  CheckCircle2,
  ChevronRight,
  Circle,
  CircleDashed,
  ShieldCheck,
  SkipForward,
} from "lucide-react";
import { useMemo, useState } from "react";

import { ChatView, type ChatLine } from "@/components/ChatView";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Progress, Skeleton } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { keys } from "@/lib/queries";
import type { OnboardingModule, OnboardingOverview } from "@/lib/types";
import { useStream } from "@/lib/useStream";
import { percent } from "@/lib/utils";

function useOverview() {
  return useQuery({
    queryKey: keys.onboarding,
    queryFn: () => api.get<OnboardingOverview>("/api/onboarding"),
  });
}

const statusIcon = {
  completed: <CheckCircle2 className="size-5 text-success" />,
  in_progress: <CircleDashed className="size-5 text-primary" />,
  skipped: <SkipForward className="size-5 text-muted-foreground" />,
  not_started: <Circle className="size-5 text-muted-foreground" />,
} as const;

export function OnboardingPage() {
  const { data, isLoading } = useOverview();
  const queryClient = useQueryClient();
  const signOff = useMutation({
    mutationFn: () => api.post("/api/profile/sign-off"),
    onSuccess: () => {
      void queryClient.invalidateQueries();
    },
  });
  if (isLoading || !data) return <Skeleton className="h-96" />;
  const next = data.modules.find((m) => m.status === "not_started" || m.status === "in_progress");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Getting to know you</h1>
        <p className="text-sm text-muted-foreground">
          Twelve short conversations, about an hour in total and fine to split up. I only act on my
          own once you've reviewed and signed off what I've learned.
        </p>
      </div>
      <Card>
        <CardContent className="space-y-3 pt-5">
          <div className="flex items-center justify-between text-sm">
            <span>
              Profile {percent(data.completeness)} complete · {data.modules_completed}/
              {data.modules_total} modules
            </span>
            {data.gate_open ? (
              <Badge variant="success">Autonomy on</Badge>
            ) : (
              <Badge variant="secondary">Autonomy off</Badge>
            )}
          </div>
          <Progress value={data.completeness} label="Profile completeness" />
          {!data.signed_off && data.completeness >= 0.8 ? (
            <Alert tone="success" title="Ready for your sign-off">
              Review your profile on the{" "}
              <Link to="/memory" className="underline">
                What I know
              </Link>{" "}
              page, then sign it off to switch on low-risk automation.
              <div className="mt-2">
                <Button size="sm" onClick={() => signOff.mutate()} disabled={signOff.isPending}>
                  <ShieldCheck /> Sign off my profile
                </Button>
              </div>
            </Alert>
          ) : null}
          {signOff.error ? <Alert tone="error">{signOff.error.message}</Alert> : null}
          {next ? (
            <Button asChild>
              <Link to="/onboarding/$moduleId" params={{ moduleId: next.id }}>
                {next.status === "in_progress" ? "Continue" : "Start"}: {next.title}{" "}
                <ChevronRight />
              </Link>
            </Button>
          ) : null}
        </CardContent>
      </Card>
      <ol className="grid gap-3 md:grid-cols-2">
        {data.modules.map((module, index) => (
          <li key={module.id}>
            <ModuleCard module={module} index={index + 1} />
          </li>
        ))}
      </ol>
    </div>
  );
}

function ModuleCard({ module, index }: { module: OnboardingModule; index: number }) {
  return (
    <Link to="/onboarding/$moduleId" params={{ moduleId: module.id }} className="block h-full">
      <Card className="h-full transition-colors hover:bg-accent/40">
        <CardHeader>
          <div className="flex items-center gap-3">
            {statusIcon[module.status]}
            <CardTitle className="flex-1">
              {index}. {module.title}
            </CardTitle>
            {module.optional ? <Badge variant="outline">optional</Badge> : null}
          </div>
          <CardDescription>{module.goal}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <Progress
            value={module.total ? module.filled / module.total : 0}
            label={`${module.title} progress`}
          />
          <p className="text-xs text-muted-foreground">
            {module.filled}/{module.total} answered
            {module.required_missing.length
              ? ` · ${module.required_missing.length} required still open`
              : ""}
          </p>
        </CardContent>
      </Card>
    </Link>
  );
}

export function OnboardingModulePage() {
  const { moduleId } = useParams({ from: "/app/onboarding/$moduleId" });
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const overview = useOverview();
  const module = overview.data?.modules.find((m) => m.id === moduleId);
  const transcript = useQuery({
    queryKey: keys.transcript(moduleId),
    queryFn: () =>
      api.get<{ role: "user" | "assistant"; content: string }[]>(
        `/api/onboarding/${moduleId}/transcript`,
      ),
  });
  const streamer = useStream();
  const [pending, setPending] = useState<string | null>(null);
  const skip = useMutation({
    mutationFn: () => api.post(`/api/onboarding/${moduleId}/skip`),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: keys.onboarding });
      await navigate({ to: "/onboarding" });
    },
  });

  const lines: ChatLine[] = useMemo(() => {
    const base = (transcript.data ?? []).map((t, i) => ({
      id: `${i}`,
      role: t.role,
      content: t.content,
    }));
    return pending ? [...base, { id: "pending", role: "user" as const, content: pending }] : base;
  }, [transcript.data, pending]);

  async function turn(text: string | null) {
    setPending(text);
    await streamer.run(`/api/onboarding/${moduleId}/message`, { text });
    await queryClient.invalidateQueries({ queryKey: keys.transcript(moduleId) });
    await queryClient.invalidateQueries({ queryKey: keys.onboarding });
    await queryClient.invalidateQueries({ queryKey: keys.profile });
    setPending(null);
  }

  const started = (transcript.data?.length ?? 0) > 0;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button asChild variant="ghost" size="sm">
          <Link to="/onboarding">
            <ArrowLeft /> All modules
          </Link>
        </Button>
        <h1 className="flex-1 text-xl font-semibold">{module?.title ?? "Onboarding"}</h1>
        <Button variant="outline" size="sm" onClick={() => skip.mutate()} disabled={skip.isPending}>
          <SkipForward /> Skip for now
        </Button>
      </div>
      {module?.status === "completed" ? (
        <Alert tone="success" title="Done">
          {module.summary ?? "This module is complete."} You can keep talking to add more.
        </Alert>
      ) : null}
      <ChatView
        lines={lines}
        streaming={streamer.text}
        busy={streamer.busy}
        error={streamer.error}
        onSend={(text) => void turn(text)}
        onStop={streamer.stop}
        placeholder="Your answer…"
        empty={
          transcript.isLoading ? (
            <Skeleton className="h-24" />
          ) : !started ? (
            <div className="flex flex-col items-center gap-3 py-10 text-center">
              <p className="max-w-md text-muted-foreground">{module?.goal}</p>
              <Button onClick={() => void turn(null)} disabled={streamer.busy}>
                Begin
              </Button>
            </div>
          ) : null
        }
      />
    </div>
  );
}
