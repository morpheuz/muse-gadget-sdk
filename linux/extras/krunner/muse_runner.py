#!/usr/bin/env python3
# Copyright (c) 2026 Artur Souza
# SPDX-License-Identifier: Apache-2.0
"""KRunner D-Bus runner: ask your Muse straight from KRunner.

Type ``muse <question>`` in KRunner (Alt+Space), press Enter. The question is
sent to your Muse via ``muse-msg ask`` and the answer appears in your Muse
chat, with a desktop notification confirming it was sent.

Implements the org.kde.krunner1 interface (Plasma 6, X-Plasma-API=DBus2).
The D-Bus runner model follows the same pattern as krunner-claude (MIT).

Files (installed by install.sh, all under $HOME):
  ~/.local/bin/muse_runner.py
  ~/.local/share/krunner/dbusplugins/muse-runner.desktop
  ~/.local/share/dbus-1/services/org.kde.muse.runner.service
"""

import os
import shutil
import subprocess
import sys

BUS_NAME = "org.kde.muse.runner"
OBJECT_PATH = "/muse"
IFACE = "org.kde.krunner1"

TRIGGER = os.environ.get("MUSE_RUNNER_TRIGGER", "muse")
ICON = os.environ.get("MUSE_RUNNER_ICON", "muse")

# A D-Bus activated process inherits a minimal environment, so never rely on
# the inherited PATH alone when looking for muse-msg or notify-send.
EXTRA_PATH = [
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/bin"),
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
]


def search_path():
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for extra in EXTRA_PATH:
        if extra not in parts:
            parts.append(extra)
    return os.pathsep.join(p for p in parts if p)


PATH = search_path()


def which(name):
    return shutil.which(name, path=PATH)


def find_muse_msg():
    override = os.environ.get("MUSE_RUNNER_MUSE_MSG_BIN")
    if override:
        return override if os.path.exists(override) else None
    return which("muse-msg")


def parse_query(query, trigger=TRIGGER):
    """Split a KRunner query into (kind, prompt).

    kind is "ignore" (another runner's business), "hint" (bare trigger word),
    or "ask" (trigger word + question).
    """
    q = query.strip()
    if not q.lower().startswith(trigger.lower()):
        return ("ignore", "")
    rest = q[len(trigger):]
    # Require a separator so "museums" does not match, but allow the bare
    # keyword so the user gets a hint as soon as they finish typing it.
    if rest and not rest[:1].isspace():
        return ("ignore", "")
    prompt = rest.strip()
    if not prompt:
        return ("hint", "")
    return ("ask", prompt)


def notify(summary, body):
    notify_send = which("notify-send")
    if notify_send:
        subprocess.Popen([notify_send, "-a", "Muse Runner", summary, body])
    else:
        print("%s: %s" % (summary, body), file=sys.stderr)


# Match reply tuple: (id, text, icon, type, relevance, properties).
MATCH_TYPE = 100


class MuseRunner:
    """D-Bus object; the dbus.service.Object base is mixed in at runtime."""

    def Match(self, query):
        kind, prompt = parse_query(query)
        if kind == "ignore":
            return []
        if kind == "hint":
            return [(
                ":hint:",
                "Ask Muse…",
                ICON,
                MATCH_TYPE,
                1.0,
                {"subtext": "Type: %s <your question>" % TRIGGER},
            )]
        if find_muse_msg() is None:
            return [(
                ":missing:",
                "muse-msg not found",
                "dialog-error",
                MATCH_TYPE,
                1.0,
                {"subtext": "Install it (box/muse-msg) and put it on PATH"},
            )]
        return [(
            prompt,
            "Ask Muse: %s" % prompt,
            ICON,
            MATCH_TYPE,
            1.0,
            {"subtext": "Sends to your Muse — the answer appears in chat"},
        )]

    def Actions(self):
        return []

    def Run(self, matchId, actionId):
        if matchId in (":hint:", ":missing:"):
            return
        muse_msg = find_muse_msg()
        if muse_msg is None:
            notify("Muse Runner", "muse-msg not found")
            return
        env = dict(os.environ)
        env["PATH"] = PATH
        try:
            proc = subprocess.run(
                [muse_msg, "ask", matchId],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 - report anything, then stop
            notify("Muse Runner", "Could not reach your Muse: %s" % exc)
            return
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()
            notify("Muse Runner",
                   "Not sent: %s" % (err[-1] if err else "muse-msg failed"))
            return
        notify("Muse Runner", "Sent — watch your Muse chat for the answer.")

    def Teardown(self):
        return


def _dbus_object_base():
    import dbus
    import dbus.service

    class _Base(dbus.service.Object):
        def __init__(self):
            bus_name = dbus.service.BusName(BUS_NAME, dbus.SessionBus())
            super().__init__(bus_name, OBJECT_PATH)

        @dbus.service.method(IFACE, in_signature="s",
                             out_signature="a(sssida{sv})")
        def Match(self, query):
            return MuseRunner.Match(self, query)

        @dbus.service.method(IFACE, in_signature="", out_signature="a(sss)")
        def Actions(self):
            return MuseRunner.Actions(self)

        @dbus.service.method(IFACE, in_signature="ss", out_signature="")
        def Run(self, matchId, actionId):
            return MuseRunner.Run(self, matchId, actionId)

        @dbus.service.method(IFACE, in_signature="", out_signature="")
        def Teardown(self):
            return MuseRunner.Teardown(self)

    return _Base


def main():
    try:
        from dbus.mainloop.glib import DBusGMainLoop
        from gi.repository import GLib
    except ImportError:
        print("muse_runner: need python3-dbus and python3-gi "
              "(e.g. apt install python3-dbus python3-gi)", file=sys.stderr)
        return 1
    DBusGMainLoop(set_as_default=True)
    cls = _dbus_object_base()
    cls()
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
