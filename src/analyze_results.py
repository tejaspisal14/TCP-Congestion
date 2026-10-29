#!/usr/bin/env python3
"""Summarize measured TCP congestion-control runs and create paper-ready plots."""
from __future__ import annotations
import argparse
import math
from pathlib import Path
import sys
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.stats import t

METRICS = {
    "throughput_mbps": ("Receiver throughput (Mbit/s)", "Throughput (Mbit/s)"),
    "rtt_mean_ms": ("Mean sampled TCP RTT (ms)", "RTT (ms)"),
    "retransmits": ("iperf3 sender retransmission count", "Retransmissions"),
    "cwnd_median_segments": ("Median sampled cwnd (segments)", "Median congestion window (segments)"),
}


def mean_ci(values: pd.Series) -> dict[str, float | int]:
    x = pd.to_numeric(values, errors="coerce").dropna().astype(float)
    n = len(x)
    if n == 0:
        return {"n": 0, "mean": np.nan, "sd": np.nan, "ci95_low": np.nan, "ci95_high": np.nan}
    mu = float(x.mean())
    sd = float(x.std(ddof=1)) if n > 1 else np.nan
    if n > 1 and not math.isnan(sd):
        half = float(t.ppf(0.975, n - 1) * sd / math.sqrt(n))
        low, high = mu - half, mu + half
    else:
        low, high = np.nan, np.nan
    return {"n": n, "mean": mu, "sd": sd, "ci95_low": low, "ci95_high": high}


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    data = df[df["test_type"].astype(str) == "single_flow"].copy()
    rows = []
    for (scenario, algorithm), group in data.groupby(["scenario", "algorithm"], dropna=False):
        row: dict[str, object] = {"scenario": scenario, "algorithm": algorithm, "n_runs": int(len(group))}
        for metric in METRICS:
            stats = mean_ci(group[metric]) if metric in group else mean_ci(pd.Series(dtype=float))
            for key, value in stats.items():
                row[f"{metric}_{key}"] = value
        rows.append(row)
    return pd.DataFrame(rows).sort_values(["scenario", "algorithm"]) if rows else pd.DataFrame()


def calculate_fairness(df: pd.DataFrame) -> pd.DataFrame:
    fair = df[df["test_type"].astype(str) == "mixed_fairness"].copy()
    rows = []
    if fair.empty:
        return pd.DataFrame(columns=["run_id", "repetition", "jain_fairness_index", "total_throughput_mbps", "flow_count"])
    for run_id, group in fair.groupby("run_id"):
        values = pd.to_numeric(group["throughput_mbps"], errors="coerce").dropna().to_numpy(dtype=float)
        if len(values) == 0 or float(np.square(values).sum()) == 0:
            index = np.nan
        else:
            index = float(values.sum() ** 2 / (len(values) * np.square(values).sum()))
        rows.append({
            "run_id": run_id,
            "repetition": group["repetition"].iloc[0] if "repetition" in group else np.nan,
            "jain_fairness_index": index,
            "total_throughput_mbps": float(values.sum()) if len(values) else np.nan,
            "flow_count": int(len(values)),
        })
    return pd.DataFrame(rows)


def plot_metric(df: pd.DataFrame, metric: str, label: str, output: Path) -> bool:
    data = df[df["test_type"].astype(str) == "single_flow"].copy()
    if metric not in data or data.empty or pd.to_numeric(data[metric], errors="coerce").notna().sum() == 0:
        return False
    order = list(dict.fromkeys(data["scenario"].astype(str).tolist()))
    algorithms = [a for a in ["Reno", "CUBIC", "BBR"] if a in set(data["algorithm"].astype(str))]
    fig, ax = plt.subplots(figsize=(9, 5.2))
    positions = np.arange(len(order))
    offset_span = 0.24 if len(algorithms) == 3 else 0.16
    offsets = np.linspace(-offset_span, offset_span, len(algorithms)) if algorithms else []
    plotted = False
    for alg, offset in zip(algorithms, offsets):
        means, errors = [], []
        for scenario in order:
            group = data[(data["scenario"].astype(str) == scenario) & (data["algorithm"].astype(str) == alg)]
            stats = mean_ci(group[metric])
            means.append(stats["mean"])
            if pd.notna(stats["ci95_low"]) and pd.notna(stats["ci95_high"]):
                errors.append((stats["ci95_high"] - stats["ci95_low"]) / 2)
            else:
                errors.append(0.0)
        values = np.array(means, dtype=float)
        if np.isfinite(values).any():
            ax.errorbar(positions + offset, values, yerr=errors, marker="o", capsize=3, linewidth=1.6, label=alg)
            plotted = True
    if not plotted:
        plt.close(fig)
        return False
    ax.set_xticks(positions, order, rotation=20, ha="right")
    ax.set_ylabel(label)
    ax.set_xlabel("Experiment scenario")
    ax.set_title(f"{label} by scenario (mean with 95% CI)")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(output, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return True


def plot_fairness(fair: pd.DataFrame, output: Path) -> bool:
    vals = pd.to_numeric(fair.get("jain_fairness_index", pd.Series(dtype=float)), errors="coerce").dropna()
    if vals.empty:
        return False
    stats = mean_ci(vals)
    fig, ax = plt.subplots(figsize=(5.8, 4.4))
    yerr = [[stats["mean"] - stats["ci95_low"]], [stats["ci95_high"] - stats["mean"]]] if pd.notna(stats["ci95_low"]) else None
    ax.bar(["Reno + CUBIC + BBR"], [stats["mean"]], yerr=yerr, capsize=4, width=0.5)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("Jain's fairness index (1 = equal throughput)")
    ax.set_title("Mixed-flow throughput fairness (mean with 95% CI)")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(output, dpi=300, bbox_inches="tight")
    plt.close(fig)
    return True


def write_report(summary: pd.DataFrame, fairness: pd.DataFrame, output: Path, input_path: Path) -> None:
    lines = [
        "# TCP Congestion-Control Experiment Summary",
        "",
        f"Source file: `{input_path}`",
        "",
        "This report is generated from the CSV supplied to the analysis script. It does not create or infer experimental observations.",
        "",
        "## Measurement coverage",
    ]
    if summary.empty:
        lines += ["No single-flow runs were found.", ""]
    else:
        lines += [f"Single-flow scenario/algorithm groups: **{len(summary)}**", "", summary.to_markdown(index=False), ""]
    lines += ["## Mixed-flow fairness", ""]
    if fairness.empty:
        lines += ["No mixed-flow fairness repetitions were found. Run the experiment runner without `--skip-fairness`.", ""]
    else:
        lines += [fairness.to_markdown(index=False), ""]
        valid = pd.to_numeric(fairness["jain_fairness_index"], errors="coerce").dropna()
        if not valid.empty:
            lines += [f"Mean Jain's index across measured mixed-flow runs: **{valid.mean():.4f}** (n={len(valid)}).", ""]
    lines += [
        "## Interpretation checklist",
        "",
        "- Discuss results using measured means, uncertainty, and the particular scenario; do not claim one algorithm is universally best.",
        "- Report unavailable RTT/cwnd values as missing rather than substituting zeros.",
        "- Verify kernel, congestion-control variant, qdisc, offload settings, and system load before comparing runs across machines.",
        "- The configured loss value is an injected impairment, not a measured packet-loss rate. The retransmission count is a distinct metric.",
        "",
    ]
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="results/runs.csv", help="CSV written by run_experiments.py")
    parser.add_argument("--output-dir", default="results/analysis", help="Directory for summaries and plots")
    args = parser.parse_args()
    source = Path(args.input)
    if not source.exists():
        parser.error(f"input CSV does not exist: {source}")
    try:
        df = pd.read_csv(source)
    except Exception as exc:
        parser.error(f"could not read {source}: {exc}")
    required = {"run_id", "test_type", "scenario", "algorithm", "throughput_mbps"}
    missing = sorted(required - set(df.columns))
    if missing:
        parser.error("input CSV is missing required columns: " + ", ".join(missing))
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = summarize(df)
    fairness = calculate_fairness(df)
    summary.to_csv(out / "summary_by_scenario_algorithm.csv", index=False)
    fairness.to_csv(out / "fairness_by_run.csv", index=False)
    made = []
    for metric, (long_label, short_label) in METRICS.items():
        filename = f"{metric}.png"
        if plot_metric(df, metric, short_label, out / filename):
            made.append(filename)
    if plot_fairness(fairness, out / "jain_fairness.png"):
        made.append("jain_fairness.png")
    write_report(summary, fairness, out / "RESULTS_REPORT.md", source)
    print(f"Analyzed {len(df)} flow rows from {source}")
    print(f"Summary: {out / 'summary_by_scenario_algorithm.csv'}")
    print(f"Fairness: {out / 'fairness_by_run.csv'}")
    print(f"Report: {out / 'RESULTS_REPORT.md'}")
    print("Plots: " + (", ".join(made) if made else "none (metrics unavailable in current CSV)"))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
