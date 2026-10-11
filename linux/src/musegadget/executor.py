# Copyright (c) Meta Platforms, Inc. and affiliates.
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

"""The commands a Muse can invoke on this device.

Shell commands and file operations run in child processes as a separate,
unprivileged account (``run_as``), never as the service account, which owns
the device credentials. A command gets exactly the access that account has.
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

from musegadget import __version__
from musegadget import plasma  # --- plasma ---

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 120
MAX_TIMEOUT_S = 600
# How long to wait for output after killing a timed-out command's process group.
KILL_GRACE_S = 2
# How long to keep reading queued output after the shell exits.
DRAIN_S = 0.5
# /link-control accepts at most 256 KiB per message from the device; leave
# room for the JSON envelope and escaping.
MAX_OUTPUT_BYTES = 96 * 1024
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

COMMAND_SPECS = {
    "system.run": {
        "description": (
            "Run a shell command on this Linux device with bash and return its "
            "stdout, stderr and exit code. Output over 96 KiB per stream is "
            "truncated."
        ),
        "required": {
            "command": {"type": "string", "description": "Shell command line to run."},
        },
        "optional": {
            "cwd": {"type": "string", "description": "Working directory. Default: the account's home."},
            "timeout_ms": {"type": "integer", "description": "Kill the command after this long. Default 120000, max 600000."},
        },
        "timeout_ms": MAX_TIMEOUT_S * 1000 + 5000,
    },
    "file.read": {
        "description": "Read up to 64 KiB of a file, base64-encoded. Call again with next_offset until eof.",
        "required": {
            "path": {"type": "string", "description": "Absolute path."},
        },
        "optional": {
            "offset": {"type": "integer", "description": "Byte offset. Default 0."},
            "limit": {"type": "integer", "description": "Max bytes, up to 65536."},
        },
    },
    "file.write": {
        "description": (
            "Write a file in chunks of up to 64 KiB (base64). Start at offset 0, "
            "send chunks in order, and set final=true on the last one; the file "
            "is replaced atomically, and only if sha256 (when given) matches."
        ),
        "required": {
            "path": {"type": "string", "description": "Absolute destination path."},
            "data_b64": {"type": "string", "description": "This chunk's bytes, base64-encoded."},
        },
        "optional": {
            "offset": {"type": "integer", "description": "Byte offset of this chunk. Default 0."},
            "final": {"type": "boolean", "description": "True on the last chunk. Default false."},
            "sha256": {"type": "string", "description": "Hex SHA-256 of the whole file, checked on the final chunk."},
            "create_parents": {"type": "boolean", "description": "Create missing parent directories. Default false."},
            "overwrite": {"type": "boolean", "description": "Replace an existing file. Default true."},
        },
    },
    "device.health": {
        "description": "Device status: uptime, load, memory, disk, temperature, software version.",
        "required": {},
        "optional": {},
    },
    # --- plasma: KDE Plasma desktop commands ---
    "desktop.status": {
        "description": (
            "Probe this gadget's KDE Plasma integration: session bus, Klipper, "
            "KWin, helper tools. Run this first on a Plasma machine before "
            "using the other desktop.*, clipboard.*, window.*, media.*, "
            "audio.*, session.*, power.* or screen.* commands."
        ),
        "required": {},
        "optional": {},
    },
    "desktop.notify": {
        "description": "Pop a desktop notification on the Plasma session.",
        "required": {
            "title": {"type": "string", "description": "Notification title."},
        },
        "optional": {
            "body": {"type": "string", "description": "Notification body text."},
            "urgency": {"type": "string", "description": "'low', 'normal' (default) or 'critical'."},
            "expire_ms": {"type": "integer", "description": "How long it stays, ms. 0 = persistent. Default 5000."},
        },
    },
    "desktop.screenshot": {
        "description": (
            "Take a screenshot with Spectacle (background mode, no popup) and "
            "save it as a PNG. Returns the file path — then use file.read to "
            "fetch the image bytes."
        ),
        "required": {},
        "optional": {
            "mode": {"type": "string", "description": "'fullscreen' (default), 'active' window, or 'region'."},
        },
        "timeout_ms": 60000,
    },
    "clipboard.get": {
        "description": "Read the latest text from the Plasma clipboard via Klipper.",
        "required": {},
        "optional": {},
    },
    "clipboard.set": {
        "description": "Put text on the Plasma clipboard via Klipper.",
        "required": {
            "text": {"type": "string", "description": "Text to stage on the clipboard."},
        },
        "optional": {
            "clear_history": {"type": "boolean", "description": "Also wipe Klipper's history (useful after staging a secret). Default false."},
        },
    },
    "clipboard.history": {
        "description": "Latest Klipper clipboard entry only (history trawling is disabled).",
        "required": {},
        "optional": {},
    },
    "clipboard.clear": {
        "description": "Wipe Klipper's clipboard history and current contents.",
        "required": {},
        "optional": {},
    },
    "window.list": {
        "description": (
            "List open windows on the Plasma desktop: caption, app class, "
            "virtual desktop, active/minimized flags. Implemented via a KWin "
            "script; see desktop.status if it reports no output."
        ),
        "required": {},
        "optional": {},
        "timeout_ms": 45000,
    },
    "window.focus": {
        "description": (
            "Bring a window to the front by substring match against its title "
            "or app class (case-insensitive). Un-minimizes it, moves it to the "
            "current desktop, and activates it."
        ),
        "required": {
            "match": {"type": "string", "description": "Substring to match, e.g. 'firefox' or 'Settings'."},
        },
        "optional": {},
        "timeout_ms": 45000,
    },
    "screen.lock": {
        "description": "Lock the Plasma session immediately (screen locker).",
        "required": {},
        "optional": {},
    },
    "screen.brightness": {
        "description": "Screen brightness via PowerDevil D-Bus (keeps Plasma's OSD/state in sync).",
        "required": {},
        "optional": {
            "action": {"type": "string", "description": "'get' (default) or 'set'."},
            "level": {"type": "number", "description": "0-100 percent, for action 'set'."},
            "silent": {"type": "boolean", "description": "Suppress the brightness OSD popup. Default false."},
        },
    },
    "media.control": {
        "description": "Control media playback via MPRIS/playerctl.",
        "required": {},
        "optional": {
            "action": {"type": "string", "description": "'play', 'pause', 'play-pause' (default), 'next', 'previous' or 'stop'."},
            "player": {"type": "string", "description": "Player name (e.g. 'spotify'). Default: the active player."},
        },
    },
    "media.info": {
        "description": "Now-playing info via playerctl: player, artist, title, album, status.",
        "required": {},
        "optional": {
            "player": {"type": "string", "description": "Player name. Default: the active player."},
        },
    },
    "audio.volume": {
        "description": "System audio volume via wpctl (PipeWire).",
        "required": {},
        "optional": {
            "action": {"type": "string", "description": "'get' (default), 'set' or 'mute' (toggles)."},
            "level": {"type": "number", "description": "Volume 0.0-1.0, for action 'set'."},
        },
    },
    "session.power": {
        "description": (
            "Power/session actions: 'suspend', 'reboot', 'shutdown' or 'logout'. "
            "Acts immediately via ksmserver (logind fallback). Note: after "
            "'suspend' the reply may never arrive — the machine is asleep."
        ),
        "required": {
            "action": {"type": "string", "description": "'suspend', 'reboot', 'shutdown' or 'logout'."},
        },
        "optional": {
            "confirm_dialog": {"type": "boolean", "description": "Show Plasma's confirmation dialog first. Default false."},
        },
        "timeout_ms": 20000,
    },
    "session.idle_inhibit": {
        "description": (
            "Block idle sleep/screen locking for a bounded time (e.g. during a "
            "long build). Auto-expires; no uninhibit needed."
        ),
        "required": {},
        "optional": {
            "duration_s": {"type": "integer", "description": "60 to 28800 seconds. Default 1800."},
            "reason": {"type": "string", "description": "Human-readable reason shown to the session."},
        },
    },
    "power.profile": {
        "description": "System power profile via powerprofilesctl.",
        "required": {},
        "optional": {
            "action": {"type": "string", "description": "'get' (default) or 'set'."},
            "profile": {"type": "string", "description": "'performance', 'balanced' or 'power-saver', for action 'set'."},
        },
    },
}


@dataclass(frozen=True)
class Account:
    name: str
    uid: int
    gid: int
    home: str

    @classmethod
    def lookup(cls, name: str) -> "Account":
        entry = pwd.getpwnam(name)
        return cls(entry.pw_name, entry.pw_uid, entry.pw_gid, entry.pw_dir)

    @classmethod
    def current(cls) -> "Account":
        entry = pwd.getpwuid(os.getuid())
        return cls(entry.pw_name, entry.pw_uid, entry.pw_gid, entry.pw_dir)


def ok(payload: dict) -> dict:
    return {"ok": True, "payload": payload}


def error(message: str) -> dict:
    return {"ok": False, "error": message}


class Executor:
    def __init__(self, account: Account) -> None:
        self.account = account

    def run(self, command: str, params: dict, timeout_ms: int | None = None) -> dict:
        try:
            if command == "system.run":
                return self.system_run(params, timeout_ms)
            if command in ("file.read", "file.write"):
                return self.file_op(command.split(".")[1], params)
            if command == "device.health":
                return ok(device_health())
            # --- plasma: our dispatch (upstream handles the rest above) ---
            if command.split(".")[0] in ("desktop", "clipboard", "window",
                    "media", "audio", "session", "power", "screen"):
                return self._plasma(command, params)
        except Exception as exc:
            log.exception("%s failed", command)
            return error(f"{type(exc).__name__}: {exc}")
        return error(f"unsupported command: {command}")

    # -- Child processes ------------------------------------------------------

    def _child_options(self) -> dict:
        env = {
            "HOME": self.account.home,
            "USER": self.account.name,
            "LOGNAME": self.account.name,
            "PATH": SAFE_PATH,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        options: dict = {"env": env, "start_new_session": True}
        if os.geteuid() == 0 and self.account.uid != 0:
            options.update(user=self.account.uid, group=self.account.gid,
                           extra_groups=os.getgrouplist(self.account.name, self.account.gid))
        return options

    def system_run(self, params: dict, timeout_ms: int | None) -> dict:
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            return error("command is required")
        requested = params.get("timeout_ms") or timeout_ms
        timeout_s = min(int(requested) / 1000 if requested else DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S)
        cwd = params.get("cwd") or self.account.home
        log.info("system.run as %s (timeout %ss)", self.account.name, timeout_s)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                ["/bin/bash", "-c", command], cwd=cwd,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0,
                **self._child_options(),
            )
        except OSError as exc:
            return error(f"could not start command: {exc}")
        timed_out, stdout, stderr = _wait_for_shell(proc, timeout_s)
        if timed_out:
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None and not pipe.closed:
                    os.set_blocking(pipe.fileno(), True)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                more_out, more_err = proc.communicate(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired as exc:
                # A process that left the group (setsid, a daemon) survived the kill and
                # still holds the pipes. Keep what was read instead of waiting on it.
                more_out, more_err = exc.stdout or b"", exc.stderr or b""
                for pipe in (proc.stdout, proc.stderr):
                    if pipe is not None:
                        pipe.close()
                proc.wait()
            stdout += more_out
            stderr += more_err
        out, out_cut = _clip(stdout)
        err, err_cut = _clip(stderr)
        return ok({
            "stdout": out,
            "stderr": err,
            "exit_code": proc.returncode,
            "timed_out": timed_out,
            "truncated": out_cut or err_cut,
            "duration_ms": int((time.monotonic() - started) * 1000),
        })

    # --- plasma: our commands (upstream methods above) ---
    def _plasma(self, command: str, params: dict) -> dict:
        try:
            return ok(plasma.run(command, params, self._child_options(), self.account))
        except plasma.PlasmaError as exc:
            return error(str(exc))

    def file_op(self, op: str, params: dict) -> dict:
        request = json.dumps({**params, "op": op})
        proc = subprocess.run(
            [sys.executable, "-m", "musegadget.fileops"], input=request.encode(),
            capture_output=True, timeout=60, cwd="/", **self._child_options(),
        )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return error(proc.stderr.decode(errors="replace")[-2000:] or "file operation failed")


def _read_available(pipe, deadline: float) -> tuple[bytes, bool]:
    # Stop at the deadline too: a writer that outpaces the reads would
    # otherwise keep this loop from ever seeing an empty pipe.
    chunks = []
    while time.monotonic() < deadline:
        try:
            chunk = pipe.read(65536)
        except BlockingIOError:
            return b"".join(chunks), True
        if chunk is None:
            return b"".join(chunks), True
        if chunk == b"":
            return b"".join(chunks), False
        chunks.append(chunk)
    return b"".join(chunks), True


def _wait_for_shell(proc, timeout_s: float) -> tuple[bool, bytes, bytes]:
    # Wait until the shell exits. Read only bytes already queued so a grandchild
    # that inherited the pipes cannot hold this open until EOF.
    streams: dict = {}
    for pipe in (proc.stdout, proc.stderr):
        if pipe is None:
            continue
        os.set_blocking(pipe.fileno(), False)
        streams[pipe] = []
    watching = list(streams)
    deadline = time.monotonic() + timeout_s
    while proc.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True, _joined(streams, proc.stdout), _joined(streams, proc.stderr)
        if not watching:
            time.sleep(min(remaining, 0.05))
            continue
        ready, _, _ = select.select(watching, [], [], min(remaining, 0.05))
        for pipe in ready:
            data, still_open = _read_available(pipe, deadline)
            if data:
                streams[pipe].append(data)
            if not still_open:
                watching.remove(pipe)
    drain_deadline = time.monotonic() + DRAIN_S
    for pipe in streams:
        if pipe.closed:
            continue
        data, _ = _read_available(pipe, drain_deadline)
        if data:
            streams[pipe].append(data)
    return False, _joined(streams, proc.stdout), _joined(streams, proc.stderr)


def _joined(streams: dict, pipe) -> bytes:
    return b"".join(streams.get(pipe, ()))


def _clip(data: bytes) -> tuple[str, bool]:
    cut = len(data) > MAX_OUTPUT_BYTES
    text = data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    # The budget is for the message, where json.dumps writes every non-ASCII
    # character as a 6- or 12-byte escape and each undecodable byte became
    # U+FFFD, so binary or non-Latin output can grow sixfold.
    while len(json.dumps(text)) - 2 > MAX_OUTPUT_BYTES:
        text = text[: len(text) * 3 // 4]
        cut = True
    return text, cut


def device_health() -> dict:
    health: dict = {"version": __version__, "hostname": socket.gethostname()}
    try:
        health["uptime_s"] = int(float(open("/proc/uptime").read().split()[0]))
    except OSError:
        pass
    try:
        health["load"] = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        pass
    try:
        meminfo = dict(line.split(":", 1) for line in open("/proc/meminfo"))
        health["memory_mb"] = {
            "total": int(meminfo["MemTotal"].split()[0]) // 1024,
            "available": int(meminfo["MemAvailable"].split()[0]) // 1024,
        }
    except (OSError, KeyError, ValueError):
        pass
    disk = shutil.disk_usage("/")
    health["disk_gb"] = {"total": round(disk.total / 1e9, 1), "free": round(disk.free / 1e9, 1)}
    try:
        health["temperature_c"] = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
    except (OSError, ValueError):
        pass
    try:
        health["model"] = open("/proc/device-tree/model").read().strip("\x00\n")
    except OSError:
        pass
    return health
