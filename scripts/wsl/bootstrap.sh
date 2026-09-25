#!/usr/bin/env bash
# One-time setup of Ubuntu on WSL2 for Jarvis. Safe to run again.
#
#   cd ~/Jarvis && sudo scripts/wsl/bootstrap.sh
#
# What it does:
#   * turns on systemd (so Docker and Jarvis start whenever WSL starts)
#   * installs Docker Engine from Docker's apt repository (not Docker Desktop)
#   * if the Windows NVIDIA driver is present, installs the NVIDIA Container
#     Toolkit so the local AI model runs on your GPU
#   * installs the `jarvis` systemd service and the keep-alive command used by
#     the Windows boot task (scripts/windows/setup.ps1)
#
# Exit code 10 means systemd was just switched on: restart WSL, then run it again.
set -euo pipefail

log() { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

[[ $EUID -eq 0 ]] || die "run it with sudo: sudo $0"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TARGET_USER="${1:-${SUDO_USER:-}}"
[[ -n "$TARGET_USER" && "$TARGET_USER" != "root" ]] ||
  die "run it with sudo from your normal account (or pass your username: sudo $0 <user>)"
id "$TARGET_USER" >/dev/null 2>&1 || die "no such user: $TARGET_USER"

# shellcheck source=/dev/null
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || die "this script supports Ubuntu (24.04 recommended); found ${PRETTY_NAME:-unknown}"
[[ "${VERSION_ID:-}" == "24.04" ]] || warn "tested on Ubuntu 24.04; you have ${PRETTY_NAME:-unknown}"

case "$REPO_DIR" in
  /mnt/*) die "the repo is on the Windows drive ($REPO_DIR). Put it inside Ubuntu instead (for example ~/Jarvis): it's much faster there and file permissions work." ;;
esac

is_wsl() { grep -qiE 'microsoft|wsl' /proc/sys/kernel/osrelease 2>/dev/null; }

# --- systemd ---------------------------------------------------------------------
if is_wsl; then
  log "Checking that systemd is on (/etc/wsl.conf)"
  touch /etc/wsl.conf
  if grep -Eq '^[[:space:]]*systemd[[:space:]]*=' /etc/wsl.conf; then
    sed -i -E 's/^[[:space:]]*systemd[[:space:]]*=.*/systemd=true/' /etc/wsl.conf
  elif grep -q '^\[boot\]' /etc/wsl.conf; then
    sed -i '/^\[boot\]/a systemd=true' /etc/wsl.conf
  else
    printf '\n[boot]\nsystemd=true\n' >>/etc/wsl.conf
  fi
  if [[ "$(ps -p 1 -o comm= 2>/dev/null)" != "systemd" ]]; then
    echo "systemd is now switched on. Restart WSL, then run this script again."
    echo "  In Windows PowerShell:  wsl --terminate ${WSL_DISTRO_NAME:-Ubuntu-24.04}"
    exit 10
  fi
fi

if command -v docker >/dev/null 2>&1 && readlink -f "$(command -v docker)" | grep -q docker-desktop; then
  die "Docker Desktop's WSL integration is on for this distro. Turn it off (Docker Desktop > Settings > Resources > WSL integration) or uninstall Docker Desktop, then run this again."
fi

export DEBIAN_FRONTEND=noninteractive

log "Installing base packages"
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg git make jq

# --- Docker Engine (https://docs.docker.com/engine/install/ubuntu/) --------------
if ! dpkg -s docker-ce >/dev/null 2>&1; then
  log "Installing Docker Engine"
  mapfile -t conflicting < <(
    dpkg --get-selections docker.io docker-compose docker-compose-v2 docker-doc docker-buildx \
      podman-docker containerd runc 2>/dev/null | awk '$2 == "install" { print $1 }'
  )
  if ((${#conflicting[@]})); then
    apt-get remove -y "${conflicting[@]}"
  fi
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${UBUNTU_CODENAME:-$VERSION_CODENAME}
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  log "Docker Engine is already installed"
fi
systemctl enable --now containerd docker
usermod -aG docker "$TARGET_USER"

# --- NVIDIA Container Toolkit ----------------------------------------------------
# In WSL the GPU driver comes from Windows. Never install a Linux NVIDIA driver here.
SMI=/usr/lib/wsl/lib/nvidia-smi
command -v nvidia-smi >/dev/null 2>&1 && SMI=$(command -v nvidia-smi)
if [[ -x "$SMI" ]] && "$SMI" -L >/dev/null 2>&1; then
  log "Found $("$SMI" --query-gpu=name,memory.total --format=csv,noheader | head -n1)"
  if ! dpkg -s nvidia-container-toolkit >/dev/null 2>&1; then
    log "Installing the NVIDIA Container Toolkit"
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey |
      gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list |
      sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        >/etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get update -q
    apt-get install -y -q nvidia-container-toolkit
  fi
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
  log "Checking that containers can use the GPU"
  if docker run --rm --gpus all ubuntu:24.04 nvidia-smi -L; then
    echo "The GPU works inside containers."
  else
    warn "the GPU test container failed (see the error above). If it's a GPU error: update the NVIDIA driver on Windows, run 'wsl --update' and 'wsl --shutdown' in PowerShell, then run this script again."
  fi
elif is_wsl; then
  warn "no NVIDIA GPU visible. Install the latest NVIDIA driver on Windows (not inside Ubuntu), run 'wsl --shutdown', then run this again. Until then the local model runs on the CPU (slowly) and cloud models still work."
fi

# --- Start Jarvis whenever WSL boots ---------------------------------------------
log "Installing the jarvis service"
cat >/etc/systemd/system/jarvis.service <<EOF
[Unit]
Description=Jarvis personal assistant (Docker Compose stack)
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=$TARGET_USER
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/make --no-print-directory boot-up
ExecStop=/usr/bin/make --no-print-directory stop
TimeoutStartSec=20min
TimeoutStopSec=2min

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable jarvis.service

# The Windows boot task runs this. While it runs, WSL keeps Ubuntu (and with it
# Docker and Jarvis) alive, even when nobody is logged in to Windows.
cat >/usr/local/bin/jarvis-keepalive <<'EOF'
#!/bin/sh
echo "$(date -Is) Windows started WSL for Jarvis" >>/var/log/jarvis-boot.log
systemctl start --no-block jarvis.service 2>/dev/null || true
exec sleep infinity
EOF
chmod 755 /usr/local/bin/jarvis-keepalive
chmod +x "$REPO_DIR"/scripts/wsl/*.sh

log "Ubuntu is ready for Jarvis"
cat <<EOF

Next, as $TARGET_USER in $REPO_DIR (open a new Ubuntu window first so your
account picks up the docker group):

  make secrets       # creates .env with generated secrets
  nano .env          # set JARVIS_PUBLIC_ORIGIN to your Tailscale https:// address
  make up            # builds and starts Jarvis (the first build takes a few minutes)
  make pull-models   # downloads the local AI and speech models
  make doctor        # checks everything and explains any fixes

scripts/windows/setup.ps1 does all of this for you when you run it on Windows.
EOF
