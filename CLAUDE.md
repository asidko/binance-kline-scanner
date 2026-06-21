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
- `--fresh`: keep only runs no later candle has CLOSED back into. Each run candle is reclaimed when
  a later candle closes past its far body edge (up-run: a close below the run's highest body-bottom;
  down-run: a close above the run's lowest body-top). A run is fresh only if NONE of its candles is
  reclaimed - one reclaimed candle makes it stale (price is eating the range back). Closes only, not
  wicks - a wick poking in is fine, the candle must CLOSE through. `base` (up = lowest low, down =
  highest high) stays an informational extreme, NOT the freshness pivot. Scanner defaults fresh ON;
  `--include-stale` opts out.
- `--type`: classify each run by what closed AFTER it - `level` (a red AND a green candle closed
  after = reaction/zone formed) vs `ongoing` (one color, or nothing, after = move still running).
  Always emitted in output; `--type ongoing|level` filters, `both` (default) does not.
- `level`: the break level a `level`-type run prints. It is NOT a stat of the run - it is the edge
  of the consolidation rectangle the candles AFTER the run form (the reaction zone), nudged off the
  bodies into the wick gap so it does not sit flush on the body cluster. Anchor at the body extreme
  (down-run: body CEILING `max(max(open,close))` = resistance; up-run: body FLOOR `min(min(open,close))`
  = support), then push toward the wicks by `LEVEL_WICK_BUFFER` (0.33) x the TYPICAL wick overhang -
  `statistics.median` of per-candle `high - bodytop` (down) / `bodybottom - low` (up), MEDIAN not max
  so one spike wick can't drag it out. Clamp to the wick extreme (`max(high)` / `min(low)`) so it never
  passes the wicks. Anchoring at the body extreme keeps the level from ever cutting INSIDE the bodies
  (a median-of-bodies center would - rejected). Computed from `candles[end+1:]`, non-empty exactly when
  the run is `level`-type (a red and a green closed after), so only `level` runs get a `level`;
  `ongoing` runs get `None`. Distinct from `base` (the lowest-low/highest-high freshness pivot inside
  the run). The unclosed last candle is excluded upstream, so the rectangle never jitters mid-candle.
- Ranking (runs within a window, and symbols within a scan) is lexicographic by user priority via
  the shared `rank_key`: 1) recency as an AGE BAND (`age_bucket`), 2) length, 3) body size
  (`body_mult_mean`), 4) exact `age`, then symbol for deterministic ties. Never a weighted score -
  priority is ordered. Age is BANDED not raw: ages 0-5 stay distinct (recency rules there), then
  widening bands (6-8, 9-10, 11-15, 16-20, 21-25, 26-30, ...) per `AGE_BANDS`, so a slightly older
  but longer/bigger run outranks a fresher-but-weaker one inside the same band. The detector and
  scanner MUST share `rank_key` - never re-derive the order in two places.

## Output contract (stable - other tools parse it)
- `--format` `json` (machine) | `text` (human). Same information in both. The detector defaults to
  `json` (it is a pipe/lib filter for tools); the scanner defaults to `text` (human-facing CLI).
- `started_at`: each run carries the UTC open time of its first candle as an ISO string with `T`
  and `Z` (`2026-06-20T17:45:00Z`); with `length` x `interval` the caller derives the exact range.
  The detector stays time-agnostic (it only knows indices) - the SCANNER adds `started_at` by
  mapping the run's `start` index to the kline `openTime` it kept in `fetch_klines` (the `t` field).
  Standalone detector output has no `started_at`; it is `None` if a candle lacks `t`.
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
- Drop the still-forming candle: `fetch_klines` requests `limit + 1` and discards the last row.
  Binance returns the in-progress interval as the final kline; counting it would shift runs,
  freshness, and `level` on every poll until it closes. The detector stays agnostic (it trusts its
  input is closed); the live boundary - knowing the last kline is unclosed - lives in the scanner.

## Symbols
- `scan_symbols.txt`: curated crypto alts (no BTC/ETH, no microcap memes) + TradFi/pre-IPO equity
  perps (`TRADIFI_PERPETUAL`: SPCXUSDT=SpaceX, OPENAIUSDT, TSLAUSDT, NVDAUSDT, ...). One per line,
  blank lines and `#` comments (incl. inline) ignored. Never assume a ticker is absent - query
  `/fapi/v1/exchangeInfo`.
- The repo `scan_symbols.txt` is the TEMPLATE (`BUNDLED_SYMBOLS_FILE`, shipped in the binary). The
  ACTIVE default list is `~/.config/bks/scan_symbols.txt` (`DEFAULT_SYMBOLS_FILE`; dir from
  `BKS_CONFIG_DIR` or XDG), auto-seeded from the template by `ensure_symbols_file()` on first run so
  the bundled list inside the read-only onefile stays editable. Seeding fires only when no
  `--symbols` and the default `--symbols-file` is in use - an explicit `--symbols-file` or
  `--symbols` never seeds. Tests stub `CONFIG_DIR`/`DEFAULT_SYMBOLS_FILE` to a tempdir.

## Conventions
- Plain ASCII only. Type hints on every function. DRY: extract shared logic into a helper.
- No magic literals for shared conventions; name a constant next to what it describes.
- Comments default to NONE; add only when the WHY is non-obvious. Never restate WHAT the code does.

## Packaging / release
- Ships as `bks`, a single Nuitka onefile binary of `scanner.py` (the scanner is the only
  user-facing CLI; the detector stays a source-level pipe/lib). Build with `uv run python build.py`
  -> `dist/bks-<os>-<arch>`. Nuitka flags live in `# nuitka-project:` comments at the top of
  `scanner.py`, so source and CI builds stay identical; `build.py` only stamps version + names the
  artifact per OS/arch.
- `scan_symbols.txt` is bundled via `--include-data-files`; `BUNDLED_SYMBOLS_FILE` resolves next to
  `__file__`, which points into the onefile unpack dir at runtime, so the template ships in the
  binary (then `ensure_symbols_file()` copies it to `~/.config/bks/` - see Symbols). Anything the
  scanner reads from disk by default MUST be bundled the same way.
- `version.py` reports version + commit: from source it reads `pyproject.toml` + live git; a frozen
  binary reads `_build.py` (stamped by `build.py`, gitignored), detected via `__main__.__compiled__`.
  `--version` prints the banner.
- Pinned to Python 3.13 (`.python-version`, `requires-python >=3.13`) - Nuitka support, not 3.14.
  Runtime deps stay empty (`dependencies = []`); `nuitka`/`ruff`/`zstandard` are dev-only.
- CI: `.github/workflows/ci.yml` runs `ruff check .` + both test scripts on push/PR.
  `release.yml` builds the binary per-OS (linux x86_64/arm64, macos arm64) on a `v*` tag, smoke-tests
  `--version`/`--help`, and publishes binaries + `SHA256SUMS`. `install.sh` verifies that checksum.
- Android/Termux is bionic, not glibc, and GitHub has no Android runner. `release.yml`'s `build-android`
  job builds inside `termux/termux-docker:aarch64` under QEMU (emulated arm64, slow) and ships
  `bks-android-arm64`. `build.py` names it `android-*` (detects `com.termux` in `$PREFIX`) so it never
  collides with the glibc `linux-*` build. `install.sh` detects Termux (`$PREFIX`/`$TERMUX_VERSION`):
  arm64 downloads that prebuilt binary (checksum-verified, into `$PREFIX/bin`); other arches fall back to
  `install_termux` - SOURCE install (`.py` + `pyproject.toml` + `scan_symbols.txt` into `$PREFIX/share/bks`
  + a `bks` shim running `python3 scanner.py`). Source path works only because the tool is stdlib-only.

## Testing (before claiming done)
- `python3 test_klines_seq_detector.py` - synthetic detector cases incl. flat-window guard, no network.
- `python3 test_scanner.py` - scanner with a stubbed fetch (ranking, fresh-default, error isolation,
  symbol tiebreak, string->float cast), no network.
- One live smoke: `uv run ./scanner.py --symbols DOGEUSDT,TSLAUSDT --format text --include-stale`.
