import { useQuery } from "@tanstack/react-query";

import { api } from "./api";
import type { AuthStatus, MailStatus, SystemStatus } from "./types";

export const keys = {
  auth: ["auth"] as const,
  status: ["status"] as const,
  actions: (view: string) => ["actions", view] as const,
  conversations: ["conversations"] as const,
  messages: (id: string) => ["messages", id] as const,
  audit: ["audit"] as const,
  profile: ["profile"] as const,
  facts: (filter: string) => ["facts", filter] as const,
  onboarding: ["onboarding"] as const,
  transcript: (id: string) => ["transcript", id] as const,
  sources: ["sources"] as const,
  google: ["google"] as const,
  models: ["models"] as const,
  policies: ["policies"] as const,
  passkeys: ["passkeys"] as const,
  push: ["push"] as const,
  voice: ["voice"] as const,
  telegram: ["telegram"] as const,
  mail: ["mail"] as const,
  mailStatus: ["mail", "status"] as const,
  threads: (tab: string) => ["mail", "threads", tab] as const,
  thread: (id: string) => ["mail", "thread", id] as const,
};

export function useAuthStatus() {
  return useQuery({ queryKey: keys.auth, queryFn: () => api.get<AuthStatus>("/api/auth/status") });
}

export function useMailStatus() {
  return useQuery({
    queryKey: keys.mailStatus,
    queryFn: () => api.get<MailStatus>("/api/mail/status"),
    refetchInterval: 30_000,
  });
}

export function useSystemStatus() {
  return useQuery({
    queryKey: keys.status,
    queryFn: () => api.get<SystemStatus>("/api/status"),
    refetchInterval: 15_000,
  });
}
