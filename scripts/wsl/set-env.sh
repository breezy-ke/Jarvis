#!/usr/bin/env bash
# Sets KEY=VALUE in the repo's .env, replacing any existing line for KEY.
#   scripts/wsl/set-env.sh JARVIS_PUBLIC_ORIGIN https://pc.tailnet.ts.net
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

key="${1:?usage: set-env.sh KEY VALUE}"
value="${2?usage: set-env.sh KEY VALUE}"
[[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || {
  echo "not a valid variable name: $key" >&2
  exit 1
}
[[ "$value" != *$'\n'* ]] || {
  echo "the value can't contain a line break" >&2
  exit 1
}

touch .env
tmp=$(mktemp "$PWD/.env.XXXXXX") # mktemp files are private (mode 600)
KEY="$key" VALUE="$value" awk '
  BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VALUE"]; done = 0 }
  index($0, k "=") == 1 { if (!done) print k "=" v; done = 1; next }
  { print }
  END { if (!done) print k "=" v }
' .env >"$tmp"
mv "$tmp" .env
echo "Set $key in .env"
