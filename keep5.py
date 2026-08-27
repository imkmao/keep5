#!/usr/bin/env python3
"""keep5 — keep the next Claude Code and Codex 5h windows starting ASAP.

Single entry point. With no argument this is the one-shot, stateless tick
that the scheduler (launchd on macOS, a systemd --user timer on Linux) runs
every N minutes:

Each enrolled runtime keeps its own reset state. At every tick it either stays
silent before that reset or fires one minimal request and records the next one.
One runtime failing never stops the other.

Claude Code uses its OAuth token and response headers. Codex uses the official
Codex App Server with an existing ChatGPT login. See BUILD-YOUR-OWN.md.

With an argument it is the management CLI:

    keep5 setup     set up Claude and enroll a ChatGPT-authenticated Codex
    keep5 enable    install the background job (launchd / systemd --user)
    keep5 disable   stop the background job
    keep5 status    setup and next reset for each runtime
    keep5 version   print the version and exit

First run:  keep5 setup  ->  keep5 enable  ->  done.
"""
import json
import os
import plistlib
import selectors
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from getpass import getpass, getuser

__version__ = "1.2.0"

DIR = os.path.expanduser("~/.keep5")
STATE = os.path.join(DIR, "next_reset")
CODEX_STATE = os.path.join(DIR, "codex_next_reset")
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
LABEL = "com.imkmao.keep5"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
# Linux: systemd --user
UNIT_DIR = os.path.expanduser("~/.config/systemd/user")
SERVICE = os.path.join(UNIT_DIR, "keep5.service")
TIMER = os.path.join(UNIT_DIR, "keep5.timer")

INTERVAL = 300  # default tick interval, seconds; the knob lives in the installed unit
CODEX_IO_TIMEOUT = 5
CODEX_TURN_TIMEOUT = 60
CODEX_CONFIRM_DELAY = 2
CODEX_MODEL = "gpt-5.6-luna"
CODEX_PENDING_RESET = "waiting to confirm reset; state unchanged"


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


def read_codex_next_reset():
    try:
        with open(CODEX_STATE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def write_codex_next_reset(ts):
    os.makedirs(DIR, exist_ok=True)
    with open(CODEX_STATE, "w") as f:
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


def claude_tick():
    if not claude_is_setup():
        return
    next_reset = read_next_reset()
    if next_reset is not None and int(time.time()) < next_reset:
        return  # silent no-op
    try:
        reset, weekly = trigger()
        write_next_reset(reset)
        when = time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(reset))
        log(f"claude weekly-limited: waiting for weekly reset {when}" if weekly
            else f"claude ok: next reset {when}")
    except Exception as e:
        log(f"claude failed: {e}")  # one line, then exit 0; next tick retries


def tick():
    claude_tick()
    codex_tick()


# ---- management CLI (only on an explicit subcommand) -----------------------

def claude_is_setup():
    """True if a token file exists and has the expected shape (not a liveness check)."""
    try:
        with open(TOKEN) as f:
            return f.read().strip().startswith(TOKEN_PREFIX)
    except FileNotFoundError:
        return False


def codex_is_setup():
    return os.path.exists(CODEX_STATE)


def is_setup():
    return claude_is_setup() or codex_is_setup()


def codex_executable():
    found = shutil.which("codex")
    if found:
        return found
    for path in ("~/.npm-global/bin/codex", "~/.local/bin/codex",
                 "/usr/local/bin/codex", "/opt/homebrew/bin/codex"):
        path = os.path.expanduser(path)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _codex_send(proc, message):
    proc.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
    proc.stdin.flush()


def _codex_receive(proc, predicate, notifications, timeout=CODEX_IO_TIMEOUT):
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            while b"\n" in proc.keep5_buffer:
                line, proc.keep5_buffer = proc.keep5_buffer.split(b"\n", 1)
                message = json.loads(line)
                if "method" in message:
                    notifications.append(message)
                if predicate(message):
                    return message
            ready = selector.select(deadline - time.monotonic())
            if not ready:
                break
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError(f"codex app-server exited {proc.poll()}")
            proc.keep5_buffer += chunk
    finally:
        selector.close()
    raise RuntimeError("codex app-server timed out")


def _codex_request(proc, request_id, method, notifications, params=None, timeout=CODEX_IO_TIMEOUT):
    message = {"method": method, "id": request_id}
    if params is not None:
        message["params"] = params
    _codex_send(proc, message)
    response = _codex_receive(proc, lambda item: item.get("id") == request_id,
                              notifications, timeout)
    if "error" in response:
        raise RuntimeError(f"{method}: {response['error']}")
    return response["result"]


def _codex_stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _codex_start():
    executable = codex_executable()
    if not executable:
        raise RuntimeError("codex executable not found (run `keep5 setup`)")
    proc = subprocess.Popen(
        [executable, "app-server", "--stdio"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
    )
    proc.keep5_buffer = b""
    return proc


def codex_account_type():
    proc = _codex_start()
    notifications = []
    try:
        _codex_request(proc, 1, "initialize", notifications, {
            "clientInfo": {"name": "keep5", "title": "keep5", "version": __version__},
        })
        _codex_send(proc, {"method": "initialized", "params": {}})
        account = _codex_request(proc, 2, "account/read", notifications,
                                 {"refreshToken": False})
        return (account.get("account") or {}).get("type")
    finally:
        _codex_stop(proc)


def codex_pick_reset(limits):
    secondary = limits.get("secondary")
    try:
        secondary_full = secondary and float(secondary.get("usedPercent", 0)) >= 100
    except (TypeError, ValueError):
        secondary_full = False
    weekly = bool(secondary_full)
    window = secondary if weekly else limits.get("primary")
    if not window or window.get("resetsAt") is None:
        raise RuntimeError("no Codex reset in rate-limit response")
    return int(window["resetsAt"]), weekly


def codex_trigger():
    proc = _codex_start()
    notifications = []
    try:
        _codex_request(proc, 1, "initialize", notifications, {
            "clientInfo": {"name": "keep5", "title": "keep5", "version": __version__},
        })
        _codex_send(proc, {"method": "initialized", "params": {}})
        account = _codex_request(proc, 2, "account/read", notifications,
                                 {"refreshToken": False})
        auth_type = (account.get("account") or {}).get("type")
        if auth_type != "chatgpt":
            raise RuntimeError("Codex needs ChatGPT login; API-key auth is not supported")

        before = _codex_request(proc, 3, "account/rateLimits/read", notifications)["rateLimits"]
        reset, weekly = codex_pick_reset(before)
        if weekly:
            time.sleep(CODEX_CONFIRM_DELAY)
            confirmed = _codex_request(
                proc, 4, "account/rateLimits/read", notifications)["rateLimits"]
            observed = codex_pick_reset(confirmed)
            if observed != (reset, True):
                raise RuntimeError(CODEX_PENDING_RESET)
            return observed

        thread = _codex_request(proc, 4, "thread/start", notifications, {
            "model": CODEX_MODEL,
            "cwd": DIR,
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "ephemeral": True,
            "serviceName": "keep5",
        })["thread"]
        notifications.clear()
        started = _codex_request(proc, 5, "turn/start", notifications, {
            "threadId": thread["id"],
            "input": [{"type": "text", "text": "Reply with exactly: ok. Do not use tools."}],
            "sandboxPolicy": {"type": "readOnly", "access": {"type": "fullAccess"}},
            "effort": "low",
            "summary": "none",
        })
        turn_id = started["turn"]["id"]
        completed = next((item for item in notifications
                          if item.get("method") == "turn/completed"
                          and item.get("params", {}).get("turn", {}).get("id") == turn_id), None)
        if completed is None:
            completed = _codex_receive(
                proc,
                lambda item: item.get("method") == "turn/completed"
                and item.get("params", {}).get("turn", {}).get("id") == turn_id,
                notifications,
                CODEX_TURN_TIMEOUT,
            )
        turn = completed["params"]["turn"]
        post = _codex_request(proc, 6, "account/rateLimits/read", notifications)["rateLimits"]
        post_reset = codex_pick_reset(post)
        if turn.get("status") != "completed":
            if post_reset[1]:
                time.sleep(CODEX_CONFIRM_DELAY)
                confirmed = _codex_request(
                    proc, 7, "account/rateLimits/read", notifications)["rateLimits"]
                observed = codex_pick_reset(confirmed)
                if observed != post_reset:
                    raise RuntimeError(CODEX_PENDING_RESET)
                return post_reset
            error = (turn.get("error") or {}).get("message") or turn.get("status")
            raise RuntimeError(f"Codex turn did not complete: {error}")

        updates = [item["params"]["rateLimits"] for item in notifications
                   if item.get("method") == "account/rateLimits/updated"]
        if updates:
            observed = codex_pick_reset(updates[-1])
        else:
            time.sleep(CODEX_CONFIRM_DELAY)
            confirmed = _codex_request(
                proc, 7, "account/rateLimits/read", notifications)["rateLimits"]
            observed = codex_pick_reset(confirmed)
        if observed != post_reset:
            raise RuntimeError(CODEX_PENDING_RESET)
        return post_reset
    finally:
        _codex_stop(proc)


def codex_tick():
    if not codex_is_setup():
        return
    next_reset = read_codex_next_reset()
    if next_reset is not None and int(time.time()) < next_reset:
        return
    try:
        reset, weekly = codex_trigger()
        write_codex_next_reset(reset)
        when = time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(reset))
        log(f"codex weekly-limited: waiting for weekly reset {when}" if weekly
            else f"codex ok: next reset {when}")
    except Exception as e:
        msg = str(e)
        log(f"codex pending: {msg}" if msg == CODEX_PENDING_RESET
            else f"codex failed: {e}")


def cmd_setup():
    claude_setup = claude_is_setup()
    if claude_setup:
        prompt = "Paste a replacement Claude token, or Enter to keep the current one: "
    else:
        prompt = "Paste your Claude token (from `claude setup-token`), or Enter to skip: "
    token = getpass(prompt).strip()
    if token:
        if not token.startswith(TOKEN_PREFIX):
            sys.exit(f"that doesn't look like a token (expected {TOKEN_PREFIX}…); nothing written.")
        os.makedirs(DIR, exist_ok=True)
        with open(TOKEN, "w") as f:
            f.write(token)
        os.chmod(TOKEN, 0o600)
        print("claude: setup")
    elif claude_setup:
        print("claude: setup")
    else:
        print("claude: skipped")

    if not codex_executable():
        print("codex: not found — install Codex, then run `keep5 setup` again.")
    else:
        try:
            auth_type = codex_account_type()
            if auth_type == "chatgpt":
                os.makedirs(DIR, exist_ok=True)
                with open(CODEX_STATE, "w") as f:
                    f.write("0")
                print("codex: setup (ChatGPT)")
            elif auth_type == "apiKey":
                print("codex: API-key login rejected — run `codex logout`, then `codex login`.")
            else:
                print("codex: not logged in — run `codex login` (headless: `codex login --device-auth`).")
        except Exception as e:
            print(f"codex: setup failed: {e}")

    if is_setup():
        print("next:  keep5 enable")
    else:
        print("nothing set up yet — finish one runtime, then run `keep5 setup` again.")


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
Description=keep5 — reopen Claude Code and Codex usage windows on time

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
        sys.exit("no runtime yet — run `keep5 setup` first.")
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
        return time.strftime("%m-%d %H:%M %Z", time.localtime(ts))

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
    claude_setup, codex_setup, enabled = claude_is_setup(), codex_is_setup(), _loaded()
    interval = read_interval()
    enabled_val = f"yes  (tick every {dur(interval)})" if enabled else "no  — run 'keep5 enable'"
    print(f"{'claude setup:':14}" + ("yes" if claude_setup else "no  — run 'keep5 setup'"))
    print(f"{'codex setup:':14}" + ("yes" if codex_setup else "no  — run 'keep5 setup'"))
    print(f"{'enabled:':14}{enabled_val}")
    if not MACOS:  # the Linux "stays awake" analogue: does it survive logout?
        print(f"{'linger:':14}" + ("yes" if _linger_on()
              else "no  — stops on logout; sudo loginctl enable-linger $USER"))

    def show_reset(label, setup, read_reset):
        if not (setup and enabled):
            print(f"{label:14}—")
            return
        nr = read_reset()
        if nr is None:
            print(f"{label:14}none yet — opens on next tick, else check log")
            return
        over = now - nr
        if over < 0:
            due = f"in {dur(-over)}"
        elif over <= interval:
            due = "due now"
        else:
            due = f"⚠ overdue {dur(over)} — check log"
        print(f"{label:14}{due}  ({fmt(nr)})")

    show_reset("claude reset:", claude_setup, read_next_reset)
    show_reset("codex reset:", codex_setup, read_codex_next_reset)


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
