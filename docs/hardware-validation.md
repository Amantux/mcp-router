# Hardware validation day — RTX 4060 Laptop (8 GB)

Scoping #1: the software half of Phase 4 ships now; **every GPU, FP16 and
VRAM number requires the target machine.** Nothing CUDA-related has been
verified yet. The dev box has no GPU, and the CUDA paths are written
device-agnostic and exercised only on CPU.

## What is being validated

| # | Target (SPEC §3 / §10) | Measured by | Pass if |
|---|---|---|---|
| T1 | Warm routing p95 < 150 ms | `routeCompositeMs[N].p95`, N = 8 and 20 | p95 < 150 |
| T2 | Typical VRAM < 4 GB | `memory.cudaMaxAllocatedBytes` (+ `nvidia-smi` peak) | < 4 GiB (4 294 967 296) |
| T3 | FP16 parity, BGE | `parity.bgeFp16MinCosine` | ≥ 0.999 |
| T4 | FP16 parity, Laya | `parity.layaChoiceAgreement`, `layaMaxProbDelta` | 16/16 and ≤ 0.05 |
| T5 | CPU fallback works | `--device cpu` run completes; health `device=cpu` | completes |
| T6 | Battery mode releases VRAM | §3 step 6 | allocated → ~0 after idle |

`routeCompositeMs[N]` is one warm request's inference path: embed(query) +
choice(domain, 5 options) + score_batch(N candidates) + noul(no-match). It
excludes DB retrieval and policy (cheap, measured by the routing workstream).
SPEC's 10–20 retrieval candidates means N = 20 is the honest headline, so
pass `--batch-sizes 1,8,20,32` on the day.

## 1. Setup (Windows 11 + WSL2 Ubuntu, or native Linux)

```bash
nvidia-smi                                   # driver sees the 4060; note driver + CUDA version
git clone <repo> && cd mcp-router
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python torch --index-url https://download.pytorch.org/whl/cu124  # match driver
uv pip install --python .venv/bin/python -e '.[dev,inference]' 'laya==0.4.0'
.venv/bin/python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
export HF_HOME=$PWD/models-cache MCPR_MODELS_CACHE_DIR=$PWD/models-cache
```

Pre-fetch the weights once at the pinned revisions. Running the slow tests
does this; after that everything runs with `HF_HUB_OFFLINE=1`:

```bash
MCPR_RUN_SLOW=1 .venv/bin/pytest -q tests/test_inference_laya.py tests/test_inference_bge.py
```

Plug in AC power and use the Windows "Best performance" power plan (except
for step 6). Close other GPU apps.

## 2. Correctness on the GPU first

The slow tests pin `device="cpu"`, so on the laptop they only prove the
install. GPU correctness is the `--parity` run in step 3/4: it loads both
models on CUDA and compares every answer to the FP32 CPU run. Run it before
trusting any latency number.

## 3. Runs

```bash
B=".venv/bin/python -m bench.routing_latency --embedding bge --decision laya --batch-sizes 1,8,20,32"
HF_HUB_OFFLINE=1 $B --device cuda --mode performance --iterations 50 --out bench/results/4060-performance.json
HF_HUB_OFFLINE=1 $B --device cuda --mode balanced    --iterations 50 --out bench/results/4060-balanced.json
HF_HUB_OFFLINE=1 $B --device cuda --parity           --iterations 10 --out bench/results/4060-parity.json
HF_HUB_OFFLINE=1 $B --device cpu  --threads 8        --iterations 20 --out bench/results/4060-cpu-fallback.json
```

During the performance run, record the true VRAM peak (the torch allocator
does not see the CUDA context, ~300–500 MB):

```bash
nvidia-smi --query-gpu=memory.used,utilization.gpu,power.draw --format=csv -l 1 > bench/results/4060-nvidia-smi.csv
```

T2 passes only if BOTH `cudaMaxAllocatedBytes` and the `nvidia-smi` peak are
below 4 GiB.

## 4. FP16 parity (T3/T4)

From `4060-parity.json`: `bgeFp16MinCosine` (BGE is `.half()`-ed by us on
CUDA) and `layaChoiceAgreement` / `layaMaxProbDelta` (Laya autocasts itself;
`layaPrecisionOnDevice` records which dtype it chose). If parity fails, run
FP32 on the GPU as a control. The flag for this needs adding; today FP16 is
unconditional on CUDA for BGE.

## 5. Concurrency sanity

Performance mode allows 4 concurrent inferences. Check that p95 doesn't
collapse under concurrency: run two bench processes in parallel against
`--mode performance` and compare p95 with the single-process run. If p95
more than doubles, lower `MODE_CONCURRENCY["performance"]`.

## 6. Battery mode (T6)

Unplug, Windows "Best power efficiency":

```bash
MCPR_OPERATING_MODE=battery MCPR_DEVICE=cuda .venv/bin/python - <<'EOF'
import time, torch
from mcprouter.inference.engine import InferenceEngine
from mcprouter.settings import Settings
e = InferenceEngine(Settings(embedding_backend="bge", decision_backend="laya",
                             device="cuda", operating_mode="battery"), idle_unload_s=20)
e.choice("read a file", "Which domain?", ["files", "communication"])
print("loaded MB", torch.cuda.memory_allocated() / 2**20)
time.sleep(30)
print("after idle MB", torch.cuda.memory_allocated() / 2**20, e.health(check_idle=False)["loaded"])
t = time.perf_counter(); e.choice("read a file", "Which domain?", ["files", "communication"])
print("cold reload s", time.perf_counter() - t)
EOF
```

Pass: allocated drops to ~0 MB and `loaded` is False after the timer fires,
with no request involved. Record the cold-reload seconds: that is the
battery-mode cost a user pays on the first request after idle.

## 7. Record results

Fill in the 4060 columns below, commit the JSON files under `bench/results/`,
and change the SPEC §3 targets from "engineering targets" to "validated"
(or record the misses).

## Results

Columns: **CPU baseline** = measured on the dev box. This is a shared
112-thread x86 server, not a laptop, and heavily contended by other tenants
during the run (see the environment note). **4060** = to be filled on
validation day.

### Latency (ms, p50 / p95 / p99)

| measurement | CPU baseline | RTX 4060 | target |
|---|---|---|---|
| embed, batch 1 (BGE) | 35 / 59 / 64 | |  |
| embed, batch 8 (BGE) | 122 / 183 / 195 | |  |
| embed, batch 32 (BGE) | 246 / 434 / 489 | |  |
| Laya choice (5 options) | 380 / 550 / 590 | |  |
| Laya noul (2-option choice) | 249 / 328 / 332 | |  |
| Laya score_batch, N=1 (one pass) | 334 / 377 / 397 | |  |
| Laya score sequential, N=1 | 263 / 335 / 434 | |  |
| Laya score_batch, N=8 (one pass) | 1366 / 1609 / 1616 | |  |
| Laya score sequential, N=8 | 2369 / 2758 / 2903 | |  |
| Laya score_batch, N=32 (one pass) | 4676 / 5329 / 5437 | |  |
| Laya score sequential, N=32 | 6476 / 6712 / 6733 | |  |
| **route composite, N=1** | 583 / 646 / 660 | | p95 < 150 |
| **route composite, N=8** | 1473 / 1818 / 1972 | | p95 < 150 |
| **route composite, N=32** | 3679 / 4170 / 4194 | | p95 < 150 |
| route composite, N=20 | not run (add `20` to `--batch-sizes`) | | p95 < 150 |

### Load and memory

| measurement | CPU baseline | RTX 4060 | target |
|---|---|---|---|
| cold load, BGE + Laya | 8.0 s | | |
| process RSS after run | 2.29 GiB (peak 2.94 GiB) | | |
| CUDA max allocated | n/a (no GPU) | | < 4 GiB |
| nvidia-smi peak | n/a | | < 4 GiB |
| FP16 parity BGE / Laya | n/a (needs CUDA) | | ≥ 0.999 / 16/16 |

### Baseline environment

`bench/results/cpu-baseline.json`, commit `80b0076`, recorded 2026-10-08T05:21:48+00:00:
Python 3.12.13, torch 2.14.1+cpu, **8 torch threads** (`--threads 8`),
112 logical CPUs, **1-min load average 135.35** during the run (other
tenants, including two ollama processes, were using ~70 cores). Backends: `bge-small-en-v1.5`
@ `5c38ec7c`, `laya@55cf4c4ebb4e` in float32, 20 timed iterations after 3 warm-up runs
(sequential N=8/N=32: 10/3 iterations).

How to read the baseline:

* **These numbers are contended and noisy.** For example, composite N=32 (p50 3.7 s)
  came in below standalone `score_batch` N=32 (p50 4.7 s), which is impossible on a
  quiet machine. Use them for orders of magnitude and ratios, not for absolute
  comparison. An earlier uncontended single-call probe measured Laya choice at
  ~86–103 ms and one score at ~95 ms (56 threads).
* A first attempt with the torch default of 56 threads on this oversubscribed host
  ran for over 35 minutes without finishing (thread contention). It was stopped, and
  that is why `--threads` exists. On the laptop, start with the physical-core count.
* CPU fallback works end to end, and it does **not** meet the 150 ms budget with Laya
  in the loop (≈0.6 s even at N=1 here). On CPU the realistic fallback is
  `decision_backend=deterministic` (hash/BGE embeddings + lexical decisions): the
  zero-ML path (`hash` + `deterministic`, `bench/results/cpu-zero-ml.json`, 30 iterations)
  measured composite p95 0.56 ms (N=1), 1.12 ms (N=8), 3.53 ms (N=32).
* `score_batch` beats sequential scoring (N=8: 1.37 s vs 2.37 s p50; N=32: 4.7 s vs
  6.5 s), but Laya cost still grows roughly linearly with N. The vendor's T4 numbers
  (39.5 ms for 1 question, 158.6 ms for 10 batched, English checkpoint) put
  **choice + score_batch(20) + noul above 150 ms even on a T4**. Unless the 4060 is
  much faster than a T4 on this model, T1 will need one of: fewer Laya-scored
  candidates (rank the 10–20 by embedding, Laya-score the top ~5), the multilingual
  checkpoint (~2.2× faster per the card), or Laya only for choice/noul. Validation
  day decides this with data.
