# binance-kline-scanner

Screen Binance USD-M futures for fresh impulse setups - runs of N consecutive
same-color "large" candles - ranked best-first.

## Setup

```
uv sync
```

No dependencies (stdlib only); `uv` just pins Python and gives you `uv run`.

## How it works

Two pieces, wired by import:

- `klines_seq_detector.py` - pure detector. An OHLC window in (JSON on stdin), a verdict out
  (JSON or text). Symbol/exchange/time agnostic - just candle math. Usable standalone or imported.
- `scanner.py` - fetches the last N klines per symbol from Binance in a bounded, jittered thread
  pool (fast but not hammering the API), runs the detector on each, and prints the matches ranked
  by: recency band, then longest run, then biggest bodies (within an age band length/body break
  the near-tie). The still-forming last candle is dropped, so runs and levels never shift mid-candle.

A "run" is N+ consecutive candles of one color, each with a body at least `K x` the window's
typical body (`median-body` default, `atr` optional). `--fresh`/default-on in the scanner keeps
only runs no later candle has closed back into - none of the run's candles reclaimed - the
still-valid setups.

## Commands

```
uv run ./scanner.py                                       # scan scan_symbols.txt, JSON
uv run ./scanner.py --format text                         # aligned human table
uv run ./scanner.py --direction down --format text        # only bearish impulses
uv run ./scanner.py --include-stale --format text         # also show already-broken runs
uv run ./scanner.py --type ongoing --format text          # only still-moving (level = already reacted)
uv run ./scanner.py --symbols SOLUSDT,XRPUSDT --count 3 --k 2
uv run ./scanner.py --symbols-file my_list.txt --interval 1h --workers 12

cat window.json | ./klines_seq_detector.py --direction down --fresh --format text
```

Symbols come from `--symbols` (comma list) or `--symbols-file` (one per line, `#` comments,
inline comments allowed), defaulting to `scan_symbols.txt`. Edit that file to choose what scans.

## Output

- `--format json` (default) for tools, `text` for eyes (aligned columns + a summary line with
  timing, counts, and the ranking order).
- Exit codes: `0` matched, `1` none, `2` error (`--exit-zero` to always exit 0).
- Each match carries direction, type (ongoing = still one color after / level = red+green reacted
  after), age (candles since it closed), length, base (the origin extreme: up = lowest low,
  down = highest high), break level (`level` = the consolidation-rectangle edge the post-run
  candles form - body ceiling for down-runs, body floor for up-runs; set only for level-type runs,
  null for ongoing), fresh flag, and body multiples.

## Symbols

`scan_symbols.txt` ships two groups, both editable:

- Crypto: liquid established USD-M perps (no BTC/ETH, no microcap memes).
- TradFi / pre-IPO equity perps (Binance `TRADIFI_PERPETUAL`): SPCXUSDT (SpaceX), OPENAIUSDT,
  TSLAUSDT, NVDAUSDT, AAPLUSDT and other popular names.

## Layout

- `klines_seq_detector.py` - the pure detector (lib + standalone CLI).
- `scanner.py` - the multi-symbol orchestrator (parallel fetch, rank, render).
- `scan_symbols.txt` - default symbol list.
- `test_klines_seq_detector.py`, `test_scanner.py` - synthetic tests, no network.

## Notes

- Read-only REST polling against `fapi.binance.com`. The pool is bounded (default 8, capped 32)
  and jittered; 418/429/network errors back off and retry. Per-symbol failures are isolated and
  reported, never fatal to the scan.
- "large" is measured against the scanned window itself, so it adapts per symbol and per regime.
