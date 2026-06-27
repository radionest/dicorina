import json
import os

import agent_core


def test_barrier_signal_creates_file(tmp_path):
    agent_core.barrier_signal(str(tmp_path), "a")
    assert (tmp_path / "ready_a").exists()


def test_barrier_wait_all_true_when_all_present(tmp_path):
    agent_core.barrier_signal(str(tmp_path), "a")
    agent_core.barrier_signal(str(tmp_path), "b")
    ok = agent_core.barrier_wait_all(str(tmp_path), ["a", "b"], timeout=5, sleep=lambda *_: None)
    assert ok is True


def test_barrier_wait_all_times_out_without_real_sleep(tmp_path):
    ticks = iter([0, 1, 2, 3, 4, 5, 6])
    ok = agent_core.barrier_wait_all(
        str(tmp_path),
        ["a", "b"],
        timeout=5,
        sleep=lambda *_: None,
        clock=lambda: next(ticks),
    )
    assert ok is False


def test_record_received_accumulates_per_phase(tmp_path):
    r = agent_core.new_result("clienta", "CLIENTA")
    agent_core.record_received(r, "s1", "1.2.3", "CLARINETPROXY")
    agent_core.record_received(r, "s1", "1.2.3", "CLARINETPROXY")
    assert agent_core.received_count(r, "s1", "1.2.3") == 2
    assert r["received"]["s1"]["1.2.3"]["from"] == "CLARINETPROXY"
    assert agent_core.received_count(r, "s1", "9.9.9") == 0


def test_write_result_atomic_and_preserves_cyrillic(tmp_path):
    r = agent_core.new_result("clienta", "CLIENTA")
    agent_core.record_event(r, "cfind_cyrillic", name="Иванов^Пётр", ok=True)
    out = tmp_path / "sub" / "clienta.json"
    agent_core.write_result(str(out), r)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["events"][0]["name"] == "Иванов^Пётр"
    assert not os.path.exists(str(out) + ".tmp")
