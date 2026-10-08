"""Smoke test: the latency harness runs end-to-end on the zero-ML backends."""

from __future__ import annotations

import json
from pathlib import Path

from bench.routing_latency import main, percentiles


def test_percentiles() -> None:
    p = percentiles([float(x) for x in range(1, 101)])
    assert (p["p50"], p["p95"], p["p99"]) == (50.5, 95.05, 99.01)
    assert percentiles([3.0])["p99"] == 3.0


def test_harness_writes_complete_json(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    rc = main(
        [
            "--embedding=hash",
            "--decision=deterministic",
            "--device=cpu",
            "--iterations=2",
            "--warmup=0",
            "--batch-sizes=1,2",
            f"--out={out}",
        ]
    )
    assert rc == 0
    r = json.loads(out.read_text())
    assert r["device"] == "cpu"
    assert set(r["embedMs"]) == {"1", "2"} and set(r["routeCompositeMs"]) == {"1", "2"}
    assert {"choice5", "noul", "scoreBatch2", "scoreSequential2"} <= set(r["decisionMs"])
    assert r["routeCompositeMs"]["2"]["p95"] >= 0
    assert r["parity"] == {"skipped": "not requested"}
    assert r["backends"]["decision"]["backend"] == "deterministic-v1"
