import { ExternalLink, ThumbsDown, ThumbsUp } from "lucide-react";

import { Button } from "@/components/ui/button";
import { host, safeHref } from "@/lib/brief";
import type { BriefEntry } from "@/lib/types";
import { cn } from "@/lib/utils";

export type Vote = 1 | -1 | null;

/** The story's title, linking to its source's own page (in a new tab). */
export function StoryLink({ entry, className }: { entry: BriefEntry; className?: string }) {
  const href = safeHref(entry.url);
  if (!href) return <span className={cn("font-medium", className)}>{entry.title}</span>;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={cn("font-medium underline-offset-2 hover:underline", className)}
    >
      {entry.title}
      <ExternalLink aria-hidden className="ml-1 inline size-3 align-baseline opacity-60" />
      <span className="sr-only"> (opens {host(href)} in a new tab)</span>
    </a>
  );
}

export function VoteButtons({
  entry,
  onVote,
  disabled,
}: {
  entry: BriefEntry;
  onVote: (vote: Vote) => void;
  disabled?: boolean;
}) {
  const up = entry.vote === 1;
  const down = entry.vote === -1;
  return (
    <div role="group" aria-label={`Your vote on “${entry.title}”`} className="flex shrink-0 gap-1">
      <Button
        variant={up ? "secondary" : "ghost"}
        size="sm"
        aria-pressed={up}
        title="More like this"
        onClick={() => onVote(up ? null : 1)}
        disabled={disabled}
      >
        <ThumbsUp className={up ? "text-primary" : undefined} />
        <span className="sr-only">More like this</span>
      </Button>
      <Button
        variant={down ? "secondary" : "ghost"}
        size="sm"
        aria-pressed={down}
        title="Less like this"
        onClick={() => onVote(down ? null : -1)}
        disabled={disabled}
      >
        <ThumbsDown className={down ? "text-destructive" : undefined} />
        <span className="sr-only">Less like this</span>
      </Button>
    </div>
  );
}

export function BriefStory({
  entry,
  onVote,
  voting,
}: {
  entry: BriefEntry;
  onVote?: (vote: Vote) => void;
  voting?: boolean;
}) {
  const titleId = `story-${entry.id}`;
  return (
    <article aria-labelledby={titleId} className="space-y-2 rounded-lg border bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <h3 id={titleId} className="leading-snug">
            {entry.section === "top" ? (
              <span className="mr-1 text-muted-foreground">{entry.rank}.</span>
            ) : null}
            <StoryLink entry={entry} />
          </h3>
          <p className="text-xs text-muted-foreground">{entry.source}</p>
        </div>
        {onVote ? <VoteButtons entry={entry} onVote={onVote} disabled={voting} /> : null}
      </div>
      {entry.summary ? <p className="text-sm">{entry.summary}</p> : null}
      {entry.watch.length ? (
        <ul className="space-y-1 text-sm" aria-label="What's affected">
          {entry.watch.map((line) => (
            <li key={line} className="font-mono text-xs">
              {line}
            </li>
          ))}
        </ul>
      ) : null}
      {entry.why ? (
        <p className="border-l-2 border-primary/50 pl-3 text-sm">
          <span className="font-medium">Why it matters: </span>
          {entry.why}
        </p>
      ) : null}
      {entry.client ? (
        <p className="border-l-2 border-warning/60 pl-3 text-sm">
          <span className="font-medium">Client angle: </span>
          {entry.client}
        </p>
      ) : null}
      {entry.reasons.length ? (
        <details className="text-xs text-muted-foreground">
          <summary className="w-fit cursor-pointer select-none">Why this?</summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {entry.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </article>
  );
}
