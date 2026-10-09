"""Run mypy (pyproject `files`) and hold tests/testbed to mypy-baseline.txt (P-505).

    python .github/scripts/mypy_baseline.py            # check (CI, make check)
    python .github/scripts/mypy_baseline.py --update   # rewrite the baseline

An error is keyed WITHOUT its line number (``path: error: message  [code]``) so
unrelated edits don't churn the baseline. Fails when:
  * any error is in src/ or bench/ (those stay strict-clean), or
  * any key occurs more often than in the baseline (a NEW error in tests/testbed).
Fixed errors only print a hint to shrink the baseline (`--update`).
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "mypy-baseline.txt"
_LINE = re.compile(r"^(?P<path>[^:]+):\d+(?::\d+)?: error: (?P<rest>.*)$")


def keys(output: str) -> Counter[str]:
    out: Counter[str] = Counter()
    for line in output.splitlines():
        m = _LINE.match(line)
        if m:
            out[f"{m['path']}: error: {m['rest']}"] += 1
    return out


def run_mypy() -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-error-summary", "--hide-error-context"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):  # 2 = mypy itself crashed / bad config
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(2)
    return proc.stdout


def main(argv: list[str]) -> int:
    current = keys(run_mypy())
    if "--update" in argv:
        BASELINE.write_text("".join(f"{k}\n" for k in sorted(current.elements())))
        print(f"mypy-baseline.txt: {sum(current.values())} errors")
        return 0
    base = keys("") if not BASELINE.exists() else Counter(BASELINE.read_text().splitlines())
    strict = sorted(k for k in current if k.startswith(("src/", "bench/")))
    new = sorted((current - base).elements())
    for k in strict:
        print(f"::error::strict tree must be clean: {k}")
    for k in new:
        print(f"::error::new mypy error (not in mypy-baseline.txt): {k}")
    total, allowed = sum(current.values()), sum(base.values())
    print(f"mypy tests+testbed: {total} errors (baseline {allowed})")
    if total < allowed:
        print("baseline can shrink: run `python .github/scripts/mypy_baseline.py --update`")
    return 1 if strict or new else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
