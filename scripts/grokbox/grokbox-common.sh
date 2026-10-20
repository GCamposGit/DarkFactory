#!/bin/bash
# grokbox-common.sh - shared library for grokbox-bootstrap/supervisor/watchdog (sourced, never executed).
# Versioned in the repo from the copy running on the grokbox VM (/workspace/darkfac/grokbox-common.sh) on 20/10/2026 (USR-315).
# No systemd here: process supervision = flock'd bash loops + mutual watchdog.
# DRYRUN=1 turns every mutator into a log line (used to validate the logic read-only).

export HOME="${HOME:-/home/box}"
DF="${DF:-/workspace/darkfac}"
REPO="${REPO:-$DF/DarkFactory}"
RUN="${RUN:-$DF/run}"
STATUS_FILE="${STATUS_FILE:-$DF/status.json}"
ALERTS_LOG="${ALERTS_LOG:-$DF/alerts.log}"
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
VENV="$REPO/.venv"; VENV_PY="$VENV/bin/python"
TSDIR=/workspace/tools/tailscale_1.104.1_amd64
TS_STATE=/workspace/tailscale-state/tailscaled.state
TS_BACKUP_DIR=/workspace/tailscale-state/backup
TS_AUTHKEY_FILE=/workspace/tailscale-state/authkey   # OPTIONAL (see README): tagged reusable key, chmod 600
TS_HOST=grokbox
TS_EXPECT_IP=100.77.130.5
TS_LOG=/workspace/ts-up.log
PORT=8080
NODE_ID=grokbox
EXPECTED_HARNESSES="codex grok claude"
WORKER_TOKEN_FILE="$DF/worker.token"                  # OPTIONAL: only if DARKFAC_WORKER_TOKEN is ever enabled
STATE_DIR="${DARKFAC_WORKER_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/darkfac/worker}"
RETENTION_DAYS=7
HB_STALE_SEC=900          # peer watchdog/supervisor considered hung after this many seconds without heartbeat
# python MUST resolve to the venv (a missing `python` made tests fail before); claude/codex/grok live in ~/.local/bin
WORKER_PATH="$VENV/bin:$HOME/.local/bin:$HOME/.grok/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games"
export PATH="$WORKER_PATH"
mkdir -p "$RUN" 2>/dev/null

LOGFILE="${LOGFILE:-$DF/supervisor.log}"
dry(){ [ "${DRYRUN:-0}" = "1" ]; }
log(){ echo "[$(date '+%F %T %z')] $*" >> "$LOGFILE"; [ "${LOG_STDOUT:-0}" = "1" ] && echo "$*"; return 0; }
now(){ date +%s; }
mtime(){ stat -c %Y "$1" 2>/dev/null || echo 0; }
age(){ echo $(( $(now) - $(mtime "$1") )); }
hb(){ touch "$RUN/$1.heartbeat" 2>/dev/null; }

# ---------- locks / detached spawn (children must NOT inherit lock fds 200-203) ----------
lock_free(){ ( flock -n 9 ) 9>>"$1" 2>/dev/null; }          # 0 => nobody holds it
with_lock(){ local name=$1 wait=$2; shift 2; ( flock -w "$wait" 203 || exit 75; "$@" ) 203>>"$RUN/$name.lock"; }
spawn_detached(){ local lf=$1; shift; dry && { log "DRY spawn: $*"; return 0; }
  ( exec 200>&- 201>&- 202>&- 203>&-; exec setsid nohup "$@" ) >>"$lf" 2>&1 </dev/null & }

# ---------- alerts (state kept in the long-lived supervisor process) ----------
declare -A ALERT_MSG ALERT_LVL ALERT_SINCE
alert(){ local key=$1 lvl=$2; shift 2; local msg="$*"
  if [ -z "${ALERT_MSG[$key]:-}" ]; then ALERT_SINCE[$key]=$(now); echo "[$(date '+%F %T %z')] RAISE $lvl $key: $msg" >> "$ALERTS_LOG"; fi
  ALERT_MSG[$key]=$msg; ALERT_LVL[$key]=$lvl; }
clear_alert(){ local key=$1; [ -n "${ALERT_MSG[$key]:-}" ] || return 0
  echo "[$(date '+%F %T %z')] CLEAR $key (was: ${ALERT_MSG[$key]})" >> "$ALERTS_LOG"
  unset "ALERT_MSG[$key]" "ALERT_LVL[$key]" "ALERT_SINCE[$key]"; }
alerts_json(){ local k; { for k in "${!ALERT_MSG[@]}"; do
    jq -nc --arg k "$k" --arg l "${ALERT_LVL[$k]}" --arg m "${ALERT_MSG[$k]}" --argjson s "${ALERT_SINCE[$k]}" '{key:$k,level:$l,since:$s,message:$m}'; done; } | jq -sc '.'; }
has_critical(){ local k; for k in "${!ALERT_LVL[@]}"; do [ "${ALERT_LVL[$k]}" = critical ] && return 0; done; return 1; }

# ---------- binaries ----------
find_uv(){ command -v uv 2>/dev/null || { [ -x /workspace/tools/bin/uv ] && echo /workspace/tools/bin/uv; } || { [ -x "$HOME/.local/bin/uv" ] && echo "$HOME/.local/bin/uv"; }; }
ensure_symlinks(){ dry && { log "DRY ensure_symlinks"; return 0; }
  local b; for b in tailscale tailscaled; do [ -x /usr/local/bin/$b ] || { sudo -n ln -sf "$TSDIR/$b" /usr/local/bin/$b && log "symlink restored: $b"; }; done
  if ! command -v uv >/dev/null 2>&1 && [ -x /workspace/tools/bin/uv ]; then sudo -n ln -sf /workspace/tools/bin/uv /usr/local/bin/uv && log "uv restored from /workspace/tools/bin"; fi
  if [ ! -x /workspace/tools/bin/uv ] && command -v uv >/dev/null 2>&1; then mkdir -p /workspace/tools/bin && cp "$(command -v uv)" /workspace/tools/bin/uv; fi
  return 0; }

# ---------- tailscale ----------
ts_cli(){ timeout 15 "$TSDIR/tailscale" "$@"; }
ts_json(){ ts_cli status --json 2>/dev/null; }
ts_self_ip(){ ts_json | jq -r '.Self.TailscaleIPs[0] // empty' 2>/dev/null; }
TS_GOLDEN=/workspace/tailscale-state/tailscaled.state.golden   # identity of the ORIGINAL grokbox (IP $TS_EXPECT_IP); only refreshed while self IP == expected
# restore the golden identity (avoids a duplicate device that a fresh auth-key login creates). Returns 0 if it came back with the expected IP.
ts_restore_golden(){ dry && { log "DRY ts_restore_golden"; return 0; }
  [ -s "$TS_GOLDEN" ] || { log "no golden tailscale state; cannot restore"; return 1; }
  [ "$(ts_self_ip)" = "$TS_EXPECT_IP" ] && return 0
  log "restoring golden tailscale identity ($TS_EXPECT_IP)"
  [ -f "$TS_STATE" ] && sudo -n install -m 600 -o "$(id -u)" -g "$(id -g)" "$TS_STATE" "$TS_BACKUP_DIR/tailscaled.state.pre-restore-$(date +%Y%m%d-%H%M%S)"
  sudo -n pkill -TERM -x tailscaled; local i; for i in $(seq 1 10); do pgrep -x tailscaled >/dev/null || break; sleep 1; done
  pgrep -x tailscaled >/dev/null && sudo -n pkill -KILL -x tailscaled; sleep 1
  sudo -n install -m 600 -o root -g root "$TS_GOLDEN" "$TS_STATE"
  _start_tailscaled; sudo -n timeout 60 "$TSDIR/tailscale" up --hostname="$TS_HOST" --timeout=30s >> "$LOG_TSUP" 2>&1
  for i in $(seq 1 10); do [ "$(ts_self_ip)" = "$TS_EXPECT_IP" ] && { log "golden identity restored, ip=$TS_EXPECT_IP"; return 0; }; sleep 2; done
  log "golden restore did not reach $TS_EXPECT_IP (ip=$(ts_self_ip))"; return 1; }
ts_up(){ dry && { log "DRY tailscale up"; return 0; }
  local extra=(); local bs; bs=$(ts_json | jq -r '.BackendState // empty' 2>/dev/null)
  if [ "$bs" = NeedsLogin ]; then
    # 1) try the golden identity first: an auth-key login would register a NEW duplicate device (grokbox-1, new IP)
    ts_restore_golden && return 0
    # 2) last resort: auth key (may create a duplicate; alert so the admin removes it)
    if [ -r "$TS_AUTHKEY_FILE" ]; then extra=(--auth-key="file:$TS_AUTHKEY_FILE"); log "NeedsLogin: golden restore failed, using $TS_AUTHKEY_FILE (may create duplicate device)"; alert ts_dup warn "tailscale re-login with auth key; check for a duplicate device and IP != $TS_EXPECT_IP"; fi
  fi
  sudo -n timeout 60 "$TSDIR/tailscale" up --hostname="$TS_HOST" --timeout=30s "${extra[@]}" >> "$LOG_TSUP" 2>&1; }
LOG_TSUP="${LOG_TSUP:-$DF/supervisor.log}"
_start_tailscaled(){ pgrep -x tailscaled >/dev/null && return 0
  ensure_symlinks
  # ALWAYS the persistent state in /workspace (the old supervisor used /var/lib/... = new identity/IP on box wipe)
  ( exec 200>&- 201>&- 202>&- 203>&-; exec sudo -n setsid nohup "$TSDIR/tailscaled" --state="$TS_STATE" ) >> "$TS_LOG" 2>&1 </dev/null &
  local i; for i in $(seq 1 20); do sleep 1; [ -n "$(ts_json | jq -r '.BackendState // empty' 2>/dev/null)" ] && return 0; done; return 1; }
start_tailscaled(){ dry && { log "DRY start_tailscaled"; return 0; }; with_lock tailscaled 90 _start_tailscaled; }
restart_tailscaled(){ dry && { log "DRY restart_tailscaled ($*)"; return 0; }
  log "restarting tailscaled ($*)"; sudo -n pkill -TERM -x tailscaled; local i
  for i in $(seq 1 10); do pgrep -x tailscaled >/dev/null || break; sleep 1; done
  pgrep -x tailscaled >/dev/null && sudo -n pkill -KILL -x tailscaled; sleep 1
  start_tailscaled; ts_up; }
backup_ts_state(){ dry && return 0; mkdir -p "$TS_BACKUP_DIR"; local day; day=$(date +%F); local dst="$TS_BACKUP_DIR/tailscaled.state.$day"
  [ -f "$dst" ] && return 0; [ "$(ts_json | jq -r '.BackendState // empty' 2>/dev/null)" = Running ] || return 0
  [ "$(ts_self_ip)" = "$TS_EXPECT_IP" ] || { log "backup_ts_state skipped: self ip $(ts_self_ip) != $TS_EXPECT_IP"; return 0; }
  sudo -n install -m 600 -o "$(id -u)" -g "$(id -g)" "$TS_STATE" "$TS_GOLDEN"
  sudo -n install -m 600 -o "$(id -u)" -g "$(id -g)" "$TS_STATE" "$dst" && log "tailscaled.state backed up -> $dst"
  ls -1t "$TS_BACKUP_DIR"/tailscaled.state.* 2>/dev/null | tail -n +8 | xargs -r rm -f; }
protect_oom(){ # protect_oom <pid> <adj>: only a FEW critical procs (tailscaled, supervisors); never the worker (its test children inherit it)
  dry && return 0; local pid=$1 adj=$2 f=/proc/$1/oom_score_adj; [ -w "$f" ] || [ -e "$f" ] || return 0
  [ "$(cat "$f" 2>/dev/null)" = "$adj" ] || echo "$adj" | sudo -n tee "$f" >/dev/null 2>&1; }

# ---------- worker ----------
port_pids(){ ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u; }
worker_pids(){ pgrep -u "$(id -u)" -f '^[^ ]*python[0-9.]* ([^ ]*/)?core/harness/remote_worker\.py' | grep -vx "$$" ; }
worker_curl(){ local tok=(); [ -r "$WORKER_TOKEN_FILE" ] && tok=(-H "Authorization: Bearer $(tr -d '\n' < "$WORKER_TOKEN_FILE")"); curl -sf -m "${CURL_T:-8}" "${tok[@]}" "$@"; }
worker_health(){ local j; j=$(CURL_T=8 worker_curl "localhost:$PORT/health") || return 1
  echo "$j" | jq -e '(.status=="ok" or .status=="busy")' >/dev/null 2>&1 || return 1; echo "$j"; }
# jobs queued/running on disk whose owner (worker) pid is alive -> never restart under them
active_jobs_on_disk(){ local n=0 f pid st; for f in "$STATE_DIR"/jobs/*.json; do [ -f "$f" ] || continue
    st=$(jq -r '.status // empty' "$f" 2>/dev/null); case "$st" in queued|running) ;; *) continue;; esac
    pid=$(jq -r '.owner_pid // empty' "$f" 2>/dev/null); [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && n=$((n+1)); done; echo $n; }
worker_idle(){ local h; if h=$(worker_health); then [ "$(echo "$h" | jq -r '(.busy|not) and (.queue_length==0) and (.active_runs==0)')" = true ] && [ "$(active_jobs_on_disk)" = 0 ]
  else [ "$(active_jobs_on_disk)" = 0 ]; fi; }
_start_worker(){ local pids; pids=$(port_pids)
  if [ -n "$pids" ] && worker_health >/dev/null; then return 0; fi
  if [ -n "$pids" ]; then local p c; for p in $pids; do c=$(tr '\0' ' ' < /proc/$p/cmdline 2>/dev/null)
      case "$c" in *remote_worker.py*) log "port $PORT held by stale worker pid $p; terminating"; kill -TERM "$p"; sleep 5; kill -KILL "$p" 2>/dev/null;;
        *) echo "port $PORT held by foreign process pid $p ($c)" > "$RUN/port_busy.msg"; log "port $PORT held by foreign pid $p: not touching it"; return 3;; esac; done; fi
  rm -f "$RUN/port_busy.msg"
  ( cd "$REPO" || exit 1; exec 200>&- 201>&- 202>&- 203>&-
    exec env PATH="$WORKER_PATH" HOME="$HOME" setsid nohup "$VENV_PY" core/harness/remote_worker.py --host 0.0.0.0 --port "$PORT" --node-id "$NODE_ID" ) >> "$DF/worker.log" 2>&1 </dev/null &
  local i; for i in $(seq 1 45); do sleep 2; worker_health >/dev/null && return 0; done; return 1; }
start_worker(){ dry && { log "DRY start_worker ($*)"; return 0; }; venv_repairing && { log "start deferred: venv repair in progress"; return 4; }; echo "$(now) $*" >> "$RUN/worker-restarts.log"
  log "starting worker ($*)"; with_lock worker 120 _start_worker; }
stop_worker(){ dry && { log "DRY stop_worker ($*)"; return 0; }; local p; log "stopping worker ($*)"
  for p in $(worker_pids); do kill -TERM "$p"; done; local i; for i in $(seq 1 15); do [ -z "$(worker_pids)" ] && return 0; sleep 1; done
  for p in $(worker_pids); do kill -KILL "$p"; done; sleep 1; }

# ---------- venv ----------
venv_ok(){ dry && { [ -x "$VENV_PY" ]; return; }; [ -x "$VENV_PY" ] && ( cd "$REPO" && PYTHONDONTWRITEBYTECODE=1 DARKFAC_WORKER_STATE_DIR="$(mktemp -d)" timeout 90 "$VENV_PY" -c "import fastapi, uvicorn, pydantic, core.harness.remote_worker" >/dev/null 2>&1 ); }
ensure_pip(){ dry && return 0; "$VENV_PY" -m pip --version >/dev/null 2>&1 && return 0; local uv; uv=$(find_uv) || return 1
  # worker._sync_requirements runs `python -m pip install`; uv venvs ship without pip -> silent failure
  "$uv" pip install -q --python "$VENV_PY" pip >> "$DF/bootstrap.log" 2>&1 && log "pip installed in venv"; }
sync_requirements(){ dry && { log "DRY sync_requirements"; return 0; }; local uv h; uv=$(find_uv) || return 1; [ -f "$REPO/requirements.txt" ] || return 0
  h=$(sha256sum "$REPO/requirements.txt" | cut -d' ' -f1); [ "$(cat "$RUN/requirements.sha256" 2>/dev/null)" = "$h" ] && return 0
  if (cd "$REPO" && timeout 600 "$uv" pip install -q --python "$VENV_PY" -r requirements.txt) >> "$DF/bootstrap.log" 2>&1; then echo "$h" > "$RUN/requirements.sha256"; log "requirements synced"; else log "requirements sync FAILED"; return 1; fi; }
_build_venv(){ local uv; uv=$(find_uv) || { log "uv not found"; return 1; }; cd "$REPO" || return 1
  "$uv" venv --python 3.13 .venv >> "$DF/bootstrap.log" 2>&1 && "$uv" pip install -q --python .venv/bin/python -r requirements.txt >> "$DF/bootstrap.log" 2>&1 && "$uv" pip install -q --python .venv/bin/python pip >> "$DF/bootstrap.log" 2>&1; }
# rebuild with rollback; callers must have verified the worker is idle (or stopped) first
venv_repairing(){ [ -f "$RUN/venv-repair.flag" ] && [ "$(age "$RUN/venv-repair.flag")" -lt 1500 ]; }
repair_venv(){ dry && { log "DRY repair_venv"; return 0; }; log "venv broken/missing: rebuilding (rollback kept)"; touch "$RUN/venv-repair.flag"
  ( cd "$REPO" || exit 1; [ -d .venv ] && mv .venv ".venv.bad.$$"
    if _build_venv && venv_ok; then rm -rf .venv.bad.*; log "venv rebuilt OK"; else rm -rf .venv; [ -d ".venv.bad.$$" ] && mv ".venv.bad.$$" .venv; log "venv rebuild FAILED; previous venv restored"; exit 1; fi ); local rc=$?; rm -f "$RUN/venv-repair.flag"; return $rc; }

# ---------- log rotation (copytruncate: writers use O_APPEND `>>`, so truncate in place is safe) ----------
rotate_log(){ local f=$1 max=${2:-5242880} keep=${3:-5} sz i; [ -f "$f" ] || return 0; sz=$(stat -c %s "$f"); [ "$sz" -gt "$max" ] || return 0
  dry && { log "DRY rotate $f ($sz bytes)"; return 0; }
  for ((i=keep-1;i>=1;i--)); do [ -f "$f.$i.gz" ] && mv -f "$f.$i.gz" "$f.$((i+1)).gz"; done
  rm -f "$f.$((keep+1)).gz"; cp -p "$f" "$f.1" && : > "$f" && gzip -f "$f.1"; log "rotated $f ($sz bytes)"; }
rotate_all(){ rotate_log "$DF/worker.log" 20971520 5; rotate_log "$TS_LOG" 10485760 5
  local f; for f in supervisor bootstrap watchdog alerts; do rotate_log "$DF/$f.log" 5242880 5; done
  find "$DF" /workspace -maxdepth 1 -name '*.log.*.gz' -mtime +14 -delete 2>/dev/null; return 0; }

# ---------- cleanup of OLD worker state only (> RETENTION_DAYS) ----------
cleanup_old(){ local d=${1:-$RETENTION_DAYS} n=0 f stem
  dry && { local c; c=$(find "$STATE_DIR/logs" "$STATE_DIR/jobs" -type f -mtime +"$d" 2>/dev/null | wc -l); log "DRY cleanup_old: $c files older than ${d}d"; return 0; }
  while IFS= read -r -d '' f; do rm -f "$f" && n=$((n+1)); done < <(find "$STATE_DIR/logs" "$STATE_DIR/jobs" -type f \( -name '*.log' -o -name '*.json' -o -name '*.tmp' \) -mtime +"$d" -print0 2>/dev/null)
  [ -d "$STATE_DIR/bundles" ] && find "$STATE_DIR/bundles" -type f -mtime +"$d" -delete 2>/dev/null
  [ "$n" -gt 0 ] && log "cleanup: removed $n worker log/job files older than ${d}d"
  find /tmp -maxdepth 1 -user "$(id -un)" -name 'darkfac*' -mtime +"$d" -exec rm -rf {} + 2>/dev/null
  [ -d /tmp/pytest-of-$(id -un) ] && find /tmp/pytest-of-$(id -un) -mindepth 1 -maxdepth 1 -mtime +3 -exec rm -rf {} + 2>/dev/null
  return 0; }
cleanup_orphan_worktrees(){ local d=${1:-1} w n=0; [ -d "$STATE_DIR/worktrees" ] || return 0
  dry && { log "DRY orphan worktrees: $(find "$STATE_DIR/worktrees" -mindepth 1 -maxdepth 1 -type d -mtime +"$d" | wc -l)"; return 0; }
  worker_idle || return 0
  for w in $(find "$STATE_DIR/worktrees" -mindepth 1 -maxdepth 1 -type d -mtime +"$d"); do rm -rf "$w" && n=$((n+1)); done
  [ "$n" -gt 0 ] && { git -C "$REPO" worktree prune; log "cleanup: removed $n orphan worktrees (> ${d}d, worker idle)"; }; return 0; }
emergency_disk_cleanup(){ dry && { log "DRY emergency_disk_cleanup"; return 0; }; log "EMERGENCY disk cleanup"
  cleanup_old 1; cleanup_orphan_worktrees 0; find "$DF" /workspace -maxdepth 1 -name '*.log.*.gz' -delete 2>/dev/null
  local uv; uv=$(find_uv) && "$uv" cache prune >> "$DF/supervisor.log" 2>&1; }

# ---------- misc probes ----------
internet_ok(){ curl -s -o /dev/null -m 6 https://controlplane.tailscale.com/; }   # any HTTP answer = path to the internet works
clock_drift(){ local d; d=$(curl -sI -m 8 https://controlplane.tailscale.com/ 2>/dev/null | tr -d '\r' | awk -F': ' 'tolower($1)=="date"{print $2}'); [ -n "$d" ] || return 1; echo $(( $(date +%s) - $(date -d "$d" +%s) )); }
