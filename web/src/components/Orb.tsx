import type { Ref } from "react";

import { cn } from "@/lib/utils";

export type OrbState = "off" | "connecting" | "ready" | "listening" | "thinking" | "speaking";

/**
 * Jarvis's presence indicator. It breathes while Jarvis is thinking; in a voice
 * conversation it also shows listening and speaking, following the sound level
 * the Talk page writes to its `--level` CSS variable.
 */
export function Orb({
  size = 32,
  thinking = false,
  state,
  className,
  ref,
}: {
  size?: number;
  thinking?: boolean;
  state?: OrbState;
  className?: string;
  ref?: Ref<HTMLSpanElement>;
}) {
  return (
    <span
      ref={ref}
      aria-hidden="true"
      className={cn(
        "orb inline-block shrink-0 rounded-full",
        (thinking || state === "thinking") && "orb-thinking",
        state && `orb-voice orb-${state}`,
        className,
      )}
      style={{ width: size, height: size }}
    />
  );
}
