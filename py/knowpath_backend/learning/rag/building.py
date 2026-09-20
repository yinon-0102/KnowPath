"""Explicit, replayable construction of content indexes alongside legacy RAG."""
from __future__ import annotations

import hashlib
import json
import time

from .bm25 import BM25Index
from .chunking import semantic_chunks
from .contracts import SourceSpan
from .lifecycle import ManifestLifecycle
from .scope import allowed_chunk_ids
from .retrieval import RetrievalError
from ..materials.rag_cleanup import (begin_rag_write, finish_rag_write,
                                    reserve_rag_embedding, finish_rag_embedding)


def audit_sources(version, chunks):
    originals = {c.id:c for c in version.chunks}
    coverage = {identifier:bytearray(len(c.text)) for identifier,c in originals.items()}
    for chunk in chunks:
        parts = []
        for span in chunk['source_spans']:
            original = originals[span['block']]
            if original.content_hash != span['artifact_hash'] or not 0 <= span['start'] < span['end'] <= len(original.text):
                raise ValueError('invalid source span')
            parts.append(original.text[span['start']:span['end']])
            for position in range(span['start'],span['end']):
                coverage[original.id][position] = min(2,coverage[original.id][position]+1)
        if '\n'.join(parts) != chunk['source_text']:
            raise ValueError('source reconstruction mismatch')
    missing = sum(not coverage[c.id][i] for c in version.chunks for i,char in enumerate(c.text) if not char.isspace())
    duplicated = sum(coverage[c.id][i]>1 for c in version.chunks for i,char in enumerate(c.text) if not char.isspace())
    if missing or duplicated:
        raise ValueError('semantic parsing lost or duplicated source text')
    return dict(missing_nonwhitespace_characters=missing, duplicated_nonwhitespace_characters=duplicated,
        mapping_failures=0, exact_source_reconstruction=True,
        damaged_chunks=sum(c['quality'].get('content_status')=='damaged' for c in chunks),
        structure_unknown_chunks=sum(c['quality']['structure']=='unknown' for c in chunks))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class RagBuilder:
    def __init__(self, repository, materials, spaces, embedder, dense):
        self.repository, self.materials, self.spaces = repository, materials, spaces
        self.embedder, self.dense = embedder, dense
        self.lifecycle = ManifestLifecycle(repository)

    def _current(self, scope):
        if self.spaces.rag_scope_snapshot(scope.space_id).scope_snapshot_id != scope.scope_snapshot_id:
            raise ValueError("scope changed during index build")

    def build(self, space_id, material_version_id, *, max_tokens=1800, retry=False):
        scope = self.spaces.rag_scope_snapshot(space_id)
        binding = next((b for b in scope.bindings if b.material_version_id == material_version_id), None)
        if binding is None:
            raise ValueError("material version is not bound to this learning space")
        version = self.materials.get_version(material_version_id)
        material = self.materials.get_material(binding.material_id)
        if material is None or version is None or version.status != "ready" or material.status == "archived":
            raise ValueError("material version is unavailable")
        profile = dict(parser="legacy-original-bridge-v2", chunking="semantic-boundaries-v2",
            token_counter="utf8-byte-upper-bound-v1", max_tokens=max_tokens,
            document_title=material.name, embedding_model=self.embedder.model_version,
            embedding_dimension=self.embedder.dimension, vector_collection=self.dense.collection,
            collection_prefix=self.dense.collection_prefix,
            bm25={"tokenizer": "han-lexical-exploratory-v1", "k1": 1.5, "b": 0.75})
        # Original chunks are immutable extraction artifacts. Their identities
        # matter even if a later parser produces equal text at different offsets.
        retrieval_version_id = digest([material_version_id, version.content_hash,
            [(c.id, c.content_hash, c.page, c.section_path) for c in version.chunks], profile])
        chunks = semantic_chunks(version, retrieval_version_id, max_tokens=max_tokens,
                                 document_title=material.name)
        if not chunks:
            raise ValueError("no reliable source chunks to index")
        audit = audit_sources(version, chunks)
        self.repository.put_retrieval_version(dict(retrieval_version_id=retrieval_version_id,
            material_version_id=material_version_id, profile=profile, profile_hash=digest(profile)))
        for chunk in chunks:
            self.repository.put_chunk({k: v for k, v in chunk.items() if k != "source_spans"}, chunk["source_spans"])
        self.repository.put_scope_snapshot(scope.model_dump(exclude={"allowed_spans"}),
                                            [s.model_dump() for s in scope.allowed_spans])
        allowed = allowed_chunk_ids(scope, {c["chunk_id"]: tuple(SourceSpan.model_validate(s)
            for s in c["source_spans"]) for c in chunks})
        for identifier in allowed:
            self.repository.put_scope_chunk_map(dict(scope_snapshot_id=scope.scope_snapshot_id,
                retrieval_version_id=retrieval_version_id, chunk_id=identifier))
        expected = [dict(chunk_id=c["chunk_id"], content_hash=hashlib.sha256(c["retrieval_text"].encode()).hexdigest())
                    for c in chunks]
        manifest_id = digest([retrieval_version_id, scope.scope_snapshot_id, "a", expected])
        configuration = dict(profile, build_report=dict(audit, total_chunks=len(chunks),
            allowed_chunks=len(allowed), excluded_whole_chunks=len(chunks)-len(allowed)))
        manifest = self.lifecycle.start(retrieval_version_id, manifest_id, scope, expected, configuration, retry=retry)
        if manifest["status"] == "ready":
            return manifest
        attempt = manifest["attempt"]
        write_intent = None
        write_acknowledged = True
        build_usage = []
        stage = 'write_intent'
        try:
            write_intent = begin_rag_write(self.repository, manifest, binding.material_id, self.dense.collection)
            for start in range(0, len(chunks), 10):
                self._current(scope)
                batch = chunks[start:start + 10]
                if retry:
                    try:
                        self.dense.verify(batch)
                        continue
                    except RetrievalError as error:
                        if error.code != 'VECTOR_INDEX_NOT_READY':
                            raise
                stage = 'embedding'
                call_id = reserve_rag_embedding(self.repository, write_intent,
                    model=self.embedder.model_version, input_count=len(batch))
                call_started = time.monotonic()
                try:
                    vectors = self.embedder.embed([c["retrieval_text"] for c in batch])
                except Exception:
                    try:
                        finish_rag_embedding(self.repository, write_intent, call_id,
                            elapsed_seconds=time.monotonic() - call_started, failed=True)
                    except Exception:
                        pass  # The committed started reservation still says billing is unknown.
                    raise
                # A crash before this acknowledgement leaves an explicit unknown
                # call; retries have separate intent/attempt histories.
                audited = finish_rag_embedding(self.repository, write_intent, call_id,
                    elapsed_seconds=time.monotonic() - call_started, usage=getattr(self.embedder, 'last_usage', None))
                build_usage.append(dict(audited["usage"], model=audited["model"], elapsed_seconds=audited["elapsed_seconds"]))
                self._current(scope)
                write_acknowledged = False
                stage = 'upsert'
                self.dense.upsert(batch, vectors)
                write_acknowledged = True
                self._current(scope)
            lexical = BM25Index(chunks, profile=profile["bm25"])
            stage = 'validation'
            def validate(current, actual):
                self._current(scope)
                receipts = {"dense": self.dense.verify(actual), "lexical": lexical.verify(actual)}
                receipts['embedding_usage'] = build_usage
                self._current(scope)
                return receipts
            result = self.lifecycle.validate(retrieval_version_id, manifest_id, validate, attempt=attempt)
        except Exception as error:
            # Persist only a stable code; provider exceptions can include secrets.
            try:
                safe_codes = {"VECTOR_INDEX_NOT_READY", "VECTOR_PROFILE_MISMATCH", "VECTOR_UNAVAILABLE",
                    "VECTOR_INVALID_RESPONSE", "EMBEDDING_INVALID_RESPONSE", "EMBEDDING_UNAVAILABLE",
                    "EMBEDDING_INPUT_INVALID", "RATE_LIMITED", "UNSUPPORTED_MODEL"}
                code = error.code if isinstance(error, RetrievalError) and error.code in safe_codes else 'INTERNAL_FAILURE'
                self.lifecycle.mark_failed(retrieval_version_id, manifest_id,
                    attempt=attempt, reason='INDEX_BUILD_FAILED/' + stage + '/' +
                        code)
            except Exception:
                pass  # A deletion or newer attempt may already own the state.
            try:
                if self.materials.get_version(material_version_id) is None:
                    self.dense.delete_retrieval_version(retrieval_version_id)
            except Exception:
                pass  # The durable deletion event still owns a verified sweep.
            # A network error during mutation may leave the remote request
            # executing. Retain its durable fence until completion is proven.
            if write_intent is not None and write_acknowledged:
                try:
                    finish_rag_write(self.repository, write_intent)
                except Exception:
                    pass  # Leave an unresolved durable fence, never claim erasure.
            raise RetrievalError("INDEX_BUILD_FAILED") from None
        # Do not put this in finally: process interrupts must retain their fence
        # until an administrator confirms the writer/request has actually stopped.
        try:
            finish_rag_write(self.repository, write_intent)
        except Exception:
            raise RetrievalError("INDEX_BUILD_FINALIZATION_FAILED") from None
        return result

    def validate(self, manifest):
        rv, mid = manifest["retrieval_version_id"], manifest["manifest_id"]
        def readback(current, chunks):
            lexical = BM25Index(chunks, profile=current["configuration"]["bm25"])
            return {"dense": self.dense.verify(chunks), "lexical": lexical.verify(chunks)}
        return self.lifecycle.validate_ready(rv, mid, readback)

    def build_tree(self, ordinary, *, retry=False):
        """Add verified structural navigation without changing A's leaf index."""
        rv = ordinary['retrieval_version_id']
        ordinary = self.repository.get_manifest(rv, ordinary['manifest_id'])
        if ordinary is None or ordinary['status'] != 'ready' or not ordinary['a_ready']:
            raise ValueError('a ready ordinary manifest is required')
        scope = self.spaces.rag_scope_snapshot(ordinary['space_id'])
        if scope.scope_snapshot_id != ordinary['scope_snapshot_id']:
            raise ValueError('scope changed before tree construction')
        chunks = self.repository.list_chunks(rv)
        root = digest(['tree-root', rv])
        nodes = [dict(retrieval_version_id=rv, node_id=root, parent_node_id=None, kind='document')]
        from .chunking import _identifier
        sections = {tuple(c['section_path'][:depth]) for c in chunks
                    for depth in range(1, len(c['section_path']) + 1)}
        for path in sorted(sections, key=lambda p: (len(p), p)):
            node_id = _identifier('section', rv, ordinary['material_version_id'], path)
            parent = _identifier('section', rv, ordinary['material_version_id'], path[:-1]) if len(path) > 1 else root
            nodes.append(dict(retrieval_version_id=rv, node_id=node_id, parent_node_id=parent,
                              kind='section', section_path=list(path)))
        for chunk in sorted(chunks, key=lambda c: c['ordinal']):
            nodes.append(dict(retrieval_version_id=rv, node_id=digest(['leaf', rv, chunk['chunk_id']]),
                parent_node_id=chunk.get('parent_id') or root, chunk_id=chunk['chunk_id'], kind='leaf'))
        for node in nodes:
            self.repository.put_node(node)
        tree_version = digest(['structural-tree-v1', nodes])
        mid = digest([ordinary['manifest_id'], tree_version])
        manifest = self.lifecycle.start(rv, mid, scope, ordinary['expected_chunks'], ordinary['configuration'],
                                        tree_version_id=tree_version, retry=retry)
        if manifest['status'] == 'ready':
            return manifest
        def validate(current, actual):
            self._current(scope)
            receipts = self.validate(ordinary)
            receipts['tree'] = {'verified': True, 'tree_version_id': tree_version}
            self._current(scope)
            return receipts
        result = self.lifecycle.validate(rv, mid, validate, attempt=manifest['attempt'])
        if not result['b1_ready']:
            raise ValueError('tree structural validation failed')
        return result

    def publish(self, manifest, *, expected_generation):
        self.validate(manifest)
        return self.lifecycle.publish(manifest["retrieval_version_id"], manifest["manifest_id"],
                                      expected_generation=expected_generation)
