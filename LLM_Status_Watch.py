#!/usr/bin/env python3
"""Notify my phone when the LAN LLM server goes idle.

Runs on the Raspberry Pi, polls the llama.cpp/halogen /metrics endpoint on the LLM
server over the LAN, and pushes edge-triggered notifications over ntfy:

    BUSY -> IDLE   once the idle test has held for `idle_after_minutes`
    OK   -> DOWN   after `fail_threshold` consecutive failed polls, or engine_up=0
    DOWN -> OK     recovery notice with the outage duration

Why this is not just `halogen-watch --once` on a cron line: that script is a human
dashboard for the LLM host itself. It shells out to `podman` and reads /proc/meminfo
and /proc/pressure, so it only works locally, it reprints its whole screen instead of
reporting changes, and it has no notion of "I already told you about this". This daemon
keeps state on disk and speaks only HTTP, which is what makes it safe to leave running
unattended on a different box.

State is remembered across restarts, so a restart neither re-alerts nor forgets what it
already announced. `--dry-run` logs decisions instead of publishing.

    ./LLM_Status_Watch.py --explain          one poll, print the verdict, change nothing
    ./LLM_Status_Watch.py --once             one poll, act on it (cron friendly)
    ./LLM_Status_Watch.py --test-notify      publish a test message and exit
    ./LLM_Status_Watch.py --dry-run          loop, log instead of publishing
    ./LLM_Status_Watch.py                    loop, publish for real
"""

import argparse
import configparser
import json
import logging
import os
import sys
import time
import sqlite3
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "LLM_Status_Watch.conf")

# Defaults are the values measured during bring-up; the config file overrides them.
DEFAULTS = {
    "watcher": {
        "metrics_url": "http://192.168.1.59:8080/metrics",
        "poll_seconds": "30",
        "idle_after_minutes": "10",
        "fail_threshold": "4",          # consecutive bad polls before calling it DOWN
        "http_timeout": "8",
        "empty_body_retries": "3",      # /metrics has been seen to answer 200 with no body
        "state_file": "~/.local/state/LLM_Status_Watch/state.json",
        "engine_up_counts_as_down": "true",
        # Don't cry wolf at boot. After a power cut the Pi is up in ~20 s and the LLM
        # server needs minutes to load 62 GiB of weights; alerting through that gap would
        # mean a "DOWN" notification for something that is simply still starting. Once the
        # server has answered even once, normal fail_threshold rules apply immediately.
        "startup_grace_minutes": "10",
    },
    "tokens": {
        "record": "true",
        "db_file": "~/.local/state/LLM_Status_Watch/tokens.sqlite3",
        # one row per local day is written once; this is only how often that single row
        # is refreshed, so a power cut cannot lose more than this much of today.
        "flush_minutes": "15",
    },
    "notify": {
        "method": "ntfy",
        "ntfy_server": "https://ntfy.sh",
        "topic": "",
        "server_label": "llm",
        "notify_idle": "true",
        "notify_down": "true",
        "notify_recovery": "true",
        "notify_on_startup_idle": "false",  # don't announce a server that was already idle
                                            # before the watcher ever looked at it
        "idle_priority": "default",
        "down_priority": "high",
        "click_url": "",
        "dry_run": "false",
        "publish_timeout": "10",
    },
}

PRIORITIES = {"max": "5", "high": "5", "default": "3", "low": "2", "min": "1"}


class FetchError(Exception):
    """Poll produced no usable metrics; the text becomes the reason in the DOWN notice."""


def parse_metrics(text):
    """Prometheus text -> {name: float}. Labels dropped, junk lines skipped."""
    out = {}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            out[parts[0].split("{", 1)[0]] = float(parts[-1])
        except ValueError:
            continue
    return out


def fetch(url, timeout, retries):
    """GET /metrics. An empty body has been observed on this endpoint and a 3x retry before
    concluding anything is deliberate - a blank response would otherwise read as idle."""
    last = "unknown error"
    for attempt in range(max(1, retries)):
        status = "?"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LLM_Status_Watch/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.status
                body = resp.read().decode("utf-8", "replace")
            if "llamacpp:" in body or "halogen:" in body:
                return parse_metrics(body)
            last = "empty or metric-less body (http %s)" % status
        except urllib.error.HTTPError as e:
            last = "http %s %s" % (e.code, e.reason)
        except urllib.error.URLError as e:
            last = "connection error: %s" % e.reason
        except Exception as e:  # timeouts, truncated payloads, bad certs
            last = "%s: %s" % (type(e).__name__, e)
        if attempt < retries - 1:
            time.sleep(0.3)
    raise FetchError(last)


def fdur(seconds):
    seconds = int(max(0, seconds))
    if seconds < 90:
        return "%ds" % seconds
    mins = seconds // 60
    if mins < 60:
        return "%dm" % mins
    return "%dh %02dm" % (mins // 60, mins % 60)


def ascii_only(s):
    """urllib encodes header values as latin-1, so keep titles and tags plain ASCII."""
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in str(s))


def send_ntfy(cfg, title, body, priority):
    """POST to ntfy. Returns (ok, detail)."""
    topic = cfg["notify"]["topic"]
    if not topic:
        return False, "no topic configured in [notify]"
    url = "%s/%s" % (cfg["notify"]["ntfy_server"].rstrip("/"), topic)
    headers = {
        "Title": ascii_only(title),
        "Priority": PRIORITIES.get(str(priority).lower(), "3"),
        "Content-Type": "text/plain; charset=utf-8",
        "User-Agent": "LLM_Status_Watch/1.0",
    }
    if cfg["notify"].get("click_url"):
        headers["Click"] = ascii_only(cfg["notify"]["click_url"])
    data = body.encode("utf-8")
    last = "unknown error"
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=float(cfg["notify"]["publish_timeout"])) as resp:
                if resp.status == 200:
                    return True, "published to %s" % topic
                last = "http %s" % resp.status
        except Exception as e:
            last = "%s: %s" % (type(e).__name__, e)
        if attempt < 2:
            time.sleep(1 + attempt)
    return False, last


def fmin(minutes):
    """Threshold display. 0 is a real setting now - alert on the first confirming poll -
    and printing it as '0m' would read like a bug."""
    minutes = float(minutes)
    if minutes <= 0:
        return "immediately (first confirming poll)"
    return "%gm" % minutes if abs(minutes - round(minutes)) < 1e-9 else "%.1fm" % minutes



# What we record, straight from the server. The names are disjoint by design: the
# server documents prompt_tokens_cached_total as "prompt tokens the prompt cache
# covered (not processed)", so input presented = processed + cached.
TOKEN_COUNTERS = {
    "prompt_processed": "llamacpp:prompt_tokens_total",
    "prompt_cached": "halogen:prompt_tokens_cached_total",
    "generated": "llamacpp:tokens_predicted_total",
    "requests": "halogen:requests_total",
    "cache_hits": "halogen:cache_hits_total",
    "cache_misses": "halogen:cache_misses_total",
}
TOKEN_KEYS = list(TOKEN_COUNTERS)

ROW_COLS = ["day"] + TOKEN_KEYS + ["kv_peak", "resets", "first_ts", "last_ts"]

DDL = """
CREATE TABLE IF NOT EXISTS daily(
  day TEXT PRIMARY KEY,
  prompt_processed REAL NOT NULL DEFAULT 0,
  prompt_cached    REAL NOT NULL DEFAULT 0,
  generated        REAL NOT NULL DEFAULT 0,
  requests         REAL NOT NULL DEFAULT 0,
  cache_hits       REAL NOT NULL DEFAULT 0,
  cache_misses     REAL NOT NULL DEFAULT 0,
  kv_peak REAL, resets INTEGER NOT NULL DEFAULT 0,
  first_ts REAL, last_ts REAL);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


class TokenRecorder:
    """Folds the server's lifetime counters into one row per local day, kept forever.

    The watcher already polls every 30 s, so deltas are taken at 30 s resolution while
    only one row per day is ever written - roughly 100 bytes a day, nothing an SD card
    will notice. Deltas rather than raw values are stored because the engine's counters
    restart from zero when the engine does: a negative delta is a restart, and the new
    value is counted from zero instead of subtracting into a negative hole, which is how
    a homegrown ledger quietly starts lying.

    "All time ever" = the seed (whatever the engine had already counted the first moment
    we looked, recorded once) plus the sum of the daily deltas since.
    """

    def __init__(self, cfg, log):
        self.cfg, self.log = cfg, log
        t = cfg.get("tokens", {})
        self.enabled = str(t.get("record", "true")).strip().lower() in ("1", "true", "yes", "on")
        self.db_path = os.path.expanduser(t.get("db_file"))
        self.flush_every = max(0.0, float(t.get("flush_minutes", 15))) * 60
        self.db = None
        self.prev = None
        self.bucket = None
        self.engine_now = None
        self.last_flush = 0.0
        self.open()

    def open(self):
        d = os.path.dirname(self.db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(self.db_path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(DDL)
        self.db.commit()
        # Resume from the last flushed baseline instead of from nothing. Otherwise every
        # restart throws away the gap since the last write, and any tokens the server
        # burned while the watcher was down would simply disappear from the ledger.
        self.prev = self.meta_get("prev")
        self.bucket = self._today_row(time.time())
        self.engine_now = self.meta_get("engine_now")

    def close(self):
        try:
            self.flush(force=True)
            self.db.close()
        except Exception:
            pass

    # -- helpers ---------------------------------------------------------------
    @staticmethod
    def today(now=None):
        return time.strftime("%Y-%m-%d", time.localtime(now or time.time()))

    def meta_get(self, key, default=None):
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def meta_set(self, key, value):
        self.db.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (key, json.dumps(value)))

    # -- ingest ----------------------------------------------------------------
    def observe(self, m, now):
        """Called with every successful metrics poll. Never raises to the caller."""
        vals = {}
        for key, name in TOKEN_COUNTERS.items():
            if name not in m:
                return
            vals[key] = m[name]
        kv = m.get("llamacpp:kv_cache_usage_ratio")

        if self.prev is None:
            # first ever look: seed once with what the engine had already counted. Those
            # tokens are a floor, not a measurement, and are labelled as such in --tokens-all.
            if self.meta_get("seed") is None:
                self.meta_set("seed", vals)
                self.meta_set("seed_ts", now)
                self.meta_set("prev", vals)
                self.log.info("token ledger seeded with %s prompt-processed / %s cached / %s generated "
                              "already counted by the engine", f(vals["prompt_processed"]),
                              f(vals["prompt_cached"]), f(vals["generated"]))
                self.prev = vals
            self.engine_now = vals
            self.db.commit()
            return

        resets = 0
        deltas = {}
        for key in TOKEN_KEYS:
            d = vals[key] - self.prev[key]
            if d < 0:
                d = vals[key]          # engine restarted: count from zero, do not go negative
                resets += 1
            deltas[key] = d
        self.engine_now = vals
        self.prev = vals

        if self.bucket is None or self.bucket["day"] != self.today(now):
            self.flush(force=True)
            self.bucket = self._today_row(now)
        b = self.bucket
        for key in TOKEN_KEYS:
            b[key] += deltas[key]
        b["resets"] += resets
        b["last_ts"] = now
        if kv is not None:
            b["kv_peak"] = max(b.get("kv_peak") or 0.0, float(kv))
        if self.flush_every and now - self.last_flush >= self.flush_every:
            self.flush(force=True)
        else:
            self.db.commit()

    def _today_row(self, now):
        day = self.today(now)
        row = self.db.execute("SELECT * FROM daily WHERE day=?", (day,)).fetchone()
        cols = ROW_COLS
        if row:
            b = dict(zip(cols, row))
        else:
            b = {"day": day, "kv_peak": None, "resets": 0, "first_ts": now, "last_ts": now}
            for key in TOKEN_KEYS:
                b[key] = 0.0
        b["last_ts"] = now
        return b

    def flush(self, force=False):
        """Upsert today's single row. first_ts is deliberately not updated, so the row keeps
        the moment the day started even after a restart re-flushes it."""
        if not self.bucket:
            return
        b = self.bucket
        args = [b.get(c) for c in ROW_COLS]
        sets = ", ".join("%s=excluded.%s" % (c, c) for c in ROW_COLS if c not in ("day", "first_ts"))
        self.db.execute(
            "INSERT INTO daily(%s) VALUES(%s) ON CONFLICT(day) DO UPDATE SET %s"
            % (",".join(ROW_COLS), ",".join("?" * len(ROW_COLS)), sets), args)
        # the baseline rides along with the row it belongs to, so one flush = one write
        if self.prev:
            self.meta_set("prev", self.prev)
        if self.engine_now:
            self.meta_set("engine_now", self.engine_now)
        self.db.commit()
        self.last_flush = time.time()

    # -- reporting -------------------------------------------------------------
    def all_time(self):
        seed = self.meta_get("seed") or {k: 0.0 for k in TOKEN_KEYS}
        sums = self.db.execute("SELECT %s, COALESCE(SUM(resets),0) FROM daily"
                               % ", ".join("COALESCE(SUM(%s),0)" % k for k in TOKEN_KEYS)).fetchone()
        out = {}
        for i, k in enumerate(TOKEN_KEYS):
            out[k] = seed.get(k, 0.0) + (sums[i] or 0.0)
        out["resets"] = sums[len(TOKEN_KEYS)] or 0
        out["seed"] = seed
        out["seed_ts"] = self.meta_get("seed_ts")
        out["engine_now"] = self.engine_now or self.meta_get("engine_now") or {}
        out["days"] = self.db.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
        return out

    def recent(self, days=14):
        return self.db.execute("SELECT * FROM daily ORDER BY day DESC LIMIT ?", (days,)).fetchall()

    def every(self):
        return self.db.execute("SELECT * FROM daily ORDER BY day ASC").fetchall()


def f(n):
    return "{:,}".format(int(round(n or 0)))


class Watcher:
    def __init__(self, cfg, log, dry_run=False):
        self.cfg = cfg
        self.log = log
        self.dry_run = dry_run or cfg["notify"]["dry_run"]
        w, n = cfg["watcher"], cfg["notify"]
        self.poll_seconds = float(w["poll_seconds"])
        self.idle_after = float(w["idle_after_minutes"]) * 60
        self.fail_threshold = max(1, int(w["fail_threshold"]))
        self.engine_up_down = w["engine_up_counts_as_down"]
        self.grace = max(0.0, float(w.get("startup_grace_minutes", 10) or 0)) * 60
        self.started = time.time()
        self.ever_ok = False
        self.label = n["server_label"] or "llm"
        self.state_file = w["state_file"]
        self.rec_fail = 0
        self.recorder = None
        try:
            r = TokenRecorder(cfg, log)
            self.recorder = r if r.enabled else None
        except Exception as e:
            log.error("token ledger DISABLED, cannot open %s: %s", cfg["tokens"].get("db_file"), e)
        self.state = self._load_state()

    # ---- persisted state ---------------------------------------------------
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                st = json.load(f)
            if isinstance(st, dict):
                return st
        except FileNotFoundError:
            pass
        except Exception as e:
            self.log.warning("unreadable state file %s (%s); starting fresh", self.state_file, e)
        return {"state": "unknown", "fail_count": 0}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f, indent=1, sort_keys=True)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log.warning("state not saved: %s", e)

    # ---- notify ------------------------------------------------------------
    def notify(self, title, body, priority, tag=""):
        text = ("%s %s" % (tag, body)) if tag else body
        if self.dry_run:
            self.log.info("DRY-RUN would notify [%s] %s :: %s",
                          priority, title, " / ".join(body.splitlines()))
            return True
        ok, detail = send_ntfy(self.cfg, "%s | %s" % (title, self.label), text, priority)
        if ok:
            self.log.info("notified: %s", title)
        else:
            self.log.error("notify FAILED (%s); retrying next poll", detail)
        return ok

    # ---- the verdict -------------------------------------------------------
    def is_idle(self, m):
        """Idle = nothing in flight, nothing queued, the engine's own busy gauge at 0, and
        the token counters frozen since the last sample. All four, because each one on its
        own can read 0 without the server actually being free."""
        proc = m.get("llamacpp:requests_processing", 0)
        defer = m.get("llamacpp:requests_deferred", 0)
        busy = m.get("halogen:busy_for_seconds")
        if proc > 0 or defer > 0:
            return False, "%d in flight, %d deferred" % (proc, defer)
        if busy is not None and busy > 0:
            return False, "engine busy_for %ds" % int(busy)
        if busy is None:
            return False, "no busy_for_seconds gauge, not guessing"
        prev = self.state.get("counters") or {}
        for key in ("llamacpp:tokens_predicted_total", "llamacpp:prompt_tokens_total"):
            if key in prev and key in m and m[key] > prev[key]:
                return False, "%s still advancing" % key.split(":")[-1]
        return True, "no requests in flight, counters frozen"

    def throughput(self, m, now):
        """tok/s from counter deltas. The predicted_tokens_seconds gauge is 'since the last
        scrape', and nothing scrapes it between our polls, so it reads stale."""
        prev = self.state.get("counters") or {}
        t0, w0 = prev.get("_t"), prev.get("llamacpp:tokens_predicted_total")
        rate = None
        if t0 and w0 is not None and now > t0:
            delta = m.get("llamacpp:tokens_predicted_total", 0) - w0
            if delta > 0:
                rate = delta / (now - t0)
                self.state["last_tps"] = round(rate, 1)
        return rate

    def context(self, m, rate, idle_for=None):
        """Body text: enough to decide what to do without sshing anywhere first."""
        now = time.time()
        lines = []
        if idle_for is not None:
            if idle_for < 60:
                lines.append("just went idle (confirmed %s)" % time.strftime("%H:%M", time.localtime(now)))
            else:
                lines.append("idle for %s (since %s)" % (
                    fdur(idle_for),
                    time.strftime("%H:%M", time.localtime(self.state.get("idle_since") or now))))
        lines.append("requests in flight %d | deferred %d | engine_up %d" % (
            m.get("llamacpp:requests_processing", 0),
            m.get("llamacpp:requests_deferred", 0),
            m.get("halogen:engine_up", 1)))
        lines.append("KV pool %.1f%% of positions still held - engine still loaded"
                     % (m.get("llamacpp:kv_cache_usage_ratio", 0) * 100))
        tail = "served %d requests" % m.get("halogen:requests_total", 0)
        if rate:
            tail += " | %.1f tok/s just now" % rate
        elif self.state.get("last_tps"):
            tail += " | last seen %.1f tok/s" % self.state["last_tps"]
        lines.append(tail)
        return "\n".join(lines)

    # ---- polling -----------------------------------------------------------
    def poll_once(self, allow_notify=True):
        now = time.time()
        w = self.cfg["watcher"]
        st = self.state
        try:
            m = fetch(w["metrics_url"], float(w["http_timeout"]), int(w["empty_body_retries"]))
        except FetchError as e:
            return self.handle_down(str(e), now, allow_notify)

        # HTTP answered but the engine behind it is gone: that is down too, and it is the
        # one failure mode where a naive "server answered 200" check would lie.
        if self.engine_up_down and m.get("halogen:engine_up", 1) == 0:
            return self.handle_down("engine_up=0 (http answered, engine is gone)", now, allow_notify)

        self.state["fail_count"] = 0
        self.ever_ok = True
        if self.recorder:
            try:
                self.recorder.observe(m, now)
                self.rec_fail = 0
            except Exception as e:
                self.rec_fail += 1
                self.log.error("token ledger write failed (alerts unaffected) x%d: %s", self.rec_fail, e)
                if self.rec_fail >= 5:
                    self.log.error("token ledger disabled after 5 failures")
                    self.recorder = None
        rate = self.throughput(m, now)
        idle, why = self.is_idle(m)
        self.update_counters(m)
        st["last_state"] = why

        if st.get("state") == "down":
            outage = now - st.get("down_since", now)
            gap = st.get("down_gap", "")
            # read the flags before the state is overwritten, and clear them either way so the
            # next outage starts from a clean slate
            was_announced = bool(st.pop("down_notified", False))
            st.pop("down_pending_boot", None)
            st.pop("down_since", None)
            st.pop("down_gap", None)
            st["state"] = "idle" if idle else "busy"
            if not idle:
                st["idle_since"] = None
                st["notified_idle_for"] = None
            if was_announced:
                if allow_notify and self.cfg["notify"]["notify_recovery"]:
                    self.notify("LLM back up after %s" % fdur(outage),
                                "reachable again. %s\n%s" % (gap, self.context(m, rate)),
                                "default", ":arrow_up:")
            else:
                # an outage we deliberately never announced must not produce a recovery ping
                # either, or the first thing the user sees after a boot is "back up"
                self.log.info("reachable again after %s; the outage was never announced (boot grace), "
                              "so no recovery notice either", fdur(outage))
            self._save_state()
            return {"ok": True, "idle": idle, "why": why, "metrics": m, "rate": rate,
                    "recovered": True}

        if idle:
            if not st.get("idle_since"):
                st["idle_since"] = now
                self.log.info("now idle (%s); will announce after %s", why, fmin(self.idle_after / 60))
                # A server that was already idle when we first looked must not produce a
                # notification of its own accord; only a real busy -> idle transition does.
                if not st.get("bootstrapped") and not self.cfg["notify"]["notify_on_startup_idle"]:
                    st["notified_idle_for"] = st["idle_since"]
                    self.log.info("first observation is already idle; treating it as the baseline")
            waited = now - st["idle_since"]
            if (allow_notify and self.cfg["notify"]["notify_idle"]
                    and waited >= self.idle_after
                    and st.get("notified_idle_for") != st["idle_since"]):
                headline = "LLM idle now" if waited < 60 else "LLM idle %s" % fdur(waited)
                if self.notify(headline,
                               self.context(m, rate, idle_for=waited),
                               self.cfg["notify"]["idle_priority"], ":white_check_mark:"):
                    st["notified_idle_for"] = st["idle_since"]
            st["state"] = "idle"
        else:
            if st.get("state") == "idle":
                self.log.info("busy again (%s)", why)
            st["state"] = "busy"
            st["idle_since"] = None
            st["notified_idle_for"] = None

        st["bootstrapped"] = True
        self._save_state()
        return {"ok": True, "idle": idle, "why": why, "metrics": m, "rate": rate}

    def update_counters(self, m):
        keep = dict(self.state.get("counters") or {})
        for k in ("llamacpp:tokens_predicted_total", "llamacpp:prompt_tokens_total",
                  "halogen:requests_total"):
            if k in m:
                keep[k] = m[k]
        keep["_t"] = time.time()
        self.state["counters"] = keep

    def handle_down(self, reason, now, allow_notify):
        st = self.state
        st["fail_count"] = st.get("fail_count", 0) + 1
        gap = "%s (%d failed polls in a row)" % (reason, st["fail_count"])
        self.log.warning("poll failed: %s [%d/%d]", reason, st["fail_count"], self.fail_threshold)

        if st.get("state") == "down":
            st["down_gap"] = gap
            # A boot-time suppression must not silently become permanent. If the grace ran out
            # with the server still unreachable, that IS an outage and now gets announced.
            if st.get("down_pending_boot") and not self.ever_ok and now - self.started >= self.grace:
                if allow_notify and self.cfg["notify"]["notify_down"]:
                    self.notify("LLM DOWN",
                                "%s\nUnreachable for %s: never answered since this watcher started at %s."
                                % (gap, fdur(now - self.started),
                                   time.strftime("%H:%M", time.localtime(self.started))),
                                self.cfg["notify"]["down_priority"], ":warning:")
                    st["down_notified"] = True
                    st.pop("down_pending_boot", None)
            self._save_state()
            return {"ok": False, "why": gap, "down": True}

        if st["fail_count"] < self.fail_threshold:
            self._save_state()
            return {"ok": False, "why": gap, "confirming": True}

        st["state"] = "down"
        st["down_since"] = now
        st["down_gap"] = gap
        st["idle_since"] = None
        st["notified_idle_for"] = None
        st["down_notified"] = False

        if not self.ever_ok and now - self.started < self.grace:
            # No answer since this process began, still inside boot grace: probably the
            # server is still coming up, not broken. Stay quiet, but keep watching.
            st["down_pending_boot"] = True
            self.log.warning("not alerting: nothing has answered since this watcher started, "
                             "%d min of boot grace left", int((self.grace - (now - self.started)) / 60))
        elif allow_notify and self.cfg["notify"]["notify_down"]:
            self.notify("LLM DOWN",
                        "%s\nNo usable answer since %s.\nLast state: %s" % (
                            gap, time.strftime("%H:%M", time.localtime(now)),
                            st.get("last_state", "?")),
                        self.cfg["notify"]["down_priority"], ":warning:")
            st["down_notified"] = True
        self._save_state()
        return {"ok": False, "why": gap, "down": True}

    def run(self):
        self.log.info("LLM_Status_Watch up: %s every %.0fs, idle threshold %s, %s",
                      self.cfg["watcher"]["metrics_url"], self.poll_seconds,
                      fmin(self.idle_after / 60), "DRY-RUN (no pushes)" if self.dry_run else "live")
        self.log.info("ntfy topic: %s", self.cfg["notify"]["topic"] or "<none configured>")
        if not self.state.get("bootstrapped"):
            self.log.info("first run: the current state becomes the baseline, nothing is announced for it")
        while True:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                if self.recorder:
                    self.recorder.close()
                raise
            except Exception as e:  # a watcher that dies quietly is worse than one that lies
                self.log.exception("watch loop error: %s", e)
            time.sleep(self.poll_seconds)


def print_tokens(rec, days=14, alltime=False, as_csv=False):
    """Human table by default, --tokens-csv for spreadsheets."""
    def hit_pct(h, mm):
        tot = (h or 0) + (mm or 0)
        return 100.0 * (h or 0) / tot if tot else 0.0

    if as_csv:
        print("day,prompt_processed,prompt_cached,prompt_total,generated,requests,"
              "cache_hits,cache_misses,cache_hit_pct,kv_peak_pct,resets")
        for r in rec.every():
            d = dict(zip(ROW_COLS, r))
            print("%s,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.1f,%s,%d" % (
                d["day"], d["prompt_processed"], d["prompt_cached"],
                d["prompt_processed"] + d["prompt_cached"], d["generated"], d["requests"],
                d["cache_hits"], d["cache_misses"], hit_pct(d["cache_hits"], d["cache_misses"]),
                "%.2f" % (d["kv_peak"] * 100) if d["kv_peak"] is not None else "", d["resets"]))
        return

    rows = rec.recent(days)
    print("%-11s %14s %15s %15s %12s %8s %8s %6s" % (
        "day", "in processed", "in cached", "in TOTAL", "out", "reqs", "hit%", "rst"))
    for r in reversed(rows):
        d = dict(zip(ROW_COLS, r))
        print("%-11s %14s %15s %15s %12s %8s %7.1f%% %6d" % (
            d["day"], f(d["prompt_processed"]), f(d["prompt_cached"]),
            f(d["prompt_processed"] + d["prompt_cached"]), f(d["generated"]), f(d["requests"]),
            hit_pct(d["cache_hits"], d["cache_misses"]), d["resets"]))
    if not rows:
        print("(no days recorded yet - the ledger fills in as the watcher polls)")

    t = rec.all_time()
    print("-" * 90)
    print("%-11s %14s %15s %15s %12s %8s %7.1f%% %6d" % (
        "ALL TIME", f(t["prompt_processed"]), f(t["prompt_cached"]),
        f(t["prompt_processed"] + t["prompt_cached"]), f(t["generated"]), f(t["requests"]),
        hit_pct(t["cache_hits"], t["cache_misses"]), t["resets"]))
    seed = t["seed"]
    seed_in = seed.get("prompt_processed", 0) + seed.get("prompt_cached", 0)
    if alltime:
        print()
        print("of everything presented as prompt: %s tokens; %.1f%% needed real prefill, %.1f%% came from the prompt cache"
              % (f(seed_in and t["prompt_processed"] + t["prompt_cached"]),
                 100.0 * t["prompt_processed"] / (t["prompt_processed"] + t["prompt_cached"] or 1),
                 100.0 * t["prompt_cached"] / (t["prompt_processed"] + t["prompt_cached"] or 1)))
        print("generated (output) tokens: %s" % f(t["generated"]))
        if seed:
            print("seed: the engine had already counted %s prompt / %s output when the ledger first looked (%s);"
                  " everything after that is measured, not estimated"
                  % (f(seed.get("prompt_processed", 0) + seed.get("prompt_cached", 0)),
                     f(seed.get("generated", 0)),
                     time.strftime("%Y-%m-%d %H:%M", time.localtime(t["seed_ts"])) if t["seed_ts"] else "?"))
        if t["resets"]:
            print("note: %d engine counter reset(s) seen; each was counted from zero, none subtracted into a hole"
                  % t["resets"])
        print("days stored: %d  (never pruned)  db: %s" % (t["days"], rec.db_path))


def load_config(path):
    cfg = {sec: dict(vals) for sec, vals in DEFAULTS.items()}
    if path and os.path.exists(path):
        cp = configparser.ConfigParser()
        cp.read(path)
        for sec in cp.sections():
            cfg.setdefault(sec, {}).update(dict(cp.items(sec)))
    else:
        cfg.setdefault("_warnings", {})["missing"] = path or ""
    cfg["watcher"]["state_file"] = os.path.expanduser(cfg["watcher"]["state_file"])
    for key in ("notify_idle", "notify_down", "notify_recovery", "notify_on_startup_idle",
                "dry_run", "engine_up_counts_as_down"):
        for sec in cfg:
            if key in cfg[sec]:
                cfg[sec][key] = str(cfg[sec][key]).strip().lower() in ("1", "true", "yes", "on")
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description="notify my phone when the LLM server goes idle")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--once", action="store_true", help="single poll, act on it, exit")
    ap.add_argument("--explain", action="store_true", help="single poll, print verdict, change nothing")
    ap.add_argument("--test-notify", action="store_true", help="publish a test notification and exit")
    ap.add_argument("--dry-run", action="store_true", help="log decisions instead of publishing")
    ap.add_argument("--tokens", nargs="?", type=int, const=14, default=None, metavar="DAYS",
                    help="print the token ledger for the last N days (default 14)")
    ap.add_argument("--tokens-all", action="store_true", help="with --tokens: totals and provenance")
    ap.add_argument("--tokens-csv", action="store_true", help="dump every daily row as CSV")
    ap.add_argument("--idle-minutes", type=float, help="override idle_after_minutes")
    ap.add_argument("--poll-seconds", type=float, help="override poll_seconds")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    log = logging.getLogger("LLM_Status_Watch")

    cfg = load_config(args.config)
    if "missing" in cfg.get("_warnings", {}):
        log.warning("no config at %s, using built-in defaults", cfg["_warnings"]["missing"])
    if args.idle_minutes is not None:
        cfg["watcher"]["idle_after_minutes"] = str(args.idle_minutes)
    if args.poll_seconds is not None:
        cfg["watcher"]["poll_seconds"] = str(args.poll_seconds)

    if args.tokens is not None or args.tokens_csv:
        try:
            rec = TokenRecorder(cfg, log)
        except Exception as e:
            log.error("cannot open the token ledger: %s", e)
            return 1
        print_tokens(rec, days=args.tokens or 14, alltime=args.tokens_all, as_csv=args.tokens_csv)
        return 0

    if args.explain:
        v = Watcher(cfg, log, dry_run=True).poll_once(allow_notify=False)
        m = v.get("metrics", {})
        print("poll ok      : %s" % v.get("ok"))
        print("idle verdict : %s  (%s)" % (v.get("idle"), v.get("why")))
        print("target       : %s" % cfg["watcher"]["metrics_url"])
        for k in sorted(m):
            if k.startswith(("halogen:", "llamacpp:")):
                print("  %-44s %s" % (k, m[k]))
        return 0 if v.get("ok") else 1

    w = Watcher(cfg, log, dry_run=args.dry_run)
    if args.test_notify:
        v = w.poll_once(allow_notify=False)
        body = "test notification from LLM_Status_Watch on %s\n%s" % (
            os.uname().nodename,
            w.context(v["metrics"], v.get("rate")) if v.get("ok")
            else "metrics poll failed: %s" % v.get("why"))
        ok, detail = send_ntfy(cfg, "LLM_Status_Watch test | %s" % cfg["notify"]["server_label"],
                               ":bell: " + body, "default")
        print("publish %s: %s" % ("OK" if ok else "FAILED", detail))
        print("subscribe: %s/%s  (open in a browser, or add this topic in the app)"
              % (cfg["notify"]["ntfy_server"].rstrip("/"), cfg["notify"]["topic"]))
        return 0 if ok else 1

    if args.once:
        v = w.poll_once()
        print("ok=%s idle=%s (%s)" % (v.get("ok"), v.get("idle"), v.get("why")))
        return 0 if v.get("ok") else 1

    try:
        w.run()
    except KeyboardInterrupt:
        log.info("interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
