import json

import bench_report


def s(scenario, path, t_ms, ok=True, rep=0):
    return {"scenario": scenario, "path": path, "rep": rep, "study": None,
            "t_ms": t_ms, "ok": ok, "error": None if ok else "boom"}


META = {"reps": 20, "move_reps": 10, "cold_rounds": 2,
        "big_instances": 1000, "find_studies": 15, "find_instances": 2}


def test_cell_stats_median_p95_errors():
    samples = [s("qido", "proxy", t) for t in [10.0, 20.0, 30.0, 40.0, 100.0]]
    samples.append(s("qido", "proxy", 999.0, ok=False))
    cells = bench_report.summarize(samples)
    cell = cells[("qido", "proxy")]
    assert cell["n"] == 6 and cell["errors"] == 1
    assert cell["median"] == 30.0
    assert cell["p95"] == 100.0  # nearest-rank on 5 ok samples
    assert cell["min"] == 10.0


def test_render_overhead_and_ratio():
    summary = bench_report.summarize(
        [s("qido", "direct", 10.0), s("qido", "proxy", 25.0)]
    )
    md = bench_report.render_markdown(summary, META)
    row = next(line for line in md.splitlines() if line.startswith("| qido "))
    assert "+15.0" in row and "2.50×" in row  # noqa: RUF001


def test_warm_scenario_reuses_cold_direct_column():
    summary = bench_report.summarize(
        [s("wado_frame_cold", "direct", 40.0),
         s("wado_frame_cold", "proxy", 400.0),
         s("wado_frame_warm", "proxy", 8.0)]
    )
    md = bench_report.render_markdown(summary, META)
    row = next(line for line in md.splitlines() if line.startswith("| wado_frame_warm "))
    assert "40.0" in row and "same as cold" in row


def test_failed_cell_rendered_not_dropped():
    summary = bench_report.summarize([s("cfind_study", "proxy", 5.0, ok=False)])
    md = bench_report.render_markdown(summary, META)
    row = next(line for line in md.splitlines() if line.startswith("| cfind_study "))
    assert "FAILED" in row


def test_main_writes_outputs_and_exit_codes(tmp_path):
    good = {"role": "bench", "meta": META,
            "samples": [s("qido", "direct", 10.0), s("qido", "proxy", 25.0)]}
    src = tmp_path / "raw.json"
    src.write_text(json.dumps(good), encoding="utf-8")
    out_md, out_json = tmp_path / "r.md", tmp_path / "r.json"
    rc = bench_report.main([str(src), "--out-md", str(out_md), "--out-json", str(out_json)])
    assert rc == 0
    assert "| qido " in out_md.read_text(encoding="utf-8")
    assert json.loads(out_json.read_text(encoding="utf-8"))["cells"]

    bad = {"role": "bench", "meta": META, "samples": [s("qido", "proxy", 1.0, ok=False)]}
    src.write_text(json.dumps(bad), encoding="utf-8")
    rc = bench_report.main([str(src), "--out-md", str(out_md), "--out-json", str(out_json)])
    assert rc == 2


def test_header_shows_bench_data_shape():
    md = bench_report.render_markdown({}, META)
    header = md.splitlines()[2]
    assert "big_instances=1000" in header
    assert "find_studies=15" in header
    assert "find_instances=2" in header


def test_header_tolerates_legacy_meta():
    legacy = {"reps": 20, "move_reps": 10, "cold_rounds": 2,
              "instances_per_study": 50, "studies": 6}
    md = bench_report.render_markdown({}, legacy)  # must not raise
    assert "reps=20" in md


def test_small_n_flagged_in_cells():
    summary = bench_report.summarize(
        [s("cmove_cold", "direct", 10.0 + i, rep=i) for i in range(2)]
        + [s("qido", "proxy", 5.0, rep=i) for i in range(5)]
    )
    md = bench_report.render_markdown(summary, META)
    cold = next(line for line in md.splitlines() if line.startswith("| cmove_cold "))
    qido = next(line for line in md.splitlines() if line.startswith("| qido "))
    assert "(n=2)" in cold
    assert "(n=" not in qido


def test_wipe_failure_row_rendered():
    summary = bench_report.summarize([s("wipe", "proxy", 0.0, ok=False)])
    md = bench_report.render_markdown(summary, META)
    row = next(line for line in md.splitlines() if line.startswith("| wipe "))
    assert "FAILED" in row
