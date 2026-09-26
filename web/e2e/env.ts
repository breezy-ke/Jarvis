import { randomBytes } from "node:crypto";

export const port = Number(process.env.E2E_PORT ?? 8766);
export const baseURL = `http://localhost:${port}`;
const speechPort = Number(process.env.E2E_SPEECH_PORT ?? 8767);
const googlePort = Number(process.env.E2E_GOOGLE_PORT ?? 8768);
// A fake Google (sign-in, Gmail, Calendar) with a few emails waiting (see e2e_server.py).
export const fakeGoogle = `http://127.0.0.1:${googlePort}`;

// The browser's fake microphone plays this recording once ("What is on my calendar today?").
export const fakeMicrophone = new URL(
  "../../core/tests/fixtures/audio/question.wav",
  import.meta.url,
).pathname;

// A fresh encryption key per run (Fernet: url-safe base64 of 32 random bytes).
const secretKey =
  process.env.E2E_SECRET_KEY ??
  randomBytes(32).toString("base64").replace(/\+/g, "-").replace(/\//g, "_");
process.env.E2E_SECRET_KEY = secretKey;

export const serverEnv: Record<string, string> = {
  PATH: process.env.PATH ?? "",
  HOME: process.env.HOME ?? "",
  JARVIS_ENV: "test",
  JARVIS_ALLOW_FAKE_LLM: "1",
  JARVIS_EMBEDDER: "hash",
  JARVIS_PUBLIC_ORIGIN: baseURL,
  JARVIS_PORT: String(port),
  JARVIS_HOST: "127.0.0.1",
  JARVIS_WEB_DIST: new URL("../dist", import.meta.url).pathname,
  JARVIS_MODELS_FILE: new URL("../../core/tests/fixtures/models.fake.yaml", import.meta.url)
    .pathname,
  JARVIS_SECRET_KEY: secretKey,
  // Sentence data for the voice pipeline (`make install` or CI fetches it).
  NLTK_DATA: new URL("../../data/nltk_data", import.meta.url).pathname,
  // A fake speech server stands in for faster-whisper and Kokoro (see e2e_server.py).
  E2E_SPEECH_PORT: String(speechPort),
  SPEECH_BASE_URL: `http://127.0.0.1:${speechPort}/v1`,
  E2E_GOOGLE_PORT: String(googlePort),
  JARVIS_GOOGLE_FAKE_BASE: fakeGoogle,
  GOOGLE_OAUTH_CLIENT_ID: "e2e-client",
  GOOGLE_OAUTH_CLIENT_SECRET: "e2e-fake",
  GOOGLE_OAUTH_REDIRECT_URI: `${baseURL}/api/integrations/google/callback`,
  JARVIS_ENABLE_SCHEDULER: "false",
  JARVIS_LOG_LEVEL: "WARNING",
  DATABASE_URL:
    process.env.E2E_DATABASE_URL ?? "postgresql+asyncpg://jarvis:jarvis@127.0.0.1:5432/jarvis_e2e",
  PYDANTIC_AI_NO_BANNER: "1",
};
