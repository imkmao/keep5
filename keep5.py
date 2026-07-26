#!/usr/bin/env python3
"""keep5 — keep the next Claude Code 5h window starting ASAP.

Single entry point. With no argument this is the one-shot, stateless tick
that the scheduler (launchd on macOS, a systemd --user timer on Linux) runs
every N minutes:

    now < next_reset   -> silent no-op
    now >= next_reset  -> fire one minimal request, refresh next_reset
    no/corrupt state   -> cold start: fire once, write next_reset

The one request both opens the next window and returns the new window's
reset time in a response header. See BUILD-YOUR-OWN.md.

With an argument it is the management CLI:

    keep5 setup     paste your Claude token -> ~/.keep5/oat (chmod 600)
    keep5 enable    install the background job (launchd / systemd --user)
    keep5 disable   stop the background job
    keep5 status    setup? enabled? next reset (or overdue)?
    keep5 version   print the version and exit

First run:  keep5 setup  ->  keep5 enable  ->  done.
"""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from getpass import getpass, getuser

__version__ = "1.0.0"

DIR = os.path.expanduser("~/.keep5")
STATE = os.path.join(DIR, "next_reset")
LOG = os.path.join(DIR, "log")
TOKEN = os.path.join(DIR, "oat")  # chmod 600
TOKEN_PREFIX = "sk-ant-oat01-"

API = "https://api.anthropic.com/v1/messages"
H5_RESET = "anthropic-ratelimit-unified-5h-reset"
H7_RESET = "anthropic-ratelimit-unified-7d-reset"
H7_STATUS = "anthropic-ratelimit-unified-7d-status"

# Scheduler is picked by OS — hardcoded, no abstraction. The tick above is pure
# stdlib and identical on both; only enable/disable/_loaded/read_interval branch.
MACOS = sys.platform == "darwin"

# macOS: launchd
LABEL = "com.imsodasu.keep5"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
# Linux: systemd --user
UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
SERVICE = os.path.join(UNIT_DIR, "keep5.service")
TIMER = os.path.join(UNIT_DIR, "keep5.timer")

INTERVAL = 300  # default tick interval, seconds; the knob lives in the installed unit


# ---- the tick (what launchd calls, no argument) ----------------------------

def log(msg):
    os.makedirs(DIR, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")


def read_next_reset():
    try:
        with open(STATE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None  # missing or corrupt -> treat as cold start


def write_next_reset(ts):
    os.makedirs(DIR, exist_ok=True)
    with open(STATE, "w") as f:
        f.write(str(ts))


def pick_reset(headers):
    """Choose when to fire next from the ratelimit headers.

    Normally the 5h window governs. But when the weekly cap is the wall
    (`-7d-status: rate_limited`) no request can land until the weekly resets,
    so back off to the weekly reset instead of hammering the 5h boundary every
    tick. Returns (reset_ts, weekly_limited)."""
    weekly = headers.get(H7_STATUS) == "rate_limited"
    name = H7_RESET if weekly else H5_RESET
    reset = headers.get(name)
    if not reset:
        raise RuntimeError(f"no {name} header in response")
    return int(reset), weekly


def trigger():
    """Fire one minimal request; return (reset_ts, weekly_limited).

    On success the request opens the next 5h window and the header carries its
    reset. If the weekly cap is exhausted the request 429s but still returns the
    ratelimit headers, so we read the weekly reset off the error and back off."""
    try:
        with open(TOKEN) as f:
            token = f.read().strip()
    except FileNotFoundError:
        raise RuntimeError(f"no token: {TOKEN} missing (run `keep5 setup`)")
    if not token:
        raise RuntimeError(f"no token: {TOKEN} empty (run `keep5 setup`)")
    body = json.dumps({
        "model": "claude-haiku-4-5",
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode()
    req = urllib.request.Request(API, data=body, headers={
        "authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20",
        "content-type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return pick_reset(resp.headers)
    except urllib.error.HTTPError as e:
        if e.headers.get(H7_RESET) or e.headers.get(H5_RESET):
            return pick_reset(e.headers)  # weekly wall (or 5h) — back off, don't hammer
        raise RuntimeError(f"HTTP {e.code}: {e.reason}")


def tick():
    next_reset = read_next_reset()
    if next_reset is not None and int(time.time()) < next_reset:
        return  # silent no-op
    try:
        reset, weekly = trigger()
        write_next_reset(reset)
        when = time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(reset))
        log(f"weekly-limited: waiting for weekly reset {when}" if weekly
            else f"ok: next reset {when}")
    except Exception as e:
        log(f"failed: {e}")  # one line, then exit 0; next tick retries


# ---- management CLI (only on an explicit subcommand) -----------------------

def is_setup():
    """True if a token file exists and has the expected shape (not a liveness check)."""
    try:
        with open(TOKEN) as f:
            return f.read().strip().startswith(TOKEN_PREFIX)
    except FileNotFoundError:
        return False


def cmd_setup():
    token = getpass("Paste your Claude token (from `claude setup-token`), then Enter: ").strip()
    if not token.startswith(TOKEN_PREFIX):
        sys.exit(f"that doesn't look like a token (expected {TOKEN_PREFIX}…); nothing written.")
    os.makedirs(DIR, exist_ok=True)
    with open(TOKEN, "w") as f:
        f.write(token)
    os.chmod(TOKEN, 0o600)
    print(f"wrote {TOKEN} (chmod 600)")
    print("next:  keep5 enable")


PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>
    </array>
    <!-- Trigger interval, in seconds. This is the interval knob: edit and reload. -->
    <key>StartInterval</key>
    <integer>{interval}</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardErrorPath</key>
    <string>/tmp/keep5.err</string>
</dict>
</plist>
"""

SERVICE_TEMPLATE = """[Unit]
Description=keep5 — reopen the Claude usage window on time

[Service]
Type=oneshot
ExecStart={python} {script}
StandardError=append:/tmp/keep5.err
"""

TIMER_TEMPLATE = """[Unit]
Description=keep5 tick timer

[Timer]
# Trigger interval, in seconds. This is the interval knob: edit and reload with
# `keep5 disable && keep5 enable` (enable won't overwrite an existing timer).
OnActiveSec={interval}
OnUnitActiveSec={interval}
AccuracySec=30s

[Install]
WantedBy=timers.target
"""


def _sd(*args):
    """systemctl --user <args>, output captured."""
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def _systemd_secs(s):
    """A systemd time span from the timer ('300', '5min', '1h') -> int seconds."""
    s = s.strip()
    if s.isdigit():
        return int(s)  # bare number = seconds
    mult = {"sec": 1, "min": 60, "hr": 3600, "s": 1, "m": 60, "h": 3600}
    for suf in sorted(mult, key=len, reverse=True):  # 'min' before 'm', 'sec' before 's'
        if s.endswith(suf):
            try:
                return int(float(s[:-len(suf)].strip()) * mult[suf])
            except ValueError:
                break
    return INTERVAL


def _linger_on():
    try:
        r = subprocess.run(["loginctl", "show-user", getuser(), "--property=Linger"],
                           capture_output=True, text=True)
        return "Linger=yes" in r.stdout
    except Exception:
        return False


def _ensure_linger():
    """A --user service stops at logout unless lingering is on. Detect it, try to
    turn it on, but never fail `enable` over it — enable-linger usually needs root
    and will just be declined in a plain session. Degrade to a clear hint."""
    user = getuser()
    try:
        if _linger_on():
            return
        subprocess.run(["loginctl", "enable-linger", user, "--no-ask-password"],
                       check=True, capture_output=True)
        print("linger enabled — keeps running after you log out.")
    except Exception:
        print("⚠ linger is off — the service stops when you log out.")
        print(f"  to keep it running: sudo loginctl enable-linger {user}")


def _enable_linux():
    if not shutil.which("systemctl"):  # not every Linux ships systemd (Alpine, minimal containers, WSL w/o systemd)
        sys.exit("keep5 needs systemd on Linux, but `systemctl` isn't on PATH.")
    if not os.path.exists(TIMER):  # keep an edited interval; regenerate only if missing
        os.makedirs(UNIT_DIR, exist_ok=True)
        with open(SERVICE, "w") as f:
            f.write(SERVICE_TEMPLATE.format(python=sys.executable, script=os.path.realpath(__file__)))
        with open(TIMER, "w") as f:
            f.write(TIMER_TEMPLATE.format(interval=INTERVAL))
    _sd("daemon-reload")
    if _sd("enable", "--now", "keep5.timer").returncode != 0:
        sys.exit("systemctl --user enable failed (already enabled?)")
    _sd("start", "keep5.service")  # RunAtLoad parity: fire one tick right now
    print(f"enabled — systemd runs a tick every {INTERVAL // 60} min.")
    print(f"interval knob: OnUnitActiveSec in {TIMER}")
    _ensure_linger()


def cmd_enable():
    if not is_setup():
        sys.exit("no token yet — run `keep5 setup` first.")
    if not MACOS:
        return _enable_linux()
    if not os.path.exists(PLIST):  # keep an edited StartInterval; regenerate only if missing
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        with open(PLIST, "w") as f:
            f.write(PLIST_TEMPLATE.format(
                label=LABEL, python=sys.executable,
                script=os.path.realpath(__file__), interval=INTERVAL,
            ))
    if subprocess.run(["launchctl", "load", PLIST]).returncode != 0:
        sys.exit("launchctl load failed (already enabled?)")
    print(f"enabled — launchd runs a tick every {INTERVAL // 60} min.")
    print(f"interval knob: StartInterval in {PLIST}")


def cmd_disable():
    if MACOS:
        if subprocess.run(["launchctl", "unload", PLIST]).returncode != 0:
            sys.exit("launchctl unload failed (already disabled?)")
    else:
        if _sd("disable", "--now", "keep5.timer").returncode != 0:
            sys.exit("systemctl --user disable failed (already disabled?)")
        for unit in (TIMER, SERVICE):
            try:
                os.remove(unit)
            except FileNotFoundError:
                pass
        _sd("daemon-reload")
    print("disabled — no more background ticks until `keep5 enable`.")


def _loaded():
    try:
        if MACOS:
            return subprocess.run(["launchctl", "list", LABEL],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL).returncode == 0
        return _sd("is-active", "keep5.timer").returncode == 0
    except FileNotFoundError:
        return False  # no scheduler binary on PATH


def read_interval():
    """The tick interval from the installed unit; fall back to the default.
    macOS: StartInterval (seconds) in the plist. Linux: OnUnitActiveSec in the
    timer, which may carry a unit suffix (300 / 5min / 1h) — normalise to secs."""
    try:
        if MACOS:
            with open(PLIST, "rb") as f:
                return int(plistlib.load(f).get("StartInterval", INTERVAL))
        with open(TIMER) as f:
            for line in f:
                if line.strip().startswith("OnUnitActiveSec="):
                    return _systemd_secs(line.split("=", 1)[1])
    except Exception:
        pass
    return INTERVAL


def cmd_status():
    def fmt(ts):  # absolute, for precision
        return time.strftime("%m-%d %H:%M", time.localtime(ts))

    def dur(secs):  # relative, computed live; largest unit first (d>h>m), <=2 units
        secs = int(secs)
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m = rem // 60
        if d:
            return f"{d}d{h}h"
        if h:
            return f"{h}h{m:02d}m"
        return f"{m}m"

    now = time.time()
    setup, enabled = is_setup(), _loaded()
    interval = read_interval()
    setup_val = "yes" if setup else "no  — run 'keep5 setup'"
    enabled_val = f"yes  (tick every {dur(interval)})" if enabled else "no  — run 'keep5 enable'"
    print(f"{'setup:':13}{setup_val}")
    print(f"{'enabled:':13}{enabled_val}")
    if not MACOS:  # the Linux "stays awake" analogue: does it survive logout?
        print(f"{'linger:':13}" + ("yes" if _linger_on()
              else "no  — stops on logout; sudo loginctl enable-linger $USER"))

    # `next reset` only means anything while set up AND enabled; otherwise stay silent
    if not (setup and enabled):
        print(f"{'next reset:':13}—")
        return
    nr = read_next_reset()
    if nr is None:
        print(f"{'next reset:':13}none yet — opens on next tick, else check log")
        return
    over = now - nr
    if over < 0:
        due = f"in {dur(-over)}"
    elif over <= interval:
        due = "due now"
    else:
        due = f"⚠ overdue {dur(over)} — check log"
    print(f"{'next reset:':13}{fmt(nr)}  ({due})")


def cmd_help():
    print(__doc__.strip())


def cmd_version():
    print(f"keep5 {__version__}")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    cmds = {
        None: tick,
        "setup": cmd_setup, "enable": cmd_enable,
        "disable": cmd_disable, "status": cmd_status,
        "version": cmd_version, "--version": cmd_version, "-V": cmd_version,
        "--help": cmd_help, "-h": cmd_help, "help": cmd_help,
    }
    fn = cmds.get(arg)
    if fn is None:
        sys.exit(f"unknown command: {arg}\n\n{__doc__.strip()}")
    fn()


if __name__ == "__main__":
    main()
    sys.exit(0)
