import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Fingerprint, LifeBuoy } from "lucide-react";
import { useState } from "react";

import { Orb } from "@/components/Orb";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Alert } from "@/components/ui/misc";
import { api } from "@/lib/api";
import { loginWithPasskey } from "@/lib/webauthn";

export function LoginPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [recovery, setRecovery] = useState(false);
  const [code, setCode] = useState("");

  async function done() {
    await queryClient.invalidateQueries();
    await navigate({ to: "/" });
  }

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await done();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Sign-in failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-dvh items-center justify-center p-4">
      <Card className="w-full max-w-sm">
        <CardHeader className="items-center text-center">
          <Orb size={56} className="mb-2" />
          <CardTitle className="text-xl">Jarvis</CardTitle>
          <CardDescription>Sign in with your passkey.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {error ? <Alert tone="error">{error}</Alert> : null}
          {!recovery ? (
            <>
              <Button
                size="lg"
                className="w-full"
                disabled={busy}
                onClick={() => void run(loginWithPasskey)}
              >
                <Fingerprint /> {busy ? "Waiting for your passkey…" : "Sign in"}
              </Button>
              <Button variant="link" className="w-full" onClick={() => setRecovery(true)}>
                <LifeBuoy /> Use a recovery code
              </Button>
            </>
          ) : (
            <form
              className="space-y-3"
              onSubmit={(e) => {
                e.preventDefault();
                void run(async () => {
                  await api.post("/api/auth/recovery", { code });
                });
              }}
            >
              <Label htmlFor="recovery-code">Recovery code</Label>
              <Input
                id="recovery-code"
                placeholder="ABCD-EFGH-JKLM"
                autoComplete="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
              />
              <p className="text-xs text-muted-foreground">
                After signing in, add a new passkey in Settings. Each code works once.
              </p>
              <Button type="submit" className="w-full" disabled={busy || code.length < 8}>
                Sign in with code
              </Button>
              <Button
                type="button"
                variant="link"
                className="w-full"
                onClick={() => setRecovery(false)}
              >
                Back to passkey
              </Button>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
