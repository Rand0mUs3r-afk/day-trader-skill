# Manual simulated-account evaluation

## Implementation and validation status

This changeset adds the executable ledger, historical ORB runner, tests and
manual GitHub Actions workflow. It does not modify upstream strategy files.
The implementation is available to run after this changeset is committed.

Development validation: eight accounting-only unit tests passed using a stub;
the pinned upstream checkout was not available in the local execution sandbox,
so five real-upstream integration tests were not executed there. The workflow
runs both suites against the actual checkout before it permits a data run.
No GitHub workflow or historical market-data backtest has yet been executed
or verified. Do not call these results a validated full-strategy backtest.

## Run on GitHub

Open the public fork's Actions tab and select **Manual simulated ORB account
evaluation**, then **Run workflow** on `main`.

Supply these material inputs rather than silently accepting a new strategy:

- `symbols`: 1 to 20 unique US stock/ETF tickers separated by commas, in the
  order to prioritize when entries compete for cash at the same time.
- `or_minutes`: the upstream ORB opening-range length, a multiple of five
  from 5 to 385 minutes. Very long ranges can have no admissible entries.
- `stop_rule`: `or_mid` (opening-range midpoint) or `or_low` (range low),
  the upstream long-ORB rules supported by this first account adapter.
- `target_r`: a positive reward-to-risk multiple. Existing upstream minimum
  reward/risk checks can block low-R configurations.
- `fee_per_fill`: flat USD fee per entry or exit, default zero. Zero costs
  and the execution assumptions below are idealized, not brokerage realism.
- `accept_limitations`: must be checked explicitly. The workflow otherwise
  refuses to fetch data or claim a result.
- `allow_partial`: leave false to fail on missing expected sessions. Set true
  only to explicitly accept reported exclusions, including half-days that
  upstream's 78-bar filter cannot replay. Success is then labeled PARTIAL.

The account starts at USD 2,000 and continues across all accepted trades and
sessions in the run. Each separate historical run restarts at USD 2,000; this
is not a scheduled/live forward account or cross-run deposit/withdrawal system.

There is no schedule, broker connection, broker secret, AI API call or real
order. GitHub's checkout token is read-only and not persisted. The runner is
standard-library Python; no pip requirements are needed.

## Local CLI

Run `python -m unittest discover -s evaluation -p 'test_*.py' -v` from a full
checkout first. Use `python evaluation/runner.py --help` for the required
strategy inputs. The flags correspond to the workflow inputs:

```
python evaluation/runner.py --symbols "$SYMBOLS" --or-minutes "$OR_MINUTES" \
  --stop-rule "$STOP_RULE" --target-r "$TARGET_R" --fee-per-fill 0 \
  --accept-limitations
```

Use your chosen upstream plan's settings; no ticker/rule combination is
recommended or claimed profitable here. An existing output directory is an
error: choose a new `--out` directory rather than mixing runs.

For reproduction, check out the `evaluated_commit` in `manifest.json`, extract
the artifact, pass the same strategy/cost/partial-coverage flags and add
`--offline PATH_TO_RAW_DIRECTORY --as-of ORIGINAL_AS_OF_UTC --out NEW_DIRECTORY`.
Raw files must use the preserved `SYMBOL-60d5m.json` names. Do not use today's
clock for a purported exact offline reproduction. The manifest's wall-clock
fetch timestamp and invocation naturally differ between executions.

## What is actually replayed

The upstream baseline is `3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd`.
Before loading code the runner verifies the Git blob hashes of `size.py`,
`check.py`, `timemachine.py` and `config.example.json`. It stops on a mismatch.
It calls upstream `history_replay` to generate original ORB trades, reconstructs
exit events, and asserts matching outcomes and reported R. Actual entries
then pass through upstream `size_trade`, `bracket_order`, and `risk.check`.
The original raw ORB candidates and account-admissible trades are both saved.

Only the initial budget changes from the upstream example to USD 2,000.
Sizing receives current equity and cash for every candidate. Other upstream
risk defaults remain unchanged; in particular, fixed-dollar risk limits are
not silently converted into percentages of the smaller account.

The workflow is NOT a faithful reconstruction of historical full-universe
scanning or Claude decisions. Historical scanner ranking, flag/bounce rules,
news, filings, congressional disclosures, live brackets/alerts, and Claude
judgments are unevaluated. Do not substitute today's scanner output for a
point-in-time universe. Supplied current tickers have selection/survivorship
bias. If evaluating the complete eleven-skill discretionary workflow is a
requirement, stop: this historical ORB path cannot establish that result.

The Time Machine Monte Carlo estimator remains intact but is NOT run or used
as an entry filter. Its sampled paths are not account trades. A 60-day account
window alone cannot supply a full trailing pre-window calibration pool for
every entry without extra data. This limitation is explicit, not a hidden
replacement for its estimates or Claude's acceptance decisions.

## Execution conventions and limitations

- Signals and entries use upstream's rounded trigger-bar close, an idealized
  same-close fill; exits use the first subsequent stop/target touch or the
  cutoff bar's close. Fills are rounded to cents in the ledger.
- A bar touching both levels stops first. Gaps still fill at the stop, matching
  upstream's idealized -1R convention, not a realistic stop-market guarantee.
- There is no spread/slippage/impact/partial-fill model. Fees are the explicit
  flat per-fill input, charged on entry and exit. Upstream planned risk limits
  exclude fees; actual net losses can exceed those planned-loss amounts.
- A fee that would violate the cash buffer adds a documented cost-feasibility
  rejection. It does not resize the trade using a new sizing strategy.
- Bar-close timestamps stand in for unknown intrabar exit times. Known exits
  are applied before same-time entries. Input ticker order breaks competing
  entry ties. This is a reproducible allocation assumption, not Claude ranking.
- Cash proceeds are immediately reusable. Settlement restrictions, margin,
  shorting, fractional shares and a broker's buying-power model are not tested.
- The upstream configuration's `flat_by` supplies the cutoff; the adapter
  does not invent a different exit time. No positions may remain overnight.
- The upstream risk checker consumes the simulated history, including exit
  orders. Its daily planned-loss budget is the sum of entry risks, not realized
  P&L, and is not reset by a profitable exit.
- Equity is observed at completed five-minute bar closes after applicable
  fills and fees. Maximum observed drawdown is not tick/intrabar drawdown.

## Data and coverage

The original Yahoo fetch requests `range=60d&interval=5m`. The account window
is 60 calendar dates inclusive, ending at the latest available complete session
among input tickers. This need not include the current incomplete trading day.
Report coverage is limited by actual source retention and availability.

The original parser and complete-session filter are reused. Exactly 78 regular
bars are required; half-days/gaps are excluded and listed. Expected sessions
come from the unchanged upstream holiday calendar; unsupported calendar years
fail. Nonfinite/invalid OHLC, duplicate timestamps, chart-symbol mismatches and
wrong intervals fail rather than being silently repaired.

Session completeness is an ex-post data-quality filter and a source of bias.
Unmodified Yahoo OHLC is used; corporate actions are not independently
reconstructed/validated. There is no delisted universe or historical constituents
model. No future bar prices enter account sizing; full-session exclusion is
nevertheless known ex post and must not be presented as a live decision.

## Artifacts and interpreting status

The workflow always attempts to retain test logs, raw snapshots, coverage and
available failure diagnostics for 30 days. On a completed evaluation it adds:

- `manifest.json`: source/configuration/data hashes, commit, clock and assumptions.
- `coverage.json`: actual dates/session counts, missing days and dropped bars.
- `candidates.json`, `decisions.json`: upstream signals, sizing and PASS/BLOCKs.
- `fills.json`, `closed_trades.json`, `equity.json`: continuous accounting ledger.
- `summary.json`, `REPORT.md`: net return, fees, trades, win rate, mean wins/losses,
  profit factor and five-minute observed drawdown.

A `FAILED` summary is not a strategy result. A test failure prevents the backtest
step. `SUCCESS_PARTIAL_COVERAGE` is not complete 60-day coverage. Undefined
metrics are JSON null. A valid zero-trade run remains flat at USD 2,000.
No backtest result, idealized or otherwise, proves future profitability.
