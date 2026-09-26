import AxeBuilder from "@axe-core/playwright";
import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";

import { fakeGoogle, serverEnv } from "./env";

function setupCode(): string {
  const out = execFileSync("uv", ["run", "--project", "../core", "jarvis", "setup-token"], {
    env: serverEnv,
    encoding: "utf8",
  });
  const match = out.match(/Setup code \(valid 24 hours\): (\S+)/);
  if (!match?.[1]) throw new Error(`no setup code in: ${out}`);
  return match[1];
}

async function send(page: Page, text: string) {
  // The composer ignores Enter while Jarvis is still answering, so wait for that
  // answer to finish (its Stop button goes away) instead of just its first words.
  await expect(page.getByRole("button", { name: "Stop", exact: true })).toHaveCount(0);
  const box = page.getByLabel("Message");
  await box.fill(text);
  await box.press("Enter");
}

async function expectAccessible(page: Page, label: string) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  const serious = results.violations.filter(
    (v) => v.impact === "serious" || v.impact === "critical",
  );
  expect(
    serious.map((v) => `${label}: ${v.id} (${v.nodes.map((n) => n.target.join(" ")).join(", ")})`),
  ).toEqual([]);
}

test.describe.serial("Jarvis end to end", () => {
  let context: BrowserContext;
  let page: Page;

  test.beforeAll(async ({ browser }) => {
    context = await browser.newContext();
    page = await context.newPage();
    const cdp = await context.newCDPSession(page);
    await cdp.send("WebAuthn.enable");
    await cdp.send("WebAuthn.addVirtualAuthenticator", {
      options: {
        protocol: "ctap2",
        transport: "internal",
        hasResidentKey: true,
        hasUserVerification: true,
        isUserVerified: true,
        automaticPresenceSimulation: true,
      },
    });
  });

  test.afterAll(async () => {
    await context.close();
  });

  test("first run: setup code, passkey and recovery codes", async () => {
    await page.goto("/");
    await expect(page).toHaveURL(/\/setup$/);
    await expectAccessible(page, "setup");
    await page.getByLabel("Setup code").fill("wrong-code");
    await page.getByRole("button", { name: "Create passkey" }).click();
    await expect(page.getByRole("alert")).toContainText("setup code");
    await page.getByLabel("Setup code").fill(setupCode());
    await page.getByRole("button", { name: "Create passkey" }).click();
    await expect(page.getByText("Save your recovery codes now")).toBeVisible();
    await expect(page.getByRole("list", { name: "Recovery codes" }).locator("li")).toHaveCount(10);
    await page.getByLabel("I've stored these somewhere safe").check();
    await page.getByRole("button", { name: "Continue to onboarding" }).click();
    await expect(page.getByRole("heading", { name: "Getting to know you" })).toBeVisible();
  });

  test("onboarding saves answers into the profile", async () => {
    await page.getByRole("link", { name: /Start: About you/ }).click();
    await page.getByRole("button", { name: "Begin" }).click();
    await expect(page.getByRole("log")).toContainText("Understood");
    await send(
      page,
      '/tool save_answer {"field": "identity.preferred_name", "value_json": "\\"Brian\\""}',
    );
    await expect(page.getByRole("log")).toContainText("Saved identity.preferred_name");
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Brian");
    await expectAccessible(page, "home");
  });

  test("chat remembers things and the memory page shows them", async () => {
    await page.goto("/chat");
    await send(
      page,
      '/tool remember {"subject": "owner", "predicate": "favourite stack", "value": "Next.js with Tailwind", "category": "preference"}',
    );
    await expect(page.getByRole("log")).toContainText("Saved to memory");
    await expect(page).toHaveURL(/\/chat\?c=/);
    await expectAccessible(page, "chat");
    await page.goto("/memory");
    await page.getByRole("tab", { name: "All memories" }).click();
    await expect(page.getByText("Next.js with Tailwind")).toBeVisible();
    await expectAccessible(page, "memory");
  });

  test("an agent's proposal waits for approval, then runs", async () => {
    await page.goto("/chat");
    await send(
      page,
      '/tool propose_action {"kind": "notify.owner", "payload": {"title": "E2E check", "body": "Hello from the test"}, "rationale": "testing approvals"}',
    );
    await expect(page.getByRole("log")).toContainText("Waiting for the owner's approval");
    await page.goto("/approvals");
    await expect(page.getByText("Notify you: E2E check")).toBeVisible();
    await expect(page.getByText(/Autonomy paused/)).toBeVisible();
    await expectAccessible(page, "approvals");
    await page.getByRole("button", { name: "Approve", exact: true }).click();
    await page.getByRole("tab", { name: "History" }).click();
    await expect(page.getByText("executed")).toBeVisible({ timeout: 20_000 });
  });

  test("kill switch pauses everything", async () => {
    await page.goto("/");
    await page.getByRole("button", { name: "Stand down" }).click();
    await page.getByRole("button", { name: "Engage kill switch" }).click();
    await expect(page.getByText("Kill switch is on.")).toBeVisible();
    await page.getByRole("button", { name: "Release", exact: true }).click();
    await expect(page.getByText("Kill switch is on.")).toBeHidden();
  });

  test("talk to Jarvis and hear the answer", async () => {
    await page.goto("/talk");
    await expectAccessible(page, "talk");
    await page.getByRole("button", { name: "Start talking", exact: true }).click();
    // The fake microphone says one sentence; Jarvis's own turn detection decides it's done.
    const transcript = page.getByRole("region", { name: "Transcript" });
    await expect(transcript).toContainText("What is on my calendar today?", { timeout: 30_000 });
    await expect(transcript).toContainText("Understood. You said: What is on my calendar today?", {
      timeout: 30_000,
    });
    await expect(page.getByRole("link", { name: "Open in Chat" })).toBeVisible();
    await page.getByRole("button", { name: "End" }).click();
    await expect(page.getByRole("button", { name: "Start talking", exact: true })).toBeVisible();
    await expectAccessible(page, "talk after a conversation");
  });

  test("settings show voice and Telegram", async () => {
    await page.goto("/settings");
    await expect(page.getByText("Speech server")).toBeVisible();
    await expect(page.getByText("George (British male)")).toBeVisible();
    await expect(page.getByText("Set up your Telegram bot")).toBeVisible();
    await expectAccessible(page, "settings");
  });

  test("the activity log is intact", async () => {
    await page.goto("/activity");
    await page.getByRole("button", { name: "Verify integrity" }).click();
    await expect(page.getByText("Log intact")).toBeVisible();
    await expectAccessible(page, "activity");
  });

  test("email: connect Google, see it sorted, reply, undo, then send", async () => {
    test.setTimeout(180_000);
    const sentMail = async () =>
      (await (await page.request.get(`${fakeGoogle}/__test/sent`)).json()) as {
        count: number;
        messages: { threadId: string; to: string }[];
      };

    // Connect: Google's consent screen opens in a new tab and sends you back to Jarvis.
    await page.goto("/sources");
    const consent = context.waitForEvent("page");
    await page.getByRole("button", { name: "Connect Google" }).click();
    const googleTab = await consent;
    await expect(googleTab.getByRole("heading", { name: "Google connected" })).toBeVisible();
    await googleTab.close();

    // Jarvis reads and sorts the inbox within seconds.
    await page.goto("/inbox");
    const client = page.getByRole("link", { name: /Achieng Otieno/ });
    await expect(async () => {
      await page.reload();
      await expect(client).toBeVisible({ timeout: 2_000 });
    }).toPass({ timeout: 45_000 });
    await expectAccessible(page, "inbox");

    // The invoice hiding instructions for an AI is flagged, not obeyed.
    await page.getByRole("tab", { name: /Suspicious/ }).click();
    await page.getByRole("link", { name: /Invoice 4471 overdue/ }).click();
    await expect(page.getByText("Be careful with this one")).toBeVisible();

    // Reply to the client: write it, save it, look at the approval, send, undo.
    await page.goto("/inbox");
    await client.click();
    await expect(page.getByRole("heading", { name: "Kickoff next week" })).toBeVisible();
    const threadId = page.url().split("/inbox/")[1] ?? "";
    await expectAccessible(page, "conversation");
    await page.getByRole("button", { name: "Write it myself" }).click();
    await page
      .getByLabel("Message", { exact: true })
      .fill("Tuesday at 10:00 works for me. See you then.");
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByRole("button", { name: "Send", exact: true })).toBeVisible();

    await page.goto("/approvals");
    const preview = page.getByLabel("Email preview");
    await expect(preview).toContainText("achieng@client.co.ke");
    await expect(preview).toContainText("Tuesday at 10:00 works for me.");
    await expect(page.getByText(/In reply to Achieng Otieno/)).toBeVisible();
    await expectAccessible(page, "email approval");

    await page.goto(`/inbox/${threadId}`);
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(page.getByText(/Sending in \d+s/)).toBeVisible();
    await page.getByRole("button", { name: "Undo" }).click();
    await expect(page.getByText("Undone: nothing was sent.")).toBeVisible();
    expect((await sentMail()).count).toBe(0);

    // Send again: once the real 60-second undo window passes, it goes out, in the thread.
    await page.getByRole("button", { name: "Send", exact: true }).click();
    await expect(page.getByText(/Sending in \d+s/)).toBeVisible();
    await expect
      .poll(async () => (await sentMail()).count, { timeout: 100_000, intervals: [2_000] })
      .toBe(1);
    expect((await sentMail()).messages[0]).toMatchObject({ threadId, to: "achieng@client.co.ke" });
    await expect(page.getByText(/^Sent\./)).toBeVisible({ timeout: 15_000 });
  });

  test("phone layout works and stays accessible", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    const bottomNav = page.getByRole("navigation", { name: "Main" }).last();
    await expect(bottomNav).toBeVisible();
    await expect(bottomNav.getByRole("link", { name: /Inbox/ })).toBeVisible();
    await bottomNav.getByRole("link", { name: "Approvals" }).click();
    await expect(page.getByRole("heading", { name: "Approvals" })).toBeVisible();
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth,
    );
    expect(overflow).toBe(false);
    for (const path of ["/", "/inbox", "/settings", "/sources", "/onboarding"]) {
      await page.goto(path);
      await expectAccessible(page, `mobile ${path}`);
    }
    await page.setViewportSize({ width: 1280, height: 800 });
  });

  test("sign out, then back in with the passkey", async () => {
    await page.goto("/settings");
    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/login$/);
    await expectAccessible(page, "login");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(page.getByRole("heading", { level: 1 })).toContainText("Brian");
  });
});
