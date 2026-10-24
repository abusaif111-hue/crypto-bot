# Project documents

The design documents live as private Claude artifacts in Sultan's account:

- **Binance AI Crypto Trading Bot — Stage 1 Design Package**: architecture, risk rules
  R-01..R-14, threat model, Binance integration plan, schema, staged deployment and gates.
- **BTC and ETH Three-Year Review** (Oct 2023 – Oct 2026, Binance daily candles):
  both coins pass the stability screen; ETH's worst drawdown (−68%) is close to the 70%
  limit; BTC–ETH correlation about 0.85; ETH had 24 days with moves beyond 10%,
  which is why its stop-slippage allowance is 1.0%.
- **Crypto Bot Dashboard**: clickable mock-up of the dashboard website with example data.

In this folder:

- [`h2b-plan.md`](h2b-plan.md): the plan for H2b (blended momentum with a volatility pause)
  and its forward test, after H2 failed the holdout. Approved by Sultan on 23 Oct 2026 and frozen.
- [`h4-plan.md`](h4-plan.md): DRAFT plan for H4 (hourly pullbacks in an uptrend, short-term
  trading) and its own forward test. Waiting for Sultan's decisions; nothing built yet.

`CLAUDE.md` in the project root carries the rules that matter for writing code.
