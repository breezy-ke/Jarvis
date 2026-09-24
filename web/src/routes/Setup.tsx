import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Copy, Fingerprint, KeyRound } from "lucide-react";
import { useState } from "react";

import { Orb } from "@/components/Orb";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Alert } from "@/components/ui/misc";
import { keys } from "@/lib/queries";
import { guessDeviceName, passkeysSupported, registerPasskey } from "@/lib/webauthn";

export function SetupPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [token, setToken] = useState("");
  const [deviceName, setDeviceName] = useState(guessDeviceName());
  const [codes, setCodes] = useState<string[] | null>(null);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const result = await registerPasskey({ setupToken: token.trim(), deviceName });
      setCodes(result.recovery_codes ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong creating your passkey.");
    } finally {
      setBusy(false);
    }
  }

  async function finish() {
    await queryClient.invalidateQueries({ queryKey: keys.auth });
    await navigate({ to: "/onboarding" });
  }

  return (
    <div className="flex min-h-dvh items-center justify-center p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="items-center text-center">
          <Orb size={56} className="mb-2" />
          <CardTitle className="text-xl">Welcome. Let's secure your Jarvis.</CardTitle>
          <CardDescription>
            You'll sign in with a passkey (Face ID, fingerprint or Windows Hello). No passwords,
            nothing to phish.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {!passkeysSupported() ? (
            <Alert tone="error" title="This browser can't create passkeys">
              Open Jarvis over HTTPS (your Tailscale address) in an up-to-date browser.
            </Alert>
          ) : null}
          {codes === null ? (
            <form
              className="space-y-4"
              onSubmit={(e) => {
                e.preventDefault();
                void create();
              }}
            >
              <div className="space-y-2">
                <Label htmlFor="setup-code">Setup code</Label>
                <Input
                  id="setup-code"
                  autoComplete="one-time-code"
                  placeholder="From the Jarvis server log"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  required
                />
                <p className="text-xs text-muted-foreground">
                  It's printed when Jarvis starts (<code>make logs</code>). Lost it? Run{" "}
                  <code>make setup-token</code>.
                </p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="device-name">Name this device</Label>
                <Input
                  id="device-name"
                  value={deviceName}
                  onChange={(e) => setDeviceName(e.target.value)}
                />
              </div>
              {error ? <Alert tone="error">{error}</Alert> : null}
              <Button type="submit" size="lg" className="w-full" disabled={busy || !token.trim()}>
                <Fingerprint /> {busy ? "Waiting for your passkey…" : "Create passkey"}
              </Button>
            </form>
          ) : (
            <div className="space-y-4">
              <Alert tone="warning" title="Save your recovery codes now">
                If you lose every device with your passkey, one of these codes gets you back in.
                Each works once. They won't be shown again.
              </Alert>
              <ol
                className="grid grid-cols-2 gap-2 rounded-md border bg-muted/40 p-3 font-mono text-sm"
                aria-label="Recovery codes"
              >
                {codes.map((code) => (
                  <li key={code}>{code}</li>
                ))}
              </ol>
              <Button
                variant="outline"
                className="w-full"
                onClick={() => void navigator.clipboard?.writeText(codes.join("\n"))}
              >
                <Copy /> Copy codes
              </Button>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={saved}
                  onChange={(e) => setSaved(e.target.checked)}
                />
                I've stored these somewhere safe
              </label>
              <Button size="lg" className="w-full" disabled={!saved} onClick={() => void finish()}>
                <KeyRound /> Continue to onboarding
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
