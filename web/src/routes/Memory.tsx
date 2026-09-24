import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Download, Pencil, Plus, Search, Trash2, X } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Textarea } from "@/components/ui/input";
import { Alert, EmptyState, Progress, Skeleton } from "@/components/ui/misc";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api } from "@/lib/api";
import { keys } from "@/lib/queries";
import type { Fact, JsonSchema, ProfilePayload } from "@/lib/types";
import { percent, timeAgo } from "@/lib/utils";

export function MemoryPage() {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">What I know about you</h1>
          <p className="text-sm text-muted-foreground">
            Everything here is yours to correct, delete or export. Anything I inferred waits for
            your confirmation.
          </p>
        </div>
        <Button variant="outline" asChild>
          <a href="/api/memory/export" download>
            <Download /> Export all
          </a>
        </Button>
      </div>
      <Tabs defaultValue="profile">
        <TabsList>
          <TabsTrigger value="profile">Profile</TabsTrigger>
          <TabsTrigger value="review">To review</TabsTrigger>
          <TabsTrigger value="all">All memories</TabsTrigger>
        </TabsList>
        <TabsContent value="profile">
          <ProfileEditor />
        </TabsContent>
        <TabsContent value="review">
          <ReviewList />
        </TabsContent>
        <TabsContent value="all">
          <AllMemories />
        </TabsContent>
      </Tabs>
    </div>
  );
}

// --- Profile ---------------------------------------------------------------------

const humanize = (s: string) => s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

function resolve(schema: JsonSchema, defs: Record<string, JsonSchema>): JsonSchema {
  if (schema.$ref) return defs[schema.$ref.split("/").pop() ?? ""] ?? schema;
  if (schema.anyOf) {
    const option = schema.anyOf.find((o) => o.type !== "null");
    return option
      ? { ...resolve(option, defs), description: schema.description ?? option.description }
      : schema;
  }
  return schema;
}

type FieldKind = "text" | "list" | "json" | "bool";

function fieldKind(schema: JsonSchema, defs: Record<string, JsonSchema>): FieldKind {
  const s = resolve(schema, defs);
  if (s.type === "string") return "text";
  if (s.type === "boolean") return "bool";
  if (s.type === "array") return resolve(s.items ?? {}, defs).type === "string" ? "list" : "json";
  return "json";
}

function useSaveProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (changes: Record<string, unknown>) =>
      api.patch<ProfilePayload>("/api/profile", { changes }),
    onSuccess: (data) => {
      queryClient.setQueryData(keys.profile, data);
      void queryClient.invalidateQueries({ queryKey: keys.status });
      void queryClient.invalidateQueries({ queryKey: keys.onboarding });
    },
  });
}

function ProfileEditor() {
  const { data, isLoading } = useQuery({
    queryKey: keys.profile,
    queryFn: () => api.get<ProfilePayload>("/api/profile"),
  });
  if (isLoading || !data) return <Skeleton className="h-96" />;
  const defs = data.schema.$defs ?? {};
  const sections = Object.entries(data.schema.properties ?? {});

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="space-y-2 pt-5">
          <div className="flex justify-between text-sm">
            <span>Profile version {data.version}</span>
            <span>{percent(data.completeness)} complete</span>
          </div>
          <Progress value={data.completeness} label="Profile completeness" />
          <p className="text-xs text-muted-foreground">
            {data.gate.open
              ? "Signed off: autonomy is on."
              : `Autonomy is off: ${data.gate.reason}.`}
          </p>
        </CardContent>
      </Card>
      {sections.map(([section, sectionSchema]) => {
        const resolved = resolve(sectionSchema, defs);
        return (
          <Card key={section}>
            <CardHeader>
              <CardTitle>{humanize(section)}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {Object.entries(resolved.properties ?? {}).map(([field, fieldSchema]) => {
                const path = `${section}.${field}`;
                return (
                  <ProfileField
                    key={path}
                    path={path}
                    label={humanize(field)}
                    description={resolve(fieldSchema, defs).description ?? fieldSchema.description}
                    kind={fieldKind(fieldSchema, defs)}
                    value={data.data[section]?.[field]}
                    required={data.required_fields.includes(path)}
                    missing={data.missing_required.includes(path)}
                  />
                );
              })}
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}

function ProfileField(props: {
  path: string;
  label: string;
  description?: string;
  kind: FieldKind;
  value: unknown;
  required: boolean;
  missing: boolean;
}) {
  const save = useSaveProfile();
  const { path, label, description, kind, value, required, missing } = props;
  const id = `field-${path.replace(".", "-")}`;
  const header = (
    <div className="flex items-center gap-2">
      <Label htmlFor={id}>{label}</Label>
      {required ? (
        <Badge variant={missing ? "warning" : "outline"}>
          {missing ? "required" : "required ✓"}
        </Badge>
      ) : null}
    </div>
  );
  const hint = description ? <p className="text-xs text-muted-foreground">{description}</p> : null;
  const error = save.error ? (
    <p className="text-xs text-destructive">{save.error.message}</p>
  ) : null;

  if (kind === "bool") {
    return (
      <div className="flex items-center gap-2">
        <input
          id={id}
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => save.mutate({ [path]: e.target.checked })}
        />
        {header}
      </div>
    );
  }
  if (kind === "text") {
    return (
      <div className="space-y-1.5">
        {header}
        <Input
          id={id}
          defaultValue={(value as string | null) ?? ""}
          key={String(value ?? "")}
          onBlur={(e) => {
            const next = e.target.value.trim();
            if (next !== ((value as string | null) ?? "")) save.mutate({ [path]: next || null });
          }}
        />
        {hint}
        {error}
      </div>
    );
  }
  if (kind === "list") {
    const items = (value as string[] | undefined) ?? [];
    return (
      <div className="space-y-1.5">
        {header}
        <ListEditor id={id} items={items} onChange={(next) => save.mutate({ [path]: next })} />
        {hint}
        {error}
      </div>
    );
  }
  return (
    <div className="space-y-1.5">
      {header}
      <JsonEditor id={id} value={value} onSave={(next) => save.mutate({ [path]: next })} />
      {hint}
      {error}
    </div>
  );
}

function ListEditor({
  id,
  items,
  onChange,
}: {
  id: string;
  items: string[];
  onChange: (items: string[]) => void;
}) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const value = draft.trim();
    if (value && !items.includes(value)) onChange([...items, value]);
    setDraft("");
  };
  return (
    <div className="space-y-2">
      {items.length ? (
        <ul className="flex flex-wrap gap-2">
          {items.map((item) => (
            <li key={item}>
              <Badge variant="secondary" className="gap-1 py-1 pl-3">
                {item}
                <button
                  type="button"
                  aria-label={`Remove ${item}`}
                  className="rounded-full p-0.5 hover:bg-background"
                  onClick={() => onChange(items.filter((i) => i !== item))}
                >
                  <X className="size-3" />
                </button>
              </Badge>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="flex gap-2">
        <Input
          id={id}
          value={draft}
          placeholder="Add and press Enter"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
        />
        <Button type="button" variant="outline" size="icon" onClick={add} aria-label="Add">
          <Plus />
        </Button>
      </div>
    </div>
  );
}

function JsonEditor({
  id,
  value,
  onSave,
}: {
  id: string;
  value: unknown;
  onSave: (value: unknown) => void;
}) {
  const initial = JSON.stringify(value ?? [], null, 2);
  const [text, setText] = useState(initial);
  const [problem, setProblem] = useState<string | null>(null);
  return (
    <div className="space-y-2">
      <Textarea
        id={id}
        className="min-h-28 font-mono text-xs"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setProblem(null);
        }}
      />
      {problem ? <p className="text-xs text-destructive">{problem}</p> : null}
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={text === initial}
        onClick={() => {
          try {
            onSave(JSON.parse(text));
          } catch {
            setProblem("That isn't valid JSON yet.");
          }
        }}
      >
        Save
      </Button>
    </div>
  );
}

// --- Review and memories ----------------------------------------------------------------------

function useFactMutations() {
  const queryClient = useQueryClient();
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["facts"] });
    void queryClient.invalidateQueries({ queryKey: keys.profile });
    void queryClient.invalidateQueries({ queryKey: keys.status });
  };
  return {
    confirm: useMutation({
      mutationFn: (id: string) => api.post(`/api/memory/facts/${id}/confirm`),
      onSuccess: refresh,
    }),
    reject: useMutation({
      mutationFn: (id: string) => api.post(`/api/memory/facts/${id}/reject`),
      onSuccess: refresh,
    }),
    remove: useMutation({
      mutationFn: (id: string) => api.del(`/api/memory/facts/${id}`),
      onSuccess: refresh,
    }),
    edit: useMutation({
      mutationFn: ({ id, value }: { id: string; value: string }) =>
        api.patch(`/api/memory/facts/${id}`, { value }),
      onSuccess: refresh,
    }),
  };
}

function prettyValue(fact: Fact): string {
  if (!fact.is_suggestion) return fact.value;
  try {
    const parsed: unknown = JSON.parse(fact.value);
    return typeof parsed === "string" ? parsed : JSON.stringify(parsed, null, 2);
  } catch {
    return fact.value;
  }
}

function ReviewList() {
  const { data, isLoading } = useQuery({
    queryKey: keys.facts("inferred"),
    queryFn: () => api.get<Fact[]>("/api/memory/facts?status=inferred"),
  });
  const { confirm, reject } = useFactMutations();
  const error = confirm.error ?? reject.error;
  if (isLoading) return <Skeleton className="h-40" />;
  if (!data?.length) {
    return (
      <EmptyState title="Nothing to review">
        When I learn something new about you, it waits here for your OK.
      </EmptyState>
    );
  }
  return (
    <div className="space-y-3">
      {error ? <Alert tone="error">{error.message}</Alert> : null}
      {data.map((fact) => (
        <Card key={fact.id}>
          <CardHeader>
            <CardTitle className="text-sm">
              {fact.is_suggestion
                ? `Suggested for your profile: ${fact.predicate}`
                : `${fact.subject}: ${fact.predicate}`}
            </CardTitle>
            <CardDescription>
              From {fact.source.replace("ingestion:", "")} · {timeAgo(fact.updated_at)} · confidence{" "}
              {percent(fact.confidence)}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <pre className="whitespace-pre-wrap break-words rounded-md bg-muted/40 p-3 font-sans text-sm">
              {prettyValue(fact)}
            </pre>
            <div className="flex gap-2">
              <Button
                size="sm"
                onClick={() => confirm.mutate(fact.id)}
                disabled={confirm.isPending}
              >
                <Check /> {fact.is_suggestion ? "Add to profile" : "That's right"}
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => reject.mutate(fact.id)}
                disabled={reject.isPending}
              >
                <X /> Not right
              </Button>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function AllMemories() {
  const [q, setQ] = useState("");
  const { data, isLoading } = useQuery({
    queryKey: keys.facts(`active:${q}`),
    queryFn: () =>
      api.get<Fact[]>(`/api/memory/facts?status=active${q ? `&q=${encodeURIComponent(q)}` : ""}`),
  });
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const { remove, edit } = useFactMutations();
  const items = (data ?? []).filter((f) => !f.is_suggestion);

  return (
    <div className="space-y-3">
      <div className="relative">
        <Search className="absolute left-3 top-3 size-4 text-muted-foreground" />
        <Input
          className="pl-9"
          placeholder="Search memories"
          aria-label="Search memories"
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </div>
      {isLoading ? <Skeleton className="h-40" /> : null}
      {!isLoading && !items.length ? (
        <EmptyState title="No memories yet">
          Tell me things in chat ("remember that…") or finish onboarding.
        </EmptyState>
      ) : null}
      <Card>
        <CardContent className="divide-y p-0">
          {items.map((fact) => (
            <div key={fact.id} className="flex flex-wrap items-start gap-3 px-4 py-3 text-sm">
              <Badge variant="outline">{fact.category}</Badge>
              <div className="min-w-0 flex-1">
                <p className="font-medium">
                  {fact.subject} · {fact.predicate}
                </p>
                {editing === fact.id ? (
                  <form
                    className="mt-2 flex gap-2"
                    onSubmit={(e) => {
                      e.preventDefault();
                      edit.mutate(
                        { id: fact.id, value: draft },
                        { onSuccess: () => setEditing(null) },
                      );
                    }}
                  >
                    <Input
                      value={draft}
                      onChange={(e) => setDraft(e.target.value)}
                      aria-label="New value"
                    />
                    <Button size="sm" type="submit">
                      Save
                    </Button>
                  </form>
                ) : (
                  <p className="text-muted-foreground">{fact.value}</p>
                )}
              </div>
              <Badge variant={fact.status === "confirmed" ? "success" : "secondary"}>
                {fact.status}
              </Badge>
              <div className="flex gap-1">
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label="Edit"
                  onClick={() => {
                    setEditing(fact.id);
                    setDraft(fact.value);
                  }}
                >
                  <Pencil />
                </Button>
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label="Delete permanently"
                  onClick={() => {
                    if (window.confirm("Delete this memory permanently?")) remove.mutate(fact.id);
                  }}
                >
                  <Trash2 />
                </Button>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
