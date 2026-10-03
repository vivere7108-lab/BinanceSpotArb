#!/usr/bin/env python3
"""Does a price-leading market's state improve the reversal model on a follower market?

Fits the phi (paper label) and fav_first (factor 1) models on the same orders with three feature sets:
own (the paper's 171 features), own + lead_* (the leader's state, e.g. BTCUSDT), and lead_* alone.
Then compares the orders each phi model selects: fill rate and 5 s post-fill markout.

Usage:
    .venv/bin/python model/leader_test.py --symbol BTCUSDC --config prop-L10ms \\
        --features features_lead-BTCUSDT_delay5ms.parquet
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import polars as pl
from sklearn.metrics import log_loss, roc_auc_score

from fit_reversal import META, DAY_MS, fit, ms, score_table


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDC")
    ap.add_argument("--config", default="prop-L10ms")
    ap.add_argument("--features", default="features_lead-BTCUSDT_delay5ms.parquet")
    ap.add_argument("--split", default="2024-02-15T12:00", help="train before, test after (UTC)")
    args = ap.parse_args()

    root = Path("results/model") / args.symbol / args.config
    df = pl.read_parquet(root / args.features)
    feats = [c for c in df.columns if c not in META]
    lead = [c for c in feats if c.startswith("lead_")]
    own = [c for c in feats if c not in lead]
    df = df.filter(pl.all_horizontal([pl.col(c).is_finite() for c in feats]) & pl.col("fav_first").is_finite())
    df = df.with_columns(pl.col("fav_first").cast(pl.Int8), pl.col("phi").cast(pl.Int8))
    split = ms(datetime.fromisoformat(args.split))
    train, test = df.filter(pl.col("t_dec") < split), df.filter(pl.col("t_dec") >= split)
    days = (test["t_dec"].max() - test["t_dec"].min()) / DAY_MS
    print(f"{args.symbol} {args.config} {args.features}: {train.height:,} train / {test.height:,} test orders, "
          f"{len(own)} own + {len(lead)} leader features")

    sets = {"own": own, "own+lead": own + lead, "lead": lead}
    perf, phi_scores, coefs = [], {}, {}
    for label in ("phi", "fav_first"):
        y = test[label].to_numpy()
        for name, cols in sets.items():
            m = fit(train.select(cols).to_numpy(), train[label].to_numpy())
            p = m.predict_proba(test.select(cols).to_numpy())[:, 1]
            perf.append({"label": label, "features": name, "auc": roc_auc_score(y, p), "log_loss": log_loss(y, p),
                         "log_loss_base_rate": log_loss(y, np.full_like(p, y.mean()))})
            if label == "phi":
                phi_scores[name] = p
            if name == "own+lead":
                coefs[label] = dict(zip(cols, m[-1].coef_[0]))
    perf = pl.DataFrame(perf)

    tabs = {name: score_table(test, phi_scores[name], days) for name in ("own", "own+lead")}
    side_by_side = tabs["own"].select("top", "orders_per_day", fill_own="fill_rate", markout_own="markout_5s_bp").join(
        tabs["own+lead"].select("top", fill_lead="fill_rate", markout_lead="markout_5s_bp", p_lead="p_value"),
        on="top")
    lead_coefs = pl.DataFrame({"feature": lead, "phi": [coefs["phi"][c] for c in lead],
                               "fav_first": [coefs["fav_first"][c] for c in lead]}).sort(
        pl.col("phi").abs(), descending=True)
    # does the follower's next tick go the way the leader already went?
    gap = test.select(pl.when(pl.col("lead_gap") > 0).then(pl.lit("leader moved in our favour"))
                      .when(pl.col("lead_gap") < 0).then(pl.lit("leader moved against us"))
                      .otherwise(pl.lit("leader unchanged")).alias("since follower's last move"),
                      "fav_first", "phi", "filled")
    gap = gap.group_by("since follower's last move").agg(
        share=pl.len() / test.height, fav_first=pl.col("fav_first").mean(), fill=pl.col("filled").mean(),
        phi=pl.col("phi").mean()).sort("since follower's last move")

    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=200, tbl_hide_dataframe_shape=True,
                   tbl_hide_column_data_types=True, float_precision=4):
        print("\n1) test AUC / log-loss by feature set\n", perf)
        print("\n2) orders selected by the phi model: own features vs own + leader\n", side_by_side)
        print("\n3) leader coefficients (standardised) in the own + leader models\n", lead_coefs)
        print("\n4) next-tick direction vs what the leader did since the follower last moved\n", gap)

    out = root / f"leader_report_{Path(args.features).stem}.json"
    out.write_text(json.dumps({"perf": perf.to_dicts(), "selection": side_by_side.to_dicts(),
                               "lead_coefs": lead_coefs.to_dicts(), "lead_gap": gap.to_dicts()}, indent=1))
    print(f"\nsaved {out}")


if __name__ == "__main__":
    main()
