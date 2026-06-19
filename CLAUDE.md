# binance-kline-scanner - project rules

Screen Binance USD-M futures for fresh impulse setups: runs of N consecutive
same-color "large" candles. Two pieces, one responsibility each.

## Architecture
- `klines_seq_detector.py` - the PURE detector. Reads an OHLC window as JSON on stdin,
  prints a verdict. Knows nothing about coins, exchanges, or time - pure candle math on
  array indices. Stdlib only. It is BOTH a standalone filter (`echo ... | ./klines_seq_detector.py`)
  AND an importable lib (`run_detection()`, `detect()`, `METRICS`, `fmt_price`, `DEFAULT_K`).
  Never add fetching/symbols here. It expects numeric OHLC - callers that bypass `parse_candles`
  (the scanner does) MUST cast Binance's string OHLC to float themselves.
- `scanner.py` - the orchestrator. Resolves a symbol list, fetches klines per symbol from
  Binance, calls `det.run_detection()` per symbol, ranks symbols, renders. All network + symbol
  knowledge lives here, nowhere else.
- Wire the two by IMPORT, never subprocess. Detection logic lives once, in the detector.
- One file = one responsibility. The detector holds zero I/O; the scanner holds zero detection logic.

## Detection spec
- A run = a maximal stretch of N+ consecutive same-color candles (bull = close>open,
  bear = close<open). COLOR segments runs, not size. A candle is "large" when body >= K *
  yardstick. `--metric median-body` (default) | `atr`. The yardstick comes from the given window
  only (symbol-agnostic). `--k` default is metric-dependent (`DEFAULT_K`: 1.5 median-body, 0.9
  atr) because ATR measures range, not body - resolve it after parsing `--metric`, never hardcode
  one K for both.
- `--dominance` (float, default 0.5, range (0,1]): a run qualifies when at least this fraction of
  its candles are large. First trim weak (non-large) candles off BOTH ends so the run is anchored
  large->large, then test `large_count >= dominance * length`. 1.0 = every candle large (old
  strict mode), 0.5 = majority. Trimming is what kills "one monster candle + filler" masking; the
  fraction is what tolerates a weak breather candle mid-impulse. Never use mean body to gate -
  one giant candle would mask dojis.
- Flat window (median/atr yardstick <= 0): match NOTHING and set a warning. Never fall back to
  "any nonzero body is large" - that fires on noise exactly when the size signal is dead.
- `--fresh`: keep only runs whose base is not yet crossed by a later wick. Base = the run's far
  edge: up-run = lowest low, down-run = highest high. A later wick poking INTO the run is fine;
  a wick fully past the base breaks it. The scanner defaults fresh ON; `--include-stale` opts out.
- `--type`: classify each run by what closed AFTER it - `level` (a red AND a green candle closed
  after = reaction/zone formed) vs `ongoing` (one color, or nothing, after = move still running).
  Always emitted in output; `--type ongoing|level` filters, `both` (default) does not.
- Ranking (runs within a window, and symbols within a scan) is lexicographic by user priority:
  1) recency (smaller `age`), 2) length, 3) body size (`body_mult_mean`). Then symbol, for
  deterministic ties. Never a weighted score - priority is ordered.

## Output contract (stable - other tools parse it)
- `--format json` (default, machine) | `text` (human). Same information in both.
- Exit codes: 0 = matched, 1 = no match, 2 = error. `--exit-zero` forces 0 for callers that parse.
- Fixed JSON shape regardless of flags: always-present keys, `error`/`warning` null when absent,
  a stable `unit` (the per-candle yardstick) + `metric` name (never `median_body` xor `atr`).
  Detector top-level `matched` is a BOOL; the scanner envelope uses `matched_count` (int) - do
  not reuse the same key name for both.
- Text mode: aligned columns + a header + a summary line (counts, elapsed, ranking order). Format
  prices through the shared `det.fmt_price` so TSLA (~400), DOGE (~0.08) and microcaps all read
  consistently and never flip to scientific notation. JSON numbers stay full-precision - beautify
  text only. Scanner prints a one-line `scanning ...` preamble to STDERR (keeps stdout clean).

## Concurrency + Binance
- uv for everything: `uv sync`, `uv run ./scanner.py`. Stdlib only - keep `dependencies = []`
  (fetch via `urllib`, no `requests`). The detector must stay runnable as plain `python3` too.
- Fetch in a bounded `ThreadPoolExecutor` (default 8, clamp 1..32) - fast but never a thundering
  herd. Jitter each request to de-sync the burst. Retry 418/429 honoring Retry-After (capped) and
  transient URLError/TimeoutError with backoff. Results/errors are appended in the main thread
  (`as_completed`), so no locks; keep `run_detection` pure (no shared mutable state).
- Read-only Binance: base `https://fapi.binance.com`, `timeout` on every call. Isolate per-symbol
  fetch failures - one bad symbol never kills a scan; collect it in `errors`.

## Symbols
- `scan_symbols.txt`: curated crypto alts (no BTC/ETH, no microcap memes) + TradFi/pre-IPO equity
  perps (`TRADIFI_PERPETUAL`: SPCXUSDT=SpaceX, OPENAIUSDT, TSLAUSDT, NVDAUSDT, ...). One per line,
  blank lines and `#` comments (incl. inline) ignored. Never assume a ticker is absent - query
  `/fapi/v1/exchangeInfo`.

## Conventions
- Plain ASCII only. Type hints on every function. DRY: extract shared logic into a helper.
- No magic literals for shared conventions; name a constant next to what it describes.
- Comments default to NONE; add only when the WHY is non-obvious. Never restate WHAT the code does.

## Testing (before claiming done)
- `python3 test_klines_seq_detector.py` - synthetic detector cases incl. flat-window guard, no network.
- `python3 test_scanner.py` - scanner with a stubbed fetch (ranking, fresh-default, error isolation,
  symbol tiebreak, string->float cast), no network.
- One live smoke: `uv run ./scanner.py --symbols DOGEUSDT,TSLAUSDT --format text --include-stale`.
