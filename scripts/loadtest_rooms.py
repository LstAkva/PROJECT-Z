"""
Load test for Jonli xona (live rooms): N simulated teams join a room, poll it like phones do (every 1.5 s),
answer each open question once after a short "thinking" delay, and one projector screen polls too.
Reports response times (p50 / p95 / max) and errors per endpoint.

Needs phone joining switched on for the target (ROOMS_PLAYER_JOIN=1).

Against the live site (no password needed): open a room at /host, then run
    python scripts/loadtest_rooms.py --base https://zakowhat.uz --code K7Q2MX --teams 40 --minutes 3
and advance the questions yourself (Space on the console). Teams are named "Yuk 01".. so you can remove them.

Locally, the script can also drive the host:  --host-token <owner session token>  (advances every 20 s).
"""
import argparse
import random
import statistics
import sys
import threading
import time
from collections import defaultdict

import requests

lock = threading.Lock()
timings = defaultdict(list)
codes = defaultdict(lambda: defaultdict(int))
stop = threading.Event()


def record(name, started, res=None, error=None):
    took = (time.perf_counter() - started) * 1000
    with lock:
        timings[name].append(took)
        codes[name][res.status_code if res is not None else f"ERR {type(error).__name__}"] += 1


def call(session, method, url, name, **kw):
    started = time.perf_counter()
    try:
        res = session.request(method, url, timeout=20, **kw)
        record(name, started, res)
        return res
    except Exception as e:  # network errors count as failures, the run goes on
        record(name, started, error=e)
        return None


def team(base, code, n, poll):
    s = requests.Session()
    s.headers["User-Agent"] = "zakowhat-loadtest"
    time.sleep(random.uniform(0, 5))                     # people do not all join in the same second
    res = call(s, "POST", f"{base}/api/rooms/join", "join", json={"code": code, "team_name": f"Yuk {n:02d}"})
    if res is None or res.status_code != 201:
        return
    token = res.json()["token"]
    headers = {"X-Team-Token": token}
    answered = set()
    while not stop.is_set():
        r = call(s, "GET", f"{base}/api/rooms/{code}/me", "poll /me", headers=headers)
        if r is not None and r.status_code == 200:
            st = r.json()
            q = st.get("question_number")
            if st.get("status") == "question" and st.get("answer_open") and q not in answered:
                answered.add(q)
                threading.Timer(random.uniform(3, 20), lambda: call(
                    s, "POST", f"{base}/api/rooms/{code}/answer", "answer", headers=headers,
                    json={"text": random.choice(["Javob 1", "bilmadim", "Toshkent", "Javob 2"])})).start()
            if st.get("status") == "finished":
                return
        time.sleep(poll * random.uniform(0.9, 1.1))


def screen(base, code, poll):
    s = requests.Session()
    while not stop.is_set():
        call(s, "GET", f"{base}/api/rooms/{code}/screen", "poll /screen")
        time.sleep(poll)


def host_driver(base, code, token, every):
    s = requests.Session()
    s.cookies.set("access_token", token)
    call(s, "POST", f"{base}/api/rooms/{code}/start", "host")
    while not stop.wait(every):
        st = s.get(f"{base}/api/rooms/{code}/host", timeout=20).json()
        if st.get("status") == "question" and st.get("clock_waiting"):
            call(s, "POST", f"{base}/api/rooms/{code}/timer", "host", json={"action": "resume"})
        elif st.get("status") == "question":
            call(s, "POST", f"{base}/api/rooms/{code}/reveal", "host")
        elif st.get("status") == "reveal":
            call(s, "POST", f"{base}/api/rooms/{code}/next", "host")


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True)
    ap.add_argument("--code", required=True)
    ap.add_argument("--teams", type=int, default=40)
    ap.add_argument("--minutes", type=float, default=3)
    ap.add_argument("--poll", type=float, default=1.5)
    ap.add_argument("--host-token", default=None, help="local runs only: owner session token to advance questions")
    ap.add_argument("--advance-every", type=float, default=20)
    a = ap.parse_args(argv)
    base = a.base.rstrip("/")
    threads = [threading.Thread(target=team, args=(base, a.code.upper(), n + 1, a.poll), daemon=True) for n in range(a.teams)]
    threads.append(threading.Thread(target=screen, args=(base, a.code.upper(), a.poll), daemon=True))
    if a.host_token:
        threads.append(threading.Thread(target=host_driver, args=(base, a.code.upper(), a.host_token, a.advance_every), daemon=True))
    for t in threads:
        t.start()
    started = time.time()
    try:
        while time.time() - started < a.minutes * 60:
            time.sleep(10)
            with lock:
                total = sum(len(v) for v in timings.values())
                bad = sum(n for name in codes for c, n in codes[name].items() if not (isinstance(c, int) and c < 400))
            print(f"  {int(time.time() - started):>4}s  requests={total}  failed={bad}", flush=True)
    finally:
        stop.set()
    print(f"\n{a.teams} teams, {a.minutes} min, poll every {a.poll}s against {base}")
    print(f"{'endpoint':<14}{'count':>7}{'p50 ms':>9}{'p95 ms':>9}{'max ms':>9}   status codes")
    for name in sorted(timings):
        v = timings[name]
        print(f"{name:<14}{len(v):>7}{statistics.median(v):>9.0f}{pct(v, 95):>9.0f}{max(v):>9.0f}   {dict(codes[name])}")
    failures = sum(n for name in codes for c, n in codes[name].items() if not (isinstance(c, int) and c < 400))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
