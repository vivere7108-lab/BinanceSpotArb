#!/usr/bin/env python3
"""Compare simulated continuous-quoting orders with the paper's published statistics.

The paper (Albers et al., arXiv 2502.18625v2) ran real min-size orders on BTCUSDT, Feb 12-19 2024.
Simulating the same week under each queue model shows which model reproduces their real fills.

Usage:
    .venv/bin/python sim/validate_paper.py --symbol BTCUSDT   # every config under results/sim/BTCUSDT
"""

import argparse
import json
from pathlib import Path

import numpy as np
import polars as pl

PAPER = {
    "orders_per_day": 232_897 / 7,
    "fill_rate": 127_051 / 232_897,
    "markout_per_fill_bp": -0.8,           # Sec. 6, "approximately -0.8 bp"
    "fig11_fill_prob_ret_neg": 0.8,        # read off Fig. 11 (returns 5 s after terminal time)
    "fig11_fill_prob_ret_pos": 0.2,
    "surface_min": 0.30, "surface_max": 0.90,  # Sec. 4: "as low as 30%", "exceeds 90%"
    "surface_beta_imb": -0.3166, "surface_r2": 0.946,
    "D_share": 182_381 / 232_897, "D_fill_rate": 0.8824, "D_ret5s_bp": -0.74, "D_reversal_share": 0.15,
}
# Table 1: mean 1 s markout (bp) by queue sizes at fill and queue position at fill.
TABLE1 = {
    ("large", "small"): [-0.058, -0.586, -0.743, -0.775],
    ("large", "large"): [-0.296, -0.882, -0.967, -1.157],
    ("small", "small"): [-0.562, -0.711, -0.622, -0.677],
    ("small", "large"): [-0.539, -0.645, -0.686, -0.763],
}
QP_BREAKS = [0.1, 0.4, 0.75]
QP_LABELS = ["0-0.1", "0.1-0.4", "0.4-0.75", "0.75-1"]


def surface(df, bins=20, min_n=30):
    """Fill rate on an equal-width (Q_near, Q_opp) grid at post time, as in the paper's Fig. 4."""
    q = df.with_columns(qn=pl.col("q_near_post") * pl.col("price"), qo=pl.col("q_opp_post") * pl.col("price"))
    caps = {c: float(q[c].quantile(0.99)) for c in ("qn", "qo")}
    q = q.with_columns([(pl.col(c).clip(0, caps[c]) / caps[c] * bins).floor().clip(0, bins - 1)
                        .cast(pl.Int32).alias(f"{c}_bin") for c in ("qn", "qo")])
    grid = q.group_by("qn_bin", "qo_bin").agg(fill=(pl.col("status") == "fill").mean(), n=pl.len())
    grid = grid.filter(pl.col("n") >= min_n).with_columns(
        qn=(pl.col("qn_bin") + 0.5) / bins, qo=(pl.col("qo_bin") + 0.5) / bins)
    grid = grid.with_columns(imb=(pl.col("qn") - pl.col("qo")) / (pl.col("qn") + pl.col("qo")))
    X = np.column_stack([np.ones(len(grid)), grid["qn"], grid["qo"], grid["imb"]])
    y = grid["fill"].to_numpy()
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    r2 = 1 - np.sum((y - X @ beta) ** 2) / np.sum((y - y.mean()) ** 2)
    return q, grid, beta, r2


def evaluate(df):
    days = df["t_post"].max() - df["t_post"].min()
    done = df.filter(pl.col("status").is_in(["fill", "cancel"]))
    fills = done.filter(pl.col("status") == "fill")
    res = {
        "orders_per_day": len(done) / (days / 86_400_000),
        "fill_rate": (done["status"] == "fill").mean(),
        "markout_per_fill_bp": fills["mo_mid_5s"].mean(),
        "markout_1s_bp": fills["mo_mid_1s"].mean(),
        "rejects_per_day": (df["status"] == "reject").sum() / (days / 86_400_000),
        "fill_kinds": dict(fills["fill_kind"].cast(pl.String).value_counts().iter_rows()),
    }
    ret = done["ret_mid_5s_from_end"]
    is_fill = done["status"] == "fill"
    res["fig11_fill_prob_ret_neg"] = is_fill.filter((ret < -0.5) & (ret > -5)).mean()
    res["fig11_fill_prob_ret_pos"] = is_fill.filter((ret > 0.5) & (ret < 5)).mean()

    q, grid, beta, r2 = surface(done)
    res["surface_min"], res["surface_max"] = grid["fill"].min(), grid["fill"].max()
    res["surface_beta"] = dict(zip(["const", "q_near", "q_opp", "imb"], beta.round(4).tolist()))
    res["surface_beta_imb"], res["surface_r2"] = float(beta[3]), float(r2)

    # D: orders whose (binned, in-sample) fill probability exceeds 0.8, as in Sec. 7.1
    q = q.join(grid.select("qn_bin", "qo_bin", p_fill="fill"), on=["qn_bin", "qo_bin"], how="left")
    D = q.filter(pl.col("p_fill") > 0.8)
    res["D_share"] = len(D) / len(q)
    res["D_fill_rate"] = (D["status"] == "fill").mean()
    res["D_ret5s_bp"] = D.filter(pl.col("status") == "fill")["mo_mid_5s"].mean()
    res["D_reversal_share"] = D["phi"].mean()

    # Table 1 grid on fills: terciles of queue size at fill (pooled near+opp), queue-position buckets
    sizes = np.concatenate([fills["q_near_fill"].to_numpy(), fills["q_opp_fill"].to_numpy()])
    lo, hi = np.quantile(sizes, [1 / 3, 2 / 3])
    size_bin = lambda c: pl.when(pl.col(c) <= lo).then(pl.lit("small")).when(pl.col(c) > hi) \
        .then(pl.lit("large")).otherwise(pl.lit("medium"))
    t1 = (fills.with_columns(near=size_bin("q_near_fill"), opp=size_bin("q_opp_fill"),
                             qp=pl.col("qp_fill").cut(QP_BREAKS, labels=QP_LABELS, left_closed=True))
          .group_by("near", "opp", "qp").agg(mo=pl.col("mo_mid_1s").mean(), n=pl.len()))
    cells = []
    for (near, opp), paper_vals in TABLE1.items():
        for qp, pv in zip(QP_LABELS, paper_vals):
            row = t1.filter((pl.col("near") == near) & (pl.col("opp") == opp) & (pl.col("qp") == qp))
            cells.append((near, opp, qp, pv, row["mo"][0] if len(row) else None, row["n"][0] if len(row) else 0))
    sim_vals = np.array([c[4] for c in cells], dtype=float)
    paper_vals = np.array([c[3] for c in cells])
    ok = ~np.isnan(sim_vals)
    res["table1_corr"] = float(np.corrcoef(sim_vals[ok], paper_vals[ok])[0, 1])
    res["table1_mae_bp"] = float(np.abs(sim_vals[ok] - paper_vals[ok]).mean())
    res["table1_cells"] = cells
    return res


def fmt(v):
    if isinstance(v, float):
        return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:.3f}"
    return str(v)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--results", default="results/sim")
    args = ap.parse_args()

    root = Path(args.results) / args.symbol
    configs = sorted(p.name for p in root.iterdir() if p.is_dir())
    results = {c: evaluate(pl.read_parquet(root / c / "*.parquet")) for c in configs}

    keys = ["orders_per_day", "fill_rate", "markout_per_fill_bp", "fig11_fill_prob_ret_neg",
            "fig11_fill_prob_ret_pos", "surface_min", "surface_max", "surface_beta_imb", "surface_r2",
            "D_share", "D_fill_rate", "D_ret5s_bp", "D_reversal_share", "table1_corr", "table1_mae_bp"]
    rows = [{"metric": k, "paper": fmt(PAPER[k]) if k in PAPER else "-",
             **{c: fmt(results[c][k]) for c in configs}} for k in keys]
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=250, tbl_hide_dataframe_shape=True,
                   tbl_hide_column_data_types=True):
        print(pl.DataFrame(rows))
        for c in configs:
            print(f"\n{c}: fill kinds {results[c]['fill_kinds']}, surface beta {results[c]['surface_beta']}, "
                  f"rejects/day {results[c]['rejects_per_day']:.0f}, 1s markout {results[c]['markout_1s_bp']:.3f}")
        best = max(configs, key=lambda c: -abs(results[c]["fill_rate"] - PAPER["fill_rate"]))
        print(f"\nTable 1, 1 s markout (bp), paper vs {best}:")
        print(pl.DataFrame(results[best]["table1_cells"], schema=["near", "opp", "qp", "paper", "sim", "n"],
                           orient="row").with_columns(pl.col("sim").round(3)))
    out = root / "validation.json"
    out.write_text(json.dumps(results, indent=1, default=str))
    print(f"\nsaved {out}")


if __name__ == "__main__":
    main()
