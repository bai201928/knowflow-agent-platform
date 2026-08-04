'''ACL-safe deterministic hybrid knowledge retrieval.

The application boundary owns evidence-selection policy and post-authorization
filtering. The backend is deliberately abstracted so the same retrieval service
works with in-memory fakes (unit tests) and Milvus (production) without
changing the trust model.
'''

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from knowflow.application.auth.policy import AccessContext


class RetrievalDisposition(Enum):
    SUFFICIENT = 'SUFFICIENT'
    INSUFFICIENT_EVIDENCE = 'INSUFFICIENT_EVIDENCE'


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    text: str


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    query: RetrievalQuery
    top_k: int
    context_token_budget: int
    min_fused_score: float = 0.0


@dataclass(frozen=True, slots=True)
class RetrievalAclFilter:
    scope_tokens: tuple[str, ...]
    scope_fingerprint: str
    acl_version: int


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    segment_id: str
    document_id: str
    document_version_id: str
    text: str
    token_count: int
    score: float
    scope_tokens: frozenset[str]
    document_active: bool
    version_active: bool
    source_anchor: str


class RetrievalBackend(Protocol):
    async def dense_search(
        self,
        *,
        query: RetrievalQuery,
        acl_filter: RetrievalAclFilter,
        limit: int,
    ) -> Sequence[RetrievalCandidate]: ...

    async def bm25_search(
        self,
        *,
        query: RetrievalQuery,
        acl_filter: RetrievalAclFilter,
        limit: int,
    ) -> Sequence[RetrievalCandidate]: ...


@dataclass(frozen=True, slots=True)
class RetrievalEvidence:
    segment_id: str
    text: str
    token_count: int
    fused_score: float
    dense_score: float | None
    bm25_score: float | None
    rank: int
    trusted: bool
    source_anchor: str


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    disposition: RetrievalDisposition
    evidence: tuple[RetrievalEvidence, ...]
    cache_key: str
    scope_fingerprint: str
    acl_version: int
    retrieval_config_version: str
    context_token_count: int


class RetrievalPort(Protocol):
    async def retrieve(
        self,
        request: RetrievalRequest,
        *,
        context: AccessContext,
    ) -> RetrievalResult: ...


@dataclass
class _FusedItem:
    segment_id: str
    candidate: RetrievalCandidate
    dense_score: float | None
    bm25_score: float | None
    fused_score: float


class HybridRetrievalService:
    """ACL-safe deterministic hybrid retrieval with RRF fusion."""

    def __init__(
        self,
        *,
        backend: RetrievalBackend,
        rrf_k: int = 60,
        retrieval_config_version: str = 'hybrid-v1',
    ) -> None:
        self._backend = backend
        self._rrf_k = rrf_k
        self._retrieval_config_version = retrieval_config_version

    async def retrieve(
        self,
        request: RetrievalRequest,
        *,
        context: AccessContext,
    ) -> RetrievalResult:
        acl_filter = RetrievalAclFilter(
            scope_tokens=context.acl_scope_tokens(),
            scope_fingerprint=context.scope_fingerprint(),
            acl_version=context.acl_version,
        )

        limit = max(request.top_k * 2, 5)
        dense_results = await self._backend.dense_search(
            query=request.query,
            acl_filter=acl_filter,
            limit=limit,
        )
        bm25_results = await self._backend.bm25_search(
            query=request.query,
            acl_filter=acl_filter,
            limit=limit,
        )

        fused = self._rrf_fuse(dense_results, bm25_results, k=self._rrf_k)

        acl_tokens = frozenset(context.acl_scope_tokens())
        filtered: list[_FusedItem] = []
        for item in fused:
            if item.fused_score < request.min_fused_score:
                continue
            if not item.candidate.scope_tokens & acl_tokens:
                continue
            if not item.candidate.document_active or not item.candidate.version_active:
                continue
            filtered.append(item)

        if not filtered:
            return RetrievalResult(
                disposition=RetrievalDisposition.INSUFFICIENT_EVIDENCE,
                evidence=(),
                cache_key=self._build_cache_key(context),
                scope_fingerprint=context.scope_fingerprint(),
                acl_version=context.acl_version,
                retrieval_config_version=self._retrieval_config_version,
                context_token_count=0,
            )

        selected: list[RetrievalEvidence] = []
        token_total = 0
        for item in filtered:
            if len(selected) >= request.top_k:
                break
            if token_total + item.candidate.token_count > request.context_token_budget:
                continue
            selected.append(
                RetrievalEvidence(
                    segment_id=item.candidate.segment_id,
                    text=item.candidate.text,
                    token_count=item.candidate.token_count,
                    fused_score=item.fused_score,
                    dense_score=item.dense_score,
                    bm25_score=item.bm25_score,
                    rank=len(selected) + 1,
                    trusted=False,
                    source_anchor=item.candidate.source_anchor,
                )
            )
            token_total += item.candidate.token_count

        if not selected:
            return RetrievalResult(
                disposition=RetrievalDisposition.INSUFFICIENT_EVIDENCE,
                evidence=(),
                cache_key=self._build_cache_key(context),
                scope_fingerprint=context.scope_fingerprint(),
                acl_version=context.acl_version,
                retrieval_config_version=self._retrieval_config_version,
                context_token_count=0,
            )

        return RetrievalResult(
            disposition=RetrievalDisposition.SUFFICIENT,
            evidence=tuple(selected),
            cache_key=self._build_cache_key(context),
            scope_fingerprint=context.scope_fingerprint(),
            acl_version=context.acl_version,
            retrieval_config_version=self._retrieval_config_version,
            context_token_count=token_total,
        )

    def _build_cache_key(self, context: AccessContext) -> str:
        parts = (
            context.scope_fingerprint()
            + ':'
            + str(context.acl_version)
            + ':'
            + self._retrieval_config_version
        )
        return hashlib.sha256(parts.encode()).hexdigest()

    def _rrf_fuse(
        self,
        dense: Sequence[RetrievalCandidate],
        bm25: Sequence[RetrievalCandidate],
        *,
        k: int,
    ) -> list[_FusedItem]:
        dense_ranks: dict[str, int] = {}
        for candidate in dense:
            if candidate.segment_id not in dense_ranks:
                dense_ranks[candidate.segment_id] = len(dense_ranks)
        bm25_ranks: dict[str, int] = {}
        for candidate in bm25:
            if candidate.segment_id not in bm25_ranks:
                bm25_ranks[candidate.segment_id] = len(bm25_ranks)

        fused_map: dict[str, dict] = {}
        for candidate in dense:
            if candidate.segment_id not in fused_map:
                fused_map[candidate.segment_id] = {
                    'candidate': candidate,
                    'dense_score': candidate.score,
                    'dense_rank': dense_ranks.get(candidate.segment_id),
                    'bm25_score': None,
                    'bm25_rank': None,
                }
            elif fused_map[candidate.segment_id]['dense_score'] is None:
                fused_map[candidate.segment_id]['dense_score'] = candidate.score
        for candidate in bm25:
            if candidate.segment_id not in fused_map:
                fused_map[candidate.segment_id] = {
                    'candidate': candidate,
                    'dense_score': None,
                    'dense_rank': None,
                    'bm25_score': candidate.score,
                    'bm25_rank': bm25_ranks.get(candidate.segment_id),
                }
            else:
                entry = fused_map[candidate.segment_id]
                if entry['bm25_score'] is None:
                    entry['bm25_score'] = candidate.score
                if entry['bm25_rank'] is None:
                    entry['bm25_rank'] = bm25_ranks.get(candidate.segment_id)

        results: list[_FusedItem] = []
        for seg_id, info in fused_map.items():
            fused = 0.0
            if info['dense_rank'] is not None:
                fused += 1.0 / (k + info['dense_rank'] + 1)
            if info['bm25_rank'] is not None:
                fused += 1.0 / (k + info['bm25_rank'] + 1)
            results.append(
                _FusedItem(
                    segment_id=seg_id,
                    candidate=info['candidate'],
                    dense_score=info['dense_score'],
                    bm25_score=info['bm25_score'],
                    fused_score=fused,
                )
            )

        results.sort(key=lambda item: (-item.fused_score, item.segment_id))
        return results


__all__ = [
    'HybridRetrievalService',
    'RetrievalAclFilter',
    'RetrievalBackend',
    'RetrievalCandidate',
    'RetrievalDisposition',
    'RetrievalEvidence',
    'RetrievalPort',
    'RetrievalQuery',
    'RetrievalRequest',
    'RetrievalResult',
]