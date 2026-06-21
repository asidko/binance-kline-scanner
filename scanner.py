#!/usr/bin/env python3
"""scanner.py - screen many symbols for fresh same-color candle impulses.

Fetches the last N klines per symbol from Binance USD-M futures (bounded, jittered
thread pool), runs the pure klines_seq_detector on each, and prints the matches
ranked best-first: most recent cascade, then longest run, then biggest bodies.

Usage:
  uv run ./scanner.py                                  # scan the default list
  uv run ./scanner.py --symbols DOGEUSDT,SOLUSDT --format text
  uv run ./scanner.py --symbols-file my_list.txt --direction down --interval 1h

Symbols come from --symbols (comma list) or --symbols-file (one per line, # comments).
With neither, the default list is ~/.config/bks/scan_symbols.txt, auto-seeded from the
bundled template on first run so you can edit it (override the dir with BKS_CONFIG_DIR).

Options:
  --symbols <list>     comma-separated symbols (overrides the file)
  --symbols-file <p>   symbol list file (default: ~/.config/bks/scan_symbols.txt)
  --all-symbols        scan every trading USD-M perpetual on Binance (crypto + TradFi)
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
  --format <fmt>       text (default) | json
  --exit-zero          always exit 0 (default: 0 matched / 1 none / 2 error)

Examples:
  uv run ./scanner.py --direction down --format text
  uv run ./scanner.py --symbols SOLUSDT,XRPUSDT --count 3 --k 2
"""
# Build flags for `nuitka` (a portable single-file binary). Applied whenever
# nuitka compiles this module, so source and CI builds stay identical.
# nuitka-project: --onefile
# nuitka-project: --output-dir={MAIN_DIRECTORY}/dist
# nuitka-project: --output-filename=bks
# nuitka-project: --include-data-files={MAIN_DIRECTORY}/scan_symbols.txt=scan_symbols.txt
# nuitka-project: --assume-yes-for-downloads
# nuitka-project: --onefile-tempdir-spec={CACHE_DIR}/bks/{VERSION}
import argparse
import json
import os
import random
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import klines_seq_detector as det
import version

FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
FAPI_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
PERPETUAL_TYPES = ("PERPETUAL", "TRADIFI_PERPETUAL")  # crypto perps + TradFi/pre-IPO equity perps
# template shipped with the code (repo file in source, bundled into the onefile binary)
BUNDLED_SYMBOLS_FILE = Path(__file__).resolve().parent / "scan_symbols.txt"
# user-editable copy, seeded from the template on first run; override the dir with BKS_CONFIG_DIR
CONFIG_DIR = Path(os.environ.get("BKS_CONFIG_DIR") or
                  (Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "bks"))
DEFAULT_SYMBOLS_FILE = CONFIG_DIR / "scan_symbols.txt"
MAX_WORKERS = 32
MAX_BACKOFF = 30.0
JITTER = 0.1


def ensure_symbols_file() -> bool:
    """Seed the user-editable symbol list from the bundled template if absent. Returns True when created."""
    if DEFAULT_SYMBOLS_FILE.exists():
        return False
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BUNDLED_SYMBOLS_FILE, DEFAULT_SYMBOLS_FILE)
    return True


def fetch_klines(symbol: str, interval: str, limit: int, retries: int = 2) -> list[dict]:
    # fetch one extra and drop the final kline - on a live feed the most recent candle is still
    # forming, so counting it would shift runs, freshness, and levels on every poll until it closes
    url = f"{FAPI_KLINES}?{urllib.parse.urlencode({'symbol': symbol, 'interval': interval, 'limit': limit + 1})}"
    time.sleep(random.uniform(0, JITTER))  # de-sync the worker burst so we don't hit Binance all at once
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                rows = json.load(resp)
            return [{"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])} for r in rows[:-1]]
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


def fetch_all_symbols(retries: int = 2) -> list[str]:
    # every actively trading USD-M perpetual (crypto + TradFi equity perps) from exchangeInfo
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(FAPI_EXCHANGE_INFO, timeout=10) as resp:
                data = json.load(resp)
            return sorted(s["symbol"] for s in data["symbols"]
                          if s.get("status") == "TRADING" and s.get("contractType") in PERPETUAL_TYPES)
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


def _iso_utc(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scan(symbols: list[str], fetch, count: int, dominance: float, metric: str, k: float, atr_period: int, direction: str,
         type_filter: str, fresh: bool, with_candles: bool, interval: str, limit: int, workers: int = 8) -> tuple[list[dict], list[dict]]:
    results: list[dict] = []
    errors: list[dict] = []

    def analyze(symbol: str) -> dict:
        candles = fetch(symbol, interval, limit)
        res = det.run_detection(candles, count, dominance, metric, k, atr_period, direction, type_filter, fresh, with_candles)
        for run in res["runs"]:  # detector is time-agnostic; map its start index to the kline open time here
            t = candles[run["start"]].get("t")
            run["started_at"] = _iso_utc(t) if t is not None else None
        return res

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
        lines.append(f"  {'SYMBOL':<12} {'DIR':<4} {'TYPE':<7} {'LEVEL':>13} {'AGE':>3} {'LEN':>3} {'STARTED':<20} {'BASE':>13} {'STATE':<5} {'AVGX':>5}  BODIES")
        for e in out["results"]:
            t = e["runs"][0]
            avgx = f"{t['body_mult_mean']:g}" if t["body_mult_mean"] is not None else "-"
            bodies = "/".join(f"{m:g}" if m is not None else "-" for m in t["body_mults"])
            lvl = det.fmt_price(t["level"]) if t["level"] is not None else "-"
            lines.append(f"  {e['symbol']:<12} {t['direction']:<4} {t['type']:<7} {lvl:>13} {t['age']:>3} {t['length']:>3} "
                         f"{t['started_at'] or '-':<20} {det.fmt_price(t['base']):>13} {'fresh' if t['fresh'] else 'stale':<5} {avgx:>5}  {bodies}")
    for e in out["errors"]:
        lines.append(f"  ! {e['symbol']}: {e['error']}")
    return "\n".join(lines)


def _emit(out: dict, fmt: str) -> None:
    print(render_text(out) if fmt == "text" else json.dumps(out))


class _VersionAction(argparse.Action):
    # build the banner only when --version is actually passed; version.banner()
    # forks git + reads pyproject, wasted on every scan if evaluated eagerly
    def __init__(self, option_strings: list[str], dest: str, **kw) -> None:
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, **kw)

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        print(version.banner())
        parser.exit()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0],
        epilog=__doc__[__doc__.index("Examples:"):].rstrip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action=_VersionAction, help="show version and exit")
    parser.add_argument("--symbols", metavar="<list>")
    parser.add_argument("--symbols-file", metavar="<path>", type=Path, default=DEFAULT_SYMBOLS_FILE)
    parser.add_argument("--all-symbols", action="store_true")
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
    parser.add_argument("--format", choices=["json", "text"], default="text", metavar="<fmt>")
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
        if args.all_symbols:
            symbols = fetch_all_symbols()
        else:
            if not args.symbols and args.symbols_file == DEFAULT_SYMBOLS_FILE and ensure_symbols_file():
                print(f"created {DEFAULT_SYMBOLS_FILE} from the bundled list - edit it to choose what scans", file=sys.stderr)
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
