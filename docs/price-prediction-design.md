# Card price-prediction modeling — design + viability (2026-06-01)

Goal (from project memory): forecast where an OP/PTCG card's price is heading,
plus a user-facing layer that explains *why* a card moves in plain economic
terms. Reference techniques: stefan-jansen/machine-learning-for-trading
(feature engineering + gradient boosting/time-series); TauricResearch/
TradingAgents (multi-agent debate → synthesized rationale, for the explainer).

## Decisive finding: forecasting is DATA-GATED right now — do NOT train a model yet

Measured the existing price-history tables in D1 (`optcg-cards`) on 2026-06-01:

| Table | rows | cards | window | snapshots |
|---|---|---|---|---|
| `card_price_history` (OPTCG) | 30,547 | 4,576 | 2026-04-17 → 2026-06-01 | **8 distinct weekly days** |
| `ptcg_price_history` (PTCG) | ~0 / barely seeded | — | — | effectively none |

That is **~8 weekly points per card over ~6 weeks** for OPTCG, and less for
PTCG. A trained forecaster (LightGBM/LSTM/ARIMA) on 6–8 points would overfit
and emit confident-looking numbers detached from reality — the exact
plausible-but-wrong output the pricing honesty rail forbids
([[feedback-no-plausible-wrong-prices]]). **A forecast on this much data would
be malpractice.** Forecasting becomes defensible after ~6–12 months of weekly
accumulation (≈26–52 points/card). The weekly Monday refresh already grows it.

So "beginning the modeling" correctly = build the foundation that makes a model
trainable later + ship only the honest, data-justified piece now.

## What to build NOW (justified by current data, no model training)

1. **Keep accumulating history** — the weekly TCGPlayer refresh (OPTCG) and the
   PTCG refresh DAG already append to `*_price_history`. Verify PTCG is
   actually writing snapshots (the table looked empty — likely the only real
   bug to fix here). This is the single most valuable action: time is the input.
2. **Momentum indicator (honest, not a forecast)** — Δ% over the available
   window + direction, computed from `*_price_history`. Label it "recent trend"
   / "30-day change", NEVER "prediction". Surfacable in the existing
   `PriceHistoryChart` (it already plots the series). Zero new deps.
3. **Driver/feature schema** — a `card_signals` table (set age, rarity, reprint
   events, rotation/legality status, release cadence, pop-report when available).
   The explainer needs these MORE than history depth does. Sourcing the driver
   data (rotation, reprints, meta results) is its own task.

## What to build LATER (gated on ≥6 months history)

4. **Per-game forecaster** — lean: LightGBM on lagged/rolling features
   (technique from ML-for-trading, NOT its Zipline/TF stack). Output a trend +
   confidence band, never a hard "$X". Offline job → writes labeled forecasts
   to a `card_forecast` table → served read-only. Runs offline (Python), not in
   the Worker. **New deps to approve when we get here: lightgbm, pandas, numpy.**
5. **Economic-terms explainer** — adapt the TradingAgents *pattern* (analyst →
   bull/bear → synthesizer), NOT the dependency. Implement as Claude API calls
   (already in the stack) over the `card_signals` + history + forecast. Output
   a labeled analyst-style note. CAVEAT: only as good as the driver data fed in
   — without real signals it produces plausible-but-ungrounded narratives (the
   same trap, applied to analysis). Gate on the `card_signals` table being real.

## Honesty rail (carries through)

A FORECAST is a prediction (labeled, with a confidence band) and the explainer
output is analysis — both must be visually + semantically DISTINCT from real
market / last-sold prices. Never render a model output as an actual sale/quote.

## Bottom line

Forecasting is not buildable yet — the data isn't there. The right "begin" is:
(1) make sure PTCG history is actually being captured, (2) ship the honest
momentum indicator, (3) stand up the driver-signal schema. Revisit the trained
model + agent explainer once we have ~6 months of weekly snapshots.
