#!/usr/bin/env bash
# Idempotent installer for the DarkFac test worker daemon
# (core/harness/remote_worker.py) as a systemd --user service on Linux
# (e.g. the VPS, if the owner chooses to add it to DARKFAC_TEST_WORKERS
# alongside -- never instead of -- the Desktop, which is the owner-decided
# ALWAYS-primary worker; see docs/HARNESS_INTEROP.md).
#
# Usage:
#   scripts/install_test_worker.sh [-r REPO_PATH] [-t TOKEN] [-p PORT] [-n NODE_ID]
#
#   -r REPO_PATH   Repo location (default: current directory's git root, or
#                   ~/DarkFac if not run from inside a clone)
#   -t TOKEN       Optional DARKFAC_WORKER_TOKEN bearer token
#   -p PORT        TCP port (default: 8080)
#   -n NODE_ID     Node id reported by /health (default: hostname, lowercased)
#
# Safe to re-run: re-clones only if missing, otherwise `git pull`; the
# systemd unit is (re)written and reloaded each time.
#
# Windows counterpart: scripts/install_test_worker.ps1 (the Desktop).

set -euo pipefail

REPO_PATH=""
TOKEN=""
PORT="8080"
NODE_ID=""

while getopts "r:t:p:n:h" opt; do
  case "$opt" in
    r) REPO_PATH="$OPTARG" ;;
    t) TOKEN="$OPTARG" ;;
    p) PORT="$OPTARG" ;;
    n) NODE_ID="$OPTARG" ;;
    h)
      echo "Usage: $0 [-r REPO_PATH] [-t TOKEN] [-p PORT] [-n NODE_ID]"
      exit 0
      ;;
    *)
      echo "Unknown option" >&2
      exit 1
      ;;
  esac
done

echo "======================================================================"
echo " DarkFac Test Worker Installer (Linux / systemd --user) -- HF-27-11"
echo "======================================================================"

echo
echo "== Step 1/6: checking prerequisites (python3.12+, git) =="

if ! command -v git >/dev/null 2>&1; then
  echo "[FAIL] git not found on PATH. Install it (e.g. 'apt install git') and re-run." >&2
  exit 1
fi
echo "[OK] $(git --version)"

PYTHON_BIN="$(command -v python3.12 || command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
  echo "[FAIL] No python3.12/python3 found on PATH. Install Python 3.12+ and re-run." >&2
  exit 1
fi
PY_VERSION="$("$PYTHON_BIN" --version 2>&1)"
echo "[OK] $PY_VERSION ($PYTHON_BIN)"
case "$PY_VERSION" in
  "Python 3.1"[2-9]*|"Python 3."[2-9][0-9]*) ;;
  *) echo "[WARN] Expected Python 3.12+; found '$PY_VERSION'. Continuing anyway." ;;
esac

if [ -z "$REPO_PATH" ]; then
  if REPO_PATH="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    :
  else
    REPO_PATH="$HOME/DarkFac"
  fi
fi

echo
echo "== Step 2/6: syncing the repository at $REPO_PATH =="

if [ ! -d "$REPO_PATH" ]; then
  echo "[INFO] '$REPO_PATH' does not exist; cloning https://github.com/GCamposGit/DarkFactory.git ..."
  mkdir -p "$(dirname "$REPO_PATH")"
  git clone https://github.com/GCamposGit/DarkFactory.git "$REPO_PATH"
  echo "[OK] Cloned into $REPO_PATH"
elif [ -d "$REPO_PATH/.git" ]; then
  echo "[INFO] Repo already present; running 'git pull' ..."
  git -C "$REPO_PATH" pull || echo "[WARN] git pull failed -- continuing with the current checkout."
else
  echo "[FAIL] '$REPO_PATH' exists but is not a git repository." >&2
  exit 1
fi

REMOTE_WORKER_SCRIPT="$REPO_PATH/core/harness/remote_worker.py"
if [ ! -f "$REMOTE_WORKER_SCRIPT" ]; then
  echo "[FAIL] Cannot find '$REMOTE_WORKER_SCRIPT'. Is REPO_PATH a real DarkFac checkout?" >&2
  exit 1
fi

echo
echo "== Step 3/6: installing Python dependencies =="

if [ -f "$REPO_PATH/requirements.txt" ]; then
  "$PYTHON_BIN" -m pip install -q -r "$REPO_PATH/requirements.txt" \
    || echo "[WARN] pip install failed -- the worker may not start (missing fastapi/uvicorn)."
  echo "[OK] Dependencies installed/up to date."
else
  echo "[WARN] requirements.txt not found; skipping."
fi

if [ -z "$NODE_ID" ]; then
  NODE_ID="$(hostname | tr '[:upper:]' '[:lower:]')"
fi

echo
echo "== Step 4/6: writing the systemd --user unit =="

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
UNIT_PATH="$UNIT_DIR/darkfac-test-worker.service"

TOKEN_ENV_LINE=""
if [ -n "$TOKEN" ]; then
  TOKEN_ENV_LINE="Environment=DARKFAC_WORKER_TOKEN=$TOKEN"
fi

cat > "$UNIT_PATH" <<UNIT
[Unit]
Description=DarkFac primary validation-harness test worker (core/harness/remote_worker.py)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_PATH
ExecStart=$PYTHON_BIN core/harness/remote_worker.py --host 0.0.0.0 --port $PORT --node-id $NODE_ID
Restart=always
RestartSec=5
$TOKEN_ENV_LINE

[Install]
WantedBy=default.target
UNIT

echo "[OK] Wrote $UNIT_PATH"

systemctl --user daemon-reload
systemctl --user enable darkfac-test-worker.service
systemctl --user restart darkfac-test-worker.service
echo "[OK] Service enabled (survives reboot via 'loginctl enable-linger \$USER' if this user has no active login session) and (re)started."

echo
echo "== Step 5/6: firewall reminder =="
echo "[INFO] This script does NOT touch the firewall (ufw/iptables/cloud provider security groups"
echo "       vary too much to automate safely). Restrict inbound TCP $PORT to the Tailscale CGNAT"
echo "       range yourself, e.g. with ufw:"
echo "         sudo ufw allow from 100.64.0.0/10 to any port $PORT proto tcp"

echo
echo "== Step 6/6: verifying health =="

HEALTH_OK=0
for _ in $(seq 1 10); do
  sleep 1
  if curl -fsS "http://127.0.0.1:$PORT/health" >/tmp/darkfac_worker_health.json 2>/dev/null; then
    HEALTH_OK=1
    break
  fi
done

if [ "$HEALTH_OK" = "1" ]; then
  echo "[OK] Worker responded:"
  cat /tmp/darkfac_worker_health.json
  echo
else
  echo "[FAIL] http://127.0.0.1:$PORT/health did not respond within 10s."
  echo "       Check: systemctl --user status darkfac-test-worker.service"
  echo "              journalctl --user -u darkfac-test-worker.service -n 50"
fi

echo
echo "======================================================================"
echo " SUMMARY"
echo "======================================================================"
echo " Repo path        : $REPO_PATH"
echo " systemd unit     : $UNIT_PATH (systemctl --user {status,restart,stop} darkfac-test-worker.service)"
echo " Local health URL : http://127.0.0.1:$PORT/health"
echo " Auth token       : $([ -n "$TOKEN" ] && echo set || echo 'not set (open on tailnet)')"
echo "======================================================================"
