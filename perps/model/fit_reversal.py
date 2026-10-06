#!/usr/bin/env python3
"""Fit the paper's reversal model on simulated orders and compare it with the paper.

Replicates Albers et al. Sec. 7 on synthetic labels: logistic regression on standardised features,
first half of the week for training, second half for testing. Reports:
    1. test AUC / log-loss for the paper's label (phi) and for factor 1 alone (fav_first)
    2. a Table 3 analogue: orders selected by model score -> fill rate and 5 s post-fill markout
    3. our coefficients vs the paper's Table 5
    4. recency: walk-forward test of 1-day vs 3-day training windows
    5. a 20-feature model (the paper's |coef| >= 0.1 features) vs all 171

Usage:
    .venv/bin/python model/fit_reversal.py --symbol BTCUSDT --config prop-L10ms
"""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import polars as pl
from scipy import stats
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

META = ["t_dec", "side", "price", "q_near_post", "q_opp_post", "p_fill_surface", "phi", "filled", "mo_mid_5s",
        "fav_first"]
DIRECTIONAL = ("ret_sum", "ret_vwap")  # sign depends on the (unstated) direction convention
TOP_FRACTIONS = [1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002]
DAY_MS = 86_400_000


def ms(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def fit(X, y):
    return make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=2000)).fit(X, y)


def score_table(df, score, days):
    """Table 3 analogue: keep the top fraction of orders by score; fill rate and 5 s markout of fills."""
    base = df.filter(pl.col("filled"))["mo_mid_5s"].to_numpy()
    order = np.argsort(-score)
    rows = []
    for frac in TOP_FRACTIONS:
        sel = df[order[: max(1, int(len(df) * frac))]]
        mo = sel.filter(pl.col("filled"))["mo_mid_5s"].to_numpy()
        t, p = stats.ttest_ind(mo, base, equal_var=False) if frac < 1 and len(mo) > 2 else (0.0, 1.0)
        rows.append({"top": frac, "score_cut": float(score[order[len(sel) - 1]]), "orders_per_day": len(sel) / days,
                     "fill_rate": sel["filled"].mean(), "markout_5s_bp": float(mo.mean()), "std_bp": float(mo.std()),
                     "p_value": float(p), "phi_rate": sel["phi"].mean()})
    return pl.DataFrame(rows)


def compare_coefficients(model, features):
    paper = pl.read_csv(Path(__file__).parent / "paper_table5.csv")
    ours = pl.DataFrame({"feature": features, "ours": model[-1].coef_[0]})
    c = paper.join(ours, on="feature", how="inner")
    directional = pl.col("feature").str.starts_with(DIRECTIONAL[0]) | pl.col("feature").str.starts_with(DIRECTIONAL[1])
    out = {"n_common": c.height}
    for name, sub in (("all", c), ("sign_invariant", c.filter(~directional)), ("directional", c.filter(directional))):
        out[f"pearson_{name}"] = float(np.corrcoef(sub["coef"], sub["ours"])[0, 1])
        out[f"spearman_{name}"] = float(stats.spearmanr(sub["coef"], sub["ours"]).statistic)
    big = c.filter(pl.col("coef").abs() >= 0.1)
    out["sign_agreement_paper_abs_ge_0.1"] = f"{int((np.sign(big['coef']) == np.sign(big['ours'])).sum())}/{big.height}"
    top = c.sort(pl.col("coef").abs(), descending=True).head(15).with_columns(pl.col("ours").round(3))
    return out, top


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--config", default="prop-L10ms")
    ap.add_argument("--split", default="2024-02-15T12:00", help="train before, test after (UTC)")
    args = ap.parse_args()

    root = Path("results/model") / args.symbol / args.config
    df = pl.read_parquet(root / "features.parquet")
    features = [c for c in df.columns if c not in META]
    df = df.filter(pl.all_horizontal([pl.col(c).is_finite() for c in features]) & pl.col("fav_first").is_finite())
    df = df.with_columns(pl.col("fav_first").cast(pl.Int8), pl.col("phi").cast(pl.Int8))
    split = ms(datetime.fromisoformat(args.split))
    train, test = df.filter(pl.col("t_dec") < split), df.filter(pl.col("t_dec") >= split)
    test_days = (test["t_dec"].max() - test["t_dec"].min()) / DAY_MS
    Xtr, Xte = train.select(features).to_numpy(), test.select(features).to_numpy()
    report = {"config": args.config, "train_rows": train.height, "test_rows": test.height, "features": len(features)}
    print(f"{args.config}: {train.height:,} train / {test.height:,} test orders in D, {len(features)} features")

    models, perf = {}, []
    for label in ("phi", "fav_first"):
        m = fit(Xtr, train[label].to_numpy())
        p = m.predict_proba(Xte)[:, 1]
        y = test[label].to_numpy()
        base = np.full_like(p, y.mean())
        perf.append({"label": label, "base_rate": float(y.mean()), "auc": roc_auc_score(y, p),
                     "log_loss": log_loss(y, p), "log_loss_base_rate": log_loss(y, base)})
        models[label] = (m, p)
    perf = pl.DataFrame(perf)

    top_feats = pl.read_csv(Path(__file__).parent / "paper_table5.csv").filter(pl.col("coef").abs() >= 0.1)
    small = [f for f in top_feats["feature"] if f in features]
    m_small = fit(train.select(small).to_numpy(), train["phi"].to_numpy())
    auc_small = roc_auc_score(test["phi"].to_numpy(), m_small.predict_proba(test.select(small).to_numpy())[:, 1])

    tables = {label: score_table(test, models[label][1], test_days) for label in models}
    coef_stats, top15 = compare_coefficients(models["phi"][0], features)

    # recency: walk-forward over the test days, 1-day vs 3-day training windows
    walk = []
    day0 = ms(datetime.fromisoformat(args.split[:10]))
    for d in range(4):
        lo = day0 + d * DAY_MS if d else split
        hi = day0 + (d + 1) * DAY_MS
        day = df.filter((pl.col("t_dec") >= lo) & (pl.col("t_dec") < hi))
        if day.height == 0:
            continue
        for window in (1, 3):
            tr = df.filter((pl.col("t_dec") >= lo - window * DAY_MS) & (pl.col("t_dec") < lo))
            m = fit(tr.select(features).to_numpy(), tr["phi"].to_numpy())
            p = m.predict_proba(day.select(features).to_numpy())[:, 1]
            walk.append({"test_day_start": datetime.fromtimestamp(lo / 1000, tz=timezone.utc).strftime("%m-%d %H:%M"),
                         "train_days": window, "train_rows": tr.height,
                         "auc": roc_auc_score(day["phi"].to_numpy(), p), "log_loss": log_loss(day["phi"].to_numpy(), p)})
    walk = pl.DataFrame(walk)

    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=200, tbl_hide_dataframe_shape=True,
                   tbl_hide_column_data_types=True, float_precision=4):
        print("\n1) test-half performance\n", perf)
        print(f"   20-feature phi model (paper |coef| >= 0.1, {len(small)} features): AUC {auc_small:.4f}")
        for label, tab in tables.items():
            print(f"\n2) orders selected by the {label} model (test half; paper Table 3 analogue)\n", tab)
        print("\n3) coefficients vs paper Table 5:", json.dumps(coef_stats, indent=1))
        print(top15)
        print("\n4) walk-forward recency test (phi model)\n", walk)
        print(walk.group_by("train_days").agg(pl.col("auc").mean(), pl.col("log_loss").mean()).sort("train_days"))

    report.update(perf=perf.to_dicts(), auc_20_features=auc_small, coef=coef_stats,
                  tables={k: v.to_dicts() for k, v in tables.items()}, walk_forward=walk.to_dicts())
    (root / "report.json").write_text(json.dumps(report, indent=1, default=str))
    print(f"\nsaved {root / 'report.json'}")


if __name__ == "__main__":
    main()
