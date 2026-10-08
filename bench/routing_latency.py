"""Routing-path latency harness (SPEC §3/§7/§10; docs/hardware-validation.md).

    python -m bench.routing_latency --embedding bge --decision laya --device auto \
        --mode performance --iterations 30 --out bench/results/

Measures, on whatever device the engine resolves:

* cold load time of both backends;
* embed latency at batch sizes 1/8/32 (catalog refresh + query embedding);
* decision latency: choice (5 domains), noul, score_batch(N) — N candidate
  Score questions in ONE call — and sequential score x N for comparison;
* route_composite(N): embed(query) + choice(domain) + score_batch(N) + noul,
  the warm per-request inference path the p95 < 150ms target applies to;
* memory (RSS, and CUDA allocator peak when on cuda);
* optional --parity (CUDA only): FP16-on-device vs FP32-on-CPU agreement for
  BGE (min cosine) and Laya (choice agreement, max |dp|).

Writes one JSON file. Numbers are p50/p95/p99 in milliseconds over
`--iterations` timed runs after `--warmup` untimed runs.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from mcprouter.inference.engine import InferenceEngine, memory_stats
from mcprouter.settings import Settings

DOMAINS = ["development", "communication", "files", "databases", "productivity"]
LEVELS = ["irrelevant", "partly relevant", "relevant", "exact match"]
QUERY = "find the open GitHub issues assigned to me in the router repository"
TOOL_TEXTS = [
    "list_issues: List issues in a GitHub repository with filters [github, issues]",
    "read_file: Read the complete contents of a file from disk [files, fs]",
    "send_message: Post a message to a Slack channel [chat, slack]",
    "run_query: Execute a read-only SQL query against Postgres [db, sql]",
    "create_event: Create a calendar event with attendees [calendar]",
    "search_code: Search code across repositories by keyword [github, code]",
    "write_file: Create or overwrite a file on disk [files, fs]",
    "get_pull_request: Fetch a pull request with its review status [github, pr]",
]


def _texts(n: int) -> list[str]:
    return [f"{TOOL_TEXTS[i % len(TOOL_TEXTS)]} #{i}" for i in range(n)]


def _questions(n: int) -> list[str]:
    return [f"How relevant is the tool '{t}' to this task?" for t in _texts(n)]


def percentiles(samples_ms: list[float]) -> dict[str, float]:
    s = sorted(samples_ms)
    if len(s) == 1:
        v = round(s[0], 2)
        return {"n": 1, "p50": v, "p95": v, "p99": v, "min": v, "max": v, "mean": v}
    q = statistics.quantiles(s, n=100, method="inclusive")
    return {
        "n": len(s),
        "p50": round(q[49], 2),
        "p95": round(q[94], 2),
        "p99": round(q[98], 2),
        "min": round(s[0], 2),
        "max": round(s[-1], 2),
        "mean": round(statistics.fmean(s), 2),
    }


def timeit(fn: Callable[[], object], iterations: int, warmup: int) -> dict[str, float]:
    for _ in range(warmup):
        fn()
    out: list[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000)
    return percentiles(out)


def _cuda_reset_peak(device: str) -> None:
    torch: Any = sys.modules.get("torch")
    if torch is not None and device.startswith("cuda"):
        torch.cuda.reset_peak_memory_stats()


def _environment(device: str) -> dict[str, Any]:
    env: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpuCount": os.cpu_count(),
        # Host contention matters on shared boxes: record it next to the numbers.
        "loadAvg1m": round(os.getloadavg()[0], 2) if hasattr(os, "getloadavg") else None,
    }
    torch: Any = sys.modules.get("torch")
    if torch is not None:
        env["torch"] = torch.__version__
        env["torchThreads"] = torch.get_num_threads()
        if device.startswith("cuda"):
            env["gpu"] = torch.cuda.get_device_name(0)
            env["cudaRuntime"] = torch.version.cuda
    try:
        env["gitCommit"] = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        env["gitCommit"] = None
    return env


def _sequential(engine: InferenceEngine, questions: list[str]) -> None:
    for q in questions:
        engine.score(QUERY, q, LEVELS)


def parity(settings: Settings, device: str) -> dict[str, Any]:
    """FP16-on-CUDA vs FP32-on-CPU agreement. Only meaningful on a CUDA device."""
    if not device.startswith("cuda"):
        return {"skipped": "parity needs a CUDA device (FP16 path)"}
    result: dict[str, Any] = {}
    cache = os.path.join(settings.models_cache_dir, "hub")
    if settings.embedding_backend == "bge":
        from mcprouter.inference.bge_backend import BgeEmbeddingBackend

        cpu = BgeEmbeddingBackend.load(settings.embedding_model_id, device="cpu", cache_dir=cache)
        gpu = BgeEmbeddingBackend.load(settings.embedding_model_id, device=device, cache_dir=cache)
        texts = _texts(32) + [QUERY]
        cos = [
            sum(x * y for x, y in zip(a, b, strict=True))
            for a, b in zip(cpu.embed(texts), gpu.embed(texts), strict=True)
        ]
        result["bgeFp16MinCosine"] = round(min(cos), 6)
        result["bgeFp16MeanCosine"] = round(statistics.fmean(cos), 6)
    if settings.decision_backend == "laya":
        from mcprouter.inference.laya import LayaDecisionModel

        lc = LayaDecisionModel.load(settings.laya_model_id, device="cpu", cache_dir=cache)
        lg = LayaDecisionModel.load(settings.laya_model_id, device=device, cache_dir=cache)
        agree, max_dp = 0, 0.0
        cases = [(t, "Which domain does this tool belong to?") for t in _texts(16)]
        for state, q in cases:
            a, b = lc.choice(state, q, DOMAINS), lg.choice(state, q, DOMAINS)
            agree += a.option == b.option
            max_dp = max(max_dp, *(abs(a.probabilities[o] - b.probabilities[o]) for o in DOMAINS))
        result["layaChoiceAgreement"] = f"{agree}/{len(cases)}"
        result["layaMaxProbDelta"] = round(max_dp, 4)
        result["layaPrecisionOnDevice"] = lg.precision
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings(
        embedding_backend=args.embedding,
        decision_backend=args.decision,
        device=args.device,
        operating_mode=args.mode,
        models_cache_dir=args.cache_dir,
    )
    if args.threads:
        import torch

        torch.set_num_threads(args.threads)
    engine = InferenceEngine(settings)
    t0 = time.perf_counter()
    engine.load()
    cold_load_s = time.perf_counter() - t0
    health = engine.health()
    device = health["device"]
    _cuda_reset_peak(device)
    it, wu = args.iterations, args.warmup
    sizes = [int(x) for x in args.batch_sizes.split(",")]

    embed = {str(b): timeit(partial(engine.embed, _texts(b)), it, wu) for b in sizes}
    decision: dict[str, Any] = {
        "choice5": timeit(lambda: engine.choice(QUERY, "Which domain?", DOMAINS), it, wu),
        "noul": timeit(lambda: engine.noul(QUERY, "Can any listed tool do this task?"), it, wu),
    }
    for b in sizes:
        qs = _questions(b)
        decision[f"scoreBatch{b}"] = timeit(partial(engine.score_batch, QUERY, qs, LEVELS), it, wu)
        seq_it = max(3, it // max(1, b // 4))  # sequential gets expensive fast; fewer runs
        decision[f"scoreSequential{b}"] = timeit(partial(_sequential, engine, qs), seq_it, 1)

    def route(n: int) -> None:
        engine.embed([QUERY])
        engine.choice(QUERY, "Which domain does this task belong to?", DOMAINS)
        engine.score_batch(QUERY, _questions(n), LEVELS)
        engine.noul(QUERY, "Can any of the candidate tools do this task?")

    composite = {str(b): timeit(partial(route, b), it, wu) for b in sizes}
    final = engine.health()
    return {
        "harness": "bench.routing_latency",
        "recordedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "config": {
            "embedding": args.embedding,
            "decision": args.decision,
            "requestedDevice": args.device,
            "mode": args.mode,
            "iterations": it,
            "warmup": wu,
            "batchSizes": sizes,
            "threads": args.threads or None,
        },
        "environment": _environment(device),
        "device": device,
        "backends": {"embedding": final["embedding"], "decision": final["decision"]},
        "coldLoadSeconds": round(cold_load_s, 3),
        "embedMs": embed,
        "decisionMs": decision,
        "routeCompositeMs": composite,
        "memory": memory_stats(device),
        "parity": parity(settings, device) if args.parity else {"skipped": "not requested"},
        "targets": {
            "warmRouteP95Ms": 150,
            "vramTypicalBytes": 4 * 1024**3,
            "note": "SPEC §3/§10 engineering targets; only meaningful on the target GPU",
        },
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m bench.routing_latency", description=__doc__)
    p.add_argument("--embedding", choices=["hash", "bge"], default="bge")
    p.add_argument("--decision", choices=["deterministic", "laya"], default="laya")
    p.add_argument("--device", default="auto")
    p.add_argument("--mode", choices=["performance", "balanced", "battery"], default="performance")
    p.add_argument("--iterations", type=int, default=30)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--batch-sizes", default="1,8,32")
    p.add_argument("--cache-dir", default=os.environ.get("MCPR_MODELS_CACHE_DIR", "./models-cache"))
    p.add_argument("--threads", type=int, default=0, help="torch CPU threads (0 = torch default)")
    p.add_argument("--parity", action="store_true", help="FP16 vs FP32 agreement (CUDA only)")
    p.add_argument("--out", default="bench/results", help="output directory or .json path")
    args = p.parse_args(argv)
    if args.iterations < 2:
        p.error("--iterations must be >= 2 for percentiles")

    result = run(args)
    out = Path(args.out)
    if out.suffix != ".json":
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out = out / f"routing_latency-{result['device'].replace(':', '')}-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    route = result["routeCompositeMs"]
    print(f"device={result['device']} cold_load={result['coldLoadSeconds']}s -> {out}")
    for b, st in route.items():
        print(f"  route_composite N={b}: p50={st['p50']}ms p95={st['p95']}ms p99={st['p99']}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
