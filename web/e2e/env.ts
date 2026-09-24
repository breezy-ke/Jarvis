import { randomBytes } from "node:crypto";

export const port = Number(process.env.E2E_PORT ?? 8766);
export const baseURL = `http://localhost:${port}`;

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
  JARVIS_ENABLE_SCHEDULER: "false",
  JARVIS_LOG_LEVEL: "WARNING",
  DATABASE_URL:
    process.env.E2E_DATABASE_URL ?? "postgresql+asyncpg://jarvis:jarvis@127.0.0.1:5432/jarvis_e2e",
  PYDANTIC_AI_NO_BANNER: "1",
};
