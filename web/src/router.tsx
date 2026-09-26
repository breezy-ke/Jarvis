import type { QueryClient } from "@tanstack/react-query";
import {
  createRootRouteWithContext,
  createRoute,
  createRouter,
  Outlet,
  redirect,
} from "@tanstack/react-router";

import { AppShell } from "./components/AppShell";
import { api } from "./lib/api";
import { isTab } from "./lib/mail";
import { keys } from "./lib/queries";
import type { AuthStatus, InboxTab } from "./lib/types";
import { ActivityPage } from "./routes/Activity";
import { ApprovalsPage } from "./routes/Approvals";
import { ChatPage } from "./routes/Chat";
import { HomePage } from "./routes/Home";
import { InboxPage, ThreadPage } from "./routes/Inbox";
import { LoginPage } from "./routes/Login";
import { MemoryPage } from "./routes/Memory";
import { OnboardingModulePage, OnboardingPage } from "./routes/Onboarding";
import { SettingsPage } from "./routes/Settings";
import { SetupPage } from "./routes/Setup";
import { SourcesPage } from "./routes/Sources";
import { TalkPage } from "./routes/Talk";

interface RouterContext {
  queryClient: QueryClient;
}

const rootRoute = createRootRouteWithContext<RouterContext>()({ component: () => <Outlet /> });

async function authStatus(queryClient: QueryClient): Promise<AuthStatus> {
  return queryClient.fetchQuery({
    queryKey: keys.auth,
    queryFn: () => api.get<AuthStatus>("/api/auth/status"),
    staleTime: 5_000,
  });
}

const setupRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/setup",
  component: SetupPage,
  beforeLoad: async ({ context }) => {
    const status = await authStatus(context.queryClient);
    if (status.setup_complete && status.authenticated) throw redirect({ to: "/" });
    if (status.setup_complete) throw redirect({ to: "/login" });
  },
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  component: LoginPage,
  beforeLoad: async ({ context }) => {
    const status = await authStatus(context.queryClient);
    if (!status.setup_complete) throw redirect({ to: "/setup" });
    if (status.authenticated) throw redirect({ to: "/" });
  },
});

const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "app",
  component: AppShell,
  beforeLoad: async ({ context }) => {
    const status = await authStatus(context.queryClient);
    if (!status.setup_complete) throw redirect({ to: "/setup" });
    if (!status.authenticated) throw redirect({ to: "/login" });
  },
});

const homeRoute = createRoute({ getParentRoute: () => appRoute, path: "/", component: HomePage });

const chatRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/chat",
  component: ChatPage,
  validateSearch: (search: Record<string, unknown>): { c?: string } =>
    typeof search.c === "string" ? { c: search.c } : {},
});

const talkRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/talk",
  component: TalkPage,
  validateSearch: (search: Record<string, unknown>): { c?: string } =>
    typeof search.c === "string" ? { c: search.c } : {},
});

const inboxRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/inbox",
  component: InboxPage,
  validateSearch: (search: Record<string, unknown>): { tab?: InboxTab } =>
    isTab(search.tab) ? { tab: search.tab } : {},
});

const threadRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/inbox/$threadId",
  component: ThreadPage,
});

const approvalsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/approvals",
  component: ApprovalsPage,
});

const activityRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/activity",
  component: ActivityPage,
});

const onboardingRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/onboarding",
  component: OnboardingPage,
});

export const onboardingModuleRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/onboarding/$moduleId",
  component: OnboardingModulePage,
});

const memoryRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/memory",
  component: MemoryPage,
});

const sourcesRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/sources",
  component: SourcesPage,
});

const settingsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/settings",
  component: SettingsPage,
});

const routeTree = rootRoute.addChildren([
  setupRoute,
  loginRoute,
  appRoute.addChildren([
    homeRoute,
    chatRoute,
    talkRoute,
    inboxRoute,
    threadRoute,
    approvalsRoute,
    activityRoute,
    onboardingRoute,
    onboardingModuleRoute,
    memoryRoute,
    sourcesRoute,
    settingsRoute,
  ]),
]);

export const router = createRouter({
  routeTree,
  context: { queryClient: undefined as unknown as QueryClient },
  defaultPreload: "intent",
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
