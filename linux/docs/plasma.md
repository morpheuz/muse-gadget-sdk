# KDE Plasma integration

17 Plasma-aware commands for a Linux gadget running KDE Plasma, in
`src/musegadget/plasma.py`, wired into the executor on this branch.
Stdlib-only. 34 tests in `tests/test_plasma.py`.

## The commands

| Command | What it does |
|---|---|
| `desktop.status` | Readiness probe: session bus, Klipper, KWin, helper tools. Run first |
| `desktop.notify` | Pop a notification (`notify-send`), urgency + expiry options |
| `desktop.screenshot` | Spectacle background capture → PNG path (then `file.read` for bytes) |
| `clipboard.get` / `clipboard.set` | Klipper over D-Bus; history index; optional history wipe |
| `clipboard.history` / `clipboard.clear` | Full Klipper history (newest first); wipe history + contents |
| `window.list` / `window.focus` | KWin scripting roundtrip (caption, app class, desktop, active/minimized; focus by substring) |
| `screen.lock` | Lock the session now |
| `screen.brightness` | PowerDevil D-Bus get/set (percent, keeps Plasma OSD in sync) |
| `media.control` / `media.info` | MPRIS via `playerctl` |
| `audio.volume` | PipeWire volume via `wpctl` (get/set/mute) |
| `session.power` | `suspend` / `reboot` / `shutdown` / `logout` via ksmserver (logind fallback) |
| `session.idle_inhibit` | Block idle sleep for a bounded time (auto-expires) |
| `power.profile` | `performance` / `balanced` / `power-saver` via `powerprofilesctl` |

## Install (on the KDE machine)

The gadget service runs as root but command children run as the `--run-as`
account — for Plasma that **must be your desktop login**, because everything
here needs your session D-Bus:

```bash
bash install.sh --sdk-token mgst_… --run-as $USER
sudo systemctl restart musegadget
# then in the Muse app: run desktop.status
```

Prerequisites on the box: a Plasma session (X11 or Wayland), `qdbus`/`qdbus6`,
`notify-send`, `spectacle`, `playerctl`, `wpctl`, `journalctl` — most ship
with Plasma; `desktop.status` reports exactly what's missing. A USB Bluetooth
dongle is only needed once, for the initial phone pairing.

## How the tricky bits work

- **Session env**: the SDK strips the environment from command children, so
  `plasma.py` rebuilds it from the account uid —
  `DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/<uid>/bus`,
  `XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY` when the socket exists, `DISPLAY=:0`
  fallback otherwise. Helper subprocesses are demoted to the `--run-as`
  account (the service itself runs as root), and files they create on the
  desktop user's behalf are chown'd over — e.g. the KWin script temp file,
  which KWin itself must be able to open.
- **Window list/focus**: KWin exposes no direct "list windows" D-Bus method.
  We inject a JS snippet via `org.kde.KWin /Scripting loadScript`, run it, and
  read its `print()` output back from the journal with a unique run marker.
  If `window.list` reports no script output, the `kwin_scripting` logging
  category is disabled on that box. KWin script `print()` goes out at debug
  level, so anything filtering Qt debug output silences it — notably,
  Fedora's qt6-qtbase ships `/usr/share/qt6/qtlogging.ini` with
  `*.debug=false`. The gadget installs the KDE-documented fix itself:
  `QT_LOGGING_RULES="kwin_*.debug=true"` in
  `~/.config/environment.d/kwin-debug.conf` (idempotent; takes effect at next
  login — `window.list` reports `kwin_logging: applied` when it just wrote
  the file). `kdebugsettings` → KWin Scripting → Full Debug is the GUI
  equivalent.
- **Screenshots**: Spectacle `-b -n -o` (background, no popup). Returns the PNG
  path; the existing `file.read` command fetches the bytes.

## Security notes

This makes Muse powerful on that box — screenshots, clipboard, window control.
Install with `--run-as` set to a normal (non-sudo) desktop account, pair on a
trusted network, and treat the machine accordingly. `clipboard.set` has a
`clear_history` option for staging secrets.

## Test checklist (on real hardware)

- [ ] `desktop.status` → bus socket true, klipper/kwin true, tools present
- [ ] `desktop.notify` → notification appears on screen
- [ ] `clipboard.set` → paste into an editor shows the text
- [ ] `desktop.screenshot` → PNG opens, shows the desktop
- [ ] `window.list` → open windows listed with correct titles
- [ ] `window.focus` → target window comes forward
- [ ] `media.control` play-pause → music toggles
- [ ] `screen.brightness` get → percent matches the OSD slider; set 50 → screen dims
- [ ] `power.profile` get/set → `powerprofilesctl` agrees
- [ ] `clipboard.history` → recent copies listed newest-first
- [ ] `session.idle_inhibit` (e.g. 120s) → screen stays awake past the idle timeout

## Notes

- **Do Not Disturb was deliberately left out.** No scriptable D-Bus/config
  interface for Plasma 6 DND could be verified — shipping a guess would be
  worse than omitting it. Five-minute add on the box via
  `busctl --user tree org.kde.notificationmanager` introspection.
- The brightness interface (`org.kde.Solid.PowerManagement` /
  `BrightnessControl`) was verified against live Plasma 6.6.5 introspection
  notes; `brightnessMax` is a normalized 10000 scale, not raw sysfs.

## Reverse channel: the box talks to Muse

The SDK already ships the other direction. `musegadget send-user-msg` (on the
box, via the service's Unix socket) posts a message that lands in the Muse app
as coming from the device — main chat by default, or a side chat with
`--session-id` (an unknown id starts a new side chat). If Muse replies with a
follow-up device command, it routes back to that same box. The CLI errors with
"not connected to the Muse" when the link is down, so it doubles as a link
check.

`extras/muse-msg` wraps this with a structured convention so the agent can tell
machine traffic apart from typed chat:

    muse-msg alert [--severity info|warning|critical] [--source NAME] [--chat ID] "message"
    muse-msg ask [--chat ID] "question"
    muse-msg event NAME [--source NAME] [--chat ID] ["details..."]
    muse-msg ping

Wire format examples:

    [muse:alert] severity=warning source=disk-watch :: /home is 91% full
    [muse:ask] what's the weather like?
    [muse:event] name=screen-locked source=presence :: user locked the session
    [muse:ping]

`extras/examples/` has a working starter: `disk-watch.sh` plus systemd user
service/timer units that alert hourly when a filesystem passes 90%.

Ideas this unlocks: proactive alerts (build finished, disk full, backup
failed), ask-from-desktop (hotkey → prompt → answer back in chat, optionally
pushed to a desktop notification), lock/unlock presence, approval requests
from scripts, and per-topic side chats as notification channels
(`--chat build-watch`). Security note: messages arrive attributed to the
device, so the box is a trusted speaker — install the gadget only where you'd
trust your own voice.

## Ask Muse from KRunner

`extras/krunner/` is a Plasma 6 D-Bus runner: press Alt+Space, type
`muse <your question>`, hit Enter. The question goes to your Muse via
`muse-msg ask`; the answer appears in your Muse chat, with a desktop
notification confirming it was sent.

It implements `org.kde.krunner1` (`X-Plasma-API=DBus2`) in ~200 lines of
Python — no compilation. The D-Bus runner model and method signatures mirror
the working krunner-claude project (MIT), verified against its Plasma 6.7
implementation: `Match(s) → a(sssida{sv})`, `Run(ss)`, `Actions() → a(sss)`,
`Teardown()`. The `Match` logic and `Run` dispatch are unit-tested; live
KRunner registration needs a real Plasma 6 box.

Install on the KDE machine:

    cd extras/krunner && ./install.sh

Then Alt+Space → `muse what is the time?`. Enable it under System Settings →
Search → Plasma Search if it doesn't show. `./uninstall.sh` removes it.
`MUSE_RUNNER_TRIGGER` changes the keyword; `MUSE_RUNNER_ICON` the icon.
