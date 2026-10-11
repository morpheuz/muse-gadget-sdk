"""Tests for the KDE Plasma gadget commands (musegadget.plasma).

Run from the SDK tree:  PYTHONPATH=src python3 -m pytest tests/test_plasma.py -q
"""

import os
import sys
import types

import pytest

from musegadget import plasma
from musegadget.plasma import PlasmaError


class FakeAccount:
    name = "artur"
    uid = 1000
    gid = 1000
    home = "/home/artur"


@pytest.fixture
def calls(monkeypatch):
    """Capture plasma._run invocations; return canned outputs by prefix."""
    invocations = []
    canned = {}

    def fake_run(argv, env, timeout=20):
        invocations.append((argv, env, timeout))
        for prefix, output in canned.items():
            if tuple(argv[:len(prefix)]) == prefix:
                if isinstance(output, Exception):
                    raise output
                return output
        return ""

    monkeypatch.setattr(plasma, "_run", fake_run)
    return invocations, canned


def test_session_env_builds_bus_address():
    env = plasma._session_env({"HOME": "/home/artur"}, 1000)
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert env["HOME"] == "/home/artur"


def test_session_env_prefers_wayland(monkeypatch):
    monkeypatch.setattr(os.path, "exists",
                        lambda p: p == "/run/user/1000/wayland-0")
    env = plasma._session_env({}, 1000)
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert "DISPLAY" not in env


def test_session_env_x11_fallback(monkeypatch):
    monkeypatch.setattr(os.path, "exists",
                        lambda p: p == "/tmp/.X11-unix/X0")
    env = plasma._session_env({}, 1000)
    assert env["DISPLAY"] == ":0"
    assert "WAYLAND_DISPLAY" not in env


def test_notify_argv(calls, monkeypatch):
    invocations, _ = calls
    monkeypatch.setattr(plasma.shutil, "which", lambda n: "/usr/bin/" + n)
    out = plasma.run("desktop.notify",
                     {"title": "Hi", "body": "there", "urgency": "critical",
                      "expire_ms": 0},
                     {"env": {}}, FakeAccount())
    assert out == {"notified": True, "urgency": "critical"}
    argv = invocations[0][0]
    assert argv[:2] == ["notify-send", "--app-name=Muse"]
    assert "--urgency=critical" in argv
    assert "--expire-time=0" in argv
    assert argv[-2:] == ["Hi", "there"]


def test_notify_rejects_bad_urgency(calls):
    with pytest.raises(PlasmaError):
        plasma.run("desktop.notify", {"title": "x", "urgency": "loud"},
                   {"env": {}}, FakeAccount())


def test_notify_requires_title(calls):
    with pytest.raises(PlasmaError):
        plasma.run("desktop.notify", {"body": "no title"}, {"env": {}},
                   FakeAccount())


def test_clipboard_set_requires_text(calls, monkeypatch):
    monkeypatch.setattr(plasma, "_qdbus", lambda: "qdbus")
    with pytest.raises(PlasmaError):
        plasma.run("clipboard.set", {"text": ""}, {"env": {}}, FakeAccount())


def test_clipboard_get_current(calls, monkeypatch):
    invocations, canned = calls
    monkeypatch.setattr(plasma, "_qdbus", lambda: "qdbus")
    canned[("qdbus",)] = "hello\n"
    out = plasma.run("clipboard.get", {}, {"env": {}}, FakeAccount())
    assert out == {"text": "hello", "chars": 5}
    assert invocations[0][0][-1] == "getClipboardContents"


def test_clipboard_get_ignores_history_index(calls, monkeypatch):
    # index param removed: the gadget only ever reads the latest entry
    invocations, canned = calls
    monkeypatch.setattr(plasma, "_qdbus", lambda: "qdbus")
    canned[("qdbus",)] = "hello\n"
    out = plasma.run("clipboard.get", {"index": 2}, {"env": {}}, FakeAccount())
    assert out == {"text": "hello", "chars": 5}
    assert invocations[0][0][-1] == "getClipboardContents"


def test_audio_volume_parses_muted(calls, monkeypatch):
    _, canned = calls
    monkeypatch.setattr(plasma.shutil, "which", lambda n: "/usr/bin/" + n)
    canned[("wpctl", "get-volume")] = "Volume: 0.42 [MUTED]\n"
    out = plasma.run("audio.volume", {"action": "get"}, {"env": {}},
                     FakeAccount())
    assert out == {"volume": 0.42, "muted": True}


def test_audio_volume_set_validates_level(calls):
    with pytest.raises(PlasmaError):
        plasma.run("audio.volume", {"action": "set", "level": 1.5},
                   {"env": {}}, FakeAccount())


def test_media_control_maps_toggle(calls, monkeypatch):
    invocations, _ = calls
    monkeypatch.setattr(plasma.shutil, "which", lambda n: "/usr/bin/" + n)
    out = plasma.run("media.control", {"action": "toggle", "player": "spotify"},
                     {"env": {}}, FakeAccount())
    assert out == {"action": "toggle", "player": "spotify"}
    assert invocations[0][0] == ["/usr/bin/playerctl", "-p", "spotify",
                                 "play-pause"]


def test_media_info_no_players(calls, monkeypatch):
    _, canned = calls
    monkeypatch.setattr(plasma.shutil, "which", lambda n: "/usr/bin/" + n)
    canned[("/usr/bin/playerctl",)] = PlasmaError("playerctl failed")
    with pytest.raises(PlasmaError, match="no active media players"):
        plasma.run("media.info", {}, {"env": {}}, FakeAccount())


def test_kwin_script_template():
    js = plasma._KWIN_JS
    assert "__RUNID__" in js
    assert "__MATCH_JSON__" in js
    assert "workspace.windowList()" in js
    assert "workspace.clientList()" in js  # Plasma 5 fallback
    assert "normalWindow" in js


def test_window_list_parses_records(monkeypatch):
    runid = "mgabc123"
    lines = [
        f"{runid}\tWIN\tDolphin\torg.kde.dolphin\tdolphin\t1\t1\t0",
        f"{runid}\tWIN\tKonsole\torg.kde.konsole\tkonsole\t1\t0\t1",
        f"{runid}\tEND",
    ]
    monkeypatch.setattr(plasma, "_kwin_inject", lambda env, match: (lines, "ok"))
    out = plasma.window_list({})
    assert out["count"] == 2
    assert out["windows"][0]["caption"] == "Dolphin"
    assert out["windows"][0]["active"] is True
    assert out["windows"][1]["minimized"] is True


def test_window_list_empty_journal(monkeypatch):
    monkeypatch.setattr(plasma, "_kwin_inject", lambda env, match: ([], "ok"))
    with pytest.raises(PlasmaError, match="kwin_scripting"):
        plasma.window_list({})


def test_window_focus_nomatch(monkeypatch):
    runid = "mgabc123"
    monkeypatch.setattr(plasma, "_kwin_inject",
                        lambda env, match: ([f"{runid}\tNOMATCH",
                                            f"{runid}\tEND"], "ok"))
    with pytest.raises(PlasmaError, match="no window matched"):
        plasma.window_focus({}, {"match": "nope"})


def test_window_focus_requires_match():
    with pytest.raises(PlasmaError, match="match is required"):
        plasma.window_focus({}, {"match": "  "})


def test_ensure_kwin_logging_writes_conf(tmp_path, monkeypatch):
    monkeypatch.setattr(plasma, "_RUN_USER", 1000)
    monkeypatch.setattr(plasma, "_RUN_GROUP", 1000)
    chowned = []
    monkeypatch.setattr(os, "chown",
                        lambda p, u, g: chowned.append((p, u, g)))
    env = {"HOME": str(tmp_path)}
    assert plasma._ensure_kwin_script_logging(env) == "applied"
    conf = tmp_path / ".config" / "environment.d" / "kwin-debug.conf"
    assert conf.read_text() == 'QT_LOGGING_RULES="kwin_*.debug=true"\n'
    assert chowned == [(str(conf), 1000, 1000)]
    assert plasma._ensure_kwin_script_logging(env) == "ok"


def test_ensure_kwin_logging_unavailable_without_home():
    assert plasma._ensure_kwin_script_logging({}) == "unavailable"


def test_kwin_inject_hands_script_to_desktop_user(tmp_path, monkeypatch):
    import time as time_mod
    monkeypatch.setattr(plasma, "_RUN_USER", 1000)
    monkeypatch.setattr(plasma, "_RUN_GROUP", 1000)
    monkeypatch.setattr(plasma, "_ensure_kwin_script_logging",
                        lambda env: "ok")
    seen = {}
    monkeypatch.setattr(os, "chown",
                        lambda p, u, g: seen.update(chown=(u, g)))
    monkeypatch.setattr(os, "chmod",
                        lambda p, mode: seen.update(chmod=mode))
    monkeypatch.setattr(plasma, "_qdbus", lambda: "/usr/bin/qdbus")
    monkeypatch.setattr(plasma, "_run", lambda argv, env, timeout=20: "0")
    monkeypatch.setattr(plasma, "_journal_marker_lines",
                        lambda env, since, runid: [f"{runid}\tEND"])
    monkeypatch.setattr(time_mod, "sleep", lambda s: None)
    lines, status = plasma._kwin_inject({"HOME": str(tmp_path)}, match=None)
    assert status == "ok"
    assert seen["chown"] == (1000, 1000)
    assert seen["chmod"] == 0o644
    assert lines and lines[0].endswith("\tEND")


def test_run_rejects_unknown_command(calls):
    with pytest.raises(PlasmaError, match="unsupported plasma command"):
        plasma.run("plasma.bogus", {}, {"env": {}}, FakeAccount())


# --- batch 2: Power & Session + Small Completions ---

def test_session_power_validates_action(calls):
    with pytest.raises(PlasmaError, match="action must be one of"):
        plasma.run("session.power", {"action": "hibernate"}, {"env": {}},
                   FakeAccount())


def test_session_power_logout_via_ksmserver(calls, monkeypatch):
    invocations, canned = calls
    monkeypatch.setattr(plasma, "_qdbus", lambda: "qdbus")
    canned[("qdbus",)] = ""
    out = plasma.run("session.power", {"action": "logout"}, {"env": {}},
                     FakeAccount())
    assert out == {"action": "logout", "via": "ksmserver"}
    assert invocations[0][0] == [
        "qdbus", "org.kde.ksmserver", "/KSMServer",
        "org.kde.KSMServerInterface.logout", "0", "0", "0"]


def test_session_power_suspend_via_loginctl(calls):
    invocations, canned = calls
    canned[("loginctl",)] = ""
    out = plasma.run("session.power", {"action": "suspend"}, {"env": {}},
                     FakeAccount())
    assert out == {"action": "suspend", "via": "loginctl"}
    assert invocations[0][0] == ["loginctl", "suspend"]


def test_session_power_logout_falls_back_to_loginctl(calls, monkeypatch):
    invocations, canned = calls
    monkeypatch.setattr(plasma, "_qdbus", lambda: "qdbus")
    canned[("qdbus",)] = PlasmaError("no ksmserver")
    canned[("loginctl", "--no-legend", "list-sessions")] = \
        "2 1000 artur seat0 -\n"
    canned[("loginctl",)] = ""
    out = plasma.run("session.power", {"action": "logout"}, {"env": {}},
                     FakeAccount())
    assert out == {"action": "logout", "via": "loginctl"}
    assert invocations[-1][0] == ["loginctl", "terminate-session", "2"]


def test_idle_inhibit_validates_duration(monkeypatch):
    monkeypatch.setattr(plasma.shutil, "which", lambda n: "/usr/bin/" + n)
    with pytest.raises(PlasmaError, match="duration_s"):
        plasma.run("session.idle_inhibit", {"duration_s": 30}, {"env": {}},
                   FakeAccount())
    with pytest.raises(PlasmaError, match="duration_s"):
        plasma.run("session.idle_inhibit", {"duration_s": 99999}, {"env": {}},
                   FakeAccount())


def test_idle_inhibit_spawns_detached(monkeypatch):
    captured = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            self.pid = 1234

    monkeypatch.setattr(plasma.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr(plasma.subprocess, "Popen", FakePopen)
    out = plasma.run("session.idle_inhibit",
                     {"duration_s": 600, "reason": "long build"},
                     {"env": {"X": "y"}}, FakeAccount())
    assert out["inhibited"] is True and out["duration_s"] == 600
    assert captured["argv"][:4] == ["systemd-inhibit", "--what=idle,sleep",
                                   "--who=Muse", "--why=long build"]
    assert captured["argv"][-2:] == ["sleep", "600"]
    assert captured["kwargs"]["start_new_session"] is True


def test_power_profile_get(calls, monkeypatch):
    _, canned = calls
    monkeypatch.setattr(plasma.shutil, "which", lambda n: "/usr/bin/" + n)
    canned[("powerprofilesctl",)] = "balanced\n"
    out = plasma.run("power.profile", {"action": "get"}, {"env": {}},
                     FakeAccount())
    assert out == {"profile": "balanced"}


def test_power_profile_set_validates(calls, monkeypatch):
    monkeypatch.setattr(plasma.shutil, "which", lambda n: "/usr/bin/" + n)
    with pytest.raises(PlasmaError, match="profile must be one of"):
        plasma.run("power.profile",
                   {"action": "set", "profile": "turbo"}, {"env": {}},
                   FakeAccount())


def test_screen_brightness_get_percent(calls, monkeypatch):
    _, canned = calls
    monkeypatch.setattr(plasma, "_qdbus", lambda: "qdbus")
    base = ("qdbus", "org.kde.Solid.PowerManagement",
            "/org/kde/Solid/PowerManagement/Actions/BrightnessControl")
    canned[base + ("brightness",)] = "7500\n"
    canned[base + ("brightnessMax",)] = "10000\n"
    out = plasma.run("screen.brightness", {"action": "get"}, {"env": {}},
                     FakeAccount())
    assert out == {"brightness": 7500, "max": 10000, "percent": 75.0}


def test_screen_brightness_set_silent(calls, monkeypatch):
    invocations, canned = calls
    monkeypatch.setattr(plasma, "_qdbus", lambda: "qdbus")
    base = ("qdbus", "org.kde.Solid.PowerManagement",
            "/org/kde/Solid/PowerManagement/Actions/BrightnessControl")
    canned[base + ("brightnessMax",)] = "10000\n"
    canned[base + ("setBrightnessSilent",)] = ""
    out = plasma.run("screen.brightness",
                     {"action": "set", "level": 50, "silent": True},
                     {"env": {}}, FakeAccount())
    assert out == {"set": True, "percent": 50.0, "raw": 5000}
    assert invocations[-1][0][-2:] == ["setBrightnessSilent", "5000"]


def test_screen_brightness_set_validates_level(calls):
    with pytest.raises(PlasmaError, match="level must be"):
        plasma.run("screen.brightness", {"action": "set", "level": 150},
                   {"env": {}}, FakeAccount())


def test_clipboard_history_returns_only_latest(monkeypatch):
    seen = []

    def fake_run(argv, env, timeout=20):
        seen.append(argv)
        return "latest\n"

    monkeypatch.setattr(plasma, "_run", fake_run)
    monkeypatch.setattr(plasma, "_qdbus", lambda: "qdbus")
    out = plasma.run("clipboard.history", {}, {"env": {}}, FakeAccount())
    assert out == {"history": ["latest"], "count": 1}
    # only ever reads history index 0, never trawls
    assert seen == [["qdbus", "org.kde.klipper", "/klipper",
                     "getClipboardHistoryItem", "0"]]


def test_clipboard_history_empty(monkeypatch):
    monkeypatch.setattr(plasma, "_run", lambda argv, env, timeout=20: "\n")
    monkeypatch.setattr(plasma, "_qdbus", lambda: "qdbus")
    out = plasma.run("clipboard.history", {}, {"env": {}}, FakeAccount())
    assert out == {"history": [], "count": 0}


def test_clipboard_clear_calls_both(calls, monkeypatch):
    invocations, canned = calls
    monkeypatch.setattr(plasma, "_qdbus", lambda: "qdbus")
    canned[("qdbus",)] = ""
    out = plasma.run("clipboard.clear", {}, {"env": {}}, FakeAccount())
    assert out == {"cleared": True}
    methods = [a[0][-1] for a in invocations]
    assert methods == ["clearClipboardHistory", "clearClipboardContents"]


def test_desktop_status_reports_power(monkeypatch):
    monkeypatch.setattr(plasma, "_service_present", lambda env, svc: True)
    monkeypatch.setattr(plasma.shutil, "which", lambda n: "/usr/bin/" + n)
    out = plasma.desktop_status({"X": "y"}, FakeAccount())
    assert set(out["power"]) == {"powerdevil_brightness", "powerprofilesctl",
                                 "systemd_inhibit", "loginctl"}
