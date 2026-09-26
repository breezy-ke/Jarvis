import { useEffect, useState } from "react";

/** Seconds left until `target` (an ISO time), ticking every second; 0 once it has passed. */
export function useCountdown(target: string | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!target) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [target]);
  return target ? Math.max(0, Math.ceil((new Date(target).getTime() - now) / 1000)) : 0;
}
