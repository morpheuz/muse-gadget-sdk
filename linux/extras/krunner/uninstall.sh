#!/usr/bin/env bash
# Remove the Muse KRunner runner.
set -u
rm -f "$HOME/.local/bin/muse_runner.py" \
      "$HOME/.local/share/krunner/dbusplugins/muse-runner.desktop" \
      "$HOME/.local/share/krunner/runners/muse-runner.desktop" \
      "$HOME/.local/share/dbus-1/services/org.kde.muse.runner.service"
busctl --user call org.freedesktop.DBus /org/freedesktop/DBus \
    org.freedesktop.DBus ReloadConfig >/dev/null 2>&1 || true
if pgrep -x krunner >/dev/null 2>&1; then
    kquitapp6 krunner 2>/dev/null || true
    sleep 1
    setsid krunner >/dev/null 2>&1 < /dev/null &
fi
echo "muse-runner removed."
