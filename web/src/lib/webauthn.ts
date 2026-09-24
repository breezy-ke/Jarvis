/** Passkey ceremonies. The browser does the cryptography; the server verifies everything. */
import {
  browserSupportsWebAuthn,
  startAuthentication,
  startRegistration,
} from "@simplewebauthn/browser";

import { api } from "./api";

interface Challenge {
  challenge_id: string;
  // Server-generated WebAuthn options (JSON form); passed through unchanged.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  options: any;
}

export function passkeysSupported(): boolean {
  return browserSupportsWebAuthn();
}

export async function registerPasskey(opts: {
  setupToken?: string;
  deviceName: string;
}): Promise<{ recovery_codes: string[] | null }> {
  const challenge = await api.post<Challenge>("/api/auth/register/options", {
    setup_token: opts.setupToken ?? null,
  });
  const credential = await startRegistration({ optionsJSON: challenge.options });
  return api.post("/api/auth/register/verify", {
    challenge_id: challenge.challenge_id,
    credential,
    device_name: opts.deviceName,
    setup_token: opts.setupToken ?? null,
  });
}

export async function loginWithPasskey(): Promise<void> {
  const challenge = await api.post<Challenge>("/api/auth/login/options");
  const credential = await startAuthentication({ optionsJSON: challenge.options });
  await api.post("/api/auth/login/verify", { challenge_id: challenge.challenge_id, credential });
}

/** A fresh passkey tap, required before each high-risk approval. */
export async function confirmWithPasskey(): Promise<void> {
  const challenge = await api.post<Challenge>("/api/auth/step-up/options");
  const credential = await startAuthentication({ optionsJSON: challenge.options });
  await api.post("/api/auth/step-up/verify", { challenge_id: challenge.challenge_id, credential });
}

export function guessDeviceName(): string {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return "iPhone";
  if (/iPad/.test(ua)) return "iPad";
  if (/Android/.test(ua)) return "Android phone";
  if (/Windows/.test(ua)) return "Windows PC";
  if (/Mac OS X/.test(ua)) return "Mac";
  return "Passkey";
}
