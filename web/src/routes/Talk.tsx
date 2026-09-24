import { useQuery } from "@tanstack/react-query";
import { Link, useSearch } from "@tanstack/react-router";
import { MessageSquare, Mic, PhoneOff, ShieldCheck, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Orb } from "@/components/Orb";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/input";
import { Alert } from "@/components/ui/misc";
import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import { keys } from "@/lib/queries";
import type { VoiceStatus } from "@/lib/types";
import { cn } from "@/lib/utils";
import type { TranscriptLine } from "@/voice/protocol";
import { useVoice, type Phase } from "@/voice/useVoice";

const PHASE_TEXT: Record<Phase, string> = {
  off: "Tap the orb and speak. Jarvis answers out loud.",
  connecting: "Connecting…",
  ready: "Listening. Go ahead.",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking. Talk or tap the orb to interrupt.",
};

export function TalkPage() {
  const { c: conversationId } = useSearch({ from: "/app/talk" });
  const status = useQuery({
    queryKey: keys.voice,
    queryFn: () => api.get<VoiceStatus>("/api/voice/status"),
  });
  const voice = useVoice(conversationId);
  const orb = useRef<HTMLSpanElement>(null);
  const live = voice.status.kind === "live";
  const busy = voice.status.kind === "connecting" || live;
  const unavailable = status.data && (!status.data.enabled || status.data.problem);

  // Feed the live sound level to the orb without re-rendering the page 60 times a second.
  const { phase, levels } = voice;
  useEffect(() => {
    const element = orb.current;
    if (!live || !element) return;
    let frame = 0;
    const tick = () => {
      const level = phase === "speaking" ? levels().output : levels().input;
      element.style.setProperty("--level", Math.min(1, level * 5).toFixed(3));
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      element.style.setProperty("--level", "0");
    };
  }, [live, phase, levels]);

  function onOrb() {
    if (!busy) voice.start();
    else if (voice.phase === "speaking") voice.interrupt();
  }

  const orbLabel = !busy
    ? "Start talking to Jarvis"
    : voice.phase === "speaking"
      ? "Stop Jarvis speaking"
      : "Talking to Jarvis";

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Talk</h1>
        {voice.view.conversationId ? (
          <Button asChild variant="ghost" size="sm">
            <Link to="/chat" search={{ c: voice.view.conversationId }}>
              <MessageSquare /> Open in Chat
            </Link>
          </Button>
        ) : null}
      </div>

      {unavailable ? (
        <Alert tone="warning" title="Voice isn't ready">
          {status.data?.problem ?? status.data?.error ?? "Voice is switched off."}{" "}
          <Link to="/settings" className="underline">
            Check Settings → Voice
          </Link>
          .
        </Alert>
      ) : null}

      <div className="flex flex-col items-center gap-4 py-2">
        <button
          type="button"
          onClick={onOrb}
          aria-label={orbLabel}
          className="rounded-full p-6 outline-none transition focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Orb ref={orb} size={168} state={voice.phase} />
        </button>
        <p aria-live="polite" className="min-h-5 text-center text-sm text-muted-foreground">
          {voice.holdMode && live && voice.phase !== "speaking" && !voice.holding
            ? "Hold the button while you speak."
            : PHASE_TEXT[voice.phase]}
        </p>

        {live && voice.holdMode ? <HoldToTalk holding={voice.holding} onHold={voice.hold} /> : null}

        {/* Keyed, so React never morphs one button into another mid-transition. */}
        <div className="flex flex-wrap items-center justify-center gap-2">
          {busy ? (
            <>
              {voice.phase === "speaking" ? (
                <Button key="stop" variant="outline" onClick={voice.interrupt}>
                  <Square /> Stop Jarvis
                </Button>
              ) : null}
              <Button key="end" variant="outline" onClick={voice.stop}>
                <PhoneOff /> End
              </Button>
            </>
          ) : (
            <Button key="start" onClick={voice.start}>
              <Mic /> Start talking
            </Button>
          )}
        </div>
      </div>

      {voice.error ? (
        <Alert tone="error" title="Voice stopped">
          {voice.error}
        </Alert>
      ) : null}

      {voice.view.confirmation ? (
        <Card className="border-primary/40">
          <CardContent className="flex gap-3 p-4 text-sm">
            <ShieldCheck className="mt-0.5 size-5 shrink-0 text-primary" />
            <div className="space-y-1">
              <p className="font-medium">
                Waiting for your answer: {voice.view.confirmation.summary}
              </p>
              <p className="text-muted-foreground">
                Say “{status.data?.confirm_phrase ?? "confirm"}” to go ahead, or “
                {status.data?.cancel_phrase ?? "cancel"}”. Or{" "}
                <Link to="/approvals" className="underline">
                  review it in Approvals
                </Link>
                .
              </p>
            </div>
          </CardContent>
        </Card>
      ) : null}

      <div className="flex flex-col gap-3 rounded-lg border p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-2" role="group" aria-label="How to talk">
          <Button
            size="sm"
            variant={voice.holdMode ? "outline" : "secondary"}
            aria-pressed={!voice.holdMode}
            onClick={() => voice.setHoldMode(false)}
          >
            Hands-free
          </Button>
          <Button
            size="sm"
            variant={voice.holdMode ? "secondary" : "outline"}
            aria-pressed={voice.holdMode}
            onClick={() => voice.setHoldMode(true)}
          >
            Hold to talk
          </Button>
        </div>
        <div className="flex items-center gap-2">
          <Switch
            id="talk-over"
            checked={voice.bargeIn}
            onCheckedChange={voice.setBargeIn}
            aria-describedby="talk-over-help"
          />
          <Label htmlFor="talk-over">Talk over Jarvis to interrupt</Label>
        </div>
      </div>
      <p id="talk-over-help" className="-mt-4 text-xs text-muted-foreground">
        Using speakers without headphones? If Jarvis keeps interrupting itself, turn this off: then
        tap the orb to interrupt instead.
      </p>

      <Transcript lines={voice.view.lines} />
    </div>
  );
}

function HoldToTalk({ holding, onHold }: { holding: boolean; onHold: (down: boolean) => void }) {
  return (
    <Button
      size="lg"
      aria-pressed={holding}
      className="h-16 w-full max-w-xs touch-none select-none text-base"
      onPointerDown={(e) => {
        e.currentTarget.setPointerCapture(e.pointerId);
        onHold(true);
      }}
      onPointerUp={() => onHold(false)}
      onPointerCancel={() => onHold(false)}
      onKeyDown={(e) => {
        if ((e.key === " " || e.key === "Enter") && !e.repeat) {
          e.preventDefault();
          onHold(true);
        }
      }}
      onKeyUp={(e) => {
        if (e.key === " " || e.key === "Enter") {
          e.preventDefault();
          onHold(false);
        }
      }}
      onBlur={() => onHold(false)}
      onContextMenu={(e) => e.preventDefault()}
    >
      <Mic /> {holding ? "Listening… let go to send" : "Hold to talk"}
    </Button>
  );
}

function Transcript({ lines }: { lines: TranscriptLine[] }) {
  const [announcement, setAnnouncement] = useState("");
  const announced = useRef(0);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const done = lines.filter((l) => !l.open && l.role !== "filler" && l.id > announced.current);
    const last = done[done.length - 1];
    if (last) {
      announced.current = last.id;
      setAnnouncement(`${last.role === "user" ? "You" : "Jarvis"}: ${last.text}`);
    }
    end.current?.scrollIntoView?.({ block: "nearest" });
  }, [lines]);

  if (!lines.length) return null;
  return (
    <section aria-label="Transcript" className="space-y-2">
      <ol className="space-y-2">
        {lines.map((line) => (
          <li
            key={line.id}
            className={cn(
              "max-w-[85%] rounded-lg px-3 py-2 text-sm",
              line.role === "user" && "ml-auto bg-primary text-primary-foreground",
              line.role === "assistant" && "bg-muted",
              line.role === "filler" && "italic text-muted-foreground",
              line.role === "error" && "border border-destructive/40 text-destructive",
            )}
          >
            <span className="sr-only">{line.role === "user" ? "You: " : "Jarvis: "}</span>
            {line.text}
            {line.interrupted ? <span className="text-muted-foreground"> …</span> : null}
          </li>
        ))}
      </ol>
      <div ref={end} />
      <p className="sr-only" aria-live="polite">
        {announcement}
      </p>
    </section>
  );
}
