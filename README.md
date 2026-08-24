<div align="center">
  <img src="assets/hero.png" width="300" alt="keep5" />
  <h1>keep5</h1>
  <p><b>Reclaim the time you waste waiting.</b></p>
  <a href="https://github.com/imkmao/keep5/stargazers"><img src="https://img.shields.io/github/stars/imkmao/keep5?style=flat-square&color=f5b544" alt="Stars"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2b2b2b?style=flat-square" alt="License"></a>
  <img src="https://img.shields.io/badge/macOS%20%C2%B7%20Linux-2b2b2b?style=flat-square" alt="macOS · Linux">
  <img src="https://img.shields.io/badge/deps-none%20(stdlib)-2b2b2b?style=flat-square" alt="Zero dependencies">
  <a href="https://keep5.pages.dev"><img src="https://img.shields.io/badge/site-keep5.pages.dev-f5b544?style=flat-square" alt="Site"></a>
</div>

Keep your Claude Code subscription's next 5-hour usage window starting **as early as possible**, so you waste less time waiting.

It does **not** give you more quota. It optimizes **time, not usage** — the hours you were away become time already served on your next lockout.

## The problem

Claude Code subscriptions use rolling 5-hour usage windows. The catch:

> **After a window's reset time passes, the next window does not start on its own.** It starts only when you send your next request.

So if your window resets at 13:00 but you don't come back until 16:00, your new 5-hour window starts at 16:00 — three hours thrown away, and it compounds across the week.

## What it does

A tiny one-shot script, run every few minutes by a background scheduler (launchd on macOS, a systemd `--user` timer on Linux). Each run:

- `now < next_reset` → do nothing (silent).
- `now ≥ next_reset` → send one minimal request. This opens the next window immediately, and the response header `anthropic-ratelimit-unified-5h-reset` tells us the new window's reset time, which we save.
- If your **weekly** cap is spent, that request is refused (`…-7d-status: rate_limited`) — nothing can open until the weekly resets. So we read the weekly reset from the same headers and wait for *that* instead of retrying every few minutes. Firing right at the weekly reset also opens the fresh weekly window at once.
- No state file yet (first run) → just fire once and record.

That single request both *starts* the next window and *reports* when it ends. As long as the job runs, your window is always kept "alive" the moment it's eligible.

Zero dependencies (Python 3 stdlib). macOS or Linux. Single Claude Code account. Hardcoded on purpose — if the platform changes how windows work, this tool dies, and that's fine.

## Install

Two layers: **get it onto your machine** (git's job), then **start using it** (`keep5`'s job). Same on macOS and Linux — `keep5 enable` picks the right scheduler for you.

```sh
# 1. get it — nothing of ours is "installed" here; git just clones the source
git clone https://github.com/imkmao/keep5.git
cd keep5
chmod +x keep5.py
ln -sf "$PWD/keep5.py" /usr/local/bin/keep5   # puts `keep5` on your PATH (sudo if needed)

# 2. start using it
keep5 setup      # paste your token (from `claude setup-token`); stored chmod 600
keep5 enable     # install + start the background job
```

That's it. **After the one-time install above, first run is just `keep5 setup` → `keep5 enable`.** Then it runs itself.

### Upgrading from 1.0.0

The launchd label changed in 1.1.0 (`com.imsodasu.keep5` → `com.imkmao.keep5`, following a
GitHub rename). If you installed 1.0.0 on macOS, clear the old job once, or you'll end up
running two:

```sh
launchctl unload ~/Library/LaunchAgents/com.imsodasu.keep5.plist
rm ~/Library/LaunchAgents/com.imsodasu.keep5.plist
keep5 enable
```

Linux is unaffected — the systemd units were never named after the account.

## Keep it running

A background listener is inherently at odds with a machine that goes to sleep: a window boundary that falls while the scheduler isn't ticking is missed until it wakes. This is by design, not a bug keep5 tries to fix — so give it a machine that stays up.

- **macOS** — launchd doesn't tick while asleep. Set the Mac never to sleep (on power), as you would for any always-on tool.
- **Linux** — needs **systemd** (the default on Ubuntu, Debian, Fedora, Arch, and most desktop/server distros). A `--user` service stops when you log out unless *lingering* is on. `keep5 enable` turns it on when it can; if it can't (it usually needs root), it tells you to run `sudo loginctl enable-linger $USER`. And, as on macOS, don't let the box suspend.

`keep5 status` shows the linger state on Linux, so you can see at a glance whether it survives logout.

## Commands

`keep5` is a single script. With **no argument** it's the tick the scheduler calls; **with an argument** it's the management CLI.

| Command | What it does |
|---|---|
| `keep5 setup` | Paste your Claude OAuth token (from `claude setup-token`, valid ~1 year). Written to `~/.keep5/oat`, `chmod 600`. Re-run whenever the token expires. |
| `keep5 enable` | Install the background job (a launchd plist on macOS, a systemd `--user` service + timer on Linux) and start it. Tells you to run `keep5 setup` first if you haven't. |
| `keep5 disable` | Stop and remove the background job — no more ticks. |
| `keep5 status` | Is a token set? Is it enabled (and at what interval)? On Linux, is lingering on? When does the current window reset — or is a fire overdue? One screen; fire history lives in the log. |
| `keep5 version` | Print the version (`keep5 <x.y.z>`) and exit. |
| `keep5` | One tick — what the scheduler runs every few minutes. Silent unless it's time to fire. |

```console
$ keep5 status
setup:       yes
enabled:     yes  (tick every 5m)
next reset:  07-25 18:00  (in 2h13m)
```

## Config

Exactly two knobs:

- **Trigger interval** — where it lives depends on the OS:
  - macOS: `StartInterval` (seconds) in `~/Library/LaunchAgents/com.imkmao.keep5.plist`.
  - Linux: `OnUnitActiveSec` in `~/.config/systemd/user/keep5.timer`.

  Default `300` (5 min). To change it: edit that value, then `keep5 disable && keep5 enable` to reload (enable won't overwrite an existing unit, so your edit sticks). Worst case you lose one interval per window — 5 min ≈ 1.7% of a 5-hour window.
- **Enable / disable** — `keep5 enable` / `keep5 disable`.

No config file.

## State & logs

Everything lives under `~/.keep5/`:

- `oat` — your token (`chmod 600`).
- `next_reset` — the one state file: the current window's reset time (Unix seconds). Read every tick to decide whether to fire.
- `log` — one line **per fire**: `ok: next reset <iso>` when the request comes back with a reset time, `weekly-limited: waiting for weekly reset <iso>` when the weekly cap is the wall (we back off to that reset rather than retrying), `failed: <error>` when the request just doesn't come back (bad token, network, …). Silent do-nothing ticks write nothing, so a healthy log is roughly one `ok` line per 5-hour window; a gap much longer than that means the tick wasn't running during it (e.g. the machine slept). Unexpected crashes go to `/tmp/keep5.err`.

`keep5 status` reads these for you; the log is there when you want the full fire history.

## Build your own

There's no magic here — one stdlib Python file, and a coding agent could write you your own in an afternoon. If you'd rather do that, [**BUILD-YOUR-OWN.md**](BUILD-YOUR-OWN.md) is the map: every sharp edge we hit — which header carries the reset, why the window won't open until you poke it, how to back off when the weekly cap is the wall, the launchd/systemd wiring — written down so your agent skips the dead ends and burns fewer tokens.

Or just use this one — same result, nothing to build, nothing to maintain.

## Disclaimer

This tool interacts with the service through **automated requests**. Doing so may touch the provider's Terms of Service. You use it **at your own risk**, including any risk to your account. It is provided free, as-is, with no warranty. If the provider ever changes windows to reset automatically, the problem disappears and this tool is no longer needed — that's its ideal death.

Free, forever. No Pro version, no SaaS.

## License

[MIT](LICENSE) © 2026 imkmao.
