import { Link } from "@tanstack/react-router";
import {
  ArrowRight,
  Brain,
  CheckCircle2,
  Lock,
  MessageSquare,
  ShieldCheck,
  Sparkles,
  Unlock,
} from "lucide-react";

import { KillSwitchButton } from "@/components/KillSwitch";
import { Orb } from "@/components/Orb";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress, Skeleton } from "@/components/ui/misc";
import { useSystemStatus } from "@/lib/queries";
import { greeting, percent } from "@/lib/utils";

const ROADMAP = [
  { phase: "Phase 2", title: "Voice: “Hey Jarvis”, and Jarvis talks back" },
  { phase: "Phase 3", title: "Email: triage, drafts in your style, send with approval" },
  { phase: "Phase 4", title: "Daily tech brief and web research" },
  { phase: "Phase 5", title: "Lead engine: find and win clients" },
  { phase: "Phase 6", title: "UI Studio: build websites and apps" },
];

export function HomePage() {
  const { data, isLoading } = useSystemStatus();
  if (isLoading || !data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-64" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }
  const name = data.owner_name ? `, ${data.owner_name}` : "";
  const complete = data.profile.completeness;

  return (
    <div className="space-y-6">
      <section className="flex items-center gap-4">
        <Orb size={48} />
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {greeting()}
            {name}.
          </h1>
          <p className="text-sm text-muted-foreground">
            {data.autonomy.open
              ? "I'm fully set up. Everything outward-facing still waits for your approval."
              : "I'm still learning about you. Nothing happens without your say-so."}
          </p>
        </div>
      </section>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              {data.autonomy.open ? (
                <Unlock className="size-4 text-success" />
              ) : (
                <Lock className="size-4" />
              )}
              Autonomy {data.autonomy.open ? "on" : "off"}
            </CardTitle>
            <CardDescription className="first-letter:uppercase">
              {data.autonomy.reason}.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center justify-between text-sm">
              <span>Profile</span>
              <span className="font-medium">{percent(complete)}</span>
            </div>
            <Progress value={complete} label="Profile completeness" />
            <p className="text-xs text-muted-foreground">
              Low-risk automation switches on once your profile is at least 80% complete and you've
              signed it off.
            </p>
            {!data.profile.signed_off ? (
              <Button asChild size="sm">
                <Link to="/onboarding">
                  <Sparkles /> {complete > 0 ? "Continue onboarding" : "Start onboarding"}
                </Link>
              </Button>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ShieldCheck className="size-4" /> Safety
            </CardTitle>
            <CardDescription>You're in control of everything Jarvis does.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <Link
              to="/approvals"
              className="flex items-center justify-between rounded-md border p-3 hover:bg-accent"
            >
              <span className="flex items-center gap-2 text-sm">
                <CheckCircle2 className="size-4" /> Waiting for your approval
              </span>
              <Badge variant={data.pending_approvals ? "warning" : "secondary"}>
                {data.pending_approvals}
              </Badge>
            </Link>
            <Link
              to="/memory"
              className="flex items-center justify-between rounded-md border p-3 hover:bg-accent"
            >
              <span className="flex items-center gap-2 text-sm">
                <Brain className="size-4" /> Things I learned, to confirm
              </span>
              <Badge variant={data.memories_to_review ? "default" : "secondary"}>
                {data.memories_to_review}
              </Badge>
            </Link>
            <KillSwitchButton />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle>Talk to me</CardTitle>
            <CardDescription>Ask anything, or tell me something to remember.</CardDescription>
          </div>
          <Button asChild>
            <Link to="/chat">
              <MessageSquare /> Open chat <ArrowRight />
            </Link>
          </Button>
        </CardHeader>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Coming next</CardTitle>
          <CardDescription>Each phase ships with its own tests and safety checks.</CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="space-y-2">
            {ROADMAP.map((item) => (
              <li key={item.phase} className="flex items-center gap-3 text-sm">
                <Badge variant="outline">{item.phase}</Badge>
                <span>{item.title}</span>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
