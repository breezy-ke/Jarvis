/** Settings cards for voice (speech server, satellites) and Telegram. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Laptop, Link2, Play, Send, Trash2, Unlink } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Skeleton } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { keys } from "@/lib/queries";
import type { PairingCode, TelegramLink, TelegramStatus, VoiceStatus } from "@/lib/types";
import { timeAgo } from "@/lib/utils";
import { confirmWithPasskey } from "@/lib/webauthn";

const VOICE_NAMES: Record<string, string> = {
  bm_george: "George (British male)",
  bm_daniel: "Daniel (British male)",
  bm_lewis: "Lewis (British male)",
  bf_emma: "Emma (British female)",
  am_michael: "Michael (American male)",
  af_heart: "Heart (American female)",
};

/** "9:41" until `expiresAt`, then null. */
function useCountdown(expiresAt: string | undefined): string | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!expiresAt) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [expiresAt]);
  if (!expiresAt) return null;
  const left = Math.round((new Date(expiresAt).getTime() - now) / 1000);
  if (left <= 0) return null;
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
}

async function playSample(): Promise<void> {
  const res = await fetch("/api/voice/preview", { method: "POST", credentials: "same-origin" });
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // not JSON
    }
    throw new Error(message);
  }
  const url = URL.createObjectURL(await res.blob());
  const audio = new Audio(url);
  audio.onended = () => URL.revokeObjectURL(url);
  await audio.play();
}

function SpeechServer({ status }: { status: VoiceStatus }) {
  const speech = status.speech;
  if (!speech) return null;
  const ready = speech.reachable && speech.stt_ready && speech.tts_ready;
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="font-medium">Speech server</span>
      {ready ? (
        <Badge variant="success">ready</Badge>
      ) : speech.reachable ? (
        <Badge variant="warning">models missing</Badge>
      ) : (
        <Badge variant="destructive">not reachable</Badge>
      )}
      {!ready ? (
        <span className="w-full text-xs text-muted-foreground">
          {speech.detail}.{" "}
          {speech.reachable ? (
            <>
              Run <code>make pull-models</code> on the PC.
            </>
          ) : (
            <>
              Run <code>make up</code> on the PC, then <code>make doctor</code>.
            </>
          )}
        </span>
      ) : null}
    </div>
  );
}

export function VoiceCard() {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: keys.voice,
    queryFn: () => api.get<VoiceStatus>("/api/voice/status"),
  });
  const refresh = () => void queryClient.invalidateQueries({ queryKey: keys.voice });
  const sample = useMutation({ mutationFn: playSample });
  const pair = useMutation({
    mutationFn: async () => {
      await confirmWithPasskey();
      return api.post<PairingCode>("/api/voice/pairing-code");
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => api.del(`/api/voice/devices/${id}`),
    onSuccess: refresh,
  });
  const countdown = useCountdown(pair.data?.expires_at);
  const error = sample.error ?? pair.error ?? remove.error;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Voice</CardTitle>
        <CardDescription>
          Talk to Jarvis on the Talk page, with voice notes on Telegram, or say “Hey Jarvis” to the
          Windows app. Speech is turned into text and back on your own PC.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {isLoading ? <Skeleton className="h-24" /> : null}
        {data && !data.enabled ? (
          <Alert tone="warning" title="Voice is switched off">
            {data.error ?? "Check config/voice.yaml."}
          </Alert>
        ) : null}
        {data?.enabled ? (
          <>
            <SpeechServer status={data} />
            {data.problem ? <Alert tone="warning">{data.problem}</Alert> : null}
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
              <dt className="text-muted-foreground">Voice</dt>
              <dd>{VOICE_NAMES[data.voice ?? ""] ?? data.voice}</dd>
              <dt className="text-muted-foreground">Languages</dt>
              <dd>{data.language === "auto" ? "English or Swahili (automatic)" : data.language}</dd>
              <dt className="text-muted-foreground">Approving by voice</dt>
              <dd>
                Say “{data.confirm_phrase}” or “{data.cancel_phrase}” when Jarvis reads an action
                back. High-risk actions always need your passkey here.
              </dd>
            </dl>
            <Button variant="outline" onClick={() => sample.mutate()} disabled={sample.isPending}>
              <Play /> Play a sample
            </Button>
          </>
        ) : null}

        <div className="space-y-2">
          <p className="font-medium">Windows app (“Hey Jarvis”)</p>
          {data?.devices.length ? (
            <ul className="divide-y rounded-md border">
              {data.devices.map((device) => (
                <li key={device.id} className="flex items-center gap-3 px-3 py-2">
                  <Laptop className="size-4 text-muted-foreground" />
                  <span className="flex-1">
                    {device.name}
                    <span className="block text-xs text-muted-foreground">
                      last used {timeAgo(device.last_seen_at)}
                    </span>
                  </span>
                  <Button
                    size="icon"
                    variant="ghost"
                    aria-label={`Remove ${device.name}`}
                    onClick={() => remove.mutate(device.id)}
                  >
                    <Trash2 />
                  </Button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-muted-foreground">No Windows app paired yet.</p>
          )}
          {pair.data && countdown ? (
            <Alert tone="info" title="Pairing code">
              <p className="my-1 font-mono text-2xl tracking-widest text-foreground">
                {pair.data.code}
              </p>
              <p>
                Enter it when the Windows app's installer asks (or run{" "}
                <code>jarvis-satellite pair {pair.data.code}</code>). It works once and expires in{" "}
                {countdown}.
              </p>
            </Alert>
          ) : (
            <Button variant="outline" onClick={() => pair.mutate()} disabled={pair.isPending}>
              <Link2 /> Pair the Windows app
            </Button>
          )}
        </div>
        {error ? <Alert tone="error">{error.message}</Alert> : null}
      </CardContent>
    </Card>
  );
}

export function TelegramCard() {
  const queryClient = useQueryClient();
  const [link, setLink] = useState<TelegramLink | null>(null);
  const { data, isLoading } = useQuery({
    queryKey: keys.telegram,
    queryFn: () => api.get<TelegramStatus>("/api/telegram/status"),
    // While a link is out, check every few seconds so the card updates once you tap Start.
    refetchInterval: (query) => (link && !query.state.data?.paired ? 3_000 : false),
  });
  const refresh = () => void queryClient.invalidateQueries({ queryKey: keys.telegram });
  const makeLink = useMutation({
    mutationFn: async () => {
      await confirmWithPasskey();
      return api.post<TelegramLink>("/api/telegram/pairing-code");
    },
    onSuccess: setLink,
  });
  const test = useMutation({ mutationFn: () => api.post("/api/telegram/test") });
  const unlink = useMutation({
    mutationFn: () => api.del("/api/telegram/owner"),
    onSuccess: () => {
      setLink(null);
      refresh();
    },
  });
  const countdown = useCountdown(link?.expires_at);
  const error = makeLink.error ?? test.error ?? unlink.error;
  const waiting = link && countdown && !data?.paired;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Telegram</CardTitle>
        <CardDescription>
          Chat with Jarvis and approve actions from Telegram, by text or voice note. Telegram chats
          aren't end-to-end encrypted, so Jarvis keeps sensitive details out of them, and high-risk
          actions still need your passkey here.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {isLoading ? <Skeleton className="h-16" /> : null}
        {data && !data.configured ? (
          <Alert tone="info" title="Set up your Telegram bot">
            <ol className="mt-1 list-decimal space-y-1 pl-4">
              <li>
                In Telegram, message <strong>@BotFather</strong>, send <code>/newbot</code> and pick
                a name and a username.
              </li>
              <li>
                Put the token it gives you in <code>.env</code> as <code>TELEGRAM_BOT_TOKEN=…</code>
              </li>
              <li>
                Run <code>make restart</code>, then link your account here.
              </li>
            </ol>
          </Alert>
        ) : null}
        {data?.configured ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">Bot</span>
              {data.running ? (
                <Badge variant="success">connected</Badge>
              ) : (
                <Badge variant="warning">not connected</Badge>
              )}
              {data.bot_username ? (
                <span className="text-muted-foreground">@{data.bot_username}</span>
              ) : null}
            </div>
            {data.error ? <Alert tone="warning">{data.error}</Alert> : null}
            {data.paired && data.owner ? (
              <>
                <p>
                  Linked to{" "}
                  <strong>
                    {data.owner.username ? `@${data.owner.username}` : data.owner.name}
                  </strong>{" "}
                  <span className="text-muted-foreground">
                    since {timeAgo(data.owner.paired_at)}
                  </span>
                </p>
                <div className="flex flex-wrap gap-2">
                  <Button variant="outline" onClick={() => test.mutate()} disabled={test.isPending}>
                    <Send /> Send a test message
                  </Button>
                  <Button
                    variant="outline"
                    onClick={() => unlink.mutate()}
                    disabled={unlink.isPending}
                  >
                    <Unlink /> Unlink
                  </Button>
                </div>
                {test.isSuccess ? <Alert tone="success">Sent. Check Telegram.</Alert> : null}
              </>
            ) : waiting ? (
              <Alert tone="info" title="Open Telegram and tap Start">
                {link.link ? (
                  <Button asChild className="my-2">
                    <a href={link.link} target="_blank" rel="noopener noreferrer">
                      <Send /> Open Telegram
                    </a>
                  </Button>
                ) : null}
                <p>
                  Or send <code>/start {link.code}</code> to @{link.bot_username}. The link works
                  once and expires in {countdown}.
                </p>
              </Alert>
            ) : (
              <Button
                onClick={() => makeLink.mutate()}
                disabled={makeLink.isPending || !data.bot_username}
              >
                <Link2 /> Link Telegram
              </Button>
            )}
          </>
        ) : null}
        {error ? <Alert tone="error">{error.message}</Alert> : null}
      </CardContent>
    </Card>
  );
}
