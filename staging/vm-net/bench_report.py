"""Latency bench report: raw samples JSON -> per-cell stats + markdown table.

Runs on the HOST via `uv run python` (3.12). Overhead per scenario is
median(proxy) - median(direct); warm scenarios have no direct samples of their
own and reuse the corresponding pass-through scenario's direct column."""

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

DIRECT_FALLBACK = {
    "qido_warm": "qido",
    "cmove_warm": "cmove_cold",
    "wado_meta_warm": "wado_meta_cold",
    "wado_frame_warm": "wado_frame_cold",
}

SCENARIO_ORDER = [
    "cfind_study", "cfind_series",
    "cmove_cold", "cmove_warm",
    "qido", "qido_warm",
    "wado_meta_cold", "wado_meta_warm",
    "wado_frame_cold", "wado_frame_warm",
]


def p95(xs):
    xs = sorted(xs)
    return xs[max(0, math.ceil(0.95 * len(xs)) - 1)]


def _cell(cell_samples):
    ok = [smp["t_ms"] for smp in cell_samples if smp["ok"]]
    out = {"n": len(cell_samples), "errors": len(cell_samples) - len(ok)}
    if ok:
        out.update(
            min=min(ok), median=statistics.median(ok), p95=p95(ok),
            mean=statistics.fmean(ok),
            stdev=statistics.stdev(ok) if len(ok) > 1 else 0.0,
        )
    return out


def summarize(samples):
    groups = {}
    for smp in samples:
        groups.setdefault((smp["scenario"], smp["path"]), []).append(smp)
    return {key: _cell(group) for key, group in groups.items()}


def _fmt(cell):
    if not cell or "median" not in cell:
        return "FAILED"
    return f"{cell['median']:.1f} / {cell['p95']:.1f}"


def _errs(cell):
    return f"{cell['errors']}/{cell['n']}" if cell else "-"


def render_markdown(summary, meta):
    lines = [
        "# dicorina latency bench",
        "",
        "reps={reps} move_reps={move_reps} cold_rounds={cold_rounds} "
        "instances_per_study={instances_per_study} studies={studies}".format(**meta),
        "",
        "| scenario | direct median/p95 (ms) | proxy median/p95 (ms) "
        "| overhead (ms) | ratio | errors d/p |",
        "|---|---|---|---|---|---|",
    ]
    for sc in SCENARIO_ORDER:
        proxy = summary.get((sc, "proxy"))
        direct = summary.get((sc, "direct"))
        note = ""
        if (not direct or "median" not in direct) and sc in DIRECT_FALLBACK:
            fallback = summary.get((DIRECT_FALLBACK[sc], "direct"))
            if fallback:
                direct, note = fallback, " *(same as cold)*"
        if proxy is None and direct is None:
            continue
        overhead = ratio = "—"
        if proxy and direct and "median" in proxy and "median" in direct:
            overhead = f"{proxy['median'] - direct['median']:+.1f}"
            ratio = f"{proxy['median'] / direct['median']:.2f}×"  # noqa: RUF001
        lines.append(
            f"| {sc} | {_fmt(direct)}{note} | {_fmt(proxy)} "
            f"| {overhead} | {ratio} | {_errs(direct)} {_errs(proxy)} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples_json")
    parser.add_argument("--out-md", required=True)
    parser.add_argument("--out-json", required=True)
    args = parser.parse_args(argv)

    raw = json.loads(Path(args.samples_json).read_text(encoding="utf-8"))
    meta, samples = raw.get("meta", {}), raw.get("samples", [])
    summary = summarize(samples)
    md = render_markdown(summary, meta)
    Path(args.out_md).write_text(md, encoding="utf-8")
    Path(args.out_json).write_text(
        json.dumps(
            {"meta": meta,
             "cells": {f"{sc}/{path}": cell for (sc, path), cell in summary.items()}},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(md)
    if meta.get("fatal"):
        print(f"FATAL: {meta['fatal']}", file=sys.stderr)
        return 2
    if not any("median" in cell for cell in summary.values()):
        print("no scenario produced a valid sample", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
