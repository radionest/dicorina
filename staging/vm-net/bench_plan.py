"""Pure scheduling for the latency bench — host-unit-testable, Python 3.7-safe.

Cold rounds: within one round a study is cold only until its first transfer;
both cmove_cold and wado_frame_cold populate dicorina's study cache, so they
must not share a study inside a round. Halves swap between rounds.

QIDO uniqueness is deterministic, not timing-based: distinct (PatientName, limit)
per rep and variant, so no counted rep can hit the 5 s QIDO result cache.
Studies 2..6 carry PatientName "Patient^2".."Patient^6" (study 1 is cyrillic).
"""

import urllib.parse


def cold_round_split(round_idx, num_studies=6):
    half = num_studies // 2
    first, second = list(range(half)), list(range(half, num_studies))
    if round_idx % 2 == 0:
        return {"cmove_cold": first, "wado_frame_cold": second}
    return {"cmove_cold": second, "wado_frame_cold": first}


def qido_query(rep, warm=False, num_names=5):
    name = urllib.parse.quote(f"Patient^{2 + rep % num_names}")
    base = 201 if warm else 101
    return f"PatientName={name}&limit={base + rep}"
