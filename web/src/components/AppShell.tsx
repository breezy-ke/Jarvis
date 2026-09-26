import { useQueryClient } from "@tanstack/react-query";
import { Link, Outlet, useNavigate } from "@tanstack/react-router";
import {
  Brain,
  CheckCircle2,
  History,
  Home,
  Inbox,
  MessageSquare,
  Mic,
  Settings,
  ShieldAlert,
  Sparkles,
  UserRoundSearch,
} from "lucide-react";
import { useEffect } from "react";

import { Orb } from "@/components/Orb";
import { KillSwitchBanner } from "@/components/KillSwitch";
import { UNAUTHORIZED_EVENT } from "@/lib/api";
import { keys, useMailStatus, useSystemStatus } from "@/lib/queries";
import { cn } from "@/lib/utils";

interface NavItem {
  to: string;
  label: string;
  icon: React.ReactNode;
  badge?: number;
  mobile?: boolean;
}

export function AppShell() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { data: status } = useSystemStatus();
  const { data: mail } = useMailStatus();

  useEffect(() => {
    const onUnauthorized = () => {
      void queryClient.invalidateQueries({ queryKey: keys.auth });
      void navigate({ to: "/login" });
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [queryClient, navigate]);

  const nav: NavItem[] = [
    { to: "/", label: "Home", icon: <Home />, mobile: true },
    {
      to: "/inbox",
      label: "Inbox",
      icon: <Inbox />,
      badge: mail?.access === "none" ? undefined : mail?.counts.attention,
      mobile: true,
    },
    { to: "/chat", label: "Chat", icon: <MessageSquare />, mobile: true },
    { to: "/talk", label: "Talk", icon: <Mic />, mobile: true },
    {
      to: "/approvals",
      label: "Approvals",
      icon: <CheckCircle2 />,
      badge: status?.pending_approvals,
      mobile: true,
    },
    {
      to: "/memory",
      label: "What I know",
      icon: <Brain />,
      badge: status?.memories_to_review,
    },
    { to: "/onboarding", label: "Onboarding", icon: <Sparkles /> },
    { to: "/sources", label: "Sources", icon: <UserRoundSearch /> },
    { to: "/activity", label: "Activity", icon: <History /> },
    { to: "/settings", label: "Settings", icon: <Settings /> },
  ];

  return (
    <div className="flex min-h-dvh">
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-primary px-3 py-2 text-primary-foreground focus:not-sr-only focus:absolute focus:left-3 focus:top-3"
      >
        Skip to content
      </a>
      <aside className="sticky top-0 hidden h-dvh w-64 shrink-0 flex-col border-r bg-card/40 p-4 md:flex">
        <Link to="/" className="mb-6 flex items-center gap-3 px-2">
          <Orb size={28} />
          <span className="text-lg font-semibold tracking-tight">
            {status?.assistant_name ?? "Jarvis"}
          </span>
        </Link>
        <nav aria-label="Main" className="flex flex-1 flex-col gap-1">
          {nav.map((item) => (
            <NavLink key={item.to} item={item} />
          ))}
        </nav>
        {status?.kill_switch.engaged ? (
          <p className="mt-4 flex items-center gap-2 rounded-md bg-destructive/10 p-2 text-xs text-destructive">
            <ShieldAlert className="size-4" /> Kill switch on
          </p>
        ) : null}
        <p className="mt-4 px-2 text-xs text-muted-foreground">v{status?.version ?? "…"}</p>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex items-center justify-between border-b bg-background/85 px-4 py-3 backdrop-blur md:hidden">
          <Link to="/" className="flex items-center gap-2">
            <Orb size={24} />
            <span className="font-semibold">{status?.assistant_name ?? "Jarvis"}</span>
          </Link>
          <div className="flex items-center gap-4">
            <Link
              to="/onboarding"
              className="text-sm text-muted-foreground"
              aria-label="Onboarding"
            >
              <Sparkles className="size-5" />
            </Link>
            <Link to="/settings" className="text-sm text-muted-foreground" aria-label="Settings">
              <Settings className="size-5" />
            </Link>
          </div>
        </header>
        <KillSwitchBanner />
        <main
          id="main"
          className="mx-auto w-full max-w-5xl flex-1 px-4 pb-28 pt-4 md:px-8 md:pb-10 md:pt-8"
        >
          <Outlet />
        </main>
        <nav
          aria-label="Main"
          className="pb-safe fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t bg-background/95 backdrop-blur md:hidden"
        >
          {nav
            .filter((n) => n.mobile)
            .map((item) => (
              <Link
                key={item.to}
                to={item.to}
                activeOptions={{ exact: item.to === "/" }}
                className="relative flex flex-col items-center gap-1 px-1 pt-2 text-[11px] text-muted-foreground [&.active]:text-primary [&_svg]:size-5"
              >
                {item.icon}
                <span className="truncate">{item.label}</span>
                {item.badge ? (
                  <span className="absolute right-[22%] top-1 min-w-4 rounded-full bg-primary px-1 text-center text-[10px] font-semibold text-primary-foreground">
                    {item.badge}
                  </span>
                ) : null}
              </Link>
            ))}
        </nav>
      </div>
    </div>
  );
}

function NavLink({ item }: { item: NavItem }) {
  return (
    <Link
      to={item.to}
      activeOptions={{ exact: item.to === "/" }}
      className={cn(
        "flex items-center gap-3 rounded-md px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground [&.active]:bg-accent [&.active]:text-accent-foreground [&_svg]:size-4",
      )}
    >
      {item.icon}
      <span className="flex-1">{item.label}</span>
      {item.badge ? (
        <span className="rounded-full bg-primary px-2 text-xs font-semibold text-primary-foreground">
          {item.badge}
        </span>
      ) : null}
    </Link>
  );
}
