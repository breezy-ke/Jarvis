import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { BriefEntry } from "@/lib/types";

import { BriefStory } from "./BriefStory";

function entry(overrides: Partial<BriefEntry> = {}): BriefEntry {
  return {
    id: "e1",
    section: "top",
    rank: 1,
    title: "Next.js 16.1 is out",
    url: "https://nextjs.org/blog/next-16-1",
    source: "Next.js",
    summary: "Faster builds with Turbopack.",
    why: "Your clinic site runs Next.js.",
    client: "Offer the upgrade to your Nairobi clients.",
    reasons: ["Mentions Next.js", "420 points on Hacker News"],
    watch: [],
    details: {},
    vote: null,
    ...overrides,
  };
}

describe("BriefStory", () => {
  it("links to the source's own page, in a new tab, and says why it matters", () => {
    render(<BriefStory entry={entry()} />);
    const link = screen.getByRole("link", { name: /Next.js 16.1 is out/ });
    expect(link).toHaveAttribute("href", "https://nextjs.org/blog/next-16-1");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(link.getAttribute("rel")).toContain("noreferrer");
    expect(link).toHaveTextContent("opens nextjs.org in a new tab");
    expect(screen.getByText("Your clinic site runs Next.js.")).toBeInTheDocument();
    expect(screen.getByText("Offer the upgrade to your Nairobi clients.")).toBeInTheDocument();
    const why = screen.getByText("Why this?").closest("details");
    expect(why).not.toBeNull();
    expect(within(why as HTMLElement).getByText("420 points on Hacker News")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: /Your vote/ })).not.toBeInTheDocument();
  });

  it("never turns an address that isn't http(s) into a link", () => {
    render(<BriefStory entry={entry({ url: "javascript:alert(1)", title: "Sneaky" })} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.getByText("Sneaky")).toBeInTheDocument();
  });

  it("takes your 👍 and 👎, and a second tap takes it back", async () => {
    const onVote = vi.fn();
    const { rerender } = render(<BriefStory entry={entry()} onVote={onVote} />);
    const more = screen.getByRole("button", { name: "More like this" });
    const less = screen.getByRole("button", { name: "Less like this" });
    expect(more).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(more);
    expect(onVote).toHaveBeenLastCalledWith(1);
    await userEvent.click(less);
    expect(onVote).toHaveBeenLastCalledWith(-1);

    rerender(<BriefStory entry={entry({ vote: 1 })} onVote={onVote} />);
    expect(screen.getByRole("button", { name: "More like this" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await userEvent.click(screen.getByRole("button", { name: "More like this" }));
    expect(onVote).toHaveBeenLastCalledWith(null);
  });

  it("shows the security watch's facts as they are", () => {
    render(
      <BriefStory
        entry={entry({
          section: "security",
          title: "next: file read",
          url: "https://github.com/advisories/GHSA-aaaa-bbbb-cccc",
          watch: ["next < 16.0.4: high severity, fixed in 16.0.4."],
          why: "",
          client: "",
          reasons: [],
        })}
      />,
    );
    expect(
      within(screen.getByRole("list", { name: "What's affected" })).getByText(
        "next < 16.0.4: high severity, fixed in 16.0.4.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Why this?")).not.toBeInTheDocument();
  });
});
