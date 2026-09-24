import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Markdown } from "./Markdown";

describe("Markdown", () => {
  it("never renders images, so replies can't load remote content", () => {
    const { container } = render(
      <Markdown>{"Hi ![track](https://evil.example/pixel.png)"}</Markdown>,
    );
    expect(container.querySelector("img")).toBeNull();
  });

  it("drops raw HTML", () => {
    const { container } = render(<Markdown>{"<script>alert(1)</script><b>bold?</b>"}</Markdown>);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
  });

  it("neutralises javascript: links and hardens real ones", () => {
    const { container } = render(
      <Markdown>{"[bad](javascript:alert(1)) and [good](https://example.com)"}</Markdown>,
    );
    const links = Array.from(container.querySelectorAll("a"));
    expect(links.some((a) => a.getAttribute("href")?.startsWith("javascript"))).toBe(false);
    const good = links.find((a) => a.getAttribute("href") === "https://example.com");
    expect(good?.getAttribute("rel")).toContain("noopener");
    expect(good?.getAttribute("target")).toBe("_blank");
  });
});
