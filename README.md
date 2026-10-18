# LLM_Status_Watch

Pushes a notification to your phone when the LLM server at `192.168.1.59:8080` goes idle,
and when it stops answering. Runs on the Raspberry Pi (`192.168.1.198`), reads only
Prometheus metrics over HTTP, needs nothing installed and nothing changed on the LLM server.

```
Pi: LLM_Status_Watch.py --30s--> http://192.168.1.59:8080/metrics
    |
    +-- busy -> idle (held 5 min)  ---> ntfy.sh/<your topic> ---> phone
    +-- 4 failed polls / engine_up=0 ---> ntfy.sh/<your topic> ---> phone
    +-- back up ----------------------------------------------> phone
```

## Files

| File | Role |
| --- | --- |
| `LLM_Status_Watch.py` | the daemon, Python 3 stdlib only (no `jq` on the Pi, no pip) |
| `LLM_Status_Watch.conf` | your config, made from the `.example` — **0600**, gitignored, the ntfy topic is the only secret |
| `LLM_Status_Watch.conf.example` | the template, safe to commit (topic blank) |
| `LLM_Status_Watch.service` | systemd unit for `/etc/systemd/system/` |
| `install.sh` | trial + install + status + uninstall, run it on the Pi |
| `selftest.py` | 45 offline checks of the state machine and token ledger, driven by a fake clock |
| `halogen-watch` | the interactive dashboard this grew out of; it still runs on the LLM host, untouched |

## Setup

```bash
git clone https://github.com/jasonzheshiou/LLM_Status_Watch.git
cd LLM_Status_Watch
cp LLM_Status_Watch.conf.example LLM_Status_Watch.conf
chmod 600 LLM_Status_Watch.conf
$EDITOR LLM_Status_Watch.conf      # metrics_url, topic, server_label
python3 LLM_Status_Watch.py --explain        # one poll, prints the verdict, changes nothing
./install.sh trial                           # log-only, nothing pushed to your phone
./install.sh install                       # systemd unit, starts at boot (uses sudo internally)
```

`LLM_Status_Watch.conf` is gitignored on purpose: on the public ntfy.sh server the topic name
is the only access control, so publishing it would let anyone send you notifications. Make your
own with 22 random characters:

```bash
python3 -c "import secrets,string;print('llm-'+ ''.join(secrets.choice(string.ascii_lowercase+string.digits) for _ in range(22)))"
```

## Usage (on the Pi)

```bash
./install.sh test              # one poll: prints the idle verdict and every metric, changes nothing
./install.sh test-notify       # one real notification, so you can confirm your phone gets it
./install.sh dry-run 10        # 10 min foreground trial, logs decisions, pushes nothing
./install.sh trial             # background trial, real config, survives logout; log is the evidence
./install.sh trial-stop        # stop it
./install.sh install           # copy to ~/LLM_Status_Watch, enable the systemd service
./install.sh logs              # journalctl -u LLM_Status_Watch
./install.sh uninstall
```

`trial` is the step to leave running for a day: identical config, identical decisions, but every
"this is what I would have sent you" line goes to `~/.local/state/LLM_Status_Watch/dry-run.log`
instead of your phone. That log is how you find out whether 10 minutes is the right threshold
before it ever pings you. (Live setting is `idle_after_minutes = 0`, i.e. immediate.)

Direct invocation, same thing:

```bash
python3 LLM_Status_Watch.py --explain          # what would the watcher conclude right now
python3 LLM_Status_Watch.py --dry-run          # loop, log instead of publishing
python3 LLM_Status_Watch.py                    # loop, publish
python3 LLM_Status_Watch.py --once             # single poll + act (for a cron recipe)
python3 selftest.py                       # exercise the state machine offline
```

## How idle is decided

All four must hold, because each one alone can read zero without the server being free:

1. `llamacpp:requests_processing == 0` and `llamacpp:requests_deferred == 0`
2. `halogen:busy_for_seconds == 0` — the engine's own "seconds the oldest in-flight request
   has run (0 when idle)" gauge
3. `llamacpp:tokens_predicted_total` and `prompt_tokens_total` unchanged since the last poll
4. a 200 with an empty body is retried 3x first; this endpoint has been seen to do that, and a
   blank response would otherwise look exactly like "idle"

Then it fires on the **first poll that confirms it** (`idle_after_minutes = 0`, so ~30 s after a
request finishes) and **once** per busy→idle transition - the same idle period never alerts twice,
however many polls pass while it holds.

Because this box gets a request every few minutes, immediate mode means one alert per gap, which is
20-30 a day if the server stays busy. `journalctl -u LLM_Status_Watch | grep -c 'notified:'` over an hour
tells you the real rate; if it is too much, raise `idle_after_minutes` rather than turning the
notifier off - a 5-minute value skips the gaps between requests and still catches genuine quiet.
Throughput in the message comes from counter deltas, not the `predicted_tokens_seconds`
gauge, which is "since the last scrape" and goes stale between polls.

Down = `fail_threshold` consecutive unusable polls, or `halogen:engine_up == 0` (HTTP answering
while the engine behind it is dead — the failure mode a naive "got a 200" check would miss).

## What a notification says

Rendered by the real code path, priority 3, `:white_check_mark:`:

```
Title   LLM idle 15m | llm-59
Body    idle for 15m (since 20:50)
        requests in flight 0 | deferred 0 | engine_up 1
        KV pool 35.9% of positions still held - engine still loaded
        served 316 requests | last seen 68.2 tok/s
```

Deliberately neutral between "free, go use it" and "idle, shut it down": the numbers are there,
the decision is yours. `KV pool ... still held` matters because the engine staying loaded is the
thing you might want to reclaim, and the raw IP stays out of the push (see Privacy).

The immediate variant instead reads `just went idle (confirmed 20:51)` on the first line,
because `idle for 0s` is a silly thing to read on a phone.

Down and recovery notices are the same shape: reason (`connection error`, `empty or metric-less
body`, `engine_up=0`), how many polls failed, when it last answered, and the previous state.

## Tuning (`LLM_Status_Watch.conf`)

| Key | Default | Notes |
| --- | --- | --- |
| `idle_after_minutes` | 0 | **set to 0 = immediate here**: alert on the first poll that confirms idle. Bump to 5 or 10 if the alerts turn out too chatty |
| `poll_seconds` | 30 | 7.2 KB per poll ≈ 20 MB/day on the LAN |
| `fail_threshold` | 4 | at 30 s polling that is a ~2 min outage before an alert |
| `startup_grace_minutes` | 10 | after a reboot, stay quiet about DOWN until the server answers once, or this long has passed |
| `notify_on_startup_idle` | false | `true` also alerts for a server that was already idle when the watcher started |
| `server_label` | llm-59 | appears in the notification title; the raw IP stays off it |
| `idle_priority` / `down_priority` | default / high | ntfy priority: min, low, default, high, max |
| `click_url` | – | e.g. `http://192.168.1.59:8080/health`, tapped when you press the notification |
| `tokens.record` | true | set `false` to stop writing the ledger; alerts are unaffected |
| `tokens.db_file` | `~/.local/state/.../tokens.sqlite3` | one row per local day, never pruned |
| `tokens.flush_minutes` | 15 | how often today's single row is refreshed |

`notify_idle`, `notify_down`, `notify_recovery` each gate their own notice.

## Phone side

ntfy is the transport: the Pi publishes, the app subscribes.

1. Install the ntfy app (Android: Play/F-Droid, iOS: App Store) or use <https://ntfy.sh> in
   the browser and add it to the home screen.
2. Subscribe to the topic in `LLM_Status_Watch.conf` (`<the topic from your config>`).
3. `./install.sh test-notify` must show up on the phone. Until that works, only the sending
   half of this is proven.

Generate your own topic if this one has been shared around: `python3 -c
"import secrets,string;print('llm-'+ ''.join(secrets.choice(string.ascii_lowercase+string.digits) for _ in range(22)))"`

Privacy: a public ntfy topic name is the only access control, so it is a secret, and keep the
body free of anything sensitive (`server_label` exists for that). Self-hosting the ntfy server
on the Pi is the alternative if third-party relay is unacceptable — but then your phone needs a
route to a LAN box (Tailscale/VPN or port-forward + TLS), which is the whole reason that was not
chosen.

## Token ledger

The same poll that decides idle also folds the server's counters into one SQLite row per local
day, kept forever (`~/.local/state/LLM_Status_Watch/tokens.sqlite3`, ~100 bytes/day, no retention job).

Three disjoint figures, because the server documents them that way:

| column | counter | means |
| --- | --- | --- |
| `in processed` | `llamacpp:prompt_tokens_total` | prompt tokens actually prefilled |
| `in cached` | `halogen:prompt_tokens_cached_total` | prompt tokens the cache covered, *not processed* |
| `out` | `llamacpp:tokens_predicted_total` | generated tokens |

Input **presented** = processed + cached. Keep both: cost and energy follow *processed*,
usage-as-billed usually follows *presented*. On this server the split is roughly 6.5% real prefill
against 93.5% served from the prompt cache, so collapsing them into one number would be wrong
about 15x over.

```bash
python3 ~/LLM_Status_Watch/LLM_Status_Watch.py --tokens          # last 14 days + all-time
python3 ~/LLM_Status_Watch/LLM_Status_Watch.py --tokens 30 --tokens-all
python3 ~/LLM_Status_Watch/LLM_Status_Watch.py --tokens-csv      # every row, for a spreadsheet
```

```
day           in processed       in cached        in TOTAL          out     reqs     hit%    rst
2026-10-18          512,340       8,210,442       8,722,782     401,220     1,204    91.3%     0
```

How the numbers stay honest:

- **Deltas, not raw values.** The engine's counters restart at zero when the engine does. A
  negative delta is treated as a restart and counted from zero, and the `rst` column records how
  many times that happened, rather than subtracting a whole lifetime into a negative hole.
- **Deltas are taken every poll (30 s) but written once a day**, so an engine restart is caught at
  30 s resolution while the SD card only ever sees one row per day. `flush_minutes` controls how
  often that single row is refreshed, so a power cut loses at most that much of today.
- **The baseline survives a restart of the watcher**, so tokens the server burned while the watcher
  was down are still counted when it comes back, instead of going missing.
- **All-time = seed + measured.** The `seed` is whatever the engine had already counted the moment
  the ledger first looked (shown with its date by `--tokens-all`). That one number is a floor, not
  a measurement - the engine cannot tell you what it did before it was started. Everything after
  it is measured.

Not available, deliberately out of scope: who used the tokens. `/metrics` is aggregate only, so a
per-client or per-request split needs log parsing over ssh or a proxy on the Pi (see below), and
the ledger does not do either.

## Boot and crash behaviour

`systemctl is-enabled LLM_Status_Watch` is `enabled`, so a Pi reboot starts it. What makes that actually
work rather than just configured:

- `After=network-online.target` `Wants=network-online.target`, and on this Pi
  `NetworkManager-wait-online.service` is enabled, so the watcher is not started into a network
  that is not up yet (it took 2.7 s last boot).
- `Restart=always`, `RestartSec=15` - `always` rather than `on-failure`, so an unexpected clean
  exit still comes back. Verified by `kill -9` on the main PID: new PID, `NRestarts=1`.
- `StartLimitIntervalSec=60` `StartLimitBurst=10`, so a crash loop cannot leave the unit in a
  permanent `failed` state needing manual intervention.
- The unit survives the network being down at start: a failed poll is logged, never fatal.

**The boot-grace rule.** After a power cut the Pi is back in ~21 s while the LLM server needs
minutes to load 62 GiB of weights. Without grace the watcher would poll, fail 4 times in 2 min and
send you a `LLM DOWN` for a server that is simply still booting. So: until this process has had
*one* successful poll, DOWN is logged but not announced; if it is *still* unreachable when
`startup_grace_minutes` expires, that is a real outage and it alerts saying so. A recovery notice
is only sent for an outage that was announced, so a boot never opens with "back up".

The only thing not proven is a literal reboot, since that interrupts whatever the Pi is doing:

```bash
sudo reboot            # then, from anywhere:
ssh -F /dev/null jason@192.168.1.198 systemctl is-active LLM_Status_Watch
```

## Notes specific to these boxes

- **The metrics endpoint is bound to `0.0.0.0`**, which is what makes the whole design possible
  from the Pi. `halogen-watch` still assumes `127.0.0.1` because it runs on the LLM host.
- **From this workstation, ssh needs `-F /dev/null`**: `/etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf`
  is a symlink owned by `nobody` and any ssh/scp invocation dies with "Bad owner or permissions"
  unless the system config is skipped.
  `ssh -F /dev/null jason@192.168.1.198` / `scp -F /dev/null -r LLM_Status_Watch jason@192.168.1.198:~/`
- **What did not come across from `halogen-watch`**: PSI memory pressure, swap use, `podman
  ps`/`logs` and the `ss` client count are host-local to the LLM server, invisible from the Pi
  (there is no ssh key from the Pi to `192.168.1.59`). Idle/down detection does not need them. If
  you want the `PSI > 5` / swap warnings — which `halogen-watch` calls *the* failure mode — that
  needs a Pi→LLM ssh key or a small read-only helper on the LLM host, and it is deliberately not
  done here yet.
- **State file** `~/.local/state/LLM_Status_Watch/state.json` holds the last state, the idle
  timestamp, what was already announced, and counter baselines. Delete it to make the watcher
  forget; deleting it mid-idle means the current idle period is treated as a fresh baseline.
- The Pi is armv7l 32-bit with 921 MB RAM: anything written for it here is stdlib-only on purpose.

## No-systemd alternative

`cron` is active and needs no sudo:

```cron
* * * * * /usr/bin/flock -n /run/user/1000/llmw.lock /usr/bin/python3 /home/jason/LLM_Status_Watch/LLM_Status_Watch.py --once >/dev/null 2>&1
```

`--once` does one poll and applies the state machine, so the debounce and dedupe still work;
`flock -n` stops overlap. Weaker than the unit (no restart-on-crash visibility, 60 s granularity).

## Troubleshooting

| Symptom | Meaning |
| --- | --- |
| `poll failed: connection error` | Pi cannot reach the LLM server, or it is off |
| `poll failed: empty or metric-less body` | server answered but has nothing to say; counted as a failure on purpose |
| nothing at all in the log | not started, or wrong config path: `systemctl status LLM_Status_Watch` |
| `no topic configured in [notify]` | topic missing from the config |
| alert never comes | the current state is the baseline from first start; wait for a real busy→idle transition, or set `notify_on_startup_idle = true` |
| two ledgers, or one looks empty | your config's `state_file`/`db_file` point at an older project dir; `install.sh` now refuses to install rather than split the ledger in two |
| phone silent but `test-notify` says OK | app not subscribed to that exact topic, or battery optimisation killing it |
