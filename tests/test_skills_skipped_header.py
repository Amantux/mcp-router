"""X-Skipped-Resources is capped so a huge skip list can't blow header limits."""

from urllib.parse import unquote

from mcprouter.api.routes_skills import SKIPPED_HEADER_MAX, skipped_header


def test_short_list_is_joined_verbatim() -> None:
    assert skipped_header(["a/b.md", "c d.txt"]) == "a/b.md,c%20d.txt"
    assert skipped_header([]) == ""


def test_300_long_paths_are_capped_with_count_suffix() -> None:
    paths = [f"skill-{i:03d}/" + "x" * 120 + ".bin" for i in range(300)]
    v = skipped_header(paths)
    assert len(v.encode()) <= SKIPPED_HEADER_MAX == 2048
    head, _, tail = v.rpartition(",...+")
    kept = head.split(",")
    assert int(tail) == 300 - len(kept) > 0
    assert [unquote(p) for p in kept] == paths[: len(kept)]  # whole entries only
