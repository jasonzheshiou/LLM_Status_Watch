#!/usr/bin/env python3
"""Offline self-test for LLM_Status_Watch: drives poll_once with a stubbed fetch and a fake
clock, so the debounce, the dedupe, the restart behaviour and the down/recovery path
are all exercised in a second without waiting for real traffic.

    python3 selftest.py
"""
import importlib.util
import json
import logging
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("llm_status_watch", os.path.join(HERE, "LLM_Status_Watch.py"))
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)

T0 = 1_700_000_000.0


class Clock:
    """Stands in for the time module inside the watcher."""
    now = T0

    @classmethod
    def time(cls):
        return cls.now

    @classmethod
    def sleep(cls, _):
        pass

    @classmethod
    def strftime(cls, f, t=None):
        return __import__("time").strftime(f, t or __import__("time").localtime(cls.now))

    @classmethod
    def localtime(cls, t):
        return __import__("time").localtime(t)


class Recorder(logging.Handler):
    def __init__(self):
        super().__init__()
        self.notices = []
        self.lines = []

    def emit(self, record):
        msg = record.getMessage()
        self.lines.append((record.levelname, msg))
        if "would notify" in msg:
            self.notices.append(msg)


def metrics(processing=0, deferred=0, busy=0, predicted=1000, prompt=5000, up=1, kv=0.31):
    return {
        "llamacpp:requests_processing": float(processing),
        "llamacpp:requests_deferred": float(deferred),
        "halogen:busy_for_seconds": float(busy),
        "llamacpp:tokens_predicted_total": float(predicted),
        "llamacpp:prompt_tokens_total": float(prompt),
        "halogen:requests_total": 293.0,
        "halogen:engine_up": float(up),
        "llamacpp:kv_cache_usage_ratio": float(kv),
    }


def make(samples, state_file, idle_min=10.0, fail=4):
    """samples: list of dicts or the string 'FAIL' or an Exception message."""
    cfg = W.load_config(None)
    cfg["watcher"]["idle_after_minutes"] = str(idle_min)
    cfg["watcher"]["fail_threshold"] = str(fail)
    cfg["watcher"]["state_file"] = state_file
    rec = Recorder()
    log = logging.getLogger("t" + state_file[-8:])
    log.handlers = [rec]
    log.setLevel(logging.INFO)
    log.propagate = False
    w = W.Watcher(cfg, log, dry_run=True)
    seq = list(samples)

    def fake_fetch(url, timeout, retries):
        item = seq.pop(0) if seq else metrics()
        if item == "FAIL":
            raise W.FetchError("connection error: [Errno 111] Connection refused")
        return item
    W.fetch = fake_fetch
    return w, rec


results = []


def check(name, got, want):
    ok = got == want
    results.append((name, ok, "got %r want %r" % (got, want)))
    print("%-4s %-58s %s" % ("PASS" if ok else "FAIL", name, "" if ok else "  <-- %r != %r" % (got, want)))


def tmp_state():
    fd, p = tempfile.mkstemp(prefix="selftest-", suffix=".json")
    os.close(fd)
    os.unlink(p)
    return p


W.time = Clock
base_idle_min = 10.0

# ---- 1. no notification while busy --------------------------------------------
Clock.now = T0
sf = tmp_state()
w, rec = make([metrics(processing=1, busy=12, predicted=2000)] * 6, sf)
for _ in range(6):
    Clock.now += 30
    w.poll_once()
check("1 busy server never notifies", len(rec.notices), 0)

# ---- 2. idle is debounced and announced exactly once --------------------------
Clock.now = T0
sf = tmp_state()
seq = [metrics(processing=1, busy=12, predicted=2000)]          # busy
seq += [metrics(predicted=2000)] * 30                            # then idle
w, rec = make(seq, sf)
for _ in range(31):
    Clock.now += 30                                              # 30 s per poll
    w.poll_once()
check("2 one idle notification only", len(rec.notices), 1)
check("2 announced at the 10 min mark", "DRY-RUN would notify [default] LLM idle 10m" in rec.notices[0], True)

# ---- 3. restart must not re-announce ------------------------------------------
w2, rec2 = make([metrics(predicted=2000)] * 20, sf)
for _ in range(20):
    Clock.now += 30
    w2.poll_once()
check("3 restart mid-idle does not re-announce", len(rec2.notices), 0)

# ---- 4. flapping busy/idle/busy cannot spam -----------------------------------
Clock.now = T0
sf = tmp_state()
seq = [metrics(processing=1, busy=5, predicted=3000)]
for i in range(40):
    seq.append(metrics(processing=1, busy=2, predicted=3000 + i) if i % 2 else metrics(predicted=3000 + i))
w, rec = make(seq, sf)
for _ in range(41):
    Clock.now += 30
    w.poll_once()
check("4 flapping produces no idle alert", len(rec.notices), 0)

# ---- 5. frozen gauges but advancing counters = still busy ---------------------
Clock.now = T0
sf = tmp_state()
w, rec = make([metrics(predicted=1000)] + [metrics(predicted=1500 + i) for i in range(10)], sf)
first = w.poll_once()
for _ in range(10):
    Clock.now += 30
    v = w.poll_once()
# the first ever sample has no counter baseline, so it reads idle by design
check("5 counters advancing keeps it busy", (first["idle"], v["idle"]), (True, False))
check("5 reason names the counter", "still advancing" in v["why"], True)

# ---- 6. down after fail_threshold, recovery on return -------------------------
Clock.now = T0
sf = tmp_state()
seq = [metrics(processing=1, busy=3, predicted=2000), "FAIL", "FAIL", "FAIL", "FAIL", "FAIL", metrics(predicted=2000)]
w, rec = make(seq, sf)
for _ in range(7):
    Clock.now += 30
    w.poll_once()
downs = [n for n in rec.notices if "DOWN" in n]
ups = [n for n in rec.notices if "back up" in n]
check("6 exactly one DOWN notice", len(downs), 1)
check("6 exactly one recovery notice", len(ups), 1)
check("6 down needs 4 failed polls", [n for n in rec.lines if "poll failed" in n[1]][:1][0][0], "WARNING")

# ---- 7. engine_up=0 counts as down even though HTTP answered ------------------
Clock.now = T0
sf = tmp_state()
seq = [metrics(processing=1, busy=3, predicted=2000)] + [metrics(up=0)] * 6
w, rec = make(seq, sf)
for _ in range(7):
    Clock.now += 30
    w.poll_once()
check("7 engine_up=0 triggers DOWN", len([n for n in rec.notices if "DOWN" in n]), 1)

# ---- 8. a publish failure is retried, not swallowed ---------------------------
Clock.now = T0
sf = tmp_state()
seq = [metrics(processing=1, busy=12, predicted=2000)] + [metrics(predicted=2000)] * 14
w, rec = make(seq, sf, idle_min=1.0)   # short threshold so the alert actually fires in this window
w.dry_run = False
attempts = {"n": 0}


def failing_publish(cfg, title, body, priority):
    attempts["n"] += 1
    return (True, "published") if attempts["n"] >= 3 else (False, "timeout: simulated")


W.send_ntfy = failing_publish
for _ in range(15):
    Clock.now += 30
    w.poll_once()
check("8 failed publish retried until it lands", attempts["n"] >= 3, True)
check("8 not announced twice after retry", json.load(open(sf))["notified_idle_for"] is not None, True)

# ---- 9. already-idle at first ever run is the baseline, not an alert ----------
Clock.now = T0
sf = tmp_state()
w, rec = make([metrics(predicted=2000)] * 30, sf)
for _ in range(30):
    Clock.now += 30
    w.poll_once()
check("9 pre-existing idle stays quiet", len(rec.notices), 0)

# ---- 10. notify_on_startup_idle=true does announce it -------------------------
Clock.now = T0
sf = tmp_state()
cfg = W.load_config(None)
cfg["watcher"]["idle_after_minutes"] = "1"
cfg["watcher"]["state_file"] = sf
cfg["notify"]["notify_on_startup_idle"] = True
rec = Recorder()
log = logging.getLogger("startup"); log.handlers = [rec]; log.setLevel(logging.INFO); log.propagate = False
w = W.Watcher(cfg, log, dry_run=True)
W.fetch = lambda url, timeout, retries: metrics(predicted=2000)
for _ in range(5):
    Clock.now += 30
    w.poll_once()
check("10 opt-in announces first idle", len(rec.notices), 1)

# ---- 11. parse_metrics robustness ---------------------------------------------
p = W.parse_metrics('# TYPE x gauge\nbad line\nx{a="b"} 12\nnan 3\ny 4 5 6\n')
check("11 parse_metrics drops junk and labels", p, {"x": 12.0, "nan": 3.0, "y": 6.0})
check("11 ascii_only strips headers-unsafe chars", W.ascii_only("id\xe9 l\xe9 m>ok"), "id? l? m>ok")
# ---- 13. threshold 0 = alert on the first poll that confirms idle ---------------
Clock.now = T0
sf = tmp_state()
seq = [metrics(processing=1, busy=12, predicted=2000)] + [metrics(predicted=2000)] * 5
w, rec = make(seq, sf, idle_min=0.0)
for _ in range(6):
    Clock.now += 30
    w.poll_once()
check("13 threshold 0 alerts on the first confirming poll", len(rec.notices), 1)
check("13 headline says 'now', not 'idle 0s'", "LLM idle now" in rec.notices[0], True)

check("11 threshold formatting", [W.fmin(10), W.fmin(0.5), W.fmin(2.5)], ["10m", "0.5m", "2.5m"])
check("11 threshold 0 prints as immediate", W.fmin(0), "immediately (first confirming poll)")
check("11 duration formatting", [W.fdur(45), W.fdur(600), W.fdur(3725)], ["45s", "10m", "1h 02m"])

# ---- 15. boot grace: a reboot must not ping the phone -------------------------
Clock.now = T0
sf = tmp_state()
w, rec = make(["FAIL"] * 6, sf)          # unreachable from the very first poll
w.grace = 600
for _ in range(6):
    Clock.now += 30
    w.poll_once()
check("15 no DOWN alert while inside boot grace", len([n for n in rec.notices if "DOWN" in n]), 0)
check("15 suppression is recorded, not lost", w.state.get("down_pending_boot"), True)
w.poll_once()                            # it came back, still inside the grace
check("15 no recovery ping for an unannounced outage",
      len([n for n in rec.notices if "back up" in n]), 0)

Clock.now = T0
sf = tmp_state()
w, rec = make(["FAIL"] * 35, sf)         # stays unreachable, exactly as many as we poll
w.grace = 600
for _ in range(35):                      # 17.5 min, past the grace
    Clock.now += 30
    w.poll_once()
check("15 grace expiry does announce", len([n for n in rec.notices if "DOWN" in n]), 1)
check("15 and only once", w.state.get("down_notified"), True)
w.poll_once()                            # recovery
check("15 recovery announced after a real DOWN", len([n for n in rec.notices if "back up" in n]), 1)
# ---- 13b. token ledger --------------------------------------------------------
def tok(prompt=0, cached=0, gen=0, reqs=0, hits=0, misses=0, kv=0.5):
    return {"llamacpp:prompt_tokens_total": float(prompt),
            "halogen:prompt_tokens_cached_total": float(cached),
            "llamacpp:tokens_predicted_total": float(gen),
            "halogen:requests_total": float(reqs),
            "halogen:cache_hits_total": float(hits),
            "halogen:cache_misses_total": float(misses),
            "llamacpp:kv_cache_usage_ratio": float(kv)}


Clock.now = T0
dbp = tempfile.mktemp(prefix="selftest-", suffix=".sqlite3")
cfg = W.load_config(None)
cfg["tokens"]["db_file"] = dbp
cfg["tokens"]["flush_minutes"] = "0"          # write only on rollover/flush, exercise that path
tl = logging.getLogger("tok"); tl.handlers = [logging.NullHandler()]; tl.propagate = False
rec = W.TokenRecorder(cfg, tl)

rec.observe(tok(100, 5000, 10, 1, 1, 0, kv=0.3), Clock.now)      # first look -> seed only
t = rec.all_time()
check("14 seeding records what the engine already counted", (t["prompt_processed"], t["prompt_cached"], t["generated"]), (100.0, 5000.0, 10.0))
check("14 seeding accumulates nothing yet", rec.db.execute("SELECT COUNT(*) FROM daily").fetchone()[0], 0)

rec.observe(tok(160, 5200, 25, 2, 2, 0, kv=0.4), Clock.now + 30)  # +60 +200 +15 +1 +1
rec.observe(tok(160, 5200, 25, 2, 2, 0, kv=0.9), Clock.now + 60)  # idle poll, no movement
rec.flush(force=True)
row = rec.db.execute("SELECT day,prompt_processed,prompt_cached,generated,requests,kv_peak,resets FROM daily").fetchall()
check("14 daily row holds the deltas not the raw counters", row[0][1:6], (60.0, 200.0, 15.0, 1.0, 0.9))
check("14 idle polls add nothing", len(row), 1)

# engine restart: counters drop, must be counted from zero, not subtracted into a hole
rec.observe(tok(5, 30, 2, 1, 1, 0, kv=0.2), Clock.now + 90)
rec.flush(force=True)
row = rec.db.execute("SELECT prompt_processed,prompt_cached,generated,resets FROM daily").fetchone()
check("14 counter reset counted from zero", row[:3], (65.0, 230.0, 17.0))
check("14 reset is recorded, not silent", row[3], 5)   # 5 of 6 counters dropped; cache_misses did not

t = rec.all_time()
check("14 all time = seed + deltas", (t["prompt_processed"], t["prompt_cached"], t["generated"]), (165.0, 5230.0, 27.0))

# a new local day gets its own row, kept forever
Clock.now = T0 + 86400
rec.observe(tok(50, 60, 5, 2, 2, 0, kv=0.1), Clock.now)
rec.flush(force=True)
days = rec.db.execute("SELECT day,prompt_processed FROM daily ORDER BY day").fetchall()
check("14 day rollover creates a second row", len(days), 2)
check("14 second day starts clean", days[1][1], 45.0)
check("14 all time spans every day", rec.all_time()["prompt_processed"], 210.0)

# reopen like a service restart: the partial day must continue, not restart
rec2 = W.TokenRecorder(cfg, tl)
rec2.observe(tok(70, 60, 5, 2, 2, 0, kv=0.1), Clock.now + 30)
rec2.flush(force=True)
check("14 restart continues the same day row", rec2.db.execute(
    "SELECT prompt_processed FROM daily ORDER BY day DESC LIMIT 1").fetchone()[0], 65.0)
check("14 restart does not re-seed", rec2.all_time()["prompt_processed"], 230.0)

import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    W.print_tokens(rec, days=7, alltime=True)
out = buf.getvalue()
check("14 human report renders", "ALL TIME" in out and "in TOTAL" in out, True)
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    W.print_tokens(rec, as_csv=True)
check("14 csv report has a header and every day",
      buf2.getvalue().splitlines()[0].startswith("day,prompt_processed") and len(buf2.getvalue().strip().splitlines()) == 3, True)
check("14 ledger file stays tiny", os.path.getsize(dbp) < 4096 * 10, True)

# ---- 12. --dry-run actually reaches the Watcher -------------------------------
# This was a real bug: the flag was parsed, then dropped on the floor, so a "safe"
# trial was quietly publishing for real. A trial that lies about being a trial is the
# worst possible failure mode for this program, so it gets a test of its own.
class SpyWatcher:
    last = None

    def __init__(self, cfg, log, dry_run=False):
        SpyWatcher.last = self
        self.dry_run = dry_run
        self.cfg = cfg

    def poll_once(self, allow_notify=True):
        return {"ok": True, "idle": True, "why": "spy"}


_real_watcher = W.Watcher
W.Watcher = SpyWatcher
try:
    W.main(["--config", "/nonexistent.conf", "--dry-run", "--once"])
    check("12 --dry-run sets Watcher.dry_run", SpyWatcher.last.dry_run, True)
    W.main(["--config", "/nonexistent.conf", "--once"])
    check("12 without the flag it stays live", SpyWatcher.last.dry_run, False)
finally:
    W.Watcher = _real_watcher

print()
failed = [r for r in results if not r[1]]
print("%d checks, %d failed" % (len(results), len(failed)))
sys.exit(1 if failed else 0)
