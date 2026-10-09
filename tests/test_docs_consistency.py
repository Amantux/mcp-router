"""MT-6: the docs say only what the code does.

Each rule is a pure checker ``check_*(root, ...) -> list[str]`` (one string per
problem) so the same code runs against the repository and against a temp-dir
fixture that breaks it on purpose (the ``test_mutation_*`` tests: every rule
must go red when its fixture is broken).

Rules
  1  relative links and ``file.md#anchor`` fragments resolve
  2  backticked repo paths exist; ``path::symbol`` names a symbol in that file
  3  every documented ``MCPR_*`` exists (settings, or an allowlist that is
     itself checked against compose/scripts/tests)
  4  reverse: every setting and every compose/image ``MCPR_*`` is in the
     generated configuration reference
  5  ``.env.example``: documented enum values are real; compose variables listed
  6  documented defaults (tables, ``(default)`` rows, prose) equal the code
  7  docs/reference/configuration.md == a fresh render (generated-docs drift)
  8  ``METHOD /api/v1/...`` mentions exist in the OpenAPI route set
  9  ``python -m <module> --flag`` examples: every flag is in ``--help``
  10 Dockerfile ``NODE_VERSION`` equals ``.nvmrc``

Allowlists marked PENDING name things other wave-6 fences ship; a test fails as
soon as one of them lands, so the entry gets deleted instead of rotting.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parent.parent
THIS = Path(__file__).resolve()

# --------------------------------------------------------------------------- helpers

EXCLUDED_DIRS = ("docs/history/", "docs/audit/")
TOP_DOCS = ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "CLAUDE.md")


def doc_files(root: Path) -> list[Path]:
    files = [root / name for name in TOP_DOCS if (root / name).exists()]
    docs = root / "docs"
    if docs.is_dir():
        for p in sorted(docs.rglob("*.md")):
            rel = p.relative_to(root).as_posix()
            if not rel.startswith(EXCLUDED_DIRS):
                files.append(p)
    return files


_FENCE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.M | re.S)


def strip_fences(text: str) -> str:
    """Blank out fenced code blocks, keeping line numbers."""
    return _FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def inline_code(text: str) -> list[str]:
    return re.findall(r"`([^`\n]+)`", strip_fences(text))


def rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def load_generator(root: Path = REPO) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gen_config_docs", root / "scripts" / "gen_config_docs.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("gen_config_docs", mod)
    spec.loader.exec_module(mod)
    return mod


def settings_env_names(root: Path) -> set[str]:
    """Every MCPR_* the settings module reads (get(...) and _secret(... ) -> +_FILE)."""
    src = (root / "src" / "mcprouter" / "settings.py").read_text(encoding="utf-8")
    names = set(re.findall(r"\bget\(\s*\"(MCPR_[A-Z0-9_]+)\"", src))
    for secret in re.findall(r"_secret\(\s*\"(MCPR_[A-Z0-9_]+)\"", src):
        names |= {secret, secret + "_FILE"}
    names |= set(re.findall(r"\benv=\"(MCPR_[A-Z0-9_]+)\"", src))  # SETTINGS_SPEC entries
    return names


def code_text(root: Path, *, exclude: Iterable[Path] = ()) -> str:
    """Compose, image, scripts, CI, tests and testbed: where non-settings knobs live."""
    skip = {p.resolve() for p in exclude}
    parts: list[str] = []
    patterns = (
        "docker-compose*.yml",
        "Dockerfile*",
        "scripts/*",
        ".github/workflows/*.yml",
        "tests/**/*.py",
        "testbed/**/*.py",
        "Makefile",
    )
    for pattern in patterns:
        for p in sorted(root.glob(pattern)):
            if p.is_file() and p.resolve() not in skip:
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


# --------------------------------------------------------------------------- rule 1


def github_slugs(md: str) -> set[str]:
    seen: dict[str, int] = {}
    out: set[str] = set()
    for m in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", strip_fences(md), re.M):
        slug = re.sub(r"[^\w\- ]", "", m.group(1).strip().lower()).replace(" ", "-")
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    return out


# Files other wave-6 fences ship (E7 merges last, so they exist by then).
PENDING_FILES = {
    "docs/reference/api.md": "E2 P-212",
    "tests/test_layering.py": "E6 P-607",
}


def check_links(root: Path, pending: Iterable[str] = ()) -> list[str]:
    pending = set(pending)
    problems = []
    for f in doc_files(root):
        text = strip_fences(f.read_text(encoding="utf-8"))
        for m in re.finditer(r"\]\(([^)\s]+)\)", text):
            target = m.group(1)
            if re.match(r"[a-z]+:", target):
                continue
            path_part, _, frag = target.partition("#")
            dest = (f.parent / path_part).resolve() if path_part else f
            where = f"{rel(root, f)} -> {target}"
            if path_part:
                try:
                    dest_rel = dest.relative_to(root.resolve()).as_posix()
                except ValueError:
                    problems.append(f"{where}: points outside the repository")
                    continue
                if dest_rel in pending:
                    continue
                if not dest.exists():
                    problems.append(f"{where}: missing file")
                    continue
            if frag and dest.suffix == ".md":
                if frag not in github_slugs(dest.read_text(encoding="utf-8")):
                    problems.append(f"{where}: no heading with anchor #{frag}")
    return problems


# --------------------------------------------------------------------------- rule 2

_PATH_TOKEN = re.compile(
    r"^((?:src|tests|testbed|bench|ui|scripts|docs|\.github)/[\w.*{},/\-]*?)(?:::([\w.]+))?[.,;:]?$"
)


# Build outputs that docs name on purpose but git never holds.
BUILD_ARTIFACTS = {"ui/dist": "npm run build output, served by the API"}


def check_paths(root: Path, pending: Iterable[str] = ()) -> list[str]:
    pending = set(pending) | set(BUILD_ARTIFACTS)
    problems = []
    for f in doc_files(root):
        for token in inline_code(f.read_text(encoding="utf-8")):
            m = _PATH_TOKEN.match(token.strip())
            if not m:
                continue
            path, symbol = m.group(1).rstrip("/"), m.group(2)
            if path in pending:
                continue
            if any(c in path for c in "*{"):
                pattern = re.sub(r"\{[^}]*\}", "*", path)
                if not list(root.glob(pattern)):
                    problems.append(f"{rel(root, f)}: `{token}` matches nothing")
                continue
            p = root / path
            if not p.exists():
                problems.append(f"{rel(root, f)}: `{token}` does not exist")
                continue
            if symbol:
                name = symbol.split(".")[-1]
                body = p.read_text(encoding="utf-8") if p.is_file() else ""
                if not re.search(rf"\b{re.escape(name)}\b", body):
                    problems.append(f"{rel(root, f)}: `{token}`: no `{name}` in {path}")
    return problems


# --------------------------------------------------------------------------- rule 3

# Knobs that compose, the image, the entrypoint, CI or the tests read (not Settings).
NON_SETTINGS = {
    "MCPR_HOST_PORT",
    "MCPR_PORT",
    "MCPR_IMAGE",
    "MCPR_BASE_IMAGE",
    "MCPR_INFERENCE_IMAGE",
    "MCPR_TORCH_INDEX_URL",
    "MCPR_DB_WAIT_TRIES",
    "MCPR_RUN_SLOW",
    "MCPR_AGENT_KEY",
}
# Names that only exist in docs on purpose.
DOC_ONLY = {"MCPR_KEY": "the client-side variable in the INSTALL.md client snippets"}
# Documented as "from v0.6"; shipped by another fence.
PENDING_ENV = {
    "MCPR_BIND": "E1 P-101 (compose port string)",
    "MCPR_REQUIRE_DB": "E5 P-503",
}

_ENV_TOKEN = re.compile(r"\bMCPR_[A-Z0-9_]+")


def documented_env(root: Path) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for f in doc_files(root):
        for m in _ENV_TOKEN.finditer(f.read_text(encoding="utf-8")):
            found.setdefault(m.group(0), []).append(rel(root, f))
    return found


def check_env_documented(root: Path, allowed: set[str]) -> list[str]:
    problems = []
    for name, where in sorted(documented_env(root).items()):
        if name.endswith("_"):  # a prefix such as MCPR_AOAI_*
            if not any(a.startswith(name) for a in allowed):
                problems.append(f"{name}* (in {where[0]}): no variable has this prefix")
        elif name not in allowed:
            problems.append(f"{name} (in {sorted(set(where))}): not read by the code")
    return problems


# --------------------------------------------------------------------------- rule 4


def check_env_reverse(root: Path, settings_names: set[str]) -> list[str]:
    ref = root / "docs" / "reference" / "configuration.md"
    text = ref.read_text(encoding="utf-8") if ref.exists() else ""
    deploy: set[str] = set()
    for pattern in ("docker-compose*.yml", "Dockerfile*", "scripts/docker-entrypoint.sh"):
        for p in root.glob(pattern):
            deploy |= set(_ENV_TOKEN.findall(p.read_text(encoding="utf-8")))
    missing = sorted(n for n in settings_names | deploy if f"`{n}`" not in text)
    return [f"{n}: missing from docs/reference/configuration.md" for n in missing]


# --------------------------------------------------------------------------- rule 5


def check_env_example(root: Path, choices: dict[str, tuple[str, ...]]) -> list[str]:
    path = root / ".env.example"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    listed: set[str] = set()
    problems = []
    for line in lines:
        m = re.match(r"^\s*#?\s*([A-Z][A-Z0-9_]+)=([^\s#]*)\s*(?:#\s*(.*))?$", line)
        if not m:
            continue
        name, value, comment = m.group(1), m.group(2), m.group(3) or ""
        listed.add(name)
        allowed = choices.get(name)
        if not allowed:
            continue
        if value and value not in allowed:
            problems.append(f".env.example: {name}={value} is not one of {list(allowed)}")
        if re.fullmatch(r"[\w-]+(\s*\|\s*[\w-]+)+", comment.strip()):
            for option in (o.strip() for o in comment.split("|")):
                if option not in allowed:
                    problems.append(f".env.example: {name} lists `{option}`, not a valid value")
    compose_vars: set[str] = set()
    for p in root.glob("docker-compose*.yml"):
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.split(" #", 1)[0] if not raw.lstrip().startswith("#") else ""
            compose_vars |= set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", line))
    for name in sorted(compose_vars - listed):
        problems.append(f".env.example: compose reads ${{{name}}} but the template omits it")
    return problems


# .env.example belongs to E1 (P-107 rewrites it). These are its violations at the
# wave-6 base; each must still occur, so the set empties itself when E1 lands.
KNOWN_ENV_EXAMPLE_PROBLEMS = {
    ".env.example: MCPR_EMBEDDING_BACKEND=local is not one of ['hash', 'bge', 'aoai']",
    ".env.example: MCPR_EMBEDDING_BACKEND lists `local`, not a valid value",
    ".env.example: compose reads ${MCPR_AGENT_KEYS} but the template omits it",
    ".env.example: compose reads ${MCPR_ADMIN_TOKEN} but the template omits it",
    ".env.example: compose reads ${MCPR_ALLOWED_HOSTS} but the template omits it",
    ".env.example: compose reads ${MCPR_BASE_IMAGE} but the template omits it",
    ".env.example: compose reads ${MCPR_DEVICE} but the template omits it",
    ".env.example: compose reads ${MCPR_HOST_PORT} but the template omits it",
    ".env.example: compose reads ${MCPR_IMAGE} but the template omits it",
    ".env.example: compose reads ${MCPR_INFERENCE_IMAGE} but the template omits it",
    ".env.example: compose reads ${MCPR_TORCH_INDEX_URL} but the template omits it",
    ".env.example: compose reads ${POSTGRES_DB} but the template omits it",
    ".env.example: compose reads ${POSTGRES_PASSWORD} but the template omits it",
    ".env.example: compose reads ${POSTGRES_USER} but the template omits it",
}


# --------------------------------------------------------------------------- rule 6


def _norm_default(cell: str) -> str:
    v = cell.strip().strip("`").strip()
    return "" if v.lower() in {"unset", "", "—", "-"} else v


def _table_rows(text: str) -> Iterable[tuple[list[str], list[str]]]:
    """(header cells, row cells) for every row of every pipe table."""
    header: list[str] | None = None
    for line in strip_fences(text).splitlines():
        if not line.lstrip().startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
        elif not all(re.fullmatch(r":?-+:?", c) for c in cells):
            yield header, cells


def check_defaults(root: Path, defaults: dict[str, str]) -> list[str]:
    problems = []
    for f in doc_files(root):
        text = f.read_text(encoding="utf-8")
        where = rel(root, f)
        for header, cells in _table_rows(text):
            names = re.findall(r"`(MCPR_[A-Z0-9_]+|POSTGRES_[A-Z]+)`", cells[0])
            lowered = [h.lower() for h in header]
            if "default" in lowered and len(names) == 1 and names[0] in defaults:
                cell = cells[lowered.index("default")] if len(cells) > 1 else ""
                if _norm_default(cell) != defaults[names[0]]:
                    problems.append(
                        f"{where}: {names[0]} default `{cell}` != code `{defaults[names[0]]}`"
                    )
            enum = re.fullmatch(r"`(MCPR_[A-Z0-9_]+)`", header[0])
            if enum and enum.group(1) in defaults and "(default)" in cells[0]:
                marked = re.search(r"`([^`]+)`", cells[0])
                if marked and marked.group(1) != defaults[enum.group(1)]:
                    problems.append(
                        f"{where}: {enum.group(1)} table marks `{marked.group(1)}` as default, "
                        f"code says `{defaults[enum.group(1)]}`"
                    )
        prose = strip_fences(text)
        for m in re.finditer(r"`(MCPR_[A-Z0-9_]+)`[^`\n|]{0,20}\(default `([^`]*)`", prose):
            name, value = m.group(1), m.group(2)
            if name in defaults and value != defaults[name]:
                problems.append(f"{where}: {name} (default `{value}`) != code `{defaults[name]}`")
    return problems


# --------------------------------------------------------------------------- rule 8

_ENDPOINT = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/api/v1(?:/(?:\{[^}/]*\}|<[\w-]+>|[\w.\-]+))*)"
)


def _norm_path(path: str) -> str:
    path = re.sub(r"\{[^}]*\}|<[^>]*>", "{}", path.split("?")[0])
    return path.rstrip("/.,;:")


def check_endpoints(root: Path, routes: set[tuple[str, str]]) -> list[str]:
    known = {(m, _norm_path(p)) for m, p in routes}
    problems = []
    for f in doc_files(root):
        for m in _ENDPOINT.finditer(f.read_text(encoding="utf-8")):
            key = (m.group(1), _norm_path(m.group(2)))
            if key not in known:
                problems.append(f"{rel(root, f)}: {key[0]} {key[1]} is not a route")
    return problems


def route_set() -> tuple[set[tuple[str, str]] | None, str]:
    """(routes, source): committed OpenAPI (E2) -> live app (needs the DB) ->
    the wave-6 audit snapshot -> None."""
    committed = REPO / "docs" / "reference" / "openapi.json"
    if committed.exists():
        spec = json.loads(committed.read_text(encoding="utf-8"))
        return _openapi_routes(spec), "docs/reference/openapi.json"
    live = _live_openapi()
    if live is not None:
        return _openapi_routes(live), "create_app().openapi()"
    snapshot = REPO / "docs" / "audit" / "openapi-routes.json"
    if snapshot.exists():
        pairs = json.loads(snapshot.read_text(encoding="utf-8"))
        return {(m, p) for m, p in pairs}, "docs/audit/openapi-routes.json"
    return None, "no OpenAPI source: no committed spec, no database, no snapshot"


def _openapi_routes(spec: dict[str, object]) -> set[tuple[str, str]]:
    paths = spec.get("paths", {})
    assert isinstance(paths, dict)
    return {(m.upper(), p) for p, ops in paths.items() for m in ops if m != "parameters"}


def _live_openapi() -> dict[str, object] | None:
    from sqlalchemy import text

    from mcprouter.db import make_engine
    from mcprouter.settings import Settings

    url = os.environ.get(
        "MCPR_DATABASE_URL", "postgresql+psycopg://mcprouter:mcprouter@localhost:5434/mcprouter"
    )
    settings = Settings(database_url=url)
    try:
        eng = make_engine(settings)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
    except Exception:  # noqa: BLE001 - availability probe only
        return None
    from mcprouter.api.app import create_app

    return create_app(settings, env={}).openapi()


# --------------------------------------------------------------------------- rule 9

_CLI = re.compile(r"python -m ((?:testbed|bench|mcprouter)(?:\.\w+)+)([^\n`|]*)")
PENDING_MODULES = {"mcprouter.migrate": "E6 P-601"}


def documented_cli(root: Path) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for f in doc_files(root):
        for m in _CLI.finditer(f.read_text(encoding="utf-8")):
            rest = re.split(r"\s(?:#|&&|\|\|)\s?|;", m.group(2))[0]
            flags = set(re.findall(r"(?<![\w-])(--[a-z][\w-]*)", rest))
            found.setdefault(m.group(1), set()).update(flags)
    return found


def help_text(module: str) -> str | None:
    proc = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


def check_cli(documented: dict[str, set[str]], pending: Iterable[str] = ()) -> list[str]:
    pending = set(pending)
    problems = []
    for module, flags in sorted(documented.items()):
        if module in pending:
            continue
        out = help_text(module)
        if out is None:
            problems.append(f"python -m {module} --help fails")
            continue
        for flag in sorted(flags - {"--help"}):
            if not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", out):
                problems.append(f"python -m {module}: documented {flag} is not in --help")
    return problems


# --------------------------------------------------------------------------- rule 10


def check_node_version(root: Path) -> list[str]:
    nvmrc = (root / ".nvmrc").read_text(encoding="utf-8").strip()
    m = re.search(r"^ARG NODE_VERSION=(\S+)", (root / "Dockerfile").read_text("utf-8"), re.M)
    arg = m.group(1) if m else "(missing)"
    return [] if arg == nvmrc else [f"Dockerfile NODE_VERSION {arg} != .nvmrc {nvmrc}"]


# ============================================================ tests on the repository


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    return load_generator()


@pytest.fixture(scope="module")
def code_vars(gen: ModuleType) -> list[object]:
    return list(gen.collect())


def test_rule1_links_and_anchors_resolve() -> None:
    assert check_links(REPO, PENDING_FILES) == []


def test_rule2_backticked_paths_and_symbols_exist() -> None:
    assert check_paths(REPO, PENDING_FILES) == []


def test_pending_files_have_not_landed() -> None:
    landed = sorted(p for p in PENDING_FILES if (REPO / p).exists())
    assert landed == [], f"landed: delete from PENDING_FILES: {landed}"


def test_rule3_documented_env_vars_exist() -> None:
    allowed = settings_env_names(REPO) | NON_SETTINGS | set(DOC_ONLY) | set(PENDING_ENV)
    assert check_env_documented(REPO, allowed) == []


def test_rule3_allowlists_do_not_rot() -> None:
    code = code_text(REPO, exclude=[THIS, REPO / "scripts" / "gen_config_docs.py"])
    settings = settings_env_names(REPO)
    unused = sorted(n for n in NON_SETTINGS if n not in code or n in settings)
    assert unused == [], f"NON_SETTINGS entries not read outside settings: {unused}"
    landed = sorted(n for n in PENDING_ENV if n in code or n in settings)
    assert landed == [], f"landed: move from PENDING_ENV to the registry/NON_SETTINGS: {landed}"


def test_rule4_every_setting_is_in_the_configuration_reference() -> None:
    assert check_env_reverse(REPO, settings_env_names(REPO)) == []


def test_rule5_env_example_values_are_valid(code_vars: list[object]) -> None:
    choices = {v.env: v.choices for v in code_vars if v.choices}  # type: ignore[attr-defined]
    problems = set(check_env_example(REPO, choices))
    assert problems - KNOWN_ENV_EXAMPLE_PROBLEMS == set()
    fixed = KNOWN_ENV_EXAMPLE_PROBLEMS - problems
    assert fixed == set(), f"fixed upstream: delete from KNOWN_ENV_EXAMPLE_PROBLEMS: {fixed}"


def _defaults(gen: ModuleType, code_vars: list[object]) -> dict[str, str]:
    out = {v.env: v.default for v in code_vars}  # type: ignore[attr-defined]
    out.update({env: default for env, default, _doc in gen.DEPLOY_ONLY})
    return out


def test_rule6_documented_defaults_match_code(gen: ModuleType, code_vars: list[object]) -> None:
    assert check_defaults(REPO, _defaults(gen, code_vars)) == []


def test_rule7_configuration_reference_is_fresh(gen: ModuleType) -> None:
    committed = (REPO / "docs" / "reference" / "configuration.md").read_text(encoding="utf-8")
    assert gen.render(gen.collect()) == committed, "run scripts/gen_config_docs.py"


def test_rule8_documented_endpoints_exist() -> None:
    routes, source = route_set()
    if routes is None:
        pytest.skip(source)
    assert check_endpoints(REPO, routes) == [], f"route source: {source}"


def test_rule9_documented_cli_flags_exist() -> None:
    assert check_cli(documented_cli(REPO), PENDING_MODULES) == []


def test_pending_modules_have_not_landed() -> None:
    landed = sorted(
        m for m in PENDING_MODULES if importlib.util.find_spec(m.rsplit(".", 1)[0]) and _has(m)
    )
    assert landed == [], f"landed: delete from PENDING_MODULES: {landed}"


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        return False


def test_rule10_dockerfile_node_matches_nvmrc() -> None:
    assert check_node_version(REPO) == []


# ============================================================ mutation fixtures


def _write(root: Path, name: str, text: str) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_mutation_rule1_broken_link_and_anchor(tmp_path: Path) -> None:
    _write(tmp_path, "docs/a.md", "# Title\n\n## Real heading\n")
    _write(
        tmp_path, "README.md", "[ok](docs/a.md#real-heading) [gone](docs/b.md) [x](docs/a.md#nope)"
    )
    problems = check_links(tmp_path)
    assert any("docs/b.md: missing file" in p for p in problems)
    assert any("#nope" in p for p in problems)
    assert not any("real-heading" in p for p in problems)


def test_mutation_rule2_missing_path_and_symbol(tmp_path: Path) -> None:
    _write(tmp_path, "src/m.py", "def present():\n    pass\n")
    _write(tmp_path, "README.md", "`src/m.py::present` `src/m.py::absent` `src/gone.py`")
    problems = check_paths(tmp_path)
    assert any("`src/gone.py` does not exist" in p for p in problems)
    assert any("no `absent`" in p for p in problems)
    assert len(problems) == 2


def test_mutation_rule3_unknown_env_var(tmp_path: Path) -> None:
    _write(tmp_path, "README.md", "Set `MCPR_REAL` and `MCPR_NOPE`; see MCPR_RE_* too.")
    problems = check_env_documented(tmp_path, {"MCPR_REAL"})
    assert any(p.startswith("MCPR_NOPE") for p in problems)
    assert any(p.startswith("MCPR_RE_*") for p in problems)
    assert len(problems) == 2


def test_mutation_rule4_setting_missing_from_reference(tmp_path: Path) -> None:
    _write(tmp_path, "docs/reference/configuration.md", "| `MCPR_OLD` | 1 |\n")
    _write(tmp_path, "docker-compose.yml", "x: ${MCPR_COMPOSE_ONLY:-1}\n")
    problems = check_env_reverse(tmp_path, {"MCPR_OLD", "MCPR_NEW"})
    assert sorted(problems) == [
        "MCPR_COMPOSE_ONLY: missing from docs/reference/configuration.md",
        "MCPR_NEW: missing from docs/reference/configuration.md",
    ]


def test_mutation_rule5_env_example_invalid_enum(tmp_path: Path) -> None:
    _write(
        tmp_path,
        ".env.example",
        "# MCPR_EMBEDDING_BACKEND=local          # local | aoai\nMCPR_DEVICE=cpu\n",
    )
    _write(tmp_path, "docker-compose.yml", "a: ${MCPR_DEVICE}\nb: ${POSTGRES_PASSWORD}\n")
    problems = check_env_example(
        tmp_path, {"MCPR_EMBEDDING_BACKEND": ("hash", "bge", "aoai"), "MCPR_DEVICE": ("cpu",)}
    )
    assert any("MCPR_EMBEDDING_BACKEND=local" in p for p in problems)
    assert any("lists `local`" in p for p in problems)
    assert any("${POSTGRES_PASSWORD}" in p for p in problems)
    assert len(problems) == 3


def test_mutation_rule6_wrong_defaults(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/d.md",
        "| Variable | Default | Notes |\n|---|---|---|\n| `MCPR_X` | `9` | n |\n| `MCPR_Y` | unset | n |\n"
        "\n| `MCPR_B` | Where |\n|---|---|\n| `laya` (default) | local |\n"
        "\nUse `MCPR_Z` (default `old`).\n",
    )
    problems = check_defaults(
        tmp_path, {"MCPR_X": "8", "MCPR_Y": "", "MCPR_B": "det", "MCPR_Z": "new"}
    )
    assert len(problems) == 3
    assert any("MCPR_X default" in p for p in problems)
    assert any("marks `laya`" in p for p in problems)
    assert any("MCPR_Z (default `old`)" in p for p in problems)


def test_mutation_rule7_new_setting_without_regenerating(gen: ModuleType, tmp_path: Path) -> None:
    src = (REPO / "src" / "mcprouter" / "settings.py").read_text(encoding="utf-8")
    src = src.replace(
        "    dedup_max_pairs: int = 5000\n",
        "    dedup_max_pairs: int = 5000\n    canary_knob: int = 7\n",
        1,
    ).replace(
        "            dedup_max_pairs=",
        '            canary_knob=int(get("MCPR_CANARY_KNOB", "7")),\n            dedup_max_pairs=',
        1,
    )
    assert "MCPR_CANARY_KNOB" in src, "fixture anchor moved; update the mutation"
    path = _write(tmp_path, "settings_mut.py", src)
    spec = importlib.util.spec_from_file_location("settings_mut", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["settings_mut"] = mod
    try:
        spec.loader.exec_module(mod)
        rendered = gen.render(gen.collect(mod))
    finally:
        del sys.modules["settings_mut"]
    committed = (REPO / "docs" / "reference" / "configuration.md").read_text(encoding="utf-8")
    assert "`MCPR_CANARY_KNOB` | `7`" in rendered
    assert rendered != committed


def test_mutation_rule8_phantom_endpoint(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "docs/SPEC.md",
        "GET /api/v1/metrics and `POST /api/v1/route` and GET /api/v1/x/{id}.",
    )
    problems = check_endpoints(tmp_path, {("POST", "/api/v1/route"), ("GET", "/api/v1/x/{x_id}")})
    assert problems == ["docs/SPEC.md: GET /api/v1/metrics is not a route"]


def test_mutation_rule9_unknown_cli_flag(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "README.md",
        "```bash\n.venv/bin/python -m testbed.serve --servers 3 --no-such-flag  # x --ignored\n```\n",
    )
    documented = documented_cli(tmp_path)
    assert documented == {"testbed.serve": {"--servers", "--no-such-flag"}}
    assert check_cli(documented) == [
        "python -m testbed.serve: documented --no-such-flag is not in --help"
    ]


def test_mutation_rule10_node_version_drift(tmp_path: Path) -> None:
    _write(tmp_path, ".nvmrc", "22.1.0\n")
    _write(tmp_path, "Dockerfile", "ARG NODE_VERSION=20.0.0\nFROM node:${NODE_VERSION}-slim\n")
    assert check_node_version(tmp_path) == ["Dockerfile NODE_VERSION 20.0.0 != .nvmrc 22.1.0"]
