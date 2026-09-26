import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Each test starts from an empty page (automatic only with vitest globals, which we don't use).
afterEach(cleanup);
