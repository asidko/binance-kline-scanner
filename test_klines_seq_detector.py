import json, os, subprocess, sys

DET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "klines_seq_detector.py")


def run(window, *flags, code=None):
    p = subprocess.run([sys.executable, DET, *flags], input=json.dumps(window),
                       capture_output=True, text=True)
    if code is not None:
        assert p.returncode == code, f"exit {p.returncode} != {code}; stderr={p.stderr}"
    return p


def js(window, *flags, code=None):
    return json.loads(run(window, *flags, code=code).stdout)


def small(o, c):
    return {"o": o, "h": max(o, c) + 0.2, "l": min(o, c) - 0.2, "c": c}


baseline = [small(100, 101), small(101, 100)] * 5
up_run = [{"o": 100, "h": 106, "l": 99, "c": 105},
          {"o": 105, "h": 111, "l": 104, "c": 110},
          {"o": 110, "h": 116, "l": 109, "c": 115}]
fresh_tail = [{"o": 115, "h": 116, "l": 108, "c": 112}]    # closes 112 >= run floor 110 -> nothing reclaimed -> fresh
broken_tail = [{"o": 115, "h": 116, "l": 108, "c": 109}]   # closes 109 < floor 110 -> reclaims a run candle -> stale (wick low same, only close differs)

ok = 0

r = js(baseline + up_run + fresh_tail, "--direction", "up", "--fresh", code=0)
assert r["schema"] == 1 and r["matched"] is True and len(r["runs"]) == 1, r
run0 = r["runs"][0]
assert run0["base"] == 99 and run0["fresh"] is True and run0["length"] == 3, run0
assert run0["age"] == 1 and run0["body_mult_mean"] is not None and len(run0["body_mults"]) == 3, run0
assert run0["candles"] is None, run0
assert set(r["stats"]) == {"window", "unit", "threshold", "atr_period"} and r["error"] is None and r["warning"] is None, r
print("PASS fresh up-run: matched exit 0, stable schema, age/dominance present"); ok += 1

r = js(baseline + up_run + broken_tail, "--direction", "up", "--fresh", code=1)
assert r["matched"] is False and r["runs"] == [], r
print("PASS broken run filtered under --fresh, exit 1"); ok += 1

r = js(baseline + up_run + broken_tail, "--direction", "up", code=0)
assert r["matched"] is True and r["runs"][0]["fresh"] is False, r
print("PASS broken run reported (fresh=False) without --fresh, exit 0"); ok += 1

r = js(baseline + up_run + fresh_tail, "--direction", "down", "--fresh", code=1)
assert r["matched"] is False, r
print("PASS direction=down excludes up-run, exit 1"); ok += 1

r = js(up_run, "--count", "4", code=1)
assert r["matched"] is False, r
print("PASS count=4 unmet, exit 1"); ok += 1

weak_run = [{"o": 100, "h": 106, "l": 99, "c": 105},
            {"o": 105, "h": 106, "l": 104, "c": 106},   # weak interior (body 1 < 1.5 threshold)
            {"o": 106, "h": 112, "l": 105, "c": 111}]
r = js(baseline + weak_run, "--direction", "up", code=0)
assert r["matched"] is True and r["runs"][0]["length"] == 3 and r["params"]["dominance"] == 0.5, r
r = js(baseline + weak_run, "--direction", "up", "--dominance", "1", code=1)
assert r["matched"] is False, r
print("PASS dominance: weak interior tolerated at 0.5, rejected at 1.0"); ok += 1

r = js(baseline + up_run, "--dominance", "0", code=2)
assert r["error"] and "dominance" in r["error"], r
print("PASS dominance out of (0,1] -> error exit 2"); ok += 1

lvl_tail = [small(115, 114), small(114, 115)]   # red then green after -> reaction/consolidation
r = js(baseline + up_run + lvl_tail, "--direction", "up", code=0)
run0 = r["runs"][0]
# level = body floor 114 nudged down by 0.33 * median low-wick (0.2) = 113.934; base = lowest low 99
assert run0["type"] == "level" and round(run0["level"], 4) == 113.934 and run0["base"] == 99, run0
down_run = [{"o": 100, "h": 101, "l": 94, "c": 95},
            {"o": 95, "h": 96, "l": 89, "c": 90},
            {"o": 90, "h": 91, "l": 84, "c": 85}]
run0 = js(baseline + down_run + [small(85, 86), small(86, 85)], "--direction", "down", code=0)["runs"][0]
# level = body ceiling 86 nudged up by 0.33 * median top-wick (0.2) = 86.066; base = highest high 101
assert run0["type"] == "level" and round(run0["level"], 4) == 86.066 and run0["base"] == 101, run0
stale_lvl = [{"o": 115, "h": 116, "l": 108, "c": 109}, small(110, 111)]   # red closes 109 < floor 110 (reclaim -> stale), then green -> level
run0 = js(baseline + up_run + stale_lvl, "--direction", "up", code=0)["runs"][0]
# body floor min(109,110)=109 nudged down by 0.33 * median underhang (median of 1.0 and 0.2 = 0.6) = 108.802
assert run0["fresh"] is False and run0["type"] == "level" and round(run0["level"], 4) == 108.802, run0   # level set regardless of freshness
r = js(baseline + up_run, "--direction", "up", code=0)
assert r["runs"][0]["type"] == "ongoing" and r["runs"][0]["level"] is None, r["runs"][0]
print("PASS level = body edge nudged into the typical wick (up/down/stale), null for ongoing"); ok += 1

p = run(baseline + up_run + lvl_tail, "--direction", "up", "--format", "text", code=0)
assert "level" in p.stdout and "113.934" in p.stdout, p.stdout   # truthy fmt_price branch in render_text
print("PASS --format text renders the level column for a level run"); ok += 1

p = run([], code=1)
assert json.loads(p.stdout)["error"] is None and json.loads(p.stdout)["stats"]["window"] == 0
p = subprocess.run([sys.executable, DET], input="not json", capture_output=True, text=True)
assert p.returncode == 2 and json.loads(p.stdout)["error"], p.stdout
p = subprocess.run([sys.executable, DET, "--exit-zero"], input="not json", capture_output=True, text=True)
assert p.returncode == 0 and json.loads(p.stdout)["error"], p.stdout
print("PASS parse error -> exit 2 (0 with --exit-zero), error field set"); ok += 1

r = js([small(100, 101)], code=1)
assert r["matched"] is False and r["stats"]["window"] == 1, r
print("PASS window < count -> no match, exit 1"); ok += 1

mid = [small(115, 114), small(114, 115)]
up_run2 = [{"o": 115, "h": 121, "l": 114, "c": 120},
           {"o": 120, "h": 126, "l": 119, "c": 125},
           {"o": 125, "h": 131, "l": 124, "c": 130}]
r = js(baseline + up_run + mid + up_run2 + [small(130, 129)], "--direction", "up", code=0)
assert len(r["runs"]) == 2, r
assert r["runs"][0]["age"] < r["runs"][1]["age"], [x["age"] for x in r["runs"]]
assert r["runs"][0]["start"] > r["runs"][1]["start"], r
print("PASS two runs sorted freshest-first (age ascending)"); ok += 1

r = js(baseline + up_run + fresh_tail, "--direction", "up", "--candles", code=0)
c0 = r["runs"][0]["candles"]
assert c0 and c0[0]["i"] == r["runs"][0]["start"] and "body_mult" in c0[0], c0
print("PASS --candles attaches full OHLC with absolute index"); ok += 1

p = run(baseline + up_run + fresh_tail, "--direction", "up", "--fresh", "--format", "text", code=0)
assert p.stdout.startswith("Matched 1 run."), p.stdout
try:
    json.loads(p.stdout)
    assert False, "text should not be JSON"
except json.JSONDecodeError:
    pass
print("PASS --format text renders human output, not JSON"); ok += 1

flat = [{"o": 10, "h": 10.1, "l": 9.9, "c": 10}] * 12
r = js(flat, code=1)
assert r["matched"] is False and r["warning"] and "baseline" in r["warning"], r
print("PASS flat window (median body 0) -> no match + baseline warning"); ok += 1

print(f"\nALL {ok} TESTS PASSED")
