import { Mail } from "lucide-react";

import { changed, lineDiff } from "@/lib/diff";
import type { ActionEmail } from "@/lib/types";
import { cn } from "@/lib/utils";

interface EmailFields {
  to?: unknown;
  cc?: unknown;
  bcc?: unknown;
  subject?: unknown;
  body?: unknown;
}

function list(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

/**
 * An email as it would be sent: exactly the approved fields, as plain text.
 * Shows the email it answers, and what you changed from Jarvis's draft.
 */
export function EmailPreview({
  payload,
  email,
}: {
  payload: EmailFields;
  email?: ActionEmail | null;
}) {
  const body = typeof payload.body === "string" ? payload.body : "";
  const rows: [string, string[]][] = [
    ["To", list(payload.to)],
    ["Cc", list(payload.cc)],
    ["Bcc", list(payload.bcc)],
  ];
  const edited = changed(email?.original_body, body);
  return (
    <div className="space-y-3 text-sm" aria-label="Email preview">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        {rows
          .filter(([, addresses]) => addresses.length)
          .map(([label, addresses]) => (
            <div key={label} className="contents">
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="break-all font-medium">{addresses.join(", ")}</dd>
            </div>
          ))}
        <dt className="text-muted-foreground">Subject</dt>
        <dd className="break-words font-medium">{String(payload.subject ?? "")}</dd>
      </dl>
      <div className="whitespace-pre-wrap break-words rounded-md border bg-background p-3">
        {body}
      </div>
      {edited && email?.original_body ? (
        <details className="rounded-md border p-3">
          <summary className="cursor-pointer select-none font-medium">
            What you changed from Jarvis's draft
          </summary>
          <ul className="mt-2 space-y-0.5 font-mono text-xs" aria-label="Changes">
            {lineDiff(email.original_body, body).map((line, index) => (
              <li
                key={index}
                className={cn(
                  "whitespace-pre-wrap break-words rounded px-1",
                  line.kind === "added" && "bg-success/15",
                  line.kind === "removed" && "bg-destructive/15 line-through",
                )}
              >
                <span className="sr-only">
                  {line.kind === "added" ? "Added: " : line.kind === "removed" ? "Removed: " : ""}
                </span>
                {line.kind === "added" ? "+ " : line.kind === "removed" ? "− " : "  "}
                {line.text || " "}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {email?.replying_to ? (
        <details className="rounded-md border p-3">
          <summary className="flex cursor-pointer select-none items-center gap-2 font-medium">
            <Mail className="size-4" /> In reply to {email.replying_to.sender}:{" "}
            {email.replying_to.subject}
          </summary>
          <p className="mt-1 text-xs text-muted-foreground">
            {email.replying_to.sender_address} · {new Date(email.replying_to.date).toLocaleString()}
          </p>
          <div className="mt-2 whitespace-pre-wrap break-words text-muted-foreground">
            {email.replying_to.text}
          </div>
        </details>
      ) : null}
    </div>
  );
}
