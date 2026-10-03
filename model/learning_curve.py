#!/usr/bin/env python3
"""Is the reversal model starved of data, and which feature reduction helps?

1. Learning curve: test AUC vs training-set size (random subsamples of the training half), for all
   features and for a small feature set.
2. Model comparison on the training half: the paper's plain logistic regression, L2 / L1 logistic with
   the penalty tuned on the last quarter of the training half, PCA + logistic, and gradient-boosted trees.
   Then the 5 s markout of the orders each phi model selects.

Usage:
    .venv/bin/python model/learning_curve.py --symbol BTCUSDC --config prop-L10ms \\
        --features features_lead-BTCUSDT_delay5ms.parquet --small lead
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import polars as pl
from sklearn.decomposition import PCA
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from fit_reversal import META, DAY_MS, ms, score_table

FRACTIONS = [1 / 16, 1 / 8, 1 / 4, 1 / 2, 1]


def logistic(C=1.0, penalty="l2"):
    solver = "liblinear" if penalty == "l1" else "lbfgs"
    return make_pipeline(StandardScaler(), LogisticRegression(C=C, l1_ratio=1.0 if penalty == "l1" else 0.0,
                                                              solver=solver, max_iter=2000))


def evaluate(model, X, y):
    p = model.predict_proba(X)[:, 1]
    return p, roc_auc_score(y, p), log_loss(y, p)


def tune(make, grid, Xfit, yfit, Xval, yval):
    """Pick the grid value with the best validation log-loss."""
    losses = {g: evaluate(make(g).fit(Xfit, yfit), Xval, yval)[2] for g in grid}
    return min(losses, key=losses.get)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDC")
    ap.add_argument("--config", default="prop-L10ms")
    ap.add_argument("--features", default="features_lead-BTCUSDT_delay5ms.parquet")
    ap.add_argument("--small", default="lead", help="'lead' (lead_* features) or 'paper' (paper |coef| >= 0.1)")
    ap.add_argument("--max-train", type=int, default=300_000, help="subsample the model comparison to this size")
    ap.add_argument("--split", default="2024-02-15T12:00")
    args = ap.parse_args()

    root = Path("results/model") / args.symbol / args.config
    df = pl.read_parquet(root / args.features)
    feats = [c for c in df.columns if c not in META]
    df = df.filter(pl.all_horizontal([pl.col(c).is_finite() for c in feats]) & pl.col("fav_first").is_finite())
    df = df.with_columns(pl.col("fav_first").cast(pl.Int8), pl.col("phi").cast(pl.Int8))
    if args.small == "lead":
        small = [c for c in feats if c.startswith("lead_")]
    else:
        paper = pl.read_csv(Path(__file__).parent / "paper_table5.csv").filter(pl.col("coef").abs() >= 0.1)
        small = [f for f in paper["feature"] if f in feats]
    split = ms(datetime.fromisoformat(args.split))
    train, test = df.filter(pl.col("t_dec") < split), df.filter(pl.col("t_dec") >= split)
    days = (test["t_dec"].max() - test["t_dec"].min()) / DAY_MS
    rng = np.random.default_rng(0)
    print(f"{args.symbol} {args.config}: {train.height:,} train / {test.height:,} test, "
          f"{len(feats)} features, small set = {len(small)} ({args.small})", flush=True)

    curve = []
    for label in ("phi", "fav_first"):
        yte = test[label].to_numpy()
        for frac in FRACTIONS:
            idx = np.sort(rng.choice(train.height, int(train.height * frac), replace=False))
            sub = train[idx]
            for name, cols in (("all", feats), ("small", small)):
                m = logistic().fit(sub.select(cols).to_numpy(), sub[label].to_numpy())
                _, auc, ll = evaluate(m, test.select(cols).to_numpy(), yte)
                curve.append({"label": label, "features": name, "train_rows": sub.height,
                              "positives": int(sub[label].sum()), "auc": auc, "log_loss": ll})
    curve = pl.DataFrame(curve)

    # model comparison: tune on the last quarter of the (subsampled) training half
    tr = train if train.height <= args.max_train else train[np.sort(rng.choice(train.height, args.max_train, False))]
    cut = int(tr.height * 0.75)
    fit_part, val_part = tr[:cut], tr[cut:]
    X = {k: v.select(feats).to_numpy() for k, v in (("tr", tr), ("fit", fit_part), ("val", val_part), ("te", test))}
    comp, picks = [], {}
    for label in ("phi", "fav_first"):
        y = {k: v[label].to_numpy() for k, v in (("tr", tr), ("fit", fit_part), ("val", val_part), ("te", test))}
        base_ll = log_loss(y["te"], np.full(len(y["te"]), y["tr"].mean()))
        c2 = tune(lambda c: logistic(c), [1e-4, 1e-3, 1e-2, 1e-1, 1.0], X["fit"], y["fit"], X["val"], y["val"])
        c1 = tune(lambda c: logistic(c, "l1"), [1e-4, 1e-3, 1e-2, 1e-1], X["fit"], y["fit"], X["val"], y["val"])
        k = tune(lambda k: make_pipeline(StandardScaler(), PCA(k, random_state=0), LogisticRegression(max_iter=2000)),
                 [5, 10, 20, 40], X["fit"], y["fit"], X["val"], y["val"])
        models = {
            "logistic, paper-style": logistic(),
            f"logistic L2 (C={c2:g})": logistic(c2),
            f"logistic L1 (C={c1:g})": logistic(c1, "l1"),
            f"PCA {k} + logistic": make_pipeline(StandardScaler(), PCA(k, random_state=0),
                                                 LogisticRegression(max_iter=2000)),
            "gradient-boosted trees": HistGradientBoostingClassifier(
                learning_rate=0.05, max_iter=500, max_leaf_nodes=31, min_samples_leaf=200,
                early_stopping=True, validation_fraction=0.2, random_state=0),
        }
        for name, m in models.items():
            t0 = time.time()
            m.fit(X["tr"], y["tr"])
            p, auc, ll = evaluate(m, X["te"], y["te"])
            row = {"label": label, "model": name, "auc": auc, "log_loss": ll, "log_loss_base_rate": base_ll,
                   "fit_s": round(time.time() - t0, 1)}
            if "L1" in name:
                row["nonzero_coefs"] = int(np.count_nonzero(m[-1].coef_))
            comp.append(row)
            if label == "phi":
                picks[name] = score_table(test, p, days)
    comp = pl.DataFrame(comp)
    sel = pl.DataFrame([{"model": n, **{f"top{t['top']:g}": round(t["markout_5s_bp"], 3)
                                        for t in tab.to_dicts() if t["top"] in (1.0, 0.1, 0.02, 0.01)}}
                        for n, tab in picks.items()])

    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=200, tbl_hide_dataframe_shape=True,
                   tbl_hide_column_data_types=True, float_precision=4):
        print("\n1) learning curve (plain logistic, random subsamples of the training half)\n", curve)
        print(f"\n2) model comparison (trained on {tr.height:,} rows)\n", comp)
        print("\n3) 5 s markout (bp) of the orders each phi model selects, by top fraction\n", sel)
    out = root / f"learning_curve_{Path(args.features).stem}.json"
    out.write_text(json.dumps({"curve": curve.to_dicts(), "comparison": comp.to_dicts(),
                               "selection": sel.to_dicts()}, indent=1))
    print(f"\nsaved {out}")


if __name__ == "__main__":
    main()
