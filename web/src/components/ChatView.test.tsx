import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ChatView } from "./ChatView";

describe("ChatView", () => {
  it("sends on Enter and keeps Shift+Enter for new lines", async () => {
    Element.prototype.scrollIntoView = vi.fn();
    const onSend = vi.fn();
    render(<ChatView lines={[]} streaming={null} busy={false} error={null} onSend={onSend} />);
    const box = screen.getByLabelText("Message");
    await userEvent.type(box, "line one{Shift>}{Enter}{/Shift}line two{Enter}");
    expect(onSend).toHaveBeenCalledWith("line one\nline two");
  });

  it("shows the streaming reply and errors", () => {
    Element.prototype.scrollIntoView = vi.fn();
    render(
      <ChatView
        lines={[{ id: "1", role: "user", content: "hi" }]}
        streaming="Hello th"
        busy
        error="Model offline"
        onSend={() => undefined}
      />,
    );
    expect(screen.getByText("Hello th")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Model offline");
  });
});
