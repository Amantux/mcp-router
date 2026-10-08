"""Process-global generation counters for routing-cache invalidation.

Two monotonically increasing integers, read by the route cache when it builds
a key and bumped by whoever changes what routing would see:

* catalog — discovery sync that changed the catalog, server health crossing
  into/out of `offline`, server enable/disable/delete, tool enable/disable,
  classification writes (human override and the post-sync auto-classifier),
  and re-embedding.
* policy  — principal and policy-rule mutations.

A bump only makes cached entries unreachable (their keys carry the old
value); it never authorizes anything. Correctness does not depend on a bump
landing: a cache hit is always re-checked against current eligibility and
policy (routing/cache.py). Bumps are what make NEW tools / NEW grants show up
before the TTL expires.

In-process only (scoping.md §3: one process, no Redis). Call sites bump AFTER
their transaction commits so a concurrent route cannot cache pre-commit state
under the new generation.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_catalog = 0
_policy = 0


def catalog_generation() -> int:
    with _lock:
        return _catalog


def policy_generation() -> int:
    with _lock:
        return _policy


def bump_catalog() -> int:
    global _catalog
    with _lock:
        _catalog += 1
        return _catalog


def bump_policy() -> int:
    global _policy
    with _lock:
        _policy += 1
        return _policy
