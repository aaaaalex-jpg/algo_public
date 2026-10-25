# Local stock-picking recommender — US + EU equities, walk-forward tested

A personal, fully local Python system that scores ~50 large-cap US and European stocks every
morning, runs a paper portfolio on its own rules, and **recommends — it never places orders**.
Everything (data, scores, backtests) runs on a laptop; data comes from free sources
(Yahoo Finance, FRED, ECB).

*Code comments and the console/HTML output are in French. A French README is in `LISEZMOI.md`.*

**Looking for critique, not applause.** See [What I'd like feedback on](#what-id-like-feedback-on).

## What it does

| Module | Role |
|---|---|
| Data pipeline | Daily prices (since 2010), macro (VIX, Fed/ECB rates, EUR/USD), fundamentals snapshot → SQLite |
| Fundamental score | Percentile ranks within the universe: valuation, quality, growth, balance sheet |
| Technical timing | "Buy weakness inside an uptrend": trend (MA50/MA200), pullback (RSI, distance to 52w high), momentum. No uptrend → score capped |
| Daily report | Candidates, entry zone, invalidation level, market regime, EUR/USD alert |
| Paper portfolio | Trades the rules by itself with the same capital as the real one; orders filled at **next open** with fees, FX cost and slippage |
| Backtest | Walk-forward over 2011-2026 with the exact same rules (shared code with the paper portfolio) |

Two pockets: US stocks in a regular brokerage account, EU stocks in a French PEA (tax wrapper).

## Rules (all in `config.py`)

- **Entry:** fundamental ≥ 65, timing ≥ 60, uptrend. Size 3-9 % of the pocket depending on score, 10 % max, 12 positions max per pocket, max 3 new buys per day, 30 % max per sector at entry.
- **Cash is a position:** no buying when the market regime is defensive (VIX ≥ 28, or both S&P 500 and Euro Stoxx 50 below their 200-day MA).
- **Exit (rule "A"):** close more than 3 % below the 200-day MA; or stop at -15 % from cost; or fundamental score < 45 (live only).
- **Trimming:** a position above 20 % of its pocket is cut back to 15 %.
- **Portfolio drawdown guards:** -5 % → only high scores, half size; -8 % → no buying; -10 % → sell losing lines. Then a 1-month pause and a cautious restart (without the pause, a portfolio sitting in cash would stay locked forever — a bug I found the hard way).
- **Order budget:** 50 orders / rolling year; beyond that only protective sells and score ≥ 85 buys.

## Backtest method

- Each evening the simulator only sees data known that day; orders execute at the next open.
- **Walk-forward:** 4 timing thresholds are simulated over the whole period; each year, the setting with the best return/drawdown over the **previous 3 years** is applied to the next year. Only those out-of-sample years are reported.
- Tests check that **modifying future prices never changes past results**, and that backtest and paper portfolio take the **same sell decision on the same day** (`python verifier.py`, 70 checks).
- Benchmarks: an S&P 500 ETF and an Euro Stoxx 50 ETF (dividends included), and **the same universe bought on day one and held**.

## Results (out-of-sample, Jan 2014 → Oct 2026, net of fees)

| | Annual return | Max drawdown | Sharpe (rf = 0) |
|---|---|---|---|
| **This system (walk-forward)** | **+18.4 %** | **-22 %** | 1.17 |
| ETF benchmark (S&P 500 + Euro Stoxx 50) | +12.9 % | -34 % | 0.82 |
| Same universe, bought and held | +29.5 % | -34 % | 1.15 |

~52 orders per year, 75-80 % invested on average. Worst drops: Covid 2020 (-21 %),
tariffs 2025 (-17 %), semiconductors summer 2026 (-22 % before trimming, -13 % after).

## Known limitations — please read before judging the numbers

1. **Survivorship / look-ahead in the universe.** The stock list was written in 2026. It contains
   Nvidia (×700 since 2014), AMD, Broadcom… That is why "same universe bought and held" does
   +29.5 %/yr: it is a hindsight portfolio, impossible to build in 2014. Every result here is
   inflated by this. **The honest fix — testing on point-in-time index constituents — is not done yet.**
2. **No point-in-time fundamentals.** Free sources only give today's ratios, so the backtest tests
   timing + risk rules only, not the fundamental score. The live system stores a daily snapshot to
   measure it later.
3. **2011-2026 has no long bear market.** No 2000-02 or 2008 in the stock-level test.
4. **Many things were tried.** Sell-rule variants, VIX exits, market filters, conditional trimming.
   Each was judged out-of-sample and most were rejected, but trying many ideas on one history is
   itself a form of overfitting.

## What I'd like feedback on

- Best free / cheap source of **point-in-time S&P 500 and STOXX 600 constituents** to kill the survivorship bias.
- Is the walk-forward design (choose among 4 settings on the previous 3 years) sound, or too weak?
- Would you trust the -22 % max drawdown, given the guards trigger only after losses?
- A recession overlay is under study: reduce exposure to 40 % when the US Sahm rule fires **and** the
  index is below its 200-day MA. On indices 1991-2026 it roughly halves the 2000-02 and 2008 losses at
  no long-run cost, but it relies on two real crises only. Thoughts?

## Run it

```bash
pip install -r requirements.txt
python verifier.py                 # offline tests on simulated data
python main.py collecte            # download history (5-10 min the first time)
python main.py tout --ouvrir       # daily scores + report + paper portfolio
python main.py backtest --ouvrir   # walk-forward backtest (1-3 min)
```

Set your own starting capital in `CAPITAL_INITIAL` (`config.py`).

## Disclaimer

Personal research project, shared for discussion. **Not investment advice.** It recommends;
a human decides. Past and simulated performance does not predict future results.
