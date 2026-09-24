import AxeBuilder from "@axe-core/playwright";
import { expect, test, type BrowserContext, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";

import { serverEnv } from "./env";

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
  const box = page.getByLabel("Message");
  await box.fill(text);
  await box.press("Enter");
}

async function expectAccessible(page: Page, label: string) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21aa"]).analyze();
  const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
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

  test("the activity log is intact", async () => {
    await page.goto("/activity");
    await page.getByRole("button", { name: "Verify integrity" }).click();
    await expect(page.getByText("Log intact")).toBeVisible();
    await expectAccessible(page, "activity");
  });

  test("phone layout works and stays accessible", async () => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/");
    const bottomNav = page.getByRole("navigation", { name: "Main" }).last();
    await expect(bottomNav).toBeVisible();
    await bottomNav.getByRole("link", { name: "Approvals" }).click();
    await expect(page.getByRole("heading", { name: "Approvals" })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    expect(overflow).toBe(false);
    for (const path of ["/", "/settings", "/sources", "/onboarding"]) {
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
