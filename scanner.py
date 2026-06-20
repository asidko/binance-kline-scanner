#!/usr/bin/env python3
"""scanner.py - screen many symbols for fresh same-color candle impulses.

Fetches the last N klines per symbol from Binance USD-M futures (bounded, jittered
thread pool), runs the pure klines_seq_detector on each, and prints the matches
ranked best-first: most recent cascade, then longest run, then biggest bodies.

Usage:
  uv run ./scanner.py                                  # scan the default list
  uv run ./scanner.py --symbols DOGEUSDT,SOLUSDT --format text
  uv run ./scanner.py --symbols-file my_list.txt --direction down --interval 1h

Symbols come from --symbols (comma list) or --symbols-file (one per line, # comments),
defaulting to scan_symbols.txt next to this script.

Options:
  --symbols <list>     comma-separated symbols (overrides the file)
  --symbols-file <p>   symbol list file (default: scan_symbols.txt)
  --interval <tf>      kline interval (default 15m)
  --limit <n>          klines fetched per symbol (default 40)
  --workers <n>        parallel fetches, bounded pool 1..32 (default 8)
  --count <n>          candles in a run (default 3)
  --dominance <f>      fraction of a run's candles that must be 'large' (default 0.5;
                       1.0 = every candle large, 0.5 = majority; weak ends trimmed)
  --metric <name>      median-body (default) | atr
  --k <float>          body must be >= K * yardstick (default 1.5 median-body, 0.9 atr)
  --atr-period <n>     ATR lookback, atr metric only (default 14)
  --direction <dir>    both (default) | up | down
  --type <t>           both (default) | ongoing (one color after) | level (red+green after)
  --include-stale      also report runs whose base is already broken (default: fresh only)
  --candles            include each run's full OHLC
  --format <fmt>       json (default) | text
  --exit-zero          always exit 0 (default: 0 matched / 1 none / 2 error)

Examples:
  uv run ./scanner.py --direction down --format text
  uv run ./scanner.py --symbols SOLUSDT,XRPUSDT --count 3 --k 2
"""
import argparse
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import klines_seq_detector as det

FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
DEFAULT_SYMBOLS_FILE = Path(__file__).resolve().parent / "scan_symbols.txt"
MAX_WORKERS = 32
MAX_BACKOFF = 30.0
JITTER = 0.1


def fetch_klines(symbol: str, interval: str, limit: int, retries: int = 2) -> list[dict]:
    # fetch one extra and drop the final kline - on a live feed the most recent candle is still
    # forming, so counting it would shift runs, freshness, and levels on every poll until it closes
    url = f"{FAPI_KLINES}?{urllib.parse.urlencode({'symbol': symbol, 'interval': interval, 'limit': limit + 1})}"
    time.sleep(random.uniform(0, JITTER))  # de-sync the worker burst so we don't hit Binance all at once
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                rows = json.load(resp)
            return [{"o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])} for r in rows[:-1]]
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After", "1")
            wait = float(retry_after) if retry_after.isdigit() else 1.0
            if exc.code in (418, 429) and attempt < retries:
                time.sleep(min(wait, MAX_BACKOFF))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise


def load_symbols(symbols_arg: str | None, symbols_file: Path) -> list[str]:
    if symbols_arg:
        return [s.strip().upper() for s in symbols_arg.split(",") if s.strip()]
    out: list[str] = []
    for line in symbols_file.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.upper())
    return out


def _priority(entry: dict) -> tuple:
    return (*det.rank_key(entry["runs"][0]), entry["symbol"])


def scan(symbols: list[str], fetch, count: int, dominance: float, metric: str, k: float, atr_period: int, direction: str,
         type_filter: str, fresh: bool, with_candles: bool, interval: str, limit: int, workers: int = 8) -> tuple[list[dict], list[dict]]:
    results: list[dict] = []
    errors: list[dict] = []

    def analyze(symbol: str) -> dict:
        candles = fetch(symbol, interval, limit)
        return det.run_detection(candles, count, dominance, metric, k, atr_period, direction, type_filter, fresh, with_candles)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, MAX_WORKERS))) as pool:
        futures = {pool.submit(analyze, s): s for s in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                r = future.result()
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc)})
                continue
            if r["matched"]:
                results.append({"symbol": symbol, "stats": r["stats"], "runs": r["runs"], "warning": r["warning"]})
    results.sort(key=_priority)
    errors.sort(key=lambda e: e["symbol"])
    return results, errors


def render_text(out: dict) -> str:
    p = out["params"]
    dirn = p["direction"] if p["direction"] != "both" else "same-color"
    fresh = "fresh" if p["fresh_required"] else "any"
    took = f" in {out['elapsed_s']:g}s" if out.get("elapsed_s") is not None else ""
    lines = [f"Scanned {out['scanned']} symbols, {out['matched_count']} matched, {len(out['errors'])} errors{took}. "
             f"{p['count']}+ {dirn} {p['metric']}, k={p['k']:g}, dom={p['dominance']:g}, {fresh}, {p['interval']} x{p['limit']}. "
             f"Ranked by recency band, length, body."]
    if out["results"]:
        lines.append(f"  {'SYMBOL':<12} {'DIR':<4} {'TYPE':<7} {'LEVEL':>13} {'AGE':>3} {'LEN':>3} {'BASE':>13} {'STATE':<5} {'AVGX':>5}  BODIES")
        for e in out["results"]:
            t = e["runs"][0]
            avgx = f"{t['body_mult_mean']:g}" if t["body_mult_mean"] is not None else "-"
            bodies = "/".join(f"{m:g}" if m is not None else "-" for m in t["body_mults"])
            lvl = det.fmt_price(t["level"]) if t["level"] is not None else "-"
            lines.append(f"  {e['symbol']:<12} {t['direction']:<4} {t['type']:<7} {lvl:>13} {t['age']:>3} {t['length']:>3} "
                         f"{det.fmt_price(t['base']):>13} {'fresh' if t['fresh'] else 'stale':<5} {avgx:>5}  {bodies}")
    for e in out["errors"]:
        lines.append(f"  ! {e['symbol']}: {e['error']}")
    return "\n".join(lines)


def _emit(out: dict, fmt: str) -> None:
    print(render_text(out) if fmt == "text" else json.dumps(out))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        epilog=__doc__[__doc__.index("Examples:"):].rstrip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--symbols", metavar="<list>")
    parser.add_argument("--symbols-file", metavar="<path>", type=Path, default=DEFAULT_SYMBOLS_FILE)
    parser.add_argument("--interval", metavar="<tf>", default="15m")
    parser.add_argument("--limit", metavar="<n>", type=int, default=40)
    parser.add_argument("--workers", metavar="<n>", type=int, default=8)
    parser.add_argument("--count", "-n", metavar="<n>", type=int, default=3)
    parser.add_argument("--dominance", metavar="<f>", type=float, default=0.5)
    parser.add_argument("--metric", choices=list(det.METRICS), default="median-body", metavar="<name>")
    parser.add_argument("--k", metavar="<float>", type=float, default=None)
    parser.add_argument("--atr-period", metavar="<n>", type=int, default=14)
    parser.add_argument("--direction", choices=["both", "up", "down"], default="both", metavar="<dir>")
    parser.add_argument("--type", choices=["both", "ongoing", "level"], default="both", metavar="<t>")
    parser.add_argument("--include-stale", action="store_true")
    parser.add_argument("--candles", action="store_true")
    parser.add_argument("--format", choices=["json", "text"], default="json", metavar="<fmt>")
    parser.add_argument("--exit-zero", action="store_true")
    args = parser.parse_args()

    fresh = not args.include_stale
    k = args.k if args.k is not None else det.DEFAULT_K[args.metric]
    out: dict = {
        "schema": det.SCHEMA,
        "params": {"count": args.count, "dominance": args.dominance, "metric": args.metric, "k": k, "direction": args.direction,
                   "type": args.type, "fresh_required": fresh, "interval": args.interval, "limit": args.limit},
        "scanned": 0, "matched_count": 0, "elapsed_s": None, "errors": [], "results": [],
    }
    try:
        symbols = load_symbols(args.symbols, args.symbols_file)
    except OSError as exc:
        out["errors"] = [{"symbol": "-", "error": str(exc)}]
        _emit(out, args.format)
        return 0 if args.exit_zero else 2
    if not symbols:
        out["errors"] = [{"symbol": "-", "error": "no symbols to scan"}]
        _emit(out, args.format)
        return 0 if args.exit_zero else 2

    print(f"scanning {len(symbols)} symbols, {args.interval} x{args.limit}, {args.workers} workers ...", file=sys.stderr)
    t0 = time.monotonic()
    results, errors = scan(symbols, fetch_klines, args.count, args.dominance, args.metric, k, args.atr_period,
                           args.direction, args.type, fresh, args.candles, args.interval, args.limit, args.workers)
    out["elapsed_s"] = round(time.monotonic() - t0, 2)
    out["scanned"] = len(symbols)
    out["matched_count"] = len(results)
    out["errors"] = errors
    out["results"] = results
    _emit(out, args.format)
    if args.exit_zero:
        return 0
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
