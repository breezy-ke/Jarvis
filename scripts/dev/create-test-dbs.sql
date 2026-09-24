-- Databases for development (compose.dev.yml). jarvis_test is created by the
-- image itself and is wiped by the core tests; the browser end-to-end tests
-- use (and wipe) jarvis_e2e; `make dev-core` uses jarvis_dev.
CREATE DATABASE jarvis_e2e;
CREATE DATABASE jarvis_dev;
