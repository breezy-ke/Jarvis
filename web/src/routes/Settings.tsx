import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Bell, Fingerprint, KeyRound, LogOut, Moon, Plus, Sun, Trash2 } from "lucide-react";
import { useState } from "react";

import { KillSwitchButton } from "@/components/KillSwitch";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Skeleton } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { enablePush, pushSupported } from "@/lib/push";
import { keys, useSystemStatus } from "@/lib/queries";
import { saveTheme, storedTheme, type Theme } from "@/lib/theme";
import type { ModelsOverview, Passkey, PoliciesOverview } from "@/lib/types";
import { timeAgo } from "@/lib/utils";
import { confirmWithPasskey, guessDeviceName, registerPasskey } from "@/lib/webauthn";

import { TelegramCard, VoiceCard } from "./SettingsVoice";

export function SettingsPage() {
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Settings</h1>
      <SafetyCard />
      <PasskeysCard />
      <NotificationsCard />
      <VoiceCard />
      <TelegramCard />
      <ModelsCard />
      <PoliciesCard />
      <AppearanceCard />
      <AccountCard />
    </div>
  );
}

function SafetyCard() {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Kill switch</CardTitle>
        <CardDescription>
          Stops every action instantly, including ones you already approved. You can also say
          "Jarvis, stand down".
        </CardDescription>
      </CardHeader>
      <CardContent>
        <KillSwitchButton />
      </CardContent>
    </Card>
  );
}

function PasskeysCard() {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: keys.passkeys,
    queryFn: () => api.get<Passkey[]>("/api/auth/passkeys"),
  });
  const [codes, setCodes] = useState<string[] | null>(null);
  const refresh = () => void queryClient.invalidateQueries({ queryKey: keys.passkeys });
  const add = useMutation({
    mutationFn: () => registerPasskey({ deviceName: guessDeviceName() }),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: async (id: string) => {
      await confirmWithPasskey();
      await api.del(`/api/auth/passkeys/${id}`);
    },
    onSuccess: refresh,
  });
  const regenerate = useMutation({
    mutationFn: async () => {
      await confirmWithPasskey();
      return api.post<{ recovery_codes: string[] }>("/api/auth/recovery-codes");
    },
    onSuccess: (result) => setCodes(result.recovery_codes),
  });
  const error = add.error ?? remove.error ?? regenerate.error;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Passkeys and recovery</CardTitle>
        <CardDescription>
          Add a passkey on each device you use. Removing one needs a fresh passkey tap.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <ul className="divide-y rounded-md border">
          {(data ?? []).map((key) => (
            <li key={key.id} className="flex items-center gap-3 px-3 py-2 text-sm">
              <Fingerprint className="size-4 text-muted-foreground" />
              <span className="flex-1">
                {key.device_name}
                <span className="block text-xs text-muted-foreground">
                  last used {timeAgo(key.last_used_at)}
                </span>
              </span>
              <Button
                size="icon"
                variant="ghost"
                aria-label={`Remove ${key.device_name}`}
                disabled={(data?.length ?? 0) <= 1}
                onClick={() => remove.mutate(key.id)}
              >
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => add.mutate()} disabled={add.isPending}>
            <Plus /> Add a passkey on this device
          </Button>
          <Button
            variant="outline"
            onClick={() => regenerate.mutate()}
            disabled={regenerate.isPending}
          >
            <KeyRound /> New recovery codes
          </Button>
        </div>
        {codes ? (
          <Alert tone="warning" title="New recovery codes (the old ones no longer work)">
            <ol className="mt-2 grid grid-cols-2 gap-1 font-mono">
              {codes.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ol>
          </Alert>
        ) : null}
        {error ? <Alert tone="error">{error.message}</Alert> : null}
      </CardContent>
    </Card>
  );
}

function NotificationsCard() {
  const { data } = useQuery({
    queryKey: keys.push,
    queryFn: () =>
      api.get<{ configured: boolean; public_key: string | null }>("/api/push/public-key"),
  });
  const enable = useMutation({ mutationFn: () => enablePush(data?.public_key ?? "") });
  const test = useMutation({ mutationFn: () => api.post<{ delivered: number }>("/api/push/test") });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Notifications</CardTitle>
        <CardDescription>
          Approvals and alerts pushed straight to this device, with no third-party service. On
          iPhone, add Jarvis to your Home Screen first.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {!data?.configured ? (
          <Alert tone="warning">
            Push isn't configured on the server yet. Run <code>make secrets</code> and restart.
          </Alert>
        ) : !pushSupported() ? (
          <Alert tone="warning">This browser doesn't support push notifications.</Alert>
        ) : (
          <div className="flex flex-wrap gap-2">
            <Button onClick={() => enable.mutate()} disabled={enable.isPending}>
              <Bell /> Enable on this device
            </Button>
            <Button variant="outline" onClick={() => test.mutate()} disabled={test.isPending}>
              Send a test
            </Button>
          </div>
        )}
        {enable.isSuccess ? (
          <Alert tone="success">This device will now get notifications.</Alert>
        ) : null}
        {test.data ? (
          <Alert tone="info">Delivered to {test.data.delivered} device(s).</Alert>
        ) : null}
        {enable.error ? <Alert tone="error">{enable.error.message}</Alert> : null}
      </CardContent>
    </Card>
  );
}

function ModelsCard() {
  const { data, isLoading } = useQuery({
    queryKey: keys.models,
    queryFn: () => api.get<ModelsOverview>("/api/system/models"),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>AI models and privacy</CardTitle>
        <CardDescription>
          Personal data only ever goes to your own GPU or to providers that don't train on it.
          Change models in <code>config/models.yaml</code>.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {isLoading ? <Skeleton className="h-32" /> : null}
        {Object.entries(data?.tasks ?? {}).map(([task, info]) => (
          <div key={task} className="rounded-md border p-3">
            <div className="flex items-center gap-2">
              <span className="font-medium">{task}</span>
              {info.privacy ? <Badge variant="outline">{info.privacy} data</Badge> : null}
            </div>
            {info.error ? <p className="mt-1 text-destructive">{info.error}</p> : null}
            <ul className="mt-2 space-y-1">
              {info.candidates.map((c) => (
                <li key={c.model} className="flex items-center gap-2">
                  <span
                    className={
                      c.available
                        ? "size-2 rounded-full bg-success"
                        : "size-2 rounded-full bg-muted-foreground"
                    }
                  />
                  <span className="font-mono text-xs">{c.model}</span>
                  <span className="text-xs text-muted-foreground">
                    {c.available ? "ready" : c.reason}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function PoliciesCard() {
  const { data } = useQuery({
    queryKey: keys.policies,
    queryFn: () => api.get<PoliciesOverview>("/api/system/policies"),
  });
  const label: Record<string, string> = {
    L0: "observe only",
    L1: "draft only",
    L2: "needs your approval",
    L3: "automatic, then tells you",
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>What Jarvis may do</CardTitle>
        <CardDescription>
          From <code>config/policies.yaml</code>. Money, deletions and production deploys can never
          be automatic.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ul className="divide-y text-sm">
          {Object.entries(data?.action_kinds ?? {}).map(([kind, policy]) => (
            <li key={kind} className="flex flex-wrap items-center gap-2 py-2">
              <span className="font-mono text-xs">{kind}</span>
              <Badge variant="outline">{policy.risk}</Badge>
              <span className="flex-1 text-muted-foreground">
                {label[policy.autonomy] ?? policy.autonomy}
              </span>
              {policy.available ? (
                <Badge variant="success">available</Badge>
              ) : (
                <Badge variant="secondary">later phase</Badge>
              )}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function AppearanceCard() {
  const [theme, setTheme] = useState<Theme>(storedTheme());
  const choose = (value: Theme) => {
    setTheme(value);
    saveTheme(value);
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>Appearance</CardTitle>
      </CardHeader>
      <CardContent className="flex gap-2" role="radiogroup" aria-label="Theme">
        {(["dark", "light", "system"] as const).map((value) => (
          <Button
            key={value}
            variant={theme === value ? "default" : "outline"}
            role="radio"
            aria-checked={theme === value}
            onClick={() => choose(value)}
          >
            {value === "dark" ? <Moon /> : value === "light" ? <Sun /> : null}
            {value[0]?.toUpperCase() + value.slice(1)}
          </Button>
        ))}
      </CardContent>
    </Card>
  );
}

function AccountCard() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { data } = useSystemStatus();
  const logout = useMutation({
    mutationFn: () => api.post("/api/auth/logout"),
    onSuccess: async () => {
      queryClient.clear();
      await navigate({ to: "/login" });
    },
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Session</CardTitle>
        <CardDescription>
          Jarvis v{data?.version} · timezone {data?.timezone}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Button variant="outline" onClick={() => logout.mutate()}>
          <LogOut /> Sign out
        </Button>
      </CardContent>
    </Card>
  );
}
