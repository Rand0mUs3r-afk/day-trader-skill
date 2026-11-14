# Simulated-account evaluation

## Current status

The fork now includes an executable account ledger, historical ORB runner,
offline tests and a manual-only GitHub Actions workflow in this changeset.
Read [evaluation/README.md](evaluation/README.md) for run instructions,
reproducibility, assumptions, artifacts, test status and all limitations.

Only eight accounting-only tests were executed successfully in the development
sandbox. The five pinned-upstream integration tests require the actual checkout
and have not yet run here. GitHub runs them before allowing a market-data run.
No GitHub workflow or historical backtest has yet been executed or verified.

## Baseline and scope

- Public fork: https://github.com/Rand0mUs3r-afk/day-trader-skill
- Upstream: https://github.com/Jakeschincariol/day-trader-skill
- Exact unchanged upstream commit: `3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd`
- Additions: `evaluation/runner.py`, `evaluation/test_runner.py`,
  `evaluation/README.md`, and `.github/workflows/simulated-account.yml`.
- This document replaces the initial implementation contract with current status.
- Upstream strategy/skill files and MIT attribution remain unchanged.
- Starting simulated account: USD 2,000; current equity/cash feed original sizing.
- Data request/window: 60 calendar days of available five-minute data; actual
  dates and exclusions are recorded rather than promised in advance.

This evaluates only the upstream historical long opening-range-breakout path
with its original account sizing and applicable risk checks. It does not
reconstruct historical scanning, news, flag/bounce judgments, Claude decisions,
or all eleven skills. No replacement strategy or AI API is used.

The Time Machine sampled-path estimator is intact but unevaluated and is not
used as an entry filter; no synthetic paths are counted as historical trades.
A fixed ticker list is not a historical scanner universe. Users must explicitly
accept these limitations and supply the material ORB parameters before a run.
If complete discretionary-workflow replay is required, this implementation
cannot faithfully provide it and must not be presented as doing so.

## Safety and acceptance

Manual workflow dispatch only; no schedule, broker credential, actual order or
change to the separate Alpaca-Trading project. Execution conventions include
idealized trigger-close fills, stop-first tie-breaking, gap stops at the stop,
cent-rounded fills, immediate proceeds reuse, explicit flat fees, five-minute
marks and deterministic ticker-order cash allocation. See the detailed README
before accepting them. Zero default fees are idealized, not an assertion that
trading is free. Missing data fails by default; accepted exclusions are labeled.

The runner refuses changed upstream fingerprints, invalid data or unsupported
calendar coverage. Source/configuration/input hashes and raw snapshots are
retained for reproducibility. Remaining live-data and integration verification
must be established by an inspected workflow run, not inferred from this commit.
