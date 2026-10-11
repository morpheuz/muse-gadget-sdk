#!/usr/bin/env bash
# disk-watch.sh — example proactive alert: warn Muse when a filesystem fills up.
#
# Install:
#   cp disk-watch.sh ~/.local/bin/ && chmod +x ~/.local/bin/disk-watch.sh
#   cp disk-watch.{service,timer} ~/.config/systemd/user/
#   systemctl --user daemon-reload && systemctl --user enable --now disk-watch.timer
#
# Tune THRESHOLD below. Sends at most one alert per mount per run.

set -u
THRESHOLD="${DISK_WATCH_THRESHOLD:-90}"

df -P -x tmpfs -x devtmpfs -x overlay 2>/dev/null | awk 'NR>1 {print $5, $6}' | while read -r pct mnt; do
    n="${pct%\%}"
    case "$n" in ''|*[!0-9]*) continue ;; esac
    if [ "$n" -ge "$THRESHOLD" ]; then
        muse-msg alert --severity warning --source disk-watch \
            "$mnt is ${pct} full (threshold ${THRESHOLD}%)"
    fi
done
