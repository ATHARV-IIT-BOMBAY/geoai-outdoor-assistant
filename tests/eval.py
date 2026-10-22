"""Run the labelled set against a live API and print metrics.

    python tests/eval.py [base_url] [label] [official|messy|all]

Writes per-question results to tests/results/<label>.jsonl. Run it before and after every change.
"""
import json
import math
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from eval_set import ALL, MESSY, OFFICIAL  # noqa: E402

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
BANNED = re.compile(r"\b(?:safe|unsafe|acceptable|healthy|pleasant|perfect|ideal|great|nice|beautiful|popular|open|"
                    r"closed|scenic|famous|crowded)\b", re.I)


def km(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def day_ok(want, start, today):
    """The first planned day matches; a window that already passed may roll to the next day."""
    d = datetime.fromisoformat(start).date()
    if want in WEEKDAYS:
        return WEEKDAYS[d.weekday()] == want
    off = {"today": 0, "tomorrow": 1, "day after tomorrow": 2}[want]
    return d in (today + timedelta(days=off), today + timedelta(days=off + 1)) if off == 0 else d == today + timedelta(days=off)


def check(case, js, today):
    """Dict of check name -> True/False (only for checks the label asks for)."""
    p, out = js.get("parsed") or {}, {}
    out["status"] = js.get("status") == case["status"]
    if "activity" in case:
        out["activity"] = p.get("activity") == case["activity"]
    if "profiles" in case and js.get("status") == "ok":
        out["profiles"] = sorted(p.get("profiles") or []) == sorted(case["profiles"])
    if "near" in case and js.get("status") == "ok":
        o = js.get("origin") or {}
        out["place"] = bool(o) and km((o["lat"], o["lon"]), case["near"][:2]) <= case["near"][2]
    if "day" in case and js.get("status") == "ok":
        out["day"] = day_ok(case["day"], p["start"], today)
    if case.get("compare"):
        out["compare"] = len(js.get("comparison") or []) >= 2
    return out


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    label = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("run-%m%d-%H%M")
    cases = {"official": OFFICIAL, "messy": MESSY}.get(sys.argv[3] if len(sys.argv) > 3 else "all", ALL)
    today = date.today()
    rows = []
    out_path = Path(__file__).parent / "results" / f"{label}.jsonl"
    out_path.parent.mkdir(exist_ok=True)
    with httpx.Client(timeout=240) as c, out_path.open("w") as f:
        for case in cases:
            t = time.perf_counter()
            try:
                js = c.post(f"{base}/ask", json={"q": case["q"]}).json()
            except Exception as e:  # noqa: BLE001 - a crash is a result too
                js = {"status": "exception", "error": repr(e)}
            secs = round(time.perf_counter() - t, 1)
            checks = check(case, js, today)
            row = {"q": case["q"][:120], "secs": secs, "checks": checks, "status": js.get("status"),
                   "writer": js.get("writer"), "caution": js.get("caution"), "searched": js.get("searched"),
                   "summary": (js.get("summary") or "")[:400], "error": js.get("error"),
                   "parsed": {k: (js.get("parsed") or {}).get(k) for k in ("activity", "place", "profiles", "start", "end")}}
            rows.append(row)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            bad = [k for k, v in checks.items() if not v]
            print(f"{'PASS' if not bad else 'FAIL ' + ','.join(bad):28} {secs:5.1f}s  {case['q'][:70]}", flush=True)

    # ---- metrics ----
    def rate(name):
        vals = [r["checks"][name] for r in rows if name in r["checks"]]
        return f"{sum(vals)}/{len(vals)} ({100 * sum(vals) / max(1, len(vals)):.0f}%)"

    oks = [r for r in rows if r["status"] == "ok"]
    plain = [r for r in oks if not r["caution"]]
    lat = sorted(r["secs"] for r in rows if r["status"] == "ok")
    pct = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] if lat else 0
    print(f"\n## Eval {label} ({len(rows)} questions, {today})\n")
    print("| metric | value |\n|---|---|")
    for name in ("status", "activity", "place", "day", "profiles", "compare"):
        print(f"| {name} correct | {rate(name)} |")
    print(f"| all checks pass | {sum(all(r['checks'].values()) for r in rows)}/{len(rows)} |")
    print(f"| wrong-city answers | {sum(1 for r in rows if r['checks'].get('place') is False)} |")
    print(f"| errors / exceptions | {sum(1 for r in rows if r['status'] in ('error', 'exception'))} |")
    print(f"| model summary used (ok, no caution) | {sum(r['writer'] == 'slm' for r in plain)}/{len(plain)} |")
    print(f"| banned words in model summaries | {sum(bool(BANNED.search(r['summary'])) for r in oks if r['writer'] == 'slm')} |")
    print(f"| latency of ok answers p50 / p95 | {pct(0.5)} s / {pct(0.95)} s |")


if __name__ == "__main__":
    main()
