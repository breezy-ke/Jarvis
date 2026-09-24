#!/usr/bin/env bash
# Copies the Jarvis checkout from the Windows drive into your Ubuntu home
# (default ~/Jarvis), where it runs much faster and file permissions work.
# scripts/windows/setup.ps1 runs this for you.
#
#   bash import-repo.sh /mnt/c/Users/you/Jarvis [~/Jarvis]
set -euo pipefail

src="${1:?usage: import-repo.sh <windows checkout> [target folder]}"
dest="${2:-$HOME/Jarvis}"

if [[ -f "$dest/Makefile" ]]; then
  echo "$dest already exists; leaving it as it is."
  exit 0
fi

if [[ -d "$src/.git" ]]; then
  # A local clone needs no GitHub login and gets Linux line endings.
  git -c safe.directory='*' clone --quiet "$src" "$dest"
  origin=$(git -c safe.directory='*' -C "$src" remote get-url origin 2>/dev/null || true)
  if [[ -n "$origin" ]]; then
    git -C "$dest" remote set-url origin "$origin"
  fi
  echo "Cloned into $dest. Later, 'git pull' there fetches updates from ${origin:-your Windows copy}."
else
  mkdir -p "$dest"
  cp -a "$src/." "$dest/"
  # A ZIP download can carry Windows line endings, which break shell scripts.
  find "$dest" -type f \( -name '*.sh' -o -name 'Makefile' -o -name 'Dockerfile' -o -name '*.yml' \
    -o -name '*.yaml' -o -name '*.toml' -o -name '*.py' -o -name '*.sql' -o -name '.env.example' \) \
    -exec sed -i 's/\r$//' {} +
  echo "Copied into $dest. It isn't a git clone, so to update later, clone the repository there with git."
fi
chmod +x "$dest"/scripts/wsl/*.sh
