# Copyright (c) 2026 Artur Souza.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""KDE Plasma desktop commands for the Muse Gadgets Linux SDK.

Drop-in module: copy to ``src/musegadget/plasma.py`` in the SDK tree and
apply ``plasma_executor_patch.diff`` to ``executor.py`` (marked ``# --- plasma ---``).

Design notes
------------
The SDK runs command children as the ``run_as`` account with a minimal
environment, so none of the desktop session variables survive. This module
reconstructs the Plasma session environment from the account's uid:

* ``DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus``
* ``XDG_RUNTIME_DIR=/run/user/<uid>``
* ``WAYLAND_DISPLAY`` when the Wayland socket exists, else a ``DISPLAY=:0``
  fallback when an X11 socket exists.

That means the gadget must be installed with ``--run-as <desktop-login>``
— the account that actually owns the Plasma session. Everything here is
stdlib-only and talks to KDE over D-Bus / stock CLIs (no xdotool, no X11-only
tools).

Window listing/focusing uses the KWin scripting roundtrip: a small JS
snippet is injected via ``org.kde.KWin /Scripting loadScript``, run, and its
``print()`` output is read back from the journal with a unique run marker.
KWin exposes no direct "list windows" D-Bus method, so this is the
community-standard workaround. If ``print()`` output is missing, the
``kwin_scripting`` logging category is probably disabled on that box (see the
error message).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid

# How long we wait for an injected KWin script to print its results.
KWIN_SCRIPT_WAIT_S = 2.0
# journalctl window we scan for the script's marker lines.
JOURNAL_SINCE_S = 30


class PlasmaError(Exception):
    """Raised when a Plasma command cannot be completed."""


# ---------------------------------------------------------------------------
# Entry point (called from Executor._plasma)
# ---------------------------------------------------------------------------

# Demotion target for helper subprocesses: the executor runs as root but
# Plasma helpers must run as the --run-as desktop user (child_options
# carries user=/group= from the executor; only present when root).
_RUN_USER = None
_RUN_GROUP = None


def run(command: str, params: dict, child_options: dict, account) -> dict:
    """Run one plasma.* command. Returns the payload dict for ok()."""
    global _RUN_USER, _RUN_GROUP
    _RUN_USER = child_options.get("user")
    _RUN_GROUP = child_options.get("group")
    env = _session_env(child_options.get("env") or {}, account.uid)
    if command == "desktop.status":
        return desktop_status(env, account)
    if command == "desktop.notify":
        return desktop_notify(env, params)
    if command == "desktop.screenshot":
        return desktop_screenshot(env, account, params)
    if command == "clipboard.get":
        return clipboard_get(env, params)
    if command == "clipboard.set":
        return clipboard_set(env, params)
    if command == "window.list":
        return window_list(env)
    if command == "window.focus":
        return window_focus(env, params)
    if command == "screen.lock":
        return screen_lock(env)
    if command == "media.control":
        return media_control(env, params)
    if command == "media.info":
        return media_info(env, params)
    if command == "audio.volume":
        return audio_volume(env, params)
    if command == "session.power":
        return session_power(env, account, params)
    if command == "session.idle_inhibit":
        return session_idle_inhibit(env, params)
    if command == "power.profile":
        return power_profile(env, params)
    if command == "screen.brightness":
        return screen_brightness(env, params)
    if command == "clipboard.history":
        return clipboard_history(env, params)
    if command == "clipboard.clear":
        return clipboard_clear(env, params)
    raise PlasmaError(f"unsupported plasma command: {command}")


# ---------------------------------------------------------------------------
# Session environment
# ---------------------------------------------------------------------------

def _session_env(base_env: dict, uid: int) -> dict:
    env = dict(base_env)
    runtime = f"/run/user/{uid}"
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    # The user bus is always at $XDG_RUNTIME_DIR/bus on systemd systems.
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    if "WAYLAND_DISPLAY" not in env and os.path.exists(os.path.join(runtime, "wayland-0")):
        env["WAYLAND_DISPLAY"] = "wayland-0"
    if "DISPLAY" not in env and os.path.exists("/tmp/.X11-unix/X0"):
        env["DISPLAY"] = ":0"
    return env


def _qdbus() -> str:
    for name in ("qdbus", "qdbus6"):
        path = shutil.which(name)
        if path:
            return path
    raise PlasmaError("neither qdbus nor qdbus6 found — not a Plasma session?")


def _run(argv: list[str], env: dict, timeout: int = 20) -> str:
    """Run argv, return stdout; raise PlasmaError on failure."""
    demote: dict = {}
    if _RUN_USER is not None:
        demote["user"] = _RUN_USER
    if _RUN_GROUP is not None:
        demote["group"] = _RUN_GROUP
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, env=env, **demote)
    except subprocess.TimeoutExpired:
        raise PlasmaError(f"{argv[0]} timed out after {timeout}s")
    except OSError as exc:
        raise PlasmaError(f"could not start {argv[0]}: {exc}")
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise PlasmaError(f"{' '.join(argv[:2])} failed: {detail[:300]}")
    return proc.stdout


def _service_present(env: dict, service: str) -> bool:
    """True if a D-Bus service name is currently on the session bus."""
    try:
        _run([_qdbus(), service], env, timeout=10)
        return True
    except PlasmaError:
        return False


# ---------------------------------------------------------------------------
# desktop.status — readiness probe (run this first on a new box)
# ---------------------------------------------------------------------------

def desktop_status(env: dict, account) -> dict:
    runtime = f"/run/user/{account.uid}"
    tools = {}
    for tool in ("notify-send", "spectacle", "playerctl", "wpctl",
                 "qdbus", "qdbus6", "journalctl", "loginctl"):
        tools[tool] = shutil.which(tool) is not None
    plasma_version = None
    if shutil.which("plasmashell"):
        try:
            plasma_version = _run(["plasmashell", "--version"], env, timeout=10).strip()
        except PlasmaError:
            pass
    return {
        "user": account.name,
        "uid": account.uid,
        "session_bus_socket": os.path.exists(os.path.join(runtime, "bus")),
        "wayland": os.path.exists(os.path.join(runtime, "wayland-0")),
        "x11": os.path.exists("/tmp/.X11-unix/X0"),
        "klipper": _service_present(env, "org.kde.klipper"),
        "kwin": _service_present(env, "org.kde.KWin"),
        "screensaver": _service_present(env, "org.freedesktop.ScreenSaver"),
        "plasma_version": plasma_version,
        "tools": tools,
        "power": {
            "powerdevil_brightness": _service_present(
                env, "org.kde.Solid.PowerManagement"),
            "powerprofilesctl": shutil.which("powerprofilesctl") is not None,
            "systemd_inhibit": shutil.which("systemd-inhibit") is not None,
            "loginctl": shutil.which("loginctl") is not None,
        },
    }


# ---------------------------------------------------------------------------
# desktop.notify
# ---------------------------------------------------------------------------

def desktop_notify(env: dict, params: dict) -> dict:
    title = params.get("title")
    if not isinstance(title, str) or not title.strip():
        raise PlasmaError("title is required")
    body = params.get("body") or ""
    if not isinstance(body, str):
        raise PlasmaError("body must be a string")
    urgency = params.get("urgency") or "normal"
    if urgency not in ("low", "normal", "critical"):
        raise PlasmaError("urgency must be low, normal or critical")
    expire_ms = params.get("expire_ms", 5000)
    if not isinstance(expire_ms, int) or expire_ms < 0:
        raise PlasmaError("expire_ms must be a non-negative integer")
    if shutil.which("notify-send") is None:
        raise PlasmaError("notify-send is not installed")
    _run(["notify-send", "--app-name=Muse", f"--urgency={urgency}",
          f"--expire-time={expire_ms}", title, body], env)
    return {"notified": True, "urgency": urgency}


# ---------------------------------------------------------------------------
# desktop.screenshot (Spectacle, background mode)
# ---------------------------------------------------------------------------

_SCREENSHOT_MODES = {"fullscreen": "-f", "active": "-a", "region": "-r"}


def desktop_screenshot(env: dict, account, params: dict) -> dict:
    mode = params.get("mode") or "fullscreen"
    if mode not in _SCREENSHOT_MODES:
        raise PlasmaError(f"mode must be one of {sorted(_SCREENSHOT_MODES)}")
    if shutil.which("spectacle") is None:
        raise PlasmaError("spectacle is not installed")
    fd, path = tempfile.mkstemp(prefix="muse-shot-", suffix=".png")
    os.close(fd)
    try:
        _run(["spectacle", "-b", "-n", _SCREENSHOT_MODES[mode], "-o", path],
             env, timeout=30)
        size = os.path.getsize(path)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    if size == 0:
        os.unlink(path)
        raise PlasmaError("spectacle produced an empty screenshot")
    return {
        "path": path,
        "bytes": size,
        "hint": "use file.read on path to fetch the PNG bytes",
    }


# ---------------------------------------------------------------------------
# clipboard.get / clipboard.set (Klipper over D-Bus)
# ---------------------------------------------------------------------------

def clipboard_get(env: dict, params: dict) -> dict:
    qdbus = _qdbus()
    index = params.get("index")
    if index is None:
        out = _run([qdbus, "org.kde.klipper", "/klipper",
                    "getClipboardContents"], env)
    else:
        if not isinstance(index, int) or index < 0:
            raise PlasmaError("index must be a non-negative integer")
        out = _run([qdbus, "org.kde.klipper", "/klipper",
                    "getClipboardHistoryItem", str(index)], env)
    text = out.rstrip("\n")
    return {"text": text, "chars": len(text)}


def clipboard_set(env: dict, params: dict) -> dict:
    text = params.get("text")
    if not isinstance(text, str) or not text:
        raise PlasmaError("text is required and must be a non-empty string")
    qdbus = _qdbus()
    _run([qdbus, "org.kde.klipper", "/klipper",
          "setClipboardContents", text], env)
    if params.get("clear_history"):
        _run([qdbus, "org.kde.klipper", "/klipper",
              "clearClipboardHistory"], env)
    return {"set": True, "chars": len(text)}


# ---------------------------------------------------------------------------
# window.list / window.focus (KWin scripting roundtrip)
# ---------------------------------------------------------------------------

_KWIN_JS = """\
var RUNID = "__RUNID__";
var MATCH = __MATCH_JSON__;
var wins = (typeof workspace.windowList === "function")
    ? workspace.windowList() : workspace.clientList();
function clean(s) {
    return String((s === undefined || s === null) ? "" : s)
        .replace(/[\\t\\n\\r]+/g, " ");
}
var lines = [];
if (MATCH) {
    var re = new RegExp(MATCH, "i");
    var hit = null;
    for (var i = 0; i < wins.length; i++) {
        var w = wins[i];
        if (!w.normalWindow) continue;
        if (re.test(w.caption) || re.test(w.resourceClass)
                || re.test(w.desktopFileName)) { hit = w; break; }
    }
    if (!hit) {
        lines.push(RUNID + "\\tNOMATCH");
    } else {
        hit.minimized = false;
        try { workspace.currentDesktop = hit.desktop; } catch (e) {}
        try { if (hit.raiseWindow) hit.raiseWindow(); } catch (e) {}
        workspace.activeWindow = hit;
        lines.push(RUNID + "\\tFOCUSED\\t" + clean(hit.caption)
            + "\\t" + clean(hit.resourceClass));
    }
} else {
    for (var j = 0; j < wins.length; j++) {
        var w2 = wins[j];
        if (!w2.normalWindow) continue;
        lines.push(RUNID + "\\tWIN\\t" + clean(w2.caption)
            + "\\t" + clean(w2.resourceClass)
            + "\\t" + clean(w2.desktopFileName)
            + "\\t" + w2.desktop
            + "\\t" + (w2.active ? "1" : "0")
            + "\\t" + (w2.minimized ? "1" : "0"));
    }
}
lines.push(RUNID + "\\tEND");
print(lines.join("\\n"));
"""


def _kwin_inject(env: dict, match: str | None) -> list[str]:
    """Inject a KWin script and return its journal marker lines."""
    qdbus = _qdbus()
    runid = "mg" + uuid.uuid4().hex[:10]
    name = f"musegadget-{runid}"
    js = _KWIN_JS.replace("__RUNID__", runid)
    js = js.replace("__MATCH_JSON__",
                    json.dumps(match) if match is not None else "null")
    with tempfile.NamedTemporaryFile("w", suffix=".js", prefix="mg-kwin-",
                                     delete=False) as fh:
        fh.write(js)
        script_path = fh.name
    try:
        since = time.time() - 2
        # Defeat loadScript's "already loaded -> return existing id" cache.
        try:
            _run([qdbus, "org.kde.KWin", "/Scripting",
                  "org.kde.kwin.Scripting.unloadScript", name], env, timeout=10)
        except PlasmaError:
            pass
        slot = _run([qdbus, "org.kde.KWin", "/Scripting",
                     "org.kde.kwin.Scripting.loadScript", script_path, name],
                    env, timeout=15).strip()
        if not re.fullmatch(r"\d+", slot):
            raise PlasmaError(f"KWin loadScript returned unexpected slot: {slot!r}")
        _run([qdbus, "org.kde.KWin", f"/Scripting/Script{slot}",
              "org.kde.kwin.Script.run"], env, timeout=15)
        time.sleep(KWIN_SCRIPT_WAIT_S)
        try:
            _run([qdbus, "org.kde.KWin", "/Scripting",
                  "org.kde.kwin.Scripting.unloadScript", name], env, timeout=10)
        except PlasmaError:
            pass
        return _journal_marker_lines(env, since, runid)
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


def _journal_marker_lines(env: dict, since_epoch: float, marker: str) -> list[str]:
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since_epoch))
    for extra in (["--user"], []):
        argv = ["journalctl", *extra, "--since", since,
                "--no-pager", "-o", "cat"]
        try:
            out = _run(argv, env, timeout=15)
        except PlasmaError:
            continue
        hits = []
        for line in out.splitlines():
            idx = line.find(marker)
            if idx >= 0:
                hits.append(line[idx:])
        if hits:
            return hits
    return []


def _kwin_records(lines: list[str], runid: str, want: str) -> list[list[str]]:
    records = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0] == runid and parts[1] == want:
            records.append(parts)
    return records


def window_list(env: dict) -> dict:
    lines = _kwin_inject(env, match=None)
    if not lines:
        raise PlasmaError(
            "no output from the KWin script in the journal — the kwin_scripting "
            "logging category may be disabled on this box (enable it via "
            "System Settings > KWin Scripts debugging, or qtlogging.ini), or "
            "KWin is not running")
    runid = lines[0].split("\t")[0]
    windows = []
    for parts in _kwin_records(lines, runid, "WIN"):
        # RUNID WIN caption class desktopFile desktop active minimized
        windows.append({
            "caption": parts[2] if len(parts) > 2 else "",
            "app_class": parts[3] if len(parts) > 3 else "",
            "desktop_file": parts[4] if len(parts) > 4 else "",
            "desktop": parts[5] if len(parts) > 5 else "",
            "active": (parts[6] if len(parts) > 6 else "0") == "1",
            "minimized": (parts[7] if len(parts) > 7 else "0") == "1",
        })
    return {"windows": windows, "count": len(windows)}


def window_focus(env: dict, params: dict) -> dict:
    match = params.get("match")
    if not isinstance(match, str) or not match.strip():
        raise PlasmaError("match is required (substring of window title or app class)")
    lines = _kwin_inject(env, match=match.strip())
    if not lines:
        raise PlasmaError("no output from the KWin script in the journal")
    runid = lines[0].split("\t")[0]
    if _kwin_records(lines, runid, "NOMATCH"):
        raise PlasmaError(f"no window matched {match!r}")
    focused = _kwin_records(lines, runid, "FOCUSED")
    if not focused:
        raise PlasmaError("KWin script ran but reported no result")
    return {"focused": True,
            "caption": focused[0][2] if len(focused[0]) > 2 else "",
            "app_class": focused[0][3] if len(focused[0]) > 3 else ""}


# ---------------------------------------------------------------------------
# screen.lock
# ---------------------------------------------------------------------------

def screen_lock(env: dict) -> dict:
    try:
        _run([_qdbus(), "org.freedesktop.ScreenSaver", "/ScreenSaver",
              "org.freedesktop.ScreenSaver.Lock"], env)
        return {"locked": True, "via": "screensaver-dbus"}
    except PlasmaError:
        pass
    if shutil.which("loginctl"):
        _run(["loginctl", "lock-session"], env)
        return {"locked": True, "via": "loginctl"}
    raise PlasmaError("could not lock the session (no ScreenSaver D-Bus, no loginctl)")


# ---------------------------------------------------------------------------
# media.control / media.info (playerctl / MPRIS)
# ---------------------------------------------------------------------------

_MEDIA_ACTIONS = {"play": "play", "pause": "pause", "play-pause": "play-pause",
                  "toggle": "play-pause", "next": "next", "previous": "previous",
                  "stop": "stop"}


def _playerctl(env: dict) -> str:
    path = shutil.which("playerctl")
    if path is None:
        raise PlasmaError("playerctl is not installed")
    return path


def media_control(env: dict, params: dict) -> dict:
    action = params.get("action") or "play-pause"
    if action not in _MEDIA_ACTIONS:
        raise PlasmaError(f"action must be one of {sorted(_MEDIA_ACTIONS)}")
    player = params.get("player")
    argv = [_playerctl(env)]
    if player:
        if not isinstance(player, str) or not player.strip():
            raise PlasmaError("player must be a non-empty string")
        argv += ["-p", player.strip()]
    argv.append(_MEDIA_ACTIONS[action])
    _run(argv, env)
    return {"action": action, "player": player or "default"}


def media_info(env: dict, params: dict) -> dict:
    playerctl = _playerctl(env)
    player = params.get("player")
    base = [playerctl]
    if player:
        base += ["-p", player]
    try:
        meta = _run(base + ["metadata", "--format",
                            "{{playerName}}\t{{artist}}\t{{title}}\t{{album}}"],
                    env).rstrip("\n").split("\t")
        status = _run(base + ["status"], env).strip().lower()
    except PlasmaError:
        raise PlasmaError("no active media players found")
    while len(meta) < 4:
        meta.append("")
    return {"player": meta[0], "artist": meta[1], "title": meta[2],
            "album": meta[3], "status": status}


# ---------------------------------------------------------------------------
# audio.volume (wpctl / PipeWire)
# ---------------------------------------------------------------------------

def audio_volume(env: dict, params: dict) -> dict:
    if shutil.which("wpctl") is None:
        raise PlasmaError("wpctl is not installed (PipeWire expected)")
    action = params.get("action") or "get"
    sink = "@DEFAULT_AUDIO_SINK@"
    if action == "get":
        pass
    elif action == "mute":
        _run(["wpctl", "set-mute", sink, "toggle"], env)
    elif action == "set":
        level = params.get("level")
        if not isinstance(level, (int, float)) or not 0 <= level <= 1:
            raise PlasmaError("level must be a number between 0.0 and 1.0")
        _run(["wpctl", "set-volume", sink, str(level)], env)
    else:
        raise PlasmaError("action must be get, set or mute")
    out = _run(["wpctl", "get-volume", sink], env).strip()
    # "Volume: 0.65" or "Volume: 0.65 [MUTED]"
    m = re.search(r"Volume:\s*([0-9.]+)", out)
    return {"volume": float(m.group(1)) if m else None,
            "muted": "[MUTED]" in out}


# ---------------------------------------------------------------------------
# session.power — suspend / reboot / shutdown / logout
# ---------------------------------------------------------------------------

_POWER_ACTIONS = ("suspend", "reboot", "shutdown", "logout")
# ksmserver logout(confirm, sdtype, sdmode): sdtype 0=logout 1=reboot 2=halt
_KSM_SDTYPE = {"logout": "0", "reboot": "1", "shutdown": "2"}


def _own_session_id(env: dict, account_name: str) -> str | None:
    try:
        out = _run(["loginctl", "--no-legend", "list-sessions"], env, timeout=10)
    except PlasmaError:
        return None
    for line in out.splitlines():
        parts = line.split()
        # SESSION UID USER SEAT TTY
        if len(parts) >= 3 and parts[2] == account_name:
            return parts[0]
    return None


def session_power(env: dict, account, params: dict) -> dict:
    action = params.get("action")
    if action not in _POWER_ACTIONS:
        raise PlasmaError(f"action must be one of {list(_POWER_ACTIONS)}")
    confirm = "1" if params.get("confirm_dialog") else "0"
    if action == "suspend":
        # The reply may never arrive — the machine is asleep. That is normal.
        _run(["loginctl", "suspend"], env, timeout=15)
        return {"action": "suspend", "via": "loginctl"}
    try:
        _run([_qdbus(), "org.kde.ksmserver", "/KSMServer",
              "org.kde.KSMServerInterface.logout",
              confirm, _KSM_SDTYPE[action], "0"], env, timeout=15)
        return {"action": action, "via": "ksmserver"}
    except PlasmaError:
        pass
    # Fallback: logind directly.
    if action in ("reboot", "shutdown"):
        binary = "reboot" if action == "reboot" else "poweroff"
        _run(["loginctl", binary], env, timeout=15)
        return {"action": action, "via": "loginctl"}
    session_id = _own_session_id(env, account.name)
    if session_id is None:
        raise PlasmaError("could not determine own login session for logout")
    _run(["loginctl", "terminate-session", session_id], env, timeout=15)
    return {"action": "logout", "via": "loginctl"}


# ---------------------------------------------------------------------------
# session.idle_inhibit — block idle/sleep for a bounded duration
# ---------------------------------------------------------------------------

def session_idle_inhibit(env: dict, params: dict) -> dict:
    if shutil.which("systemd-inhibit") is None:
        raise PlasmaError("systemd-inhibit is not installed")
    duration = params.get("duration_s", 1800)
    if not isinstance(duration, int) or isinstance(duration, bool) \
            or not 60 <= duration <= 28800:
        raise PlasmaError("duration_s must be an integer between 60 and 28800")
    reason = params.get("reason") or "Muse requested inhibit"
    if not isinstance(reason, str):
        raise PlasmaError("reason must be a string")
    proc = subprocess.Popen(
        ["systemd-inhibit", "--what=idle,sleep", "--who=Muse",
         f"--why={reason[:200]}", "sleep", str(duration)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, env=env,
    )
    return {"inhibited": True, "duration_s": duration, "pid": proc.pid,
            "note": "auto-expires; no uninhibit command needed"}


# ---------------------------------------------------------------------------
# power.profile — performance / balanced / power-saver
# ---------------------------------------------------------------------------

_POWER_PROFILES = ("performance", "balanced", "power-saver")


def power_profile(env: dict, params: dict) -> dict:
    if shutil.which("powerprofilesctl") is None:
        raise PlasmaError("powerprofilesctl is not installed")
    action = params.get("action") or "get"
    if action == "get":
        out = _run(["powerprofilesctl", "get"], env, timeout=10).strip()
        return {"profile": out}
    if action == "set":
        profile = params.get("profile")
        if profile not in _POWER_PROFILES:
            raise PlasmaError(f"profile must be one of {list(_POWER_PROFILES)}")
        _run(["powerprofilesctl", "set", profile], env, timeout=10)
        return {"profile": profile, "set": True}
    raise PlasmaError("action must be get or set")


# ---------------------------------------------------------------------------
# screen.brightness — PowerDevil D-Bus (verified on Plasma 6)
# ---------------------------------------------------------------------------

_BRIGHTNESS_SERVICE = "org.kde.Solid.PowerManagement"
_BRIGHTNESS_PATH = "/org/kde/Solid/PowerManagement/Actions/BrightnessControl"


def screen_brightness(env: dict, params: dict) -> dict:
    action = params.get("action") or "get"
    if action not in ("get", "set"):
        raise PlasmaError("action must be get or set")
    level = params.get("level")
    if action == "set":
        if not isinstance(level, (int, float)) or isinstance(level, bool) \
                or not 0 <= level <= 100:
            raise PlasmaError("level must be a number between 0 and 100")
    qdbus = _qdbus()

    def call(method: str, *args: str) -> str:
        return _run([qdbus, _BRIGHTNESS_SERVICE, _BRIGHTNESS_PATH,
                     method, *args], env, timeout=15)

    if action == "get":
        cur = int(call("brightness").strip())
        maxv = int(call("brightnessMax").strip())
        percent = round(cur / maxv * 100, 1) if maxv else 0.0
        return {"brightness": cur, "max": maxv, "percent": percent}
    maxv = int(call("brightnessMax").strip())
    value = int(round(level / 100 * maxv))
    method = "setBrightnessSilent" if params.get("silent") else "setBrightness"
    call(method, str(value))
    return {"set": True, "percent": float(level), "raw": value}


# ---------------------------------------------------------------------------
# clipboard.history / clipboard.clear
# ---------------------------------------------------------------------------

_HISTORY_ITEM_LIMIT = 50
_HISTORY_CHARS = 2000


def clipboard_history(env: dict, params: dict) -> dict:
    limit = params.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) \
            or not 1 <= limit <= _HISTORY_ITEM_LIMIT:
        raise PlasmaError(f"limit must be an integer between 1 and {_HISTORY_ITEM_LIMIT}")
    qdbus = _qdbus()
    items = []
    for i in range(limit):
        try:
            out = _run([qdbus, "org.kde.klipper", "/klipper",
                        "getClipboardHistoryItem", str(i)], env, timeout=10)
        except PlasmaError:
            break
        text = out.rstrip("\n")
        if not text:
            break
        items.append(text[:_HISTORY_CHARS])
    return {"history": items, "count": len(items)}


def clipboard_clear(env: dict, params: dict) -> dict:
    qdbus = _qdbus()
    _run([qdbus, "org.kde.klipper", "/klipper",
          "clearClipboardHistory"], env, timeout=10)
    _run([qdbus, "org.kde.klipper", "/klipper",
          "clearClipboardContents"], env, timeout=10)
    return {"cleared": True}
