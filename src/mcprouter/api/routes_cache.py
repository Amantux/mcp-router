"""REST: route cache observability (FR-08).

Admin-only endpoint to inspect and flush the route cache.

GET  /api/v1/cache      — aggregate stats (enabled, entryCount, size, TTL,
                          hits/misses/evictions by reason, catalog+policy gens)
POST /api/v1/cache/flush — invalidate all entries by bumping both generations
"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from mcprouter.api.deps_auth import require_admin
from mcprouter.generation import bump_catalog, bump_policy
from mcprouter.routing.cache import QueryEmbeddingCache, RouteCache

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["cache"])
Admin = Depends(require_admin)


class _Wire(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CacheStats(_Wire):
    enabled: bool
    entry_count: int
    size: int
    ttl_s: float
    hits: int
    misses: int
    evictions_capacity: int
    evictions_expired: int
    catalog_generation: int
    policy_generation: int


class CacheFlush(_Wire):
    catalog_generation: int
    policy_generation: int


def _get_route_cache(request: Request) -> RouteCache | None:
    """Extract the route cache from the app state."""
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None:
        return None
    return getattr(pipeline, "_cache", None)


def _get_embedding_cache(request: Request) -> QueryEmbeddingCache:
    """Extract the embedding cache from the app state."""
    pipeline = getattr(request.app.state, "pipeline", None)
    if pipeline is None:
        return QueryEmbeddingCache(size=0)
    # QueryEmbeddingCache is not stored separately; create a new one
    # The actual cache is internal to the retriever, but we can report on it
    return QueryEmbeddingCache()


@router.get("/cache", response_model=CacheStats, dependencies=[Admin])
def get_cache_stats(request: Request) -> CacheStats:
    """Aggregate route cache stats.

    Returns:
        - enabled: whether the cache is configured
        - entry_count: current number of entries
        - size: configured max entries
        - ttl_s: configured TTL in seconds
        - hits/misses/evictions: Prometheus counters
        - catalog_generation/policy_generation: current generation numbers

    Never returns per-entry keys, agent ids or queries.
    """
    route_cache = _get_route_cache(request)
    if route_cache is None:
        return CacheStats(
            enabled=False,
            entry_count=0,
            size=0,
            ttl_s=0.0,
            hits=0,
            misses=0,
            evictions_capacity=0,
            evictions_expired=0,
            catalog_generation=0,
            policy_generation=0,
        )

    stats = route_cache.stats()
    from mcprouter.generation import catalog_generation, policy_generation

    # Extract values - stats dict values are already the correct types
    entry_count = cast(int, stats["entryCount"])
    size = cast(int, stats["size"])
    ttl_s = cast(float, stats["ttl_s"])
    hits = cast(int, stats["hits"])
    misses = cast(int, stats["misses"])
    evictions_capacity = cast(int, stats["evictions_capacity"])
    evictions_expired = cast(int, stats["evictions_expired"])
    catalog_gen = catalog_generation()
    policy_gen = policy_generation()

    return CacheStats(
        enabled=True,
        entry_count=entry_count,
        size=size,
        ttl_s=ttl_s,
        hits=hits,
        misses=misses,
        evictions_capacity=evictions_capacity,
        evictions_expired=evictions_expired,
        catalog_generation=catalog_gen,
        policy_generation=policy_gen,
    )


@router.post("/cache/flush", response_model=CacheFlush, dependencies=[Admin])
def flush_cache(request: Request) -> CacheFlush:
    """Invalidate all cache entries by bumping both generation counters.

    This invalidates every cached entry without requiring a restart.
    The next route request will be a cache miss and will rebuild the cache
    with fresh data.

    Returns the new catalog and policy generation numbers.
    """
    # Bump both generations to invalidate all cached entries
    new_catalog_gen = bump_catalog()
    new_policy_gen = bump_policy()

    log.info(
        "cache.flushed catalog_generation=%d policy_generation=%d",
        new_catalog_gen,
        new_policy_gen,
    )

    return CacheFlush(
        catalog_generation=new_catalog_gen,
        policy_generation=new_policy_gen,
    )
