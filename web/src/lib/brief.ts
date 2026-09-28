import type { Brief, BriefDelivery, BriefSectionId } from "./types";

export const SECTIONS: { id: BriefSectionId; title: string }[] = [
  { id: "top", title: "Top stories" },
  { id: "security", title: "Security watch" },
  { id: "africa", title: "Kenya and Africa" },
  { id: "quick", title: "Quick hits" },
];

const CHANNELS: Record<string, string> = {
  app: "The app",
  audio: "Audio version",
  push: "Notification",
  telegram: "Telegram",
  voice: "Telegram voice note",
  inbox: "Gmail inbox copy",
};

export function channelLabel(id: string): string {
  return CHANNELS[id] ?? id;
}

/** A link Jarvis may show: plain http(s) only. Anything else is shown as text. */
export function safeHref(url: string): string | null {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
}

export function host(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

/** "Monday 28 September", from "2026-09-28" (a calendar day, whatever your timezone). */
export function longDay(day: string): string {
  const [year = 1970, month = 1, date = 1] = day.split("-").map(Number);
  const utc = new Date(Date.UTC(year, month - 1, date));
  return utc.toLocaleDateString("en-GB", {
    weekday: "long",
    day: "numeric",
    month: "long",
    timeZone: "UTC",
  });
}

/** "07:00", in the timezone Jarvis keeps (your policies' timezone). */
export function clockTime(iso: string, timeZone?: string): string {
  return new Date(iso).toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone,
  });
}

const NOTED = new Set(["inbox", "audio"]);

/** Channels that didn't get the brief, and why, for a note on the page. */
export function deliveryNotes(
  deliveries: Record<string, BriefDelivery>,
): { channel: string; failed: boolean; text: string }[] {
  const notes: { channel: string; failed: boolean; text: string }[] = [];
  for (const [id, delivery] of Object.entries(deliveries)) {
    if (delivery.status === "sent") continue;
    // Skipped on purpose (quiet hours, Telegram not linked…) isn't news. Only say why
    // the inbox copy or the audio version is missing: those you can fix.
    if (delivery.status === "skipped" && !(NOTED.has(id) && delivery.detail)) continue;
    notes.push({
      channel: channelLabel(id),
      failed: delivery.status === "failed",
      text:
        delivery.status === "failed"
          ? `didn't go out yet (${delivery.error ?? "unknown error"}). Jarvis tries again every half hour today.`
          : (delivery.detail ?? "skipped"),
    });
  }
  return notes;
}

export function storyCount(brief: Brief): number {
  return SECTIONS.reduce((total, section) => total + brief.sections[section.id].length, 0);
}
