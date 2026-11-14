# Simulated-account evaluation

## Current status

The public fork is created. The account ledger, account-level backtest runner,
and manual GitHub Actions workflow described below are NOT IMPLEMENTED yet.
No historical evaluation has been run and no performance result is claimed.
This document is the evaluation contract, not an installation or success report.

## Purpose

Evaluate the existing Day Trader skill repository before changing its trading
logic. Start with USD 2,000 in simulated cash and request the last 60 calendar
days of available five-minute market data. Preserve upstream authorship and
the MIT license. Keep this project separate from Alpaca-Trading.

## Reproducible upstream baseline

- Upstream: https://github.com/Jakeschincariol/day-trader-skill
- Fork: https://github.com/Rand0mUs3r-afk/day-trader-skill
- Fork-point commit: `3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd`
- Upstream strategy and skill files are unchanged at this documentation step.

Every eventual run must record the fork-point commit, evaluated fork commit,
input/configuration files, input-data hashes, data-fetch timestamp, requested
period, actual usable dates, and the exact command. Source changes must be
listed separately from evaluation-layer additions. Do not silently update the
baseline while comparing runs.

## Additions permitted for this evaluation

1. A continuous simulated-account ledger and an adapter to existing sizing.
2. A chronological, account-level historical backtest and its reports.

A manual-only GitHub Actions workflow and this documentation support those
two additions. They are not a new trading strategy. Do not add replacement
signals, tune parameters against the evaluation period, or substitute an AI
model to improve a reported result.

### Reuse existing account sizing

`trader-budget/size.py` already provides `size_trade` and `bracket_order`.
`size_trade` accepts a budget, available cash, a reference price, and a date.
Reuse those functions rather than implementing another position-sizing formula.

The added ledger must supply account values at the simulated event time,
reserve/debit cash for accepted entries, track whole-share positions, apply
exits and explicit costs, and update equity. The same account continues across
trades; each trade does not receive a new USD 2,000 allocation. Reject unknown
position prices instead of silently valuing positions at future prices.

Preserve upstream configuration defaults unless a change is explicitly supplied
and recorded as an evaluation input. Setting the starting balance to USD 2,000
must not silently change the upstream risk limits or cash buffer.

### Reuse existing risk checks

`trader-risk/check.py` provides the pre-trade `check` function. Feed it the
simulated portfolio, prices, historical clock, and order history rather than
bypassing its cash, concentration, daily-entry, planned-loss, duplicate, or
entry-window checks. Record both PASS and BLOCK decisions and their reasons.
The upstream planned daily-loss budget is not the same as realized daily P&L;
report those quantities separately.

### Reuse existing historical rules

`trader-timemachine/timemachine.py` contains `history_replay`, `opening_range`,
and `rule_stop`. Its historical replay implements an opening-range breakout:
entry at the first close beyond the opening range, subsequent-bar stop checks
before target checks, and a final close when neither level is reached.

That is the initial replayable strategy to evaluate. Its opening-range length,
stop rule, target R, and cutoff must be explicit and traceable to upstream
configuration or an explicitly selected upstream plan. Do not invent missing
strategy settings or optimize them on the test window.

The existing history output does not provide all account-ledger event details,
including explicit exit timestamps. Any adapter reconstructing those events
must match the original replay outcomes in regression tests and document
intrabar ordering ambiguity. Preserve original upstream files.

## What this test can and cannot evaluate

The initial account backtest evaluates the upstream historical ORB rule plus
its sizing and applicable risk checks under documented execution assumptions.
It does NOT establish the quality of all eleven skills or the entire live,
Claude-assisted workflow.

Explicitly mark these as unevaluated unless a faithful, separately tested
historical implementation is added:

- Historical full-universe scanning, universe membership and ranking.
- Flag/bounce setups not implemented by the upstream historical ORB replay.
- Historical news, filings, financial reports and congressional disclosures.
- Claude's historical judgments and discretionary choices.
- Live broker submission, fills, bracket activation, alerts and user approvals.

Do not use today's headlines, fundamentals, scanner ranking, or present-day
financial knowledge to select or explain historical trades as if those inputs
were known then. A user-selected fixed ticker list must be labeled as such;
it is not a historically reconstructed scanner universe and may be biased.

## Data coverage and look-ahead controls

- The upstream Yahoo request is `range=60d&interval=5m`: 60 calendar days,
  not 60 guaranteed trading sessions.
- The existing full-session filter requires all 78 regular-session bars.
  Report incomplete days, missing bars and excluded half-days explicitly.
- Record actual coverage and per-symbol gaps. Never fill missing price bars
  with fabricated observations or silently present partial coverage as complete.
- Replay events chronologically and expose only bars completed by that time.
  Signal formation and subsequent fill assumptions must be distinguished.
- Full-session eligibility is an ex-post data-quality screen. Disclose that
  exclusion bias; do not present it as a decision the strategy knew in advance.
- Time Machine's Monte Carlo output is separate from historical account P&L.
  Never count its synthetic paths as actual historical account trades.
- A trailing simulation used as an entry filter needs data strictly before
  each trade. Do not train it on the same later bars being evaluated. The
  requested 60-day account window alone may not provide sufficient warm-up.

## Assumptions that must be resolved before a valid run

The executable workflow is not ready until each item is implemented, tested,
and recorded in the run configuration and report:

- Ticker universe and how it was selected.
- Exact upstream rule configuration and historical time window.
- Cash-only account behavior and treatment of proceeds/settlement; do not
  imply that a simple reusable-cash simulation reproduces brokerage restrictions.
- Signal-bar close versus next-bar fill timing. The upstream replay assumes
  the trigger close; retaining that convention must be labeled idealized.
- Slippage, commissions/fees, gaps through stops and target-fill assumptions.
  The upstream replay counts a stop as -1R even if a real gap would lose more.
- Same-bar stop/target ordering, simultaneous events, competing signals,
  overlapping positions, and deterministic cash-allocation ordering.
- Historical entry windows, session cutoffs, time zones and daylight saving.
  Do not silently replace an upstream cutoff with a newly chosen one.
- Raw-data validation, corporate-action/adjustment handling and data gaps.
- Mark-to-market equity and drawdown measurement frequency. A daily-only
  curve must not be described as a measured intraday maximum drawdown.

If a material setting or supported execution path is missing, stop that part
of the evaluation or clearly label it unevaluated. A zero-trade result can be
valid; fabricated trades or silent rule substitutions cannot.

## Required outputs

The eventual manual workflow must retain as downloadable artifacts:

- Source/configuration/data manifest and raw input snapshots for reproduction.
- Every candidate entry, original rule outcome, sizing decision and risk verdict.
- Chronological simulated fills, fees, realized P&L and account cash movements.
- Equity observations with their valuation times and open-position marks.
- Starting/ending balance, total return, drawdown with measurement frequency,
  trade count, win rate, average win/loss, and profit factor where defined.
- Warnings, exclusions, unsupported components and incomplete-data status.

Undefined metrics must be null or explicitly described, never made-up zeroes
or non-standard JSON infinity values. Do not report a partially failed run as
a successful strategy evaluation. Preserve diagnostic artifacts on failure.

## Safety and acceptance criteria

- Simulation only: no broker credentials, broker connectors or actual orders.
- No Claude/Groq/other model calls in the initial deterministic account test.
- Manual workflow dispatch only; no scheduled trading or automatic deployment.
- No GitHub Actions run or backtest has been performed at this documentation step.
- Required tests include continuous account accounting, cash conservation,
  insufficient cash, overlapping positions, risk blocks, upstream sizing
  equivalence, replay equivalence, chronological ordering, malformed data,
  costs, exit ordering and time-zone boundaries.
- Offline synthetic tests do not verify Yahoo availability, real market fills,
  live execution safety, or profitability.

Update Current status when implementation is actually committed and when a
real workflow run has been inspected. Report observed results and limitations;
a backtest is not proof of future profitability.
