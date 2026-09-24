# 0005: Run on the owner's Windows PC with WSL2 and Docker Engine

- Status: accepted
- Date: 2026-09-24

## Context

The owner chose to host Jarvis on an always-on Windows PC with an NVIDIA GPU
(12 GB+) for $0. It must come back after power cuts and Windows updates, with
nobody logged in.

- **Docker Desktop** only runs while a user is logged in, and its GPU support
  in WSL is less predictable.
- **WSL shuts idle distributions down** unless told not to.

## Decision

- **Ubuntu 24.04 on WSL2** with systemd, running Docker Engine from Docker's
  apt repository and the NVIDIA Container Toolkit. The GPU driver comes from
  Windows.
- **`.wslconfig`:** `vmIdleTimeout=-1` and `instanceIdleTimeout=-1`.
- **A Task Scheduler task** runs `wsl.exe … jarvis-keepalive` at startup, plus
  at log-on as a fallback. It keeps the distribution alive; systemd starts
  Docker and the `jarvis` service.
- **No sleep on mains power.** Fast Startup is off, because otherwise startup
  tasks don't run after a normal shutdown.
- **`scripts/windows/setup.ps1`** automates all of it. `make doctor` checks it,
  and the runbook's reboot test proves it on the real PC.

## Consequences

- **Setup is one script, but only the owner can run the final proof** (the
  reboot test). CI can only lint and unit-test the PowerShell.
- **The stored-password logon type** is the most reliable way to start WSL
  before anyone logs in. `-NoStoredPassword` (S4U) is offered for people who
  won't store a password, with the trade-off documented.
- **Moving later** to a small Linux server or a cloud VM needs only
  `scripts/wsl/bootstrap.sh` and the same compose files.
