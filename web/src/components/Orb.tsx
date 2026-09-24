import { cn } from "@/lib/utils";

/** Jarvis's presence indicator; it breathes while Jarvis is thinking. */
export function Orb({
  size = 32,
  thinking = false,
  className,
}: {
  size?: number;
  thinking?: boolean;
  className?: string;
}) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "orb inline-block shrink-0 rounded-full",
        thinking && "orb-thinking",
        className,
      )}
      style={{ width: size, height: size }}
    />
  );
}
