import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, Inbox, Play, RefreshCw, Unplug, Upload } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Alert, Skeleton } from "@/components/ui/misc";
import { Switch } from "@/components/ui/switch";
import { api } from "@/lib/api";
import { keys, useMailStatus } from "@/lib/queries";
import type { GoogleStatus, IngestionSource } from "@/lib/types";
import { timeAgo } from "@/lib/utils";

export function SourcesPage() {
  const sources = useQuery({
    queryKey: keys.sources,
    queryFn: () => api.get<IngestionSource[]>("/api/ingestion/sources"),
  });
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Sources</h1>
        <p className="text-sm text-muted-foreground">
          Optional places I can learn from. Each is off until you switch it on, reads only what's
          described, and everything I find arrives as a suggestion for you to confirm.
        </p>
      </div>
      <GoogleCard />
      {sources.isLoading ? <Skeleton className="h-60" /> : null}
      {(sources.data ?? []).map((source) => (
        <SourceCard key={source.id} source={source} />
      ))}
    </div>
  );
}

function GoogleCard() {
  const queryClient = useQueryClient();
  const { data } = useQuery({
    queryKey: keys.google,
    queryFn: () => api.get<GoogleStatus>("/api/integrations/google"),
  });
  const connect = useMutation({
    mutationFn: () => api.post<{ auth_url: string }>("/api/integrations/google/connect"),
    onSuccess: ({ auth_url }) => {
      window.open(auth_url, "_blank", "noopener,noreferrer");
    },
  });
  const disconnect = useMutation({
    mutationFn: () => api.post("/api/integrations/google/disconnect"),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: keys.google });
      void queryClient.invalidateQueries({ queryKey: keys.sources });
      void queryClient.invalidateQueries({ queryKey: keys.mail });
    },
  });
  const { data: mail } = useMailStatus();
  const checkNow = useMutation({
    mutationFn: () => api.post("/api/mail/sync"),
    onSuccess: () => {
      window.setTimeout(() => void queryClient.invalidateQueries({ queryKey: keys.mail }), 3_000);
    },
  });
  if (!data) return null;
  const partial = data.connected && (data.mail_access !== "full" || !data.can_add_holds);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Google (Gmail and Calendar)
          {data.connected ? (
            <Badge variant="success">connected</Badge>
          ) : (
            <Badge variant="secondary">not connected</Badge>
          )}
        </CardTitle>
        <CardDescription>
          I read and sort your mail, draft replies in your voice and add private holds to your
          calendar. Nothing is ever sent without your approval, and I can't delete anything.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {!data.configured ? (
          <Alert tone="warning" title="Not set up yet">
            Add <code>GOOGLE_OAUTH_CLIENT_ID</code> and <code>GOOGLE_OAUTH_CLIENT_SECRET</code> to{" "}
            <code>.env</code> (docs/setup.md, step 3), then restart Jarvis.
          </Alert>
        ) : data.connected ? (
          <>
            <p>Connected as {data.account_email ?? "your Google account"}.</p>
            <ul className="space-y-1" aria-label="What Jarvis may do">
              <li>
                Mail:{" "}
                {data.mail_access === "full"
                  ? "read, sort, label, draft, and send once you approve"
                  : data.mail_access === "read"
                    ? "read and sort only (no drafts or sending)"
                    : "not allowed"}
              </li>
              <li>Calendar holds: {data.can_add_holds ? "allowed" : "not allowed"}</li>
              {mail && mail.access !== "none" ? (
                <li>
                  Last checked {timeAgo(mail.sync.last_sync_at)}
                  {mail.sync.status === "reconnect" || mail.sync.status === "error"
                    ? `: ${mail.sync.error ?? "couldn't reach Gmail"}`
                    : ""}
                </li>
              ) : null}
            </ul>
            {partial ? (
              <Alert tone="info">
                On Google's screen some permissions were left unticked. Connect again and allow them
                all to let me draft, send (with your approval) and hold calendar time.
              </Alert>
            ) : null}
          </>
        ) : (
          <Alert tone="info">
            Connect from a browser <strong>on the PC running Jarvis</strong>: Google sends you back
            to <code>{data.redirect_uri}</code>.
          </Alert>
        )}
        {connect.error ? <Alert tone="error">{connect.error.message}</Alert> : null}
      </CardContent>
      <CardFooter className="flex-wrap">
        {data.connected ? (
          <>
            {partial ? (
              <Button onClick={() => connect.mutate()} disabled={connect.isPending}>
                <Inbox /> Give Jarvis your inbox
              </Button>
            ) : null}
            {data.mail_access !== "none" ? (
              <Button
                variant="outline"
                onClick={() => checkNow.mutate()}
                disabled={checkNow.isPending}
              >
                <RefreshCw /> Check now
              </Button>
            ) : null}
            <Button
              variant="outline"
              onClick={() => disconnect.mutate()}
              disabled={disconnect.isPending}
            >
              <Unplug /> Disconnect
            </Button>
          </>
        ) : (
          <Button onClick={() => connect.mutate()} disabled={!data.configured || connect.isPending}>
            <ExternalLink /> Connect Google
          </Button>
        )}
      </CardFooter>
    </Card>
  );
}

function SourceCard({ source }: { source: IngestionSource }) {
  const queryClient = useQueryClient();
  const [setting, setSetting] = useState(
    source.setting ? (source.settings[source.setting] ?? "") : "",
  );
  const [file, setFile] = useState<File | null>(null);
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: keys.sources });
    void queryClient.invalidateQueries({ queryKey: ["facts"] });
    void queryClient.invalidateQueries({ queryKey: keys.status });
  };
  const consent = useMutation({
    mutationFn: (value: boolean) =>
      api.post(`/api/ingestion/${source.id}/consent`, {
        consent: value,
        settings: source.setting ? { [source.setting]: setting } : null,
      }),
    onSuccess: refresh,
  });
  const run = useMutation({
    mutationFn: async () => {
      if (source.setting) {
        await api.post(`/api/ingestion/${source.id}/consent`, {
          consent: true,
          settings: { [source.setting]: setting },
        });
      }
      return api.post<Record<string, unknown>>(`/api/ingestion/${source.id}/run`);
    },
    onSuccess: refresh,
  });
  const upload = useMutation({
    mutationFn: () => {
      const form = new FormData();
      if (file) form.append("file", file);
      return api.upload<Record<string, unknown>>("/api/ingestion/documents/upload", form);
    },
    onSuccess: refresh,
  });
  const error = consent.error ?? run.error ?? upload.error;
  const result = run.data ?? upload.data;

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between gap-4">
        <div className="space-y-1.5">
          <CardTitle>{source.title}</CardTitle>
          <CardDescription>{source.description}</CardDescription>
        </div>
        <Switch
          aria-label={`Allow ${source.title}`}
          checked={source.consent}
          onCheckedChange={(value) => consent.mutate(value)}
        />
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {source.setting ? (
          <div className="space-y-1.5">
            <Label htmlFor={`setting-${source.id}`}>
              {source.setting === "url" ? "Website address" : "GitHub username"}
            </Label>
            <Input
              id={`setting-${source.id}`}
              value={setting}
              placeholder={source.setting === "url" ? "https://your-site.com" : "your-username"}
              onChange={(e) => setSetting(e.target.value)}
            />
          </div>
        ) : null}
        {source.id === "documents" ? (
          <div className="space-y-1.5">
            <Label htmlFor="upload">CV (PDF/TXT) or LinkedIn export (.zip)</Label>
            <Input
              id="upload"
              type="file"
              accept=".pdf,.txt,.md,.zip"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>
        ) : null}
        {source.needs_google && !source.ready ? (
          <Alert tone="warning">Connect Google first.</Alert>
        ) : null}
        {source.last_run_at ? (
          <p className="text-xs text-muted-foreground">
            Last run {timeAgo(source.last_run_at)}: {source.last_status}
            {source.last_error ? `: ${source.last_error}` : ""}
          </p>
        ) : null}
        {result ? (
          <Alert tone="success" title="Done">
            {String(result.suggestions ?? 0)} suggestion(s) are waiting on the "What I know" page.
          </Alert>
        ) : null}
        {error ? <Alert tone="error">{error.message}</Alert> : null}
      </CardContent>
      <CardFooter>
        {source.id === "documents" ? (
          <Button
            onClick={() => upload.mutate()}
            disabled={!source.consent || !file || upload.isPending}
          >
            <Upload /> {upload.isPending ? "Reading…" : "Upload and read"}
          </Button>
        ) : (
          <Button
            onClick={() => run.mutate()}
            disabled={
              !source.consent ||
              !source.ready ||
              run.isPending ||
              (Boolean(source.setting) && !setting.trim())
            }
          >
            <Play /> {run.isPending ? "Reading…" : "Run now"}
          </Button>
        )}
      </CardFooter>
    </Card>
  );
}
