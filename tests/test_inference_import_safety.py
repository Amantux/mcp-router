"""Zero-ML import safety: the whole inference stack (and its API route) must
import and run with torch & co. unimportable."""

from __future__ import annotations

import subprocess
import sys
import textwrap

# Modules that must never be imported for the zero-ML path to work.
_ML_MODULES = ("torch", "transformers", "sentence_transformers", "laya", "numpy", "huggingface_hub")

_BLOCKER = textwrap.dedent(
    """
    import sys, importlib.abc
    BLOCKED = {blocked!r}
    class _Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError("blocked ML import: " + name)
            return None
    sys.meta_path.insert(0, _Block())
    """
)


def _run_blocked(body: str) -> subprocess.CompletedProcess[str]:
    code = _BLOCKER.format(blocked=set(_ML_MODULES)) + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )


def test_whole_inference_package_imports_and_works_with_ml_blocked() -> None:
    """Every inference module imports, and the fallbacks run, with ML imports raising.

    A meta-path finder that raises ImportError on torch/transformers/... simulates a box
    with the [inference] extra absent (without uninstalling anything). Mutation check:
    adding a top-level `import torch` to any listed module makes this test fail.
    """
    proc = _run_blocked(
        """
        import mcprouter.inference as inf
        import mcprouter.inference.hash_backend
        import mcprouter.inference.deterministic
        import mcprouter.inference.validation
        import mcprouter.inference.bge_backend
        import mcprouter.inference.laya
        import mcprouter.inference.engine
        import mcprouter.inference.pipeline
        import mcprouter.api.routes_models
        from mcprouter.inference.hash_backend import HashEmbeddingBackend
        from mcprouter.inference.engine import InferenceEngine
        from mcprouter.settings import Settings

        assert len(HashEmbeddingBackend().embed(["x"])[0]) == 384
        # Asking for the ML backends must degrade, not crash.
        eng = InferenceEngine(Settings(embedding_backend="bge", decision_backend="laya"))
        eng.load()
        h = eng.health()
        assert h["embedding"]["backend"] == "hash-v1", h
        assert h["decision"]["backend"] == "deterministic-v1", h
        assert h["embedding"]["fallbackUsed"] and h["decision"]["fallbackUsed"], h
        assert h["device"] == "cpu", h
        r = eng.choice("read the file config.yaml", "Which domain?", ["files", "communication"])
        assert r.option == "files", r
        leaked = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
        assert not leaked, leaked
        print("OK")
        """
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("OK")


def test_blocker_actually_blocks() -> None:
    # Guards the guard: if the finder stopped working the test above would be vacuous.
    proc = _run_blocked("import json\nimport numpy\n")
    assert proc.returncode != 0
    assert "blocked ML import" in proc.stderr
