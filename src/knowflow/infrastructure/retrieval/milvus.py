'''Milvus retrieval backend adapter.

Implements RetrievalBackend for dense (vector) and BM25 (full-text) search
against a Milvus collection.  For now this is a stub that returns empty
results; real Milvus connectivity will be wired when the infrastructure
container is available.
'''

from __future__ import annotations

from collections.abc import Sequence

from knowflow.application.knowledge.retrieval import (
    RetrievalAclFilter,
    RetrievalCandidate,
    RetrievalQuery,
)
from knowflow.config import get_settings


class MilvusRetrievalBackend:
    '''Milvus-backed hybrid retrieval adapter.

    Accepts collection name and connection parameters from application config.
    ACL scope-token filter expressions are built server-side for Milvus
    attribute filtering.
    '''

    def __init__(
        self,
        *,
        collection_name: str = 'knowflow_knowledge',
    ) -> None:
        self._collection_name = collection_name
        settings = get_settings()
        self._milvus_uri = settings.milvus_uri

    async def dense_search(
        self,
        *,
        query: RetrievalQuery,
        acl_filter: RetrievalAclFilter,
        limit: int,
    ) -> Sequence[RetrievalCandidate]:
        _ = (query, acl_filter, limit)
        return ()

    async def bm25_search(
        self,
        *,
        query: RetrievalQuery,
        acl_filter: RetrievalAclFilter,
        limit: int,
    ) -> Sequence[RetrievalCandidate]:
        _ = (query, acl_filter, limit)
        return ()


__all__ = ['MilvusRetrievalBackend']