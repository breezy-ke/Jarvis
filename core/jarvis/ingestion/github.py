"""Learn the owner's stacks and conventions from their GitHub repositories.

Only repository metadata and a few manifest files are read (package.json,
composer.json, pubspec.yaml...). No source code leaves GitHub, and nothing
is sent to a model: detection is rule-based.
"""

from __future__ import annotations

import base64
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx

API = "https://api.github.com"
_TS_STRICT_RE = re.compile(r'"strict"\s*:\s*true')

_JS_FRAMEWORKS = {
    "next": "Next.js",
    "nuxt": "Nuxt",
    "react": "React",
    "vue": "Vue",
    "svelte": "Svelte",
    "@sveltejs/kit": "SvelteKit",
    "astro": "Astro",
    "expo": "Expo (React Native)",
    "react-native": "React Native",
    "express": "Express",
    "@nestjs/core": "NestJS",
    "tailwindcss": "Tailwind CSS",
    "@remix-run/react": "Remix",
    "vite": "Vite",
    "prisma": "Prisma",
    "drizzle-orm": "Drizzle",
}
_JS_CONVENTIONS = {
    "typescript": "TypeScript",
    "eslint": "ESLint",
    "prettier": "Prettier",
    "@biomejs/biome": "Biome",
    "vitest": "Vitest",
    "jest": "Jest",
    "@playwright/test": "Playwright",
    "cypress": "Cypress",
    "husky": "Husky git hooks",
    "@commitlint/cli": "Conventional Commits (commitlint)",
}
_PHP_PACKAGES = {
    "laravel/framework": "Laravel",
    "livewire/livewire": "Livewire",
    "inertiajs/inertia-laravel": "Inertia",
    "filament/filament": "Filament",
    "pestphp/pest": "Pest",
    "phpunit/phpunit": "PHPUnit",
}


@dataclass
class GitHubFindings:
    repos_scanned: int = 0
    languages: Counter[str] = field(default_factory=Counter)
    frameworks: Counter[str] = field(default_factory=Counter)
    conventions: Counter[str] = field(default_factory=Counter)
    package_managers: Counter[str] = field(default_factory=Counter)


def analyze_manifest(path: str, content: str, findings: GitHubFindings) -> None:
    if path == "package.json":
        try:
            data: dict[str, Any] = json.loads(content)
        except json.JSONDecodeError:
            return
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        for dep, label in _JS_FRAMEWORKS.items():
            if dep in deps:
                findings.frameworks[label] += 1
        for dep, label in _JS_CONVENTIONS.items():
            if dep in deps:
                findings.conventions[label] += 1
        manager = str(data.get("packageManager", "")).split("@")[0]
        if manager:
            findings.package_managers[manager] += 1
    elif path == "composer.json":
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return
        deps = {**data.get("require", {}), **data.get("require-dev", {})}
        for dep, label in _PHP_PACKAGES.items():
            if dep in deps:
                findings.frameworks[label] += 1
    elif path == "pubspec.yaml" and "flutter:" in content:
        findings.frameworks["Flutter"] += 1
    elif path == "tsconfig.json" and _TS_STRICT_RE.search(content):
        findings.conventions["TypeScript strict mode"] += 1
    elif path in {"pnpm-lock.yaml", "yarn.lock", "package-lock.json", "bun.lockb"}:
        findings.package_managers[
            {
                "pnpm-lock.yaml": "pnpm",
                "yarn.lock": "yarn",
                "package-lock.json": "npm",
                "bun.lockb": "bun",
            }[path]
        ] += 1


_MANIFESTS = (
    "package.json",
    "composer.json",
    "pubspec.yaml",
    "tsconfig.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
)


async def scan(
    client: httpx.AsyncClient, username: str, *, token: str | None, max_repos: int = 20
) -> GitHubFindings:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{API}/user/repos" if token else f"{API}/users/{username}/repos"
    params: dict[str, str | int] = {"sort": "pushed", "per_page": max_repos}
    if token:
        params["affiliation"] = "owner"
    resp = await client.get(url, headers=headers, params=params)
    resp.raise_for_status()
    findings = GitHubFindings()
    for repo in resp.json()[:max_repos]:
        if repo.get("fork") or repo.get("archived"):
            continue
        findings.repos_scanned += 1
        if language := repo.get("language"):
            findings.languages[language] += 1
        full = repo["full_name"]
        tree = await client.get(f"{API}/repos/{full}/contents", headers=headers)
        if tree.status_code != 200:
            continue
        names = {item["name"] for item in tree.json() if item.get("type") == "file"}
        for manifest in _MANIFESTS:
            if manifest not in names:
                continue
            if manifest.endswith((".lock", "-lock.yaml", "lock.json")):
                analyze_manifest(manifest, "", findings)
                continue
            file = await client.get(f"{API}/repos/{full}/contents/{manifest}", headers=headers)
            if file.status_code == 200 and file.json().get("encoding") == "base64":
                content = base64.b64decode(file.json()["content"]).decode("utf-8", "replace")
                analyze_manifest(manifest, content, findings)
    return findings


def to_suggestions(findings: GitHubFindings) -> dict[str, object]:
    if findings.repos_scanned == 0:
        return {}
    threshold = max(1, findings.repos_scanned // 5)
    stacks = [name for name, count in findings.frameworks.most_common(8) if count >= threshold]
    conventions = [
        name for name, count in findings.conventions.most_common(10) if count >= threshold
    ]
    if findings.package_managers:
        conventions.append(f"{findings.package_managers.most_common(1)[0][0]} as package manager")
    suggestions: dict[str, object] = {}
    if stacks:
        suggestions["engineering.primary_stacks"] = stacks
    if conventions:
        suggestions["engineering.conventions"] = conventions
    langs = [name for name, _ in findings.languages.most_common(5)]
    if langs:
        suggestions["engineering.also_uses"] = langs
    return suggestions
