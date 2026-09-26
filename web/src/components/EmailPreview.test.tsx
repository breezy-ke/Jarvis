import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EmailPreview } from "./EmailPreview";

const payload = {
  to: ["achieng@client.co.ke"],
  cc: [],
  bcc: [],
  subject: "Re: Kickoff next week",
  body: "Hi Achieng,\nWednesday works.\nBrian",
};

describe("EmailPreview", () => {
  it("shows exactly who it goes to and what it says, as plain text", () => {
    render(<EmailPreview payload={{ ...payload, body: "<b>not bold</b>" }} />);
    const preview = screen.getByLabelText("Email preview");
    expect(within(preview).getByText("achieng@client.co.ke")).toBeInTheDocument();
    expect(within(preview).getByText("Re: Kickoff next week")).toBeInTheDocument();
    expect(within(preview).getByText("<b>not bold</b>")).toBeInTheDocument();
    expect(within(preview).queryByText("Cc")).not.toBeInTheDocument();
  });

  it("shows what you changed from Jarvis's draft, and the email it answers", () => {
    render(
      <EmailPreview
        payload={payload}
        email={{
          thread_id: "t1",
          draft_id: "d1",
          original_body: "Hi Achieng,\nTuesday works.\nBrian",
          replying_to: {
            sender: "Achieng Otieno",
            sender_address: "achieng@client.co.ke",
            subject: "Kickoff next week",
            date: "2026-01-05T09:00:00+00:00",
            text: "Can we meet on Tuesday?",
          },
        }}
      />,
    );
    const changes = screen.getByLabelText("Changes");
    expect(within(changes).getByText(/Removed:/).parentElement).toHaveTextContent("Tuesday works.");
    expect(within(changes).getByText(/Added:/).parentElement).toHaveTextContent("Wednesday works.");
    expect(screen.getByText(/In reply to Achieng Otieno/)).toBeInTheDocument();
    expect(screen.getByText("Can we meet on Tuesday?")).toBeInTheDocument();
  });

  it("says nothing about changes when you sent Jarvis's draft as it was", () => {
    render(
      <EmailPreview
        payload={payload}
        email={{ thread_id: "t1", draft_id: "d1", original_body: payload.body, replying_to: null }}
      />,
    );
    expect(screen.queryByText(/What you changed/)).not.toBeInTheDocument();
  });
});
