import type { InboxTab, MailCategory } from "./types";

export const INBOX_TABS: { id: InboxTab; label: string }[] = [
  { id: "attention", label: "Needs attention" },
  { id: "leads", label: "Leads" },
  { id: "invoices", label: "Invoices" },
  { id: "fyi", label: "FYI" },
  { id: "newsletters", label: "Newsletters" },
  { id: "suspicious", label: "Suspicious" },
  { id: "all", label: "All" },
];

export const CATEGORY_LABELS: Record<MailCategory, string> = {
  urgent: "Urgent",
  needs_reply: "Needs reply",
  fyi: "FYI",
  newsletter: "Newsletter",
  lead: "Lead",
  invoice: "Invoice",
  suspicious: "Suspicious",
};

export const CATEGORY_VARIANT: Record<
  MailCategory,
  "default" | "secondary" | "warning" | "destructive"
> = {
  urgent: "warning",
  needs_reply: "default",
  fyi: "secondary",
  newsletter: "secondary",
  lead: "default",
  invoice: "default",
  suspicious: "destructive",
};

export function isTab(value: unknown): value is InboxTab {
  return INBOX_TABS.some((tab) => tab.id === value);
}

/** Addresses typed into a To or Cc box: separated by commas, semicolons or spaces. */
export function splitAddresses(value: string): string[] {
  return value
    .split(/[,;\s]+/)
    .map((part) => part.trim())
    .filter(Boolean);
}
