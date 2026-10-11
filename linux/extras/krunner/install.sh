#!/usr/bin/env bash
# Install the Muse KRunner runner (Plasma 6, user-local, no root).
#
#   ./install.sh [--yes]
#
# Installs:
#   ~/.local/bin/muse_runner.py          the D-Bus runner
#   ~/.local/bin/muse-msg                 the box->Muse sender (if not on PATH)
#   ~/.local/share/krunner/dbusplugins/muse-runner.desktop
#   ~/.local/share/dbus-1/services/org.kde.muse.runner.service
#
# Then reloads the session bus, restarts KRunner, and verifies Match over D-Bus.
set -u
YES=0
[ "${1:-}" = "--yes" ] && YES=1

SRC="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/krunner/dbusplugins"
SERVICE_DIR="$HOME/.local/share/dbus-1/services"

say() { echo "install: $*"; }
die() { echo "install: $*" >&2; exit 1; }
confirm() {
    [ "$YES" = 1 ] && return 0
    printf '%s [y/N] ' "$*"; read -r ans
    [ "$ans" = "y" ] || [ "$ans" = "Y" ]
}

command -v python3 >/dev/null || die "python3 not found"
python3 -c "import dbus, gi" 2>/dev/null || {
    say "python3-dbus / python3-gi are required."
    PM=""
    command -v apt-get >/dev/null && PM="apt-get install -y python3-dbus python3-gi"
    command -v dnf >/dev/null && PM="dnf install -y python3-dbus python3-gobject"
    command -v pacman >/dev/null && PM="pacman -S --needed python-dbus python-gobject"
    command -v zypper >/dev/null && PM="zypper install -y python3-dbus python3-gobject"
    [ -n "$PM" ] || die "install your distro's python3-dbus and python3-gi packages by hand"
    confirm "Run: sudo $PM ?" || die "aborted"
    sudo $PM || die "package install failed"
}

mkdir -p "$BIN" "$DESKTOP_DIR" "$SERVICE_DIR"
cp "$SRC/muse_runner.py" "$BIN/muse_runner.py"
chmod +x "$BIN/muse_runner.py"
if ! command -v muse-msg >/dev/null; then
    cp "$SRC/../muse-msg" "$BIN/muse-msg"
    chmod +x "$BIN/muse-msg"
    say "installed muse-msg to $BIN"
fi
cp "$SRC/muse-runner.desktop" "$DESKTOP_DIR/muse-runner.desktop"
# Some Plasma builds read dbusplugins/, others runners/ — harmless to cover both.
mkdir -p "$HOME/.local/share/krunner/runners"
cp "$SRC/muse-runner.desktop" "$HOME/.local/share/krunner/runners/muse-runner.desktop"
sed -e "s|@PYTHON@|$(command -v python3)|" \
    -e "s|@RUNNER@|$BIN/muse_runner.py|" \
    "$SRC/org.kde.muse.runner.service.in" > "$SERVICE_DIR/org.kde.muse.runner.service"

# Let the session bus notice the new service file.
busctl --user call org.freedesktop.DBus /org/freedesktop/DBus \
    org.freedesktop.DBus ReloadConfig >/dev/null 2>&1 || true

# Restart KRunner so it picks up the new runner metadata.
if pgrep -x krunner >/dev/null 2>&1; then
    kquitapp6 krunner 2>/dev/null || true
    sleep 1
    setsid krunner >/dev/null 2>&1 < /dev/null &
    say "krunner restarted"
fi

# Verify: Match must answer over D-Bus (this also D-Bus-activates the runner).
say "verifying Match over D-Bus…"
if out=$(busctl --user call org.kde.muse.runner /muse org.kde.krunner1 \
        Match s 'muse hello' 2>&1); then
    echo "$out" | grep -q "Ask Muse" && say "OK — runner answers." \
        || { echo "$out"; die "runner answered but no match came back"; }
else
    echo "$out" >&2
    die "D-Bus call failed — check journalctl --user -t muse_runner"
fi

say "done. Press Alt+Space and type: muse <your question>"
say "(enable it under System Settings → Search → Plasma Search if needed)"
