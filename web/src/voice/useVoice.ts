import { useCallback, useEffect, useReducer, useRef, useState } from "react";

import { BrowserAudio } from "./audio";
import { VoiceClient, voiceSocketUrl, type ClientStatus } from "./client";
import { emptyView, reduce, type ServerState } from "./protocol";

/** What the orb shows. */
export type Phase = "off" | "connecting" | "ready" | "listening" | "thinking" | "speaking";

export function phaseOf(status: ClientStatus, server: ServerState, playing: boolean): Phase {
  if (status.kind === "connecting") return "connecting";
  if (status.kind !== "live") return "off";
  if (playing || server === "speaking") return "speaking";
  if (server === "thinking") return "thinking";
  if (server === "listening") return "listening";
  return "ready";
}

function readFlag(key: string, fallback: boolean): boolean {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value === "1";
  } catch {
    return fallback;
  }
}

function useStoredFlag(key: string, fallback: boolean) {
  const [value, setValue] = useState(() => readFlag(key, fallback));
  const update = useCallback(
    (next: boolean) => {
      setValue(next);
      try {
        localStorage.setItem(key, next ? "1" : "0");
      } catch {
        // private browsing: the choice just isn't remembered
      }
    },
    [key],
  );
  return [value, update] as const;
}

const SPEAKING_TAIL_MS = 300; // bridges the short gaps between spoken sentences

/** A live voice conversation for the Talk page. */
export function useVoice(conversationId?: string) {
  const [view, dispatch] = useReducer(reduce, emptyView);
  const [status, setStatus] = useState<ClientStatus>({ kind: "idle" });
  const [playing, setPlaying] = useState(false);
  const [holding, setHolding] = useState(false);
  const [bargeIn, setBargeIn] = useStoredFlag("jarvis.voice.talkOver", true);
  const [holdMode, setHoldMode] = useStoredFlag("jarvis.voice.holdToTalk", false);
  const client = useRef<VoiceClient | null>(null);
  const conversation = useRef<string | undefined>(conversationId);
  const quietTimer = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (view.conversationId) conversation.current = view.conversationId;
  }, [view.conversationId]);

  const onPlayback = useCallback((now: boolean) => {
    window.clearTimeout(quietTimer.current);
    if (now) setPlaying(true);
    else quietTimer.current = window.setTimeout(() => setPlaying(false), SPEAKING_TAIL_MS);
  }, []);

  const start = useCallback(() => {
    client.current?.close();
    const audio = new BrowserAudio();
    audio.prime(); // still inside the tap: lets sound play on iPhone
    const next = new VoiceClient({
      url: voiceSocketUrl(window.location, conversation.current),
      audio,
      onEvent: dispatch,
      onStatus: setStatus,
      onPlayback,
    });
    next.bargeIn = bargeIn;
    next.holdToTalk = holdMode;
    client.current = next;
    setHolding(false);
    next.connect();
  }, [bargeIn, holdMode, onPlayback]);

  const stop = useCallback(() => {
    client.current?.close();
    client.current = null;
    setHolding(false);
    setPlaying(false);
  }, []);

  const interrupt = useCallback(() => client.current?.interrupt(), []);

  const hold = useCallback((down: boolean) => {
    client.current?.hold(down);
    setHolding(down);
  }, []);

  const levels = useCallback(() => client.current?.levels ?? { input: 0, output: 0 }, []);

  useEffect(() => {
    if (client.current) client.current.bargeIn = bargeIn;
  }, [bargeIn]);

  useEffect(() => {
    if (client.current) client.current.holdToTalk = holdMode;
  }, [holdMode]);

  // Keep the screen on while talking (phones otherwise sleep mid-conversation).
  useEffect(() => {
    if (status.kind !== "live" || !("wakeLock" in navigator)) return;
    let lock: WakeLockSentinel | null = null;
    let cancelled = false;
    navigator.wakeLock
      .request("screen")
      .then((sentinel) => {
        if (cancelled) void sentinel.release();
        else lock = sentinel;
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
      void lock?.release();
    };
  }, [status.kind]);

  useEffect(
    () => () => {
      window.clearTimeout(quietTimer.current);
      client.current?.close();
    },
    [],
  );

  return {
    view,
    status,
    phase: phaseOf(status, view.serverState, playing),
    error: status.kind === "closed" ? status.reason : null,
    holding,
    bargeIn,
    setBargeIn,
    holdMode,
    setHoldMode,
    start,
    stop,
    interrupt,
    hold,
    levels,
  };
}
