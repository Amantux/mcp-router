"""CatalogIndex: tiers A (metadata/tag) + B (embedding) from catalog_schema.md §3.

Incremental: add/update/remove a single entry never rebuilds the whole
index (requirements §4). A monotonically increasing `version` stamps every
index mutation so decisions/caches can be tied to an exact catalog snapshot
(requirements §3 replayability).
"""

from typing import Dict, Iterable, List, Optional, Set

from .schema import CatalogEntry
from .text import DEFAULT_EMBEDDER, SparseVector


class CatalogIndex:
    def __init__(self, embedder=None):
        self.embedder = embedder or DEFAULT_EMBEDDER
        self.entries: Dict[str, CatalogEntry] = {}
        self.embeddings: Dict[str, SparseVector] = {}
        self.tag_index: Dict[str, Set[str]] = {}
        self.version: int = 0

    def __len__(self) -> int:
        return len(self.entries)

    # -- mutation (incremental, no full rebuild) -----------------------
    def add(self, entry: CatalogEntry) -> None:
        self._unindex_tags(entry.id)
        self.entries[entry.id] = entry
        self.embeddings[entry.id] = self.embedder.embed(entry.embedding_text())
        self._index_tags(entry)
        self.version += 1

    def add_many(self, entries: Iterable[CatalogEntry]) -> None:
        for e in entries:
            self.add(e)

    def remove(self, entry_id: str) -> None:
        if entry_id not in self.entries:
            return
        self._unindex_tags(entry_id)
        del self.entries[entry_id]
        self.embeddings.pop(entry_id, None)
        self.version += 1

    def update(self, entry: CatalogEntry) -> None:
        entry.entry_version = self.entries.get(entry.id, entry).entry_version + 1 if entry.id in self.entries else entry.entry_version
        self.add(entry)

    def _index_tags(self, entry: CatalogEntry) -> None:
        for tag in list(entry.capability_tags) + list(entry.domain_tags):
            self.tag_index.setdefault(tag, set()).add(entry.id)

    def _unindex_tags(self, entry_id: str) -> None:
        old = self.entries.get(entry_id)
        if not old:
            return
        for tag in list(old.capability_tags) + list(old.domain_tags):
            ids = self.tag_index.get(tag)
            if ids:
                ids.discard(entry_id)
                if not ids:
                    del self.tag_index[tag]

    # -- read ------------------------------------------------------------
    def get(self, entry_id: str) -> Optional[CatalogEntry]:
        return self.entries.get(entry_id)

    def ids_for_tags(self, tags: Iterable[str]) -> Set[str]:
        out: Set[str] = set()
        for t in tags:
            out |= self.tag_index.get(t, set())
        return out

    def all_ids(self) -> List[str]:
        return list(self.entries.keys())
