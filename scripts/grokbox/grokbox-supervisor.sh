#!/bin/bash
# grokbox-supervisor.sh (v2) - keeps worker + tailscale healthy on grokbox, writes status.json.
# Versioned in the repo from /workspace/darkfac/grokbox-supervisor.sh on 20/10/2026 (USR-315). Change vs the VM copy:
# the "repo HEAD moved -> reload idle worker" rule runs EVERY tick (reload_if_repo_moved) with a 60 s minimum uptime,
# instead of inside deep_checks (every 10 ticks, 300 s uptime), so a pull reaches the worker in ~1-2 min.
# Flags: --once (one tick, then exit)  --dry-run (read-only: mutators only log; status goes to next/run-dryrun/)
DF="${DF:-/workspace/darkfac}"; ONCE=0
for a in "$@"; do case "$a" in --once) ONCE=1;; --dry-run) DRYRUN=1; LOG_STDOUT=1; DRYDIR="$DF/next/run-dryrun"; mkdir -p "$DRYDIR";
  STATUS_FILE="$DRYDIR/status.json"; ALERTS_LOG="$DRYDIR/alerts.log"; LOGFILE="$DRYDIR/supervisor.log";; esac; done
. "$(dirname "$(readlink -f "$0")")/grokbox-common.sh"
LOGFILE="${LOGFILE:-$DF/supervisor.log}"; LOG_TSUP="$LOGFILE"
hb(){ dry || touch "$RUN/$1.heartbeat"; }
if ! dry; then exec 200>>"$RUN/supervisor.lock"; flock -n 200 || exit 0; echo $$ > "$RUN/supervisor.pid"; fi
START_AT=$(now); TICK=0
W_BAD=0; W_Q_STUCK=0; W_INST=""; W_BOOT_SHA=""; W_SHA=""; W_UP=0; W_HEALTH='{}'; W_FAILS=0; W_NEXT_START=0; PROBE_BAD=0; VENV_BAD=0; PROBE_AT=0; VENV_AT=0
TS_BAD=0; TS_API_BAD=0; TS_NEXT=8; TS_STEP=16; TS_OUTAGE_START=0; TS_RESTARTS=0; TS_BACKEND=""; TS_ONLINE=""; TS_IP=""; TS_RELAY=""; TS_PEERS='[]'; TS_PING='{}'
DISK_PCT=0; INODE_PCT=0; MEM_AVAIL_MB=0; PSI60=0; LOAD1=0; DRIFT=""; EMERG_AT=0
REPO_HEAD=""; REPO_DIRTY=0; REPO_AB=""; CLEAN_AT=0; LAST_CLEAN_MSG=""
RELOAD_MIN_UPTIME_SEC="${RELOAD_MIN_UPTIME_SEC:-60}"
log "supervisor v2 started pid $$ (dry=${DRYDIR:+yes})"

# ---------------- resources ----------------
collect_resources(){
  DISK_PCT=$(df --output=pcent /workspace 2>/dev/null | tail -1 | tr -dc 0-9); DISK_PCT=${DISK_PCT:-0}
  INODE_PCT=$(df --output=ipcent / 2>/dev/null | tail -1 | tr -dc 0-9); INODE_PCT=${INODE_PCT:-0}
  MEM_AVAIL_MB=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
  PSI60=$(awk '/^some/{split($3,a,"=");print a[2]}' /proc/pressure/memory 2>/dev/null); PSI60=${PSI60:-0}
  LOAD1=$(cut -d' ' -f1 /proc/loadavg)
  if [ "$DISK_PCT" -ge 92 ]; then alert disk critical "disk ${DISK_PCT}% used"; [ $(( $(now) - EMERG_AT )) -gt 1800 ] && { EMERG_AT=$(now); emergency_disk_cleanup; }
  elif [ "$DISK_PCT" -ge 85 ]; then alert disk warn "disk ${DISK_PCT}% used"; else clear_alert disk; fi
  [ "$INODE_PCT" -ge 85 ] && alert inodes warn "inodes ${INODE_PCT}% used" || clear_alert inodes
  if [ "$MEM_AVAIL_MB" -lt 600 ]; then alert mem critical "MemAvailable ${MEM_AVAIL_MB}MB (OOM risk)"
  elif [ "$MEM_AVAIL_MB" -lt 1500 ]; then alert mem warn "MemAvailable ${MEM_AVAIL_MB}MB"; else clear_alert mem; fi
  awk -v p="$PSI60" 'BEGIN{exit !(p>20)}' && alert mem_pressure warn "memory PSI some avg60=${PSI60}%" || clear_alert mem_pressure
}

# ---------------- watchdog (mutual) ----------------
check_watchdog(){
  if lock_free "$RUN/watchdog.lock"; then log "watchdog not running; starting"; spawn_detached "$DF/watchdog.log" "$SCRIPT_DIR/grokbox-watchdog.sh"
  elif [ "$(age "$RUN/watchdog.heartbeat")" -gt "$HB_STALE_SEC" ]; then
    local p; p=$(cat "$RUN/watchdog.pid" 2>/dev/null); log "watchdog heartbeat stale; killing pid $p"; dry || { [ -n "$p" ] && kill -9 "$p" 2>/dev/null; }
  fi
}

# ---------------- tailscale ----------------
ts_netcheck_log(){ log "netcheck: $(timeout 25 "$TSDIR/tailscale" netcheck 2>&1 | tr '\n' ' ' | cut -c1-600)"; }
ts_keepalive(){ # light periodic traffic to the factory nodes: keeps the path warm (idle NAT mappings expire) + records direct/DERP
  local line name ip out map='{}'
  while IFS=$'\t' read -r name ip; do [ -n "$ip" ] || continue
    out=$(timeout 12 "$TSDIR/tailscale" ping -c 1 --timeout=5s "$ip" 2>&1 | head -1 | cut -c1-120); map=$(jq -c --arg n "$name" --arg o "$out" '.[$n]=$o' <<<"$map")
  done < <(jq -r '.Peer // {} | to_entries[] | select(.value.Online==true) | select(.value.HostName|test("^darkfac")) | [.value.HostName,.value.TailscaleIPs[0]] | @tsv' <<<"$1")
  TS_PING=$map; }
check_tailscale(){
  ensure_symlinks
  if ! pgrep -x tailscaled >/dev/null; then log "tailscaled not running"; alert tailscaled critical "tailscaled not running"; start_tailscaled; ts_up; TS_RESTARTS=$((TS_RESTARTS+1)); fi
  local j rc; j=$(ts_json); rc=$?
  if [ $rc -ne 0 ] || [ -z "$j" ]; then TS_API_BAD=$((TS_API_BAD+1)); alert tailscaled critical "tailscale status failing x$TS_API_BAD (daemon wedged?)"
    [ $TS_API_BAD -ge 3 ] && { restart_tailscaled "status unresponsive x$TS_API_BAD"; TS_RESTARTS=$((TS_RESTARTS+1)); TS_API_BAD=0; }; return; fi
  TS_API_BAD=0; clear_alert tailscaled
  TS_BACKEND=$(jq -r '.BackendState // ""' <<<"$j"); TS_ONLINE=$(jq -r '.Self.Online // false' <<<"$j"); TS_IP=$(jq -r '.Self.TailscaleIPs[0] // ""' <<<"$j"); TS_RELAY=$(jq -r '.Self.Relay // ""' <<<"$j")
  TS_PEERS=$(jq -c '[.Peer // {} | to_entries[] | {name:.value.HostName,online:.value.Online,relay:(.value.CurAddr // "" | if .=="" then "derp" else "direct" end)}]' <<<"$j")
  if [ -n "$TS_IP" ] && [ "$TS_IP" != "$TS_EXPECT_IP" ]; then alert ts_identity critical "tailscale IP is $TS_IP, expected $TS_EXPECT_IP (state lost / new node?)"; else clear_alert ts_identity; fi
  case "$TS_BACKEND" in
    NeedsLogin|NeedsMachineAuth)
      local url; url=$(jq -r '.AuthURL // ""' <<<"$j"); alert ts_login critical "tailscale needs LOGIN (state lost/key expired). ${url:-run: sudo tailscale up --hostname=grokbox}"
      [ -r "$TS_AUTHKEY_FILE" ] && [ $((TICK % 10)) -eq 0 ] && ts_up; return;;
    Stopped|NoState) log "tailscale backend $TS_BACKEND -> tailscale up"; ts_up; return;;
    Starting) return;;
  esac
  clear_alert ts_login
  if [ "$TS_ONLINE" = true ]; then
    [ $TS_BAD -ge 2 ] && log "tailscale recovered after $TS_BAD checks (~$(( $(now) - TS_OUTAGE_START ))s)"
    TS_BAD=0; TS_NEXT=8; TS_STEP=16; TS_OUTAGE_START=0; clear_alert ts_offline; clear_alert internet
    { [ $((TICK % 5)) -eq 0 ] || [ "$ONCE" = 1 ]; } && ts_keepalive "$j"
    return; fi
  # Online=false ladder (30s ticks): 1 netcheck diag; 2 `tailscale up`; then tailscaled restarts with exponential spacing (4, 12, 28 min, then every 30 min)
  [ $TS_BAD -eq 0 ] && TS_OUTAGE_START=$(now); TS_BAD=$((TS_BAD+1))
  alert ts_offline warn "tailscale Online=false for ~$(( $(now) - TS_OUTAGE_START ))s (check $TS_BAD)"
  [ $TS_BAD -ge 20 ] && alert ts_offline critical "tailscale Online=false for ~$(( $(now) - TS_OUTAGE_START ))s"
  [ $TS_BAD -eq 1 ] || [ $((TS_BAD % 20)) -eq 0 ] && ts_netcheck_log
  if ! internet_ok; then alert internet critical "no route to controlplane.tailscale.com (box network down?) - not restarting tailscaled"; return; fi
  if [ $TS_BAD -eq 2 ]; then log "Online=false x2 -> tailscale up"; ts_up
  elif [ $TS_BAD -ge $TS_NEXT ]; then restart_tailscaled "Online=false x$TS_BAD"; TS_RESTARTS=$((TS_RESTARTS+1)); TS_NEXT=$((TS_BAD+TS_STEP)); TS_STEP=$((TS_STEP*2)); [ $TS_STEP -gt 60 ] && TS_STEP=60; fi
}

# ---------------- worker ----------------
restart_worker(){ local reason=$1
  venv_repairing && { log "restart skipped: venv repair in progress ($reason)"; return 1; }
  [ "$(now)" -lt "$W_NEXT_START" ] && { log "restart suppressed by backoff ($reason)"; return 1; }
  stop_worker "$reason"
  if start_worker "$reason"; then W_BAD=0; W_FAILS=0; W_Q_STUCK=0; PROBE_BAD=0; clear_alert worker_down; clear_alert port_busy
    W_HEALTH=$(worker_health) && W_UP=1; return 0; fi
  W_FAILS=$((W_FAILS+1)); local d=$(( 30 * (1 << (W_FAILS>5?5:W_FAILS)) )); [ $d -gt 300 ] && d=300; W_NEXT_START=$(( $(now)+d ))
  # alerts must be raised here, not inside with_lock's subshell (array changes there are lost)
  [ -s "$RUN/port_busy.msg" ] && alert port_busy critical "$(cat "$RUN/port_busy.msg")"
  alert worker_down critical "worker failed to start ($reason); attempt $W_FAILS, backoff ${d}s"; return 1; }
check_worker(){
  local h; if h=$(worker_health); then
    W_BAD=0; W_UP=1; W_HEALTH=$h; clear_alert worker_down; clear_alert worker_hung_busy
    # /health git_sha is computed from HEAD at request time: the sha seen when a NEW instance first answers is the code it loaded.
    local inst sha; inst=$(jq -r '.instance_id // ""' <<<"$h"); sha=$(jq -r '.git_sha // ""' <<<"$h")
    if [ "$inst" != "$W_INST" ]; then W_INST=$inst; W_BOOT_SHA=$sha; log "worker instance $inst up, code at ${sha:0:8}"; fi; W_SHA=$sha
    if [ "$(jq -r '(.busy|not) and (.queue_length>0)' <<<"$h")" = true ]; then W_Q_STUCK=$((W_Q_STUCK+1)); else W_Q_STUCK=0; fi
    [ $W_Q_STUCK -ge 20 ] && { log "queue_length>0 but not busy for ~10min (stuck job thread)"; restart_worker "queue stuck"; }
    local n; n=$(awk -v t=$(( $(now)-3600 )) '$1>t' "$RUN/worker-restarts.log" 2>/dev/null | wc -l)
    [ "$n" -ge 5 ] && alert worker_crashloop warn "$n worker (re)starts in the last hour" || clear_alert worker_crashloop
    return; fi
  W_UP=0; W_BAD=$((W_BAD+1))
  if [ -z "$(worker_pids)" ] && [ -z "$(port_pids)" ]; then log "worker process gone"; restart_worker "process gone"; return; fi
  if [ $W_BAD -ge 3 ]; then local jobs; jobs=$(active_jobs_on_disk)
    if [ "$jobs" -gt 0 ] && [ $W_BAD -lt 40 ]; then alert worker_hung_busy warn "/health failing x$W_BAD but $jobs job(s) active: NOT restarting (hang breaker at 40 checks ~20min)"; return; fi
    restart_worker "health failing x$W_BAD (active_jobs=$jobs)"; fi
}
# repo moved (git pull by node_sync/user) while the loaded code is old -> reload when idle (USR-315: every tick, not only in deep_checks)
reload_if_repo_moved(){ [ "$W_UP" = 1 ] || return 0
  if [ -n "$W_SHA" ] && [ -n "$W_BOOT_SHA" ] && [ "$W_SHA" != "$W_BOOT_SHA" ]; then local up; up=$(( $(now) - $(jq -r '.started_at|floor' <<<"$W_HEALTH") ))
    [ "$up" -gt "$RELOAD_MIN_UPTIME_SEC" ] && worker_idle && { log "repo HEAD moved ${W_BOOT_SHA:0:8}->${W_SHA:0:8}; reloading idle worker"; sync_requirements; restart_worker "repo updated"; W_BOOT_SHA=$W_SHA; }; fi
}
repo_info(){ REPO_HEAD=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null); REPO_DIRTY=$(git --no-optional-locks -C "$REPO" status --porcelain 2>/dev/null | wc -l)
  REPO_AB=$(git -C "$REPO" rev-list --left-right --count HEAD...origin/main 2>/dev/null | tr '\t' '/')
  local lk="$REPO/.git/index.lock"; if [ -f "$lk" ] && [ "$(age "$lk")" -gt 600 ] && ! pgrep -x git >/dev/null; then alert git_lock warn "stale $lk (>10min, no git running)"; else clear_alert git_lock; fi; }
deep_checks(){ [ "$W_UP" = 1 ] || return 0; local h=$W_HEALTH idle=0
  [ "$(jq -r '(.busy|not) and (.queue_length==0) and (.active_runs==0)' <<<"$h")" = true ] && [ "$(active_jobs_on_disk)" = 0 ] && idle=1
  local miss="" x; for x in $EXPECTED_HARNESSES; do jq -e --arg x "$x" '.available_harnesses|index($x)' <<<"$h" >/dev/null || miss="$miss $x"; done
  [ -n "$miss" ] && alert harness_missing warn "harness CLI(s) not visible to worker:$miss" || clear_alert harness_missing
  [ "$(jq -r '.project_root' <<<"$h")" = "$REPO" ] && clear_alert worker_root || alert worker_root warn "worker project_root=$(jq -r '.project_root' <<<"$h")"
  CURL_T=15 worker_curl "localhost:$PORT/metrics/hardware" >/dev/null || alert worker_metrics warn "/metrics/hardware failing"; CURL_T=15 worker_curl "localhost:$PORT/metrics/hardware" >/dev/null && clear_alert worker_metrics
  repo_info
  [ "$idle" = 1 ] || return 0
  # real minimal probe through the worker itself: python must resolve to the venv (PATH regression), git must work
  if ! dry; then local body r; body=$(jq -nc --arg c 'command -v python && python -c "import sys;print(sys.executable)" && git --version' '{command:$c,timeout_seconds:30}')
    r=$(CURL_T=45 worker_curl -X POST -H 'Content-Type: application/json' -d "$body" "localhost:$PORT/system/exec")
    if jq -e '.exit_code==0 and (.stdout|contains(".venv/bin/python"))' <<<"$r" >/dev/null 2>&1; then PROBE_BAD=0; PROBE_AT=$(now); clear_alert worker_probe
    else PROBE_BAD=$((PROBE_BAD+1)); alert worker_probe critical "deep probe failed x$PROBE_BAD: $(jq -r '.stderr // "no reply"' <<<"$r" 2>/dev/null | head -c 160)"
      [ $PROBE_BAD -ge 2 ] && restart_worker "deep probe failing (PATH/env)"; fi
  else log "DRY skip POST /system/exec probe"; fi
  # python deps / venv integrity every ~30 min, only when idle
  if [ $(( $(now) - VENV_AT )) -gt 1800 ]; then VENV_AT=$(now)
    if nice -n 10 bash -c '. "'"$SCRIPT_DIR"'/grokbox-common.sh"; venv_ok'; then VENV_BAD=0; clear_alert venv; ensure_pip; sync_requirements
    else VENV_BAD=$((VENV_BAD+1)); alert venv critical "venv import check failed x$VENV_BAD"
      if [ $VENV_BAD -ge 2 ]; then stop_worker "venv repair"; hb supervisor; repair_venv; start_worker "after venv repair"; VENV_BAD=0; fi; fi; fi
}

# ---------------- maintenance ----------------
maintenance(){
  rotate_all; backup_ts_state
  local p; p=$(pgrep -x tailscaled | head -1); [ -n "$p" ] && protect_oom "$p" -500; protect_oom $$ -300
  [ $((TICK % 20)) -eq 0 ] && { DRIFT=$(clock_drift) && { [ "${DRIFT#-}" -gt 30 ] && alert clock warn "clock drift ${DRIFT}s vs control plane" || clear_alert clock; }; }
  if [ $(( $(now) - CLEAN_AT )) -gt 3600 ]; then CLEAN_AT=$(now); cleanup_old; cleanup_orphan_worktrees 1
    local uv sz; uv=$(find_uv); sz=$(du -sm "$HOME/.cache/uv" 2>/dev/null | cut -f1); [ "${sz:-0}" -gt 4096 ] && [ -n "$uv" ] && ! dry && "$uv" cache prune >> "$LOGFILE" 2>&1; fi
}

# ---------------- status.json ----------------
write_status(){
  local rst; rst=$(cat "$RUN/worker-restarts.log" 2>/dev/null | wc -l); local last; last=$(tail -1 "$RUN/worker-restarts.log" 2>/dev/null)
  local wpid; wpid=$(port_pids | head -1); local rss=0; [ -n "$wpid" ] && { local kb; kb=$(ps -o rss= -p "$wpid" 2>/dev/null | tr -d ' '); rss=$(( ${kb:-0} / 1024 )); }
  local ok=true; { has_critical || [ "$W_UP" != 1 ] || [ "$TS_ONLINE" != true ]; } && ok=false
  jq -n --argjson ok "$ok" --argjson now "$(now)" --arg nowiso "$(date -Is)" --argjson sup_pid "$$" --argjson sup_start "$START_AT" --argjson ticks "$TICK" \
    --argjson wd_alive "$(lock_free "$RUN/watchdog.lock" && echo false || echo true)" --argjson wd_hb "$(mtime "$RUN/watchdog.heartbeat")" \
    --argjson wup "$([ "$W_UP" = 1 ] && echo true || echo false)" --argjson wh "${W_HEALTH:-null}" --argjson wpid "${wpid:-0}" --argjson wrss "$rss" --argjson wrst "$rst" --arg wlast "$last" --argjson probe_at "$PROBE_AT" \
    --arg tsb "$TS_BACKEND" --arg tson "$TS_ONLINE" --arg tsip "$TS_IP" --arg tsrelay "$TS_RELAY" --argjson tsout "$TS_OUTAGE_START" --argjson tsrst "$TS_RESTARTS" --argjson peers "$TS_PEERS" --argjson ping "$TS_PING" \
    --argjson disk "$DISK_PCT" --argjson inode "$INODE_PCT" --argjson mem "$MEM_AVAIL_MB" --argjson psi "$PSI60" --arg load "$LOAD1" --argjson uptime "$(cut -d. -f1 /proc/uptime)" --arg bootid "$(cat /proc/sys/kernel/random/boot_id)" --arg drift "$DRIFT" \
    --arg head "$REPO_HEAD" --argjson dirty "$REPO_DIRTY" --arg ab "$REPO_AB" --argjson hbboot "$(mtime "$RUN/bootstrap.last")" \
    --argjson alerts "$(alerts_json)" --argjson dry "$(dry && echo true || echo false)" '
    {ok:$ok, dry_run:$dry, updated_at:$nowiso, updated_epoch:$now, host:{uptime_sec:$uptime, boot_id:$bootid, load1:($load|tonumber), disk_used_pct:$disk, inodes_used_pct:$inode, mem_available_mb:$mem, mem_psi_some_avg60:$psi, clock_drift_sec:(if $drift=="" then null else ($drift|tonumber) end)},
     supervisor:{pid:$sup_pid, started_epoch:$sup_start, ticks:$ticks, watchdog_alive:$wd_alive, watchdog_heartbeat_epoch:$wd_hb, last_bootstrap_epoch:$hbboot},
     worker:{healthy:$wup, pid:$wpid, rss_mb:$wrss, restarts_total:$wrst, last_restart:$wlast, last_deep_probe_epoch:$probe_at,
             health:(if $wh==null then null else ($wh|{status,node_id,busy,active_runs,queue_length,instance_id,started_at,git_sha,available_harnesses}) end)},
     tailscale:{backend:$tsb, online:($tson=="true"), ip:$tsip, home_derp:$tsrelay, outage_since_epoch:(if $tsout==0 then null else $tsout end), restarts_by_supervisor:$tsrst, peers:$peers, ping:$ping},
     repo:{head:$head, dirty_files:$dirty, ahead_behind_origin_main:$ab},
     alerts:$alerts}' > "$STATUS_FILE.tmp.$$" && mv -f "$STATUS_FILE.tmp.$$" "$STATUS_FILE"; }


# ---------------- VM freeze/restore detector (events only, never acts) ----------------
VM_EV="$RUN/vm-events.log"; VM_LAST_T=${VM_LAST_T:-0}; VM_LAST_GW=""; VM_LAST_BOOT=""
vm_events(){ local t gw ip bid gap; t=$(date +%s); gw=$(ip route 2>/dev/null | awk '/^default/{print $3; exit}'); ip=$(ip -4 -o addr show dev enp0s4 2>/dev/null | awk '{print $4; exit}'); bid=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)
  if [ "$VM_LAST_T" -gt 0 ]; then gap=$((t - VM_LAST_T))
    [ "$gap" -gt 75 ] && echo "$(date '+%F %T %z') FREEZE_GAP loop_gap=${gap}s (expected ~30s)" >> "$VM_EV"
    [ -n "$VM_LAST_GW" ] && [ "$gw" != "$VM_LAST_GW" ] && echo "$(date '+%F %T %z') NET_CHANGE gw ${VM_LAST_GW}->${gw} self=${ip}" >> "$VM_EV"
    [ -n "$VM_LAST_BOOT" ] && [ "$bid" != "$VM_LAST_BOOT" ] && echo "$(date '+%F %T %z') REBOOT boot_id changed" >> "$VM_EV"
  fi
  VM_LAST_T=$t; VM_LAST_GW=$gw; VM_LAST_BOOT=$bid; }

# ---------------- main loop ----------------
protect_oom $$ -300
while true; do
  T0=$SECONDS; TICK=$((TICK+1)); hb supervisor
  vm_events; collect_resources; check_watchdog; check_tailscale; check_worker; reload_if_repo_moved
  { [ $((TICK % 10)) -eq 1 ] || [ "$ONCE" = 1 ]; } && deep_checks
  { [ $((TICK % 10)) -eq 5 ] || [ "$ONCE" = 1 ]; } && maintenance
  [ $((TICK % 30)) -eq 15 ] && ! dry && spawn_detached "$DF/bootstrap.log" "$SCRIPT_DIR/grokbox-bootstrap.sh" --quiet   # redundancy: re-run the idempotent bootstrap every ~15 min
  write_status; hb supervisor
  [ "$ONCE" = 1 ] && exit 0
  D=$((30 - (SECONDS - T0))); [ $D -lt 5 ] && D=5; sleep "$D" 200>&-
done
