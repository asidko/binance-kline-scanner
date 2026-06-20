import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner


def small(o, c):
    return {"o": o, "h": max(o, c) + 0.2, "l": min(o, c) - 0.2, "c": c}


def window(run_len, body, trailing, start=100.0):
    candles = [small(100, 101), small(101, 100)] * 5
    p = start
    for _ in range(run_len):
        candles.append({"o": p, "h": p + body + 1, "l": p - 1, "c": p + body})
        p += body
    for _ in range(trailing):
        candles.append(small(p, p - 0.3))
    return candles


WINDOWS = {
    "A": window(3, 5, trailing=4),                 # age 4, len 3
    "B": window(3, 5, trailing=1),                 # age 1, len 3  (fresher)
    "C": window(3, 5, trailing=2),                 # age 2, len 3
    "D": window(5, 5, trailing=2),                 # age 2, len 5  (longer)
    "E": window(3, 5, trailing=2),                 # age 2, len 3, small bodies
    "F": window(3, 9, trailing=2),                 # age 2, len 3, big bodies
    "G": window(3, 5, trailing=0) + [{"o": 115, "h": 116, "l": 108, "c": 109}],  # later close 109 < run floor 110 reclaims -> stale
    "AAA": window(3, 5, trailing=2),               # identical to ZZZ -> tiebreak by symbol
    "ZZZ": window(3, 5, trailing=2),
    "LVL": window(3, 5, trailing=0) + [small(115, 114), small(114, 115)],  # red+green after -> level
    "ONG": window(3, 5, trailing=0),               # run at tip, nothing after -> ongoing
    "P": window(3, 5, trailing=6),                 # age 6, len 3  (same age band as Q)
    "Q": window(5, 5, trailing=8),                 # age 8, len 5  (older but longer)
    "WK": [small(100, 101), small(101, 100)] * 5 + [          # large/weak/large up run
        {"o": 100, "h": 106, "l": 99, "c": 105},
        {"o": 105, "h": 106, "l": 104, "c": 106},            # weak interior (body 1 < 1.5)
        {"o": 106, "h": 112, "l": 105, "c": 111},
    ] + [small(111, 110.7)] * 2,
}


def fake_fetch(symbol, interval, limit):
    if symbol == "BAD":
        raise RuntimeError("boom")
    return WINDOWS[symbol]


def run_scan(symbols, fresh=True, type_filter="both", dominance=0.5):
    return scanner.scan(symbols, fake_fetch, 3, dominance, "median-body", 1.5, 14, "up", type_filter, fresh, False, "15m", 40)


ok = 0

results, errors = run_scan(["A", "B"])
assert [r["symbol"] for r in results] == ["B", "A"] and not errors, results
print("PASS factor 1: more recent cascade (smaller age) ranks first"); ok += 1

results, _ = run_scan(["C", "D"])
assert [r["symbol"] for r in results] == ["D", "C"], results
print("PASS factor 2: same age, longer run ranks first"); ok += 1

results, _ = run_scan(["E", "F"])
assert [r["symbol"] for r in results] == ["F", "E"], [(r["symbol"], r["runs"][0]["body_mult_mean"]) for r in results]
print("PASS factor 3: same age+length, bigger bodies rank first"); ok += 1

ab = scanner.det.age_bucket
assert ab(0) < ab(3) < ab(5), (ab(0), ab(3), ab(5))                       # 0-5 stay distinct
assert ab(6) == ab(8) and ab(6) > ab(5), (ab(5), ab(6), ab(8))            # 6-8 collapse, above the granular zone
assert ab(8) < ab(9), (ab(8), ab(9))                                      # 6-8 band ranks ahead of 9-10 band
assert ab(9) == ab(10) and ab(11) == ab(15) and ab(11) > ab(10), (ab(9), ab(10), ab(11), ab(15))
assert ab(8) < ab(11) < ab(16), (ab(8), ab(11), ab(16))                   # bands monotonic with age
print("PASS age_bucket: granular 0-5, widening bands (6-8, 9-10, 11-15, ...)"); ok += 1

results, _ = run_scan(["P", "Q"])
assert [r["symbol"] for r in results] == ["Q", "P"], results              # same band (6-8): longer Q beats fresher-but-shorter P
print("PASS age band: within a band, length breaks the near-tie (older+longer > fresher+shorter)"); ok += 1

results, _ = run_scan(["G"], fresh=True)
assert results == [], results
results, _ = run_scan(["G"], fresh=False)
assert len(results) == 1 and results[0]["runs"][0]["fresh"] is False, results
print("PASS fresh default excludes broken base; --all includes it (stale)"); ok += 1

results, errors = run_scan(["BAD", "B"])
assert [r["symbol"] for r in results] == ["B"] and [e["symbol"] for e in errors] == ["BAD"], (results, errors)
print("PASS per-symbol fetch error isolated, scan continues"); ok += 1

assert scanner.load_symbols("doge,sol , xrp", scanner.DEFAULT_SYMBOLS_FILE) == ["DOGE", "SOL", "XRP"]
from_file = scanner.load_symbols(None, scanner.DEFAULT_SYMBOLS_FILE)
assert "SOLUSDT" in from_file and "BTCUSDT" not in from_file and "ETHUSDT" not in from_file, from_file
print("PASS load_symbols: --symbols parse + curated file (alts, no BTC/ETH)"); ok += 1

results, _ = run_scan(["ZZZ", "AAA"])
assert [r["symbol"] for r in results] == ["AAA", "ZZZ"], results
print("PASS deterministic symbol tiebreak when age+length+body tie"); ok += 1


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self._b


scanner.urllib.request.urlopen = lambda url, timeout=10: _FakeResp(
    [[0, "1.5", "2.0", "1.0", "1.8", "9"], [1, "9", "9", "9", "9", "9"]])  # 2nd row = the still-forming candle
fk = scanner.fetch_klines("X", "15m", 40)
assert fk == [{"o": 1.5, "h": 2.0, "l": 1.0, "c": 1.8}] and all(isinstance(v, float) for v in fk[0].values()), fk
print("PASS fetch_klines casts to float and drops the unclosed last kline"); ok += 1

r, _ = run_scan(["LVL"], type_filter="level")
assert [x["symbol"] for x in r] == ["LVL"] and r[0]["runs"][0]["type"] == "level", r
assert r[0]["runs"][0]["level"] == 114 and r[0]["runs"][0]["base"] == 99, r[0]["runs"][0]
assert run_scan(["LVL"], type_filter="ongoing")[0] == [], "level must not match --type ongoing"
r, _ = run_scan(["ONG"], type_filter="ongoing")
assert [x["symbol"] for x in r] == ["ONG"] and r[0]["runs"][0]["type"] == "ongoing", r
assert r[0]["runs"][0]["level"] is None, r[0]["runs"][0]
assert run_scan(["ONG"], type_filter="level")[0] == [], "ongoing must not match --type level"
print("PASS --type filters level vs ongoing; level price set for level, null for ongoing"); ok += 1

rs, errs = run_scan(["LVL", "ONG"], type_filter="both")
out = {"params": {"count": 3, "dominance": 0.5, "metric": "median-body", "k": 1.5, "direction": "up",
                  "type": "both", "fresh_required": True, "interval": "15m", "limit": 40},
       "scanned": 2, "matched_count": len(rs), "elapsed_s": 0.0, "errors": errs, "results": rs}
txt = scanner.render_text(out)
header, rows = txt.splitlines()[1], {ln.split()[0]: ln.split() for ln in txt.splitlines() if ln.strip()[:3] in ("LVL", "ONG")}
assert "LEVEL" in header, header
assert rows["LVL"][5] == "99" and rows["LVL"][6] == "114", rows["LVL"]   # base col, level col
assert rows["ONG"][6] == "-", rows["ONG"]                                  # ongoing -> dash
print("PASS scanner text renders LEVEL column: level price for level run, '-' for ongoing"); ok += 1

r, _ = run_scan(["WK"], dominance=0.5)
assert [x["symbol"] for x in r] == ["WK"] and r[0]["runs"][0]["length"] == 3, r
assert run_scan(["WK"], dominance=1.0)[0] == [], "dominance=1.0 must reject a weak interior candle"
print("PASS dominance: majority-large run passes at 0.5, rejected at 1.0"); ok += 1

print(f"\nALL {ok} SCANNER TESTS PASSED")
