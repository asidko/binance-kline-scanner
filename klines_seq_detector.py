#!/usr/bin/env python3
"""klines_seq_detector.py - detect runs of N consecutive, mostly-large same-color candles.

Standalone filter: reads an OHLC window as JSON on stdin, prints a verdict on stdout.
Knows nothing about coins, exchanges, or time - pure candle math on array indices.
Built to be called in a loop, one window per invocation, to screen for impulse setups.

Usage:
  echo '[{"o":1,"h":2,"l":0.9,"c":1.8},...]' | ./klines_seq_detector.py
  ./klines_seq_detector.py --direction down --fresh --format text < window.json

Input  : JSON list of candles, oldest first. Each is {o,h,l,c} (or open/high/low/close),
         or a raw Binance kline array [openTime,o,h,l,c,...]. Numbers or numeric strings.
Output : runs ranked by recency band, then length, then body size. JSON (default) or --format text.
Exit   : 0 = matched, 1 = no match, 2 = error (use --exit-zero to always exit 0).

Options:
  --count <n>        candles in a run (default 3)
  --dominance <f>    fraction of a run's candles that must be 'large' (default 0.5):
                     1.0 = every candle large, 0.5 = majority. Weak ends are trimmed.
  --metric <name>    'large' yardstick: median-body (default) | atr
  --k <float>        body must be >= K * yardstick (default 1.5 median-body, 0.9 atr)
  --atr-period <n>   ATR lookback, atr metric only (default 14)
  --direction <dir>  both (default) | up | down
  --type <t>         both (default) | ongoing (one color after) | level (red+green after)
  --fresh            only runs whose base is not yet crossed by a later wick
  --candles          include each run's full OHLC (default: body multiples only)
  --format <fmt>     json (default) | text
  --exit-zero        always exit 0 (default uses grep-style 0/1/2 codes)

Examples:
  curl -s 'https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=15m&limit=50' | ./klines_seq_detector.py --fresh
  cat window.json | ./klines_seq_detector.py --direction down --format text
"""
import argparse
import json
import math
import statistics
import sys

SCHEMA = 1

DEFAULT_K = {"median-body": 1.5, "atr": 0.9}


def parse_candles(raw: str) -> list[dict]:
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("input must be a JSON list of candles")
    out: list[dict] = []
    for i, k in enumerate(data):
        if isinstance(k, dict):
            o, h, lo, c = k.get("o", k.get("open")), k.get("h", k.get("high")), k.get("l", k.get("low")), k.get("c", k.get("close"))
        elif isinstance(k, (list, tuple)) and len(k) >= 5:
            o, h, lo, c = k[1], k[2], k[3], k[4]
        else:
            raise ValueError(f"candle {i}: expected an object or a Binance kline array")
        if None in (o, h, lo, c):
            raise ValueError(f"candle {i}: missing one of o/h/l/c")
        out.append({"o": float(o), "h": float(h), "l": float(lo), "c": float(c)})
    return out


def body(k: dict) -> float:
    return abs(k["c"] - k["o"])


def direction(k: dict) -> int:
    return 1 if k["c"] > k["o"] else (-1 if k["c"] < k["o"] else 0)


def _median_body_ref(candles: list[dict], k: float, atr_period: int) -> dict:
    med = statistics.median([body(c) for c in candles]) if candles else 0.0
    return {"metric": "median-body", "unit": med, "threshold": k * med, "atr_period": None}


def _true_range(cur: dict, prev: dict | None) -> float:
    if prev is None:
        return cur["h"] - cur["l"]
    return max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"]))


def _atr_ref(candles: list[dict], k: float, atr_period: int) -> dict:
    trs = [_true_range(candles[i], candles[i - 1] if i else None) for i in range(len(candles))]
    window = trs[-atr_period:] if len(trs) >= atr_period else trs
    atr = sum(window) / len(window) if window else 0.0
    return {"metric": "atr", "unit": atr, "threshold": k * atr, "atr_period": atr_period}


METRICS = {"median-body": _median_body_ref, "atr": _atr_ref}


def large_flags(candles: list[dict], threshold: float) -> list[bool]:
    if threshold <= 0:
        return [False] * len(candles)
    return [body(c) >= threshold for c in candles]


def find_runs(candles: list[dict], large: list[bool], count: int, dominance: float) -> list[tuple[int, int, int]]:
    runs: list[tuple[int, int, int]] = []
    n = len(candles)
    i = 0
    while i < n:
        d = direction(candles[i])
        if d == 0:
            i += 1
            continue
        j = i
        while j + 1 < n and direction(candles[j + 1]) == d:
            j += 1
        s, e = i, j
        while s <= e and not large[s]:
            s += 1
        while e >= s and not large[e]:
            e -= 1
        length = e - s + 1
        if s <= e and length >= count and sum(large[s:e + 1]) >= dominance * length:
            runs.append((s, e, d))
        i = j + 1
    return runs


def run_base(seg: list[dict], d: int) -> float:
    return min(c["l"] for c in seg) if d == 1 else max(c["h"] for c in seg)


LEVEL_WICK_BUFFER = 0.33  # fraction of the typical wick to leave between the level and the body cluster


def level_price(after: list[dict], d: int, buffer: float = LEVEL_WICK_BUFFER) -> float:
    # break level = the consolidation body edge nudged toward the wicks by a fraction of the TYPICAL
    # (median) wick overhang, so it sits in the gap above/below the bodies, short of the wick tips.
    # anchored at the body extreme (never cuts into the bodies), clamped to the wick extreme (never past
    # it). median, not max, so one spike wick can't drag it out. down-run = resistance, up-run = support.
    if d == 1:
        floor = min(min(c["o"], c["c"]) for c in after)
        gap = buffer * statistics.median([min(c["o"], c["c"]) - c["l"] for c in after])
        return max(floor - gap, min(c["l"] for c in after))
    ceil = max(max(c["o"], c["c"]) for c in after)
    gap = buffer * statistics.median([c["h"] - max(c["o"], c["c"]) for c in after])
    return min(ceil + gap, max(c["h"] for c in after))


AGE_BANDS = (8, 10, 15, 20, 25, 30, 40, 60)


def age_bucket(age: int) -> int:
    # ages 0-5 stay distinct (recency rules there); older ages collapse into widening bands so
    # length/body can break a near-tie - "age 8 vs 10 is basically the same setup"
    if age <= 5:
        return age
    for i, hi in enumerate(AGE_BANDS):
        if age <= hi:
            return 6 + i
    return 6 + len(AGE_BANDS)


def rank_key(run: dict) -> tuple:
    return (age_bucket(run["age"]), -run["length"], -(run["body_mult_mean"] or 0), run["age"])


def is_fresh(candles: list[dict], start: int, end: int, d: int) -> bool:
    # fresh only if NO run candle is reclaimed: a later candle must CLOSE past a run candle's far
    # body edge to break it (wicks poking in are fine). One reclaimed candle makes the run stale.
    seg = candles[start:end + 1]
    after = candles[end + 1:]
    if d == 1:  # up-run: a close below the run's highest body bottom reclaims that candle
        floor = max(min(c["o"], c["c"]) for c in seg)
        return all(k["c"] >= floor for k in after)
    ceil = min(max(c["o"], c["c"]) for c in seg)  # down-run: a close above the lowest body top reclaims it
    return all(k["c"] <= ceil for k in after)


def run_type(candles: list[dict], end: int) -> str:
    after = {direction(c) for c in candles[end + 1:]}
    return "level" if 1 in after and -1 in after else "ongoing"


def detect(candles: list[dict], count: int, dominance: float, ref: dict, direction_filter: str,
           fresh_only: bool, with_candles: bool) -> list[dict]:
    large = large_flags(candles, ref["threshold"])
    unit = ref["unit"]
    n = len(candles)
    out: list[dict] = []
    for start, end, d in find_runs(candles, large, count, dominance):
        dname = "up" if d == 1 else "down"
        if direction_filter != "both" and dname != direction_filter:
            continue
        seg = candles[start:end + 1]
        base = run_base(seg, d)
        fresh = is_fresh(candles, start, end, d)
        if fresh_only and not fresh:
            continue
        rtype = run_type(candles, end)
        mults = [round(body(c) / unit, 2) if unit else None for c in seg]
        mean_mult = round(sum(mults) / len(mults), 2) if all(m is not None for m in mults) else None
        run = {"direction": dname, "type": rtype, "start": start, "end": end,
               "length": end - start + 1, "age": n - 1 - end, "base": base,
               "level": level_price(candles[end + 1:], d) if rtype == "level" else None, "fresh": fresh,
               "body_mult_mean": mean_mult, "body_mults": mults, "candles": None}
        if with_candles:
            run["candles"] = [{"i": start + x, **{f: seg[x][f] for f in ("o", "h", "l", "c")},
                               "body_mult": mults[x]} for x in range(len(seg))]
        out.append(run)
    out.sort(key=rank_key)
    return out


def _skeleton(count: int, dominance: float, metric: str, k: float, direction: str, type_filter: str, fresh: bool) -> dict:
    return {
        "schema": SCHEMA, "matched": False,
        "params": {"count": count, "dominance": dominance, "metric": metric, "k": k, "direction": direction,
                   "type": type_filter, "fresh_required": fresh},
        "stats": {"window": 0, "unit": None, "threshold": None, "atr_period": None},
        "runs": [], "error": None, "warning": None,
    }


def run_detection(candles: list[dict], count: int, dominance: float, metric: str, k: float, atr_period: int,
                  direction: str, type_filter: str, fresh: bool, with_candles: bool) -> dict:
    result = _skeleton(count, dominance, metric, k, direction, type_filter, fresh)
    result["stats"]["window"] = len(candles)
    if count < 1:
        result["error"] = "count must be >= 1"
        return result
    if not 0 < dominance <= 1:
        result["error"] = "dominance must be in (0, 1]"
        return result
    if len(candles) >= count:
        ref = METRICS[metric](candles, k, atr_period)
        result["stats"].update({"unit": ref["unit"], "threshold": ref["threshold"], "atr_period": ref["atr_period"]})
        runs = detect(candles, count, dominance, ref, direction, fresh, with_candles)
        if type_filter != "both":
            runs = [r for r in runs if r["type"] == type_filter]
        result["runs"] = runs
        result["matched"] = len(runs) > 0
        if ref["unit"] <= 0:
            result["warning"] = "no body baseline (flat window); nothing flagged"
        elif len(candles) < 10:
            result["warning"] = "window < 10 candles; 'large' baseline is unreliable"
    return result


def fmt_price(x: float) -> str:
    if x == 0:
        return "0"
    digits = max(0, 5 - int(math.floor(math.log10(abs(x)))))
    return f"{x:.{digits}f}".rstrip("0").rstrip(".")


def render_text(result: dict) -> str:
    p, s, runs = result["params"], result["stats"], result["runs"]
    dirn = p["direction"] if p["direction"] != "both" else "same-color"
    lines: list[str] = []
    if not runs:
        lines.append(f"No match. {p['count']}+ {dirn} {p['metric']} candles required "
                     f"(k={p['k']:g}, dom={p['dominance']:g}, {s['window']}-candle window).")
    else:
        fresh_req = ", fresh required" if p["fresh_required"] else ""
        lines.append(f"Matched {len(runs)} run{'s' if len(runs) != 1 else ''}. "
                     f"{p['count']}+ {dirn} {p['metric']} candles, k={p['k']:g}, dom={p['dominance']:g}{fresh_req}, "
                     f"{s['window']}-candle window. unit={s['unit']:.4g}, threshold={s['threshold']:.4g}.")
        for r in runs:
            avgx = f"{r['body_mult_mean']:g}" if r["body_mult_mean"] is not None else "-"
            bodies = "/".join(f"{m:g}" if m is not None else "-" for m in r["body_mults"])
            lvl = fmt_price(r["level"]) if r["level"] is not None else "-"
            lines.append(f"  {r['direction']:<4} {r['type']:<7} level {lvl:>12} [{r['start']}-{r['end']}]  len {r['length']:<2} "
                         f"age {r['age']:<3} base {fmt_price(r['base']):>12}  {'fresh' if r['fresh'] else 'stale':<5} "
                         f"avgx {avgx:<5} bodies {bodies}")
            if r["candles"]:
                for c in r["candles"]:
                    lines.append(f"      i{c['i']} O{fmt_price(c['o'])} H{fmt_price(c['h'])} L{fmt_price(c['l'])} C{fmt_price(c['c'])}")
    if result["error"]:
        lines.append(f"Error: {result['error']}")
    if result["warning"]:
        lines.append(f"Warning: {result['warning']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        epilog=__doc__[__doc__.index("Examples:"):].rstrip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--count", "-n", type=int, default=3, metavar="<n>")
    parser.add_argument("--dominance", type=float, default=0.5, metavar="<f>")
    parser.add_argument("--metric", choices=list(METRICS), default="median-body", metavar="<name>")
    parser.add_argument("--k", type=float, default=None, metavar="<float>")
    parser.add_argument("--atr-period", type=int, default=14, metavar="<n>")
    parser.add_argument("--direction", choices=["both", "up", "down"], default="both", metavar="<dir>")
    parser.add_argument("--type", choices=["both", "ongoing", "level"], default="both", metavar="<t>")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--candles", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json", metavar="<fmt>")
    parser.add_argument("--exit-zero", action="store_true")
    args = parser.parse_args()
    k = args.k if args.k is not None else DEFAULT_K[args.metric]

    if sys.stdin.isatty():
        parser.print_help()
        return 0

    try:
        candles = parse_candles(sys.stdin.read())
    except (ValueError, json.JSONDecodeError) as exc:
        result = _skeleton(args.count, args.dominance, args.metric, k, args.direction, args.type, args.fresh)
        result["error"] = str(exc)
        _emit(result, args.format)
        return 0 if args.exit_zero else 2

    result = run_detection(candles, args.count, args.dominance, args.metric, k, args.atr_period,
                           args.direction, args.type, args.fresh, args.candles)
    _emit(result, args.format)
    if args.exit_zero:
        return 0
    if result["error"]:
        return 2
    return 0 if result["matched"] else 1


def _emit(result: dict, fmt: str) -> None:
    print(render_text(result) if fmt == "text" else json.dumps(result))


if __name__ == "__main__":
    sys.exit(main())
