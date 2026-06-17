#!/bin/sh
# Repose: stop an in-container agent daemon by its python module marker.
# Used by ExecStop (clean shutdown) AND ExecStartPre (orphan guard, RPOSE Gap 13)
# because (a) `docker exec` does not forward SIGTERM to the exec'd process, and
# (b) pgrep/pkill are not installed in the container.
#
# Sends SIGTERM to every process whose cmdline contains the marker, then WAITS
# for them to actually exit (so a held port / singleton PID-lock is released
# before a fresh ExecStart binds), escalating to SIGKILL if any outlive the
# grace window. Always exits 0 so it never blocks unit start/stop.
# Usage: stop_agent.sh <module.marker>   e.g. repose.agents.event_monitor
marker="$1"
me=$$

match_pids() {
    for d in /proc/[0-9]*; do
        p=${d#/proc/}
        [ "$p" = "$me" ] && continue
        c=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)
        case "$c" in
            *"$marker "*) echo "$p" ;;
        esac
    done
}

pids=$(match_pids)
[ -z "$pids" ] && exit 0
for p in $pids; do kill "$p" 2>/dev/null; done

# Wait up to ~10s for graceful exit (releases :8080 / singleton lock), then
# SIGKILL any stragglers.
i=0
while [ "$i" -lt 20 ]; do
    [ -z "$(match_pids)" ] && exit 0
    sleep 0.5
    i=$((i + 1))
done
for p in $(match_pids); do kill -9 "$p" 2>/dev/null; done
exit 0
