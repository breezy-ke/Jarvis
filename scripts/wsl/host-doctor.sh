#!/usr/bin/env bash
# Host checks for `make doctor`: the parts of the setup the Jarvis container
# can't see (Docker, GPU, autostart, Windows boot task, Tailscale, disk).
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

problems=0
ok() { printf '✔ %s\n' "$1"; }
warn() {
  printf '⚠ %s\n' "$1"
  [[ -z "${2:-}" ]] || printf '    fix: %s\n' "$2"
}
fail() {
  printf '✘ %s\n' "$1"
  [[ -z "${2:-}" ]] || printf '    fix: %s\n' "$2"
  problems=$((problems + 1))
}

is_wsl=0
grep -qiE 'microsoft|wsl' /proc/sys/kernel/osrelease 2>/dev/null && is_wsl=1

echo "Host"
if [[ "$(ps -p 1 -o comm= 2>/dev/null)" == "systemd" ]]; then
  ok "systemd is running"
else
  fail "systemd is not running, so nothing starts by itself" "sudo scripts/wsl/bootstrap.sh"
fi

if ! command -v docker >/dev/null 2>&1; then
  fail "Docker is not installed" "sudo scripts/wsl/bootstrap.sh"
elif ! docker info >/dev/null 2>&1; then
  fail "can't talk to Docker" "sudo systemctl start docker; if that's not it, open a new terminal so your account picks up the docker group"
else
  ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null) with Compose $(docker compose version --short 2>/dev/null)"
fi

vram=$(scripts/wsl/detect-gpu.sh --vram)
if [[ "$(scripts/wsl/detect-gpu.sh)" == "1" ]]; then
  ok "NVIDIA GPU available to containers (${vram} GB VRAM)"
elif [[ -n "$vram" ]]; then
  fail "found a GPU (${vram} GB) but Docker can't use it" "sudo scripts/wsl/bootstrap.sh (installs the NVIDIA Container Toolkit)"
else
  warn "no NVIDIA GPU found: the local model runs on the CPU (slowly); cloud models still work" \
    "install the latest NVIDIA driver on Windows (never inside Ubuntu), then run 'wsl --shutdown'"
fi

origin=""
if [[ ! -f .env ]]; then
  fail ".env is missing" "make secrets"
else
  perms=$(stat -c %a .env)
  if [[ "$perms" == "600" ]]; then
    ok ".env exists and only you can read it"
  else
    warn ".env can be read by other accounts (mode $perms)" "chmod 600 .env"
  fi
  origin=$(grep -E '^JARVIS_PUBLIC_ORIGIN=' .env | tail -n1 | cut -d= -f2- | sed -E 's/[[:space:]]+#.*$//; s/[[:space:]]+$//')
  if [[ -z "$origin" || "$origin" == *your-tailnet* ]]; then
    fail "JARVIS_PUBLIC_ORIGIN isn't set in .env" \
      "set it to the https:// address that 'tailscale serve status' prints on Windows, then: make up"
  fi
fi

if systemctl is-enabled --quiet jarvis.service 2>/dev/null; then
  ok "Jarvis starts by itself whenever WSL starts"
else
  warn "Jarvis won't start by itself after a reboot" "sudo scripts/wsl/bootstrap.sh"
fi

free_gb=$(df -BG --output=avail / 2>/dev/null | tail -n1 | tr -dc '0-9')
if [[ -n "$free_gb" ]] && ((free_gb < 30)); then
  warn "only ${free_gb} GB free disk space (models and backups need room)" "docker system prune, or free space on the Windows drive"
elif [[ -n "$free_gb" ]]; then
  ok "${free_gb} GB free disk space"
fi

if [[ $is_wsl == 1 ]]; then
  echo
  echo "Windows"
  if ! command -v powershell.exe >/dev/null 2>&1; then
    warn "can't reach Windows from here (interop is off), so Windows checks were skipped"
  else
    # One PowerShell call for everything: it takes a second or two to start.
    # shellcheck disable=SC2016  # $variables here are PowerShell's, not bash's
    win=$(powershell.exe -NoProfile -NonInteractive -Command '
      $task = Get-ScheduledTask -TaskName "Jarvis" -ErrorAction SilentlyContinue
      "task=" + $(if ($task) { $task.State } else { "missing" })
      $cfg = Join-Path $env:USERPROFILE ".wslconfig"
      "wslconfig=" + $(if (Test-Path $cfg) { (Get-Content $cfg) -join ";" } else { "" })
      $power = Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power" -ErrorAction SilentlyContinue
      "hiberboot=" + $power.HiberbootEnabled
      $ts = Join-Path $env:ProgramFiles "Tailscale\tailscale.exe"
      if (Test-Path $ts) {
        $s = & $ts status --json | ConvertFrom-Json
        "tailscale=" + $s.BackendState
        "tsname=" + $s.Self.DNSName
        "serve=" + ((& $ts serve status 2>&1) -join " ")
      } else { "tailscale=missing" }
    ' 2>/dev/null | tr -d '\r')
    get() { sed -n "s/^$1=//p" <<<"$win" | head -n1; }

    case "$(get task)" in
      Ready | Running) ok "the Jarvis boot task is registered ($(get task))" ;;
      missing | "") fail "the Windows boot task is missing, so Jarvis stays off after a reboot until you log in" "run scripts/windows/setup.ps1 again (as administrator)" ;;
      *) warn "the Windows boot task is $(get task)" "enable it in Task Scheduler, or run scripts/windows/setup.ps1 again" ;;
    esac

    wslconfig=$(get wslconfig | tr -d ' ')
    if [[ "$wslconfig" == *"instanceIdleTimeout=-1"* && "$wslconfig" == *"vmIdleTimeout=-1"* ]]; then
      ok ".wslconfig keeps WSL running when idle"
    else
      fail ".wslconfig lets WSL shut down when idle" "run scripts/windows/setup.ps1 again, or add instanceIdleTimeout=-1 ([general]) and vmIdleTimeout=-1 ([wsl2])"
    fi

    if [[ "$(get hiberboot)" == "0" ]]; then
      ok "Fast Startup is off (boot tasks run after every shutdown)"
    else
      warn "Fast Startup is on: after a normal shutdown, Windows skips the Jarvis boot task" \
        "run scripts/windows/setup.ps1 again, or turn off 'Fast startup' in Control Panel > Power Options"
    fi

    tailscale=$(get tailscale)
    tsname=$(get tsname)
    tsname=${tsname%.}
    if [[ "$tailscale" == "missing" ]]; then
      fail "Tailscale isn't installed on Windows, so your phone can't reach Jarvis" "run scripts/windows/setup.ps1 again"
    elif [[ -z "$tailscale" ]]; then
      fail "Tailscale isn't running on Windows" "start Tailscale from the Start menu and sign in"
    elif [[ "$tailscale" != "Running" ]]; then
      fail "Tailscale is $tailscale" "open Tailscale on Windows and sign in"
    else
      ok "Tailscale is running as $tsname"
      if [[ "$(get serve)" == *"8080"* ]]; then
        ok "Tailscale Serve sends https://$tsname to Jarvis"
      else
        fail "Tailscale Serve isn't pointing at Jarvis" "in PowerShell: tailscale serve --bg 8080"
      fi
      if [[ -n "$origin" && "$origin" != "https://$tsname" ]]; then
        fail "JARVIS_PUBLIC_ORIGIN is $origin but your Tailscale address is https://$tsname" \
          "set JARVIS_PUBLIC_ORIGIN=https://$tsname in .env, then: make up"
      fi
    fi
  fi
fi

echo
exit $((problems > 0))
