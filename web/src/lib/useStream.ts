import { useCallback, useRef, useState } from "react";

import { stream } from "./api";

interface Options {
  onStart?: (data: Record<string, unknown>) => void;
  onDone?: (data: Record<string, unknown>) => void;
}

/** Run one streamed request at a time, exposing the partial text as it arrives. */
export function useStream() {
  const [text, setText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);

  const run = useCallback(async (path: string, body: unknown, options: Options = {}) => {
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setBusy(true);
    setError(null);
    setText("");
    let finalText: string | null = null;
    try {
      for await (const event of stream(path, body, abort.signal)) {
        if (event.event === "start") options.onStart?.(event.data);
        else if (event.event === "delta") setText((t) => (t ?? "") + String(event.data.text ?? ""));
        else if (event.event === "done") {
          finalText = String(event.data.text ?? "");
          options.onDone?.(event.data);
        } else if (event.event === "error")
          setError(String(event.data.message ?? "Something went wrong."));
      }
    } catch (e) {
      if (!abort.signal.aborted) setError(e instanceof Error ? e.message : "Connection lost.");
    } finally {
      setBusy(false);
      setText(null);
    }
    return finalText;
  }, []);

  const stop = useCallback(() => controller.current?.abort(), []);

  return { text, busy, error, run, stop };
}
