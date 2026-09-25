/** Shapes returned by the Jarvis API. */

export interface AuthStatus {
  setup_complete: boolean;
  authenticated: boolean;
}

export interface SystemStatus {
  version: string;
  owner_name: string | null;
  assistant_name: string;
  kill_switch: { engaged: boolean; reason: string | null };
  autonomy: { open: boolean; reason: string };
  profile: { completeness: number; signed_off: boolean; version: number };
  pending_approvals: number;
  memories_to_review: number;
  timezone: string;
}

export interface Validation {
  validator: string;
  outcome: "pass" | "warn" | "block";
  message: string;
}

export interface Action {
  id: string;
  kind: string;
  summary: string;
  status: string;
  status_reason: string | null;
  risk: "low" | "medium" | "high" | "critical";
  autonomy: string;
  payload: Record<string, unknown>;
  payload_hash: string;
  rationale: string;
  validation: Validation[];
  created_by: string;
  created_at: string;
  decided_at: string | null;
  decided_via: string | null;
  execute_after: string | null;
  executed_at: string | null;
  result: Record<string, unknown> | null;
  error: string | null;
  needs_passkey: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  channel: string;
  updated_at: string;
}

export interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  model: string | null;
  error: boolean;
}

export interface AuditEvent {
  id: number;
  ts: string;
  actor: string;
  event_type: string;
  summary: string;
  subject_type: string | null;
  subject_id: string | null;
  data: Record<string, unknown>;
  hash: string;
}

export interface ProfilePayload {
  version: number;
  data: Record<string, Record<string, unknown>>;
  schema: JsonSchema;
  required_fields: string[];
  completeness: number;
  missing_required: string[];
  signed_off_at: string | null;
  ever_signed_off: boolean;
  gate: { open: boolean; reason: string };
}

export interface JsonSchema {
  type?: string;
  description?: string;
  properties?: Record<string, JsonSchema>;
  items?: JsonSchema;
  anyOf?: JsonSchema[];
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  title?: string;
}

export interface Fact {
  id: string;
  category: string;
  subject: string;
  predicate: string;
  value: string;
  status: "confirmed" | "inferred";
  source: string;
  source_ref: string | null;
  confidence: number;
  sensitivity: string;
  updated_at: string;
  is_suggestion: boolean;
}

export interface OnboardingModule {
  id: string;
  title: string;
  status: "not_started" | "in_progress" | "completed" | "skipped";
  optional: boolean;
  filled: number;
  total: number;
  required_missing: string[];
  summary: string | null;
  opening: string;
  goal: string;
}

export interface OnboardingOverview {
  completeness: number;
  missing_required: string[];
  modules_completed: number;
  modules_total: number;
  gate_open: boolean;
  gate_reason: string;
  signed_off: boolean;
  modules: OnboardingModule[];
}

export interface IngestionSource {
  id: string;
  title: string;
  description: string;
  needs_google: boolean;
  setting: string | null;
  consent: boolean;
  settings: Record<string, string>;
  last_run_at: string | null;
  last_status: string | null;
  last_error: string | null;
  stats: Record<string, unknown>;
  ready: boolean;
}

export interface GoogleStatus {
  configured: boolean;
  connected: boolean;
  account_email: string | null;
  scopes: string | null;
  redirect_uri: string;
}

export interface ModelsOverview {
  budget_usd: number;
  models: Record<
    string,
    {
      provider: string;
      model: string;
      local: boolean;
      trains_on_data: boolean;
      zero_data_retention: boolean;
      limits: Record<string, number>;
    }
  >;
  tasks: Record<
    string,
    {
      privacy?: string;
      error?: string;
      candidates: { model: string; available: boolean; reason: string }[];
    }
  >;
}

export interface PoliciesOverview {
  defaults: { undo_window_seconds: number; timezone: string };
  approval_channels: Record<string, string[]>;
  action_kinds: Record<
    string,
    {
      description: string;
      autonomy: string;
      risk: string;
      available: boolean;
      undo_window_seconds: number | null;
    }
  >;
}

export interface Passkey {
  id: string;
  device_name: string;
  created_at: string;
  last_used_at: string | null;
}

export interface VoiceDevice {
  id: string;
  name: string;
  created_at: string;
  last_seen_at: string | null;
}

export interface VoiceStatus {
  enabled: boolean;
  /** Why voice is switched off (a broken config/voice.yaml). */
  error?: string | null;
  /** Why live voice can't run right now, although it's configured. */
  problem?: string | null;
  speech?: {
    reachable: boolean;
    stt_ready: boolean;
    tts_ready: boolean;
    detail: string;
    key_refused: boolean;
  };
  voice?: string;
  stt_model?: string;
  language?: string;
  confirm_phrase?: string;
  cancel_phrase?: string;
  devices: VoiceDevice[];
}

export interface PairingCode {
  code: string;
  expires_at: string;
}

export interface TelegramStatus {
  configured: boolean;
  paired: boolean;
  running?: boolean;
  bot_username?: string | null;
  error?: string | null;
  owner?: { name: string; username: string | null; paired_at: string } | null;
}

export interface TelegramLink extends PairingCode {
  link: string | null;
  bot_username: string;
}
