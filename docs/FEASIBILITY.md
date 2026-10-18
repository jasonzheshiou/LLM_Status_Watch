# LLM Server Idle Watcher — Feasibility Report

Investigated live on 2026-10-18. Nothing has been installed or changed on the Pi — all probes were
read-only GET/POST requests plus one throwaway ntfy topic.

## Verdict

**Feasible, and easier than the existing script suggests.** No new hardware, no accounts, no
open ports, no changes on the LLM server. Estimated build: one ~250-line Python file, one config
file, one systemd unit.

The single assumption in the original plan that turned out to be wrong is a good one: `halogen-watch`
reads `http://127.0.0.1:8080/metrics` and shells out to `podman`, so it must run **on** the LLM
server. But the metrics endpoint is bound to `0.0.0.0`, so **the Pi can poll it directly**
(`http://192.168.1.59:8080/metrics` → HTTP 200, 7,216 bytes, confirmed from both this machine and
the Pi). No SSH, no agent, no exporter needed on the LLM box.

## What the LLM server already exposes (measured)

`http://192.168.1.59:8080` — `/metrics` 200, `/health` 200, `/v1/models` 200, `/props` 200.
Model: `Qwen3.8-Flash-Next`, `n_ctx=262144`, `total_slots=1`.

Signals that matter for idle detection, all readable remotely:

| Metric | Value at probe time | Use |
| --- | --- | --- |
| `halogen:busy_for_seconds` | `0` | **Purpose-built idle gauge** — "seconds the oldest request in flight has run (0 when idle)". This is the cleanest idle signal and `halogen-watch` does not use it. |
| `llamacpp:requests_processing` | `0` | in-flight requests |
| `llamacpp:requests_deferred` | `0` | queued |
| `halogen:engine_up` | `1` | liveness — distinct from "HTTP answered" |
| `llamacpp:tokens_predicted_total` | `290735` → `290735` over 12 s | counter delta proves the engine is really idle, not just mid-request |
| `llamacpp:kv_cache_usage_ratio` | `0.31` | context still held after the last request |
| `halogen:requests_total`, `queue_waited_total`, `queue_gave_up_total` | `293`, `54`, `0` | useful body text for the notification |

Verified idle semantics by sampling twice, 12 s apart, with no load: counters frozen, `busy_for_seconds`
still `0`. So `busy_for_seconds == 0 && requests_processing == 0 && counter delta == 0` is a
high-confidence idle test.

## What the Pi can actually do

`jason@192.168.1.198`, Raspbian 12 bookworm, kernel 6.12.109, **armv7l (32-bit)**, 921 MB RAM,
load ~1.0, 1.9 GB free on `/` (72% used).

| Available | Notes |
| --- | --- |
| `curl`, `wget`, `python3` 3.11.2, `bash`, `awk`, `cron` (active) | enough for the whole watcher with **zero installs** |
| SSH key auth **to** the Pi from this machine | already working, no password prompts |
| `sudo -n` → **passwordless sudo works** | so a system service or `enable-linger` is available if wanted |
| Outbound HTTPS to `ntfy.sh` | **publish verified: HTTP 200 + real ntfy JSON echo**, valid Let's Encrypt cert, no proxy/MITM |
| GitHub reachable | could install the `ntfy` CLI armv7 binary if wanted (not needed — `curl` is enough) |

| Missing / constrained | Impact |
| --- | --- |
| `jq` | none — use Python stdlib instead of bash+jq |
| `ntfy` CLI | none — publish is one `curl -d` |
| No SSH key from Pi → 192.168.1.59 (`Permission denied (publickey)`) | blocks the host-local checks only (see below) |
| `systemd --user` exists but `Linger=no` | user service won't survive boot until `enable-linger` |
| Pi is 32-bit armv7l, 921 MB | rules out nothing here, but argues against self-hosting a notification server |

### Two things in `halogen-watch` that cannot move to the Pi as-is

`/proc/pressure/memory` (PSI), `/proc/meminfo`, `swap`, `podman ps/logs`, and `ss` client counts are
**host-local** to the LLM server. From the Pi we can't see them. Practical consequences:

- Idle/busy/down detection: fully covered by remote metrics — nothing lost.
- The `MEMORY PRESSURE PSI>5` and `swap in use` warnings from the script: **need** either an SSH key
  Pi→LLM, or a tiny read-only helper on the LLM host. Optional add-on, not required for the notify-
  me-when-idle goal. Worth noting because the script's own comments call PSI stall *the* failure mode.
- `engine_up` plus N failed polls is a good enough remote stand-in for "the box is in trouble".

## Notification channel options

`ntfy.sh` publish is verified working from the Pi. Retention measured as `expires = time + 43200`
(12 h), ~4 KB per message, free-tier rate limits are far above the ~1–5 messages/day this needs.

Live proof you can check right now without installing anything: open
<https://ntfy.sh/LLM_Status_Watch-demo-4821> — two test messages are sitting there.

| Option | Verdict | Why |
| --- | --- | --- |
| **A. `ntfy.sh` + curl from Pi** | **Recommended** | Zero setup, zero cost, works on cellular (no LAN/VPN needed), priority/tags/click-actions, phone app or the web UI. Only secret is the topic name; body transits third-party over TLS — trivial to keep internal IPs out of the body if that matters. |
| **B. Self-hosted ntfy server on the Pi** | Not worth it | Privacy is real, but the phone must *reach* a box on a private LAN IP → needs Tailscale/VPN or port-forward + TLS + auth. Extra daemon on a 921 MB Pi with 1.9 GB free for 3 messages/day. Only pick this if "no third party may see it" is a hard requirement. |
| **C. Telegram bot** | Best "better method" | Free, unlimited, bot API is plain HTTPS `curl` so still zero deps, better history/reliability and multi-device than a public topic. Costs ~10 min with BotFather to get a token + chat id. Slightly better than ntfy; more setup. |
| D. Pushover | Paid, very reliable | $5 one-time per platform. Only if you want the most dependable path. |
| E. Email (msmtp) | No | Slow, spam-folder, no lock-screen actions. |

Recommended shape regardless of choice: make the transport pluggable (one `notify()` with an `ntfy`
and a `telegram` backend in config), so switching later is a config edit, not a rewrite.

## Proposed watcher design (adapting, not reusing blindly)

Daemon on the Pi, Python 3 stdlib only, polling `http://192.168.1.59:8080/metrics` every 30 s.

Carried over from `halogen-watch`: metric whitelist + retry-on-empty-body (the script notes `/metrics`
has been seen to return an empty body, so a 3× retry before concluding anything is deliberate),
counter-delta throughput instead of trusting instantaneous gauges, and the warning-heuristic idea.

New, needed for unattended use:

- **Edge-triggered, debounced idle**: notify once, on the busy→idle transition, only after the idle
  test holds for N consecutive minutes. Never spam every 30 s.
- **State file** (`~/.local/state/LLM_Status_Watch/state.json`) so a restart doesn't re-alert or forget that
  it already announced the current state.
- **Down alert** after M failed polls or `engine_up=0`, with a recovery notice — arguably more valuable
  than the idle ping, and `halogen-watch` has no equivalent for remote use.
- **Keepalive** heartbeat (e.g. daily summary, or "watcher has been silent" signal) so a dead watcher is
  distinguishable from an idle server.
- **`--dry-run` mode** that logs the decision instead of publishing — for the trial day.
- Message body: idle duration, KV %, requests served today, last tok/s, queue stats.

Cost: 7.2 KB per poll → ~20 MB/day on LAN, negligible CPU on this Pi.

## Deployment options (once you approve)

1. **systemd system service** `/etc/systemd/system/LLM_Status_Watch.service` running as `jason` — works at
   boot, `Restart=on-failure`, logs in journald. Passwordless sudo makes this painless.
2. **systemd user service + `loginctl enable-linger jason`** — same effect, keeps files in `$HOME`.
3. **cron + `flock`** — no sudo at all, but weaker supervision.

Recommendation: try with `--dry-run` under a foreground SSH session for a day to tune the idle
threshold, then install as option 1 or 2.

## Open unknowns (need you)

1. **Delivery to your phone is the one unverified link.** ntfy's *publish* is proven; the subscribe side
   needs you to install the ntfy app (or add the web app to your phone) and subscribe to a private,
   unguessable topic. Until then I can only prove the message leaves the Pi.
2. What "idle" means to you — free so you can use the GPU, or idle so you can shut it down? Different
   message, possibly different threshold.
3. Threshold in minutes, and whether "busy again" and "server down" should also ping you.
4. Whether the host-level PSI/swap warnings matter enough to justify adding a Pi→LLM SSH key.
