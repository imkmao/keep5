# Build your own keep5

keep5 is small on purpose — one Python file, standard library only, no
dependencies. If you have a coding agent (Claude Code or anything similar),
you can have it write your own version in an afternoon. Genuinely.

This page exists so you don't have to *rediscover* what we already paid for.
Everything below is a sharp edge we hit by trial and error. Hand this file to
your agent before it starts and it will burn far fewer tokens getting to a
working tool — and skip the walls we walked into.

None of this is secret. It's just annoying to find out one 429 at a time.

---

## The one idea the whole thing rests on

Claude Code subscriptions use **rolling 5-hour usage windows**. The catch that
makes a tool worth writing at all:

> After a window's reset time passes, the next window **does not start on its
> own**. It starts only when you send your next request.

So a window that resets at 13:00 while you're away doesn't begin until you come
back at 16:00 — those three hours are gone. The entire job of keep5 is to send
**one** request the moment a window becomes eligible, so the clock starts
immediately instead of whenever you happen to return.

If you internalize only one thing: **you are not buying quota, you are starting
the timer early.** Optimize for *time served*, not usage.

---

## The token (this trips people up first)

You do **not** use a normal Anthropic API key (`sk-ant-api…`). Those bill
per-token against a separate balance — using one would defeat the entire point
(you'd pay money to open a window your subscription already covers).

You want the **subscription OAuth token** the CLI itself uses:

```sh
claude setup-token
```

It returns a token prefixed `sk-ant-oat01-`, valid roughly a year. Store it
somewhere `chmod 600`. It is a credential — treat it like one.

Because it's an OAuth token rather than an API key, the request needs an extra
beta header (see below). Miss that header and you'll get confusing auth errors
even though the token is valid.

---

## The request

One minimal POST. The smallest thing that counts as "activity":

```
POST https://api.anthropic.com/v1/messages

headers:
  authorization:     Bearer <your sk-ant-oat01- token>
  anthropic-version: 2023-06-01
  anthropic-beta:    oauth-2025-04-20      # required for the OAuth token
  content-type:      application/json

body:
  { "model": "claude-haiku-4-5", "max_tokens": 1,
    "messages": [{ "role": "user", "content": "ping" }] }
```

Use the cheapest fast model and `max_tokens: 1` — you don't care about the
answer, only that the request lands and opens the window.

## The headers are the whole reward

The response (success **or** rate-limited) carries the rate-limit state you
need. These three are what you read:

| Header | Meaning |
|---|---|
| `anthropic-ratelimit-unified-5h-reset` | Unix seconds — when the current 5h window ends. **This is your next fire time.** |
| `anthropic-ratelimit-unified-7d-status` | `rate_limited` when the **weekly** cap is the wall |
| `anthropic-ratelimit-unified-7d-reset` | Unix seconds — when the weekly window resets |

That single request both *starts* the next window and *tells you when it ends*.
Save the 5h reset; that's the only state you need to keep.

---

## The pitfalls that cost us the most

**1. A 429 still gives you the headers.** When the weekly cap is exhausted your
request comes back `429`, not `200`. The naive version treats that as a failure
and retries every few minutes forever, getting nowhere. Don't. The `429`
response *still carries the rate-limit headers* — read them off the error, see
`…-7d-status: rate_limited`, and **back off to the weekly reset** instead of
hammering the 5h boundary. Firing right at the weekly reset also opens the fresh
weekly window at once. (In Python: `urllib.error.HTTPError` still has
`.headers`.)

**2. Don't poll the API — poll a clock.** The scheduler runs the tick every few
minutes, but the tick should hit the network **only** when it's actually time.
Keep one tiny state file with the next reset (Unix seconds). Each tick:
`now < next_reset` → do nothing, silently; `now >= next_reset` → fire once,
save the new reset. No state yet (first run) → fire once to bootstrap. This is
what keeps it invisible and un-abusive: roughly one real request per 5h window.

**3. The scheduler is the actual hard part, not the HTTP.** The one-shot script
is easy; keeping it running forever is the work.
- **macOS** — a launchd LaunchAgent with `StartInterval` (seconds) and
  `RunAtLoad`. launchd does not tick while the machine is asleep.
- **Linux** — a systemd **`--user`** service (`Type=oneshot`) plus a timer
  (`OnUnitActiveSec`). A `--user` service **stops when you log out** unless
  *lingering* is on: `loginctl enable-linger $USER` (usually needs root). Forget
  this and it silently dies the moment you close your SSH session. On first
  enable, also `systemctl --user start` once for parity with launchd's
  `RunAtLoad`.

**4. A background listener can't fire while the box is asleep.** A window
boundary that falls during sleep is missed until the machine wakes. There's no
clever fix — run it somewhere that stays up (an always-on desktop, a cheap VPS).
Treat this as a design premise, not a bug to engineer around.

**5. Pick a sane tick interval.** Every few minutes is plenty. Worst case you
lose one interval per window — 5 minutes is ~1.7% of a 5-hour window. Tighter
buys you almost nothing and just adds noise.

**6. Fail quiet, retry next tick.** If a request doesn't come back (bad token,
network blip), log one line and exit 0. The next tick will try again. Don't
crash, don't spiral, don't alert.

---

## That's the whole map

There isn't more. If your agent has the ideas above it will land in an
afternoon with very little wasted compute — which is the point of this page.

And if, having read all that, you'd rather not maintain your own copy of a
throwaway patch: [keep5 is right here](https://github.com/imkmao/keep5#install),
we already burned the tokens, and it does exactly this — clone it and you're
running in two commands. Either way — glad the notes help.
