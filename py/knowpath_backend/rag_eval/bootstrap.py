"""Build an isolated real-material development workspace; never touches business SQL."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

import sqlalchemy as sa
from qdrant_client import QdrantClient

from .dataset import digest, freeze
from .cli import runtime_configuration, configured_runtime
from .runner import run
from .scoring import report


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def export_bytes(path, content, *, resume):
    """Atomically export a known generated artifact in the isolated workspace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not resume:
        raise FileExistsError(path)
    temporary = path.with_name(path.name + '.export-' + uuid4().hex + '.tmp')
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _require_whole_document(row, version, filename, raw_hash):
    """Prove exact original-artifact union before widening to a document space."""
    spans = row.get('allowed_source_spans', [])
    if (not spans or row.get('excluded_source_spans')
            or {s.get('source_filename') for s in spans} != {filename}):
        raise ValueError('bootstrap expects exact whole-document scopes')
    intervals = {i: [] for i in range(len(version.chunks))}
    for span in spans:
        ordinal, start, end = span.get('source_ordinal'), span.get('start'), span.get('end')
        if (type(ordinal) is not int or ordinal not in intervals
                or type(start) is not int or type(end) is not int):
            raise ValueError('invalid whole-document artifact coordinates')
        chunk = version.chunks[ordinal]
        if (span.get('raw_source_sha256') != raw_hash or span.get('artifact_hash') != chunk.content_hash
                or span.get('page') != chunk.page or not 0 <= start < end <= len(chunk.text)):
            raise ValueError('whole-document artifact identity or bounds mismatch')
        intervals[ordinal].append((start, end))
    for ordinal, parts in intervals.items():
        cursor = 0
        for start, end in sorted(parts):
            if start > cursor:
                raise ValueError('whole-document scope has an uncovered interval')
            cursor = max(cursor, end)
        if cursor != len(version.chunks[ordinal].text):
            raise ValueError('whole-document scope does not cover every original artifact')


class _AuditedEmbedder:
    """Capture attempted calls, including failed calls with unknown billing.

    Historical manifest usage is recorded separately from these current-attempt
    calls so a reused index is not charged again by an offline accountant.
    """
    def __init__(self, provider):
        self.provider = provider
        self.records = []
        self.last_usage = None

    @property
    def dimension(self):
        return self.provider.dimension

    @property
    def model_version(self):
        return self.provider.model_version

    def embed(self, texts, *, query=False):
        started = time.monotonic()
        record = dict(status='failed', texts=len(texts), usage=None,
                      billing_status='unknown', bill_amount=None)
        self.records.append(record)
        self.last_usage = None
        try:
            result = self.provider.embed(texts, query=query)
            usage = getattr(self.provider, 'last_usage', None)
            if isinstance(usage, dict):
                self.last_usage = {k: deepcopy(v) for k, v in usage.items() if k in {
                    'model', 'input_tokens', 'prompt_tokens', 'output_tokens', 'completion_tokens',
                    'total_tokens', 'calls', 'complete', 'elapsed_seconds', 'latency_seconds'}}
            record.update(status='succeeded', usage=deepcopy(self.last_usage))
            return result
        finally:
            record['seconds'] = time.monotonic() - started


def prepare(samples, dataset_dir, output, *, resume=False):
    from knowpath_backend.learning.config import LearningSettings
    from knowpath_backend.learning.persistence.db import init_db
    from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
    from knowpath_backend.learning.persistence.rag_repository import SqlRagRepository
    from knowpath_backend.learning.providers.models import embedding_model
    from knowpath_backend.learning.rag.building import RagBuilder
    from knowpath_backend.learning.rag.vector import QdrantContentIndex
    from knowpath_backend.learning.state import LearningState
    samples, dataset_dir, output = Path(samples), Path(dataset_dir), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=resume)
    database_url = 'sqlite:///' + (output/'evaluation.db').as_posix()
    previous=json.loads((output/'setup.json').read_text(encoding='utf-8')) if resume else None
    if previous and (previous['database_url'] != database_url or previous['status'] != 'building'):
        raise ValueError('resume only an unfinished isolated workspace at its original path')
    prefix = previous['collection_prefix'] if previous else 'knowpath_rag_dev_' + uuid4().hex[:16]
    if not re.fullmatch(r'knowpath_rag_dev_[0-9a-f]{16}',prefix): raise ValueError('invalid isolated collection')
    setup = deepcopy(previous) if previous else dict(database_url=database_url, collection_prefix=prefix,
        documents={}, build_records=[], status='building')
    setup.setdefault('documents', {})
    setup.setdefault('build_records', [])
    attempt = dict(attempt_id=uuid4().hex, resume=resume, status='building', stage='initialization',
                   started_at=datetime.now(timezone.utc).isoformat())
    setup.setdefault('attempts', []).append(attempt)
    attempt_started = time.monotonic()
    def journal():
        export_bytes(output/'setup.json', (json.dumps(setup,ensure_ascii=False,indent=2)+'\n').encode('utf-8'), resume=True)
    if resume: journal()
    else: write_json(output/'setup.json', setup)
    engine = client = None
    try:
        settings = LearningSettings.from_env()
        engine = sa.create_engine(database_url, hide_parameters=True)
        sa.event.listen(engine, 'connect', lambda db, _: db.execute('PRAGMA foreign_keys=ON'))
        init_db(engine)
        client = QdrantClient(url=os.getenv('QDRANT_URL','http://127.0.0.1:6333'),
            api_key=os.getenv('QDRANT_API_KEY') or None, timeout=30, check_compatibility=False)
        embedder = _AuditedEmbedder(embedding_model(settings))
        dense = QdrantContentIndex(client, prefix, embedder.dimension, dict(provider=settings.embedding_provider,
            model=embedder.model_version, dimension=embedder.dimension, endpoint=settings.embedding_base_url))
        if previous and previous.get('collection') is not None and previous['collection'] != dense.collection:
            raise ValueError('resume index configuration changed')
        setup['collection'] = dense.collection
        state = LearningState(SqlAlchemyMaterialRepository(engine))
        repo = SqlRagRepository(engine)
        builder = RagBuilder(repo, state.material_repository, state.space_service, embedder, dense)
        attempt['stage'] = 'draft_validation'
        rows = [json.loads(line) for line in (dataset_dir/'dataset.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
        for row in rows:
            if len({s['source_filename'] for s in row['allowed_source_spans']}) != 1 or row.get('excluded_source_spans'):
                raise ValueError('bootstrap expects single whole-document scopes')
        documents = sorted({s['source_filename'] for row in rows for s in row['allowed_source_spans']})
        live, bindings = {}, {}
        for filename in documents:
            started = time.monotonic()
            record = dict(filename=filename, attempt_id=attempt['attempt_id'], status='building', stage='source_validation',
                          started_at=datetime.now(timezone.utc).isoformat(), embedding_attempts=[], embedding_usage=None,
                          reused_index=False)
            setup['build_records'].append(record)
            embedder.records = record['embedding_attempts']
            attempt['stage'] = 'document_build'
            try:
                if Path(filename).name != filename:
                    raise ValueError('source filename must be a basename')
                raw = (samples/filename).read_bytes()
                raw_hash = hashlib.sha256(raw).hexdigest()
                expected_hashes={span['raw_source_sha256'] for row in rows for span in row['allowed_source_spans']
                    if span['source_filename']==filename}
                if expected_hashes != {raw_hash}:
                    raise ValueError('public source snapshot changed')
                uploaded = state.material_service.create(filename=filename, content=raw, idempotency_key='eval-'+filename)
                for row in rows:
                    if row['allowed_source_spans'][0]['source_filename'] == filename:
                        _require_whole_document(row, uploaded.version, filename, raw_hash)
                space = next((s for s in state.space_service.list() if s['name']=='RAG development '+filename and
                    {b['material_version_id'] for b in s['bindings']}=={uploaded.version.id}),None)
                if space is None: space = state.create_space({'name':'RAG development '+filename, 'material_ids':[uploaded.material.id]})
                from knowpath_backend.learning.persistence.rag_models import TABLES
                with engine.connect() as connection:
                    existing = [r['payload'] for r in connection.execute(sa.select(TABLES['rag_manifests']).filter_by(
                        space_id=space['id'], status='ready')).mappings()]
                ready_ids = {m['manifest_id'] for m in existing}
                record['existing_manifest_embedding_usage'] = {m['manifest_id']:deepcopy(m.get('verification',{}).get('embedding_usage'))
                    for m in existing if not m.get('tree_version_id') and m.get('material_version_id') == uploaded.version.id}
                record['stage'] = 'ordinary_index'
                a = builder.build(space['id'], uploaded.version.id,retry=resume)
                record.update(reused_index=a['manifest_id'] in ready_ids, ordinary_manifest_id=a['manifest_id'],
                              embedding_usage=deepcopy(a.get('verification',{}).get('embedding_usage')))
                record['stage'] = 'tree_index'
                b1 = builder.build_tree(a,retry=resume)
                with engine.connect() as connection:
                    publication=connection.execute(sa.select(TABLES['rag_publications']).filter_by(
                        space_id=space['id'],material_version_id=uploaded.version.id)).mappings().first()
                record['stage'] = 'publication'
                if not publication or publication['manifest_id']!=b1['manifest_id']:
                    builder.publish(b1, expected_generation=publication['generation'] if publication else 0)
                scope = state.space_service.rag_scope_snapshot(space['id'])
                binding = dict(space_id=space['id'], scope_snapshot_id=scope.scope_snapshot_id,
                    expected_scope_version=scope.scope_version, expected_bindings=[b.model_dump() for b in scope.bindings],
                    expected_manifest_ids=[b1['manifest_id']], expected_retrieval_versions=[b1['retrieval_version_id']],
                    manifest_configuration_hashes={b1['manifest_id']:digest(b1['configuration'])})
                live[filename] = uploaded
                bindings[filename] = binding
                setup['documents'][filename] = {'material_id':uploaded.material.id,'material_version_id':uploaded.version.id,
                    'source_sha256':raw_hash, 'binding':binding}
                record.update(status='ready', chunks=len(repo.list_chunks(b1['retrieval_version_id'])),
                              a_ready=b1['a_ready'], b1_ready=b1['b1_ready'])
                print(json.dumps({'built':filename,'chunks':record['chunks']}), flush=True)
            finally:
                record['seconds'] = time.monotonic()-started
                if record['status'] != 'ready':
                    record.update(status='failed', failure_code='BOOTSTRAP_DOCUMENT_BUILD_FAILED')
                journal()
        attempt['stage'] = 'mapping_export'
        def remap(value):
            if isinstance(value, dict):
                result = {key:remap(item) for key,item in value.items()}
                if 'source_filename' in value and 'source_ordinal' in value and 'artifact_hash' in value:
                    original = live[value['source_filename']]
                    if type(value['source_ordinal']) is not int or not 0 <= value['source_ordinal'] < len(original.version.chunks):
                        raise ValueError('draft/live original mapping mismatch')
                    chunk = original.version.chunks[value['source_ordinal']]
                    if (chunk.content_hash != value['artifact_hash'] or chunk.page != value.get('page') or
                            value.get('raw_source_sha256') != original.version.content_hash or
                            ('quote' in value and chunk.text[value['start']:value['end']] != value['quote']) or
                            not 0 <= value['start'] < value['end'] <= len(chunk.text)):
                        raise ValueError('draft/live original mapping mismatch')
                    result.update(material_id=original.material.id, material_version_id=original.version.id, block=chunk.id)
                return result
            return [remap(item) for item in value] if isinstance(value,list) else value
        rows = remap(rows)
        runtime_bindings = {}
        for row in rows:
            filenames = {s['source_filename'] for s in row['allowed_source_spans']}
            if len(filenames) != 1 or row.get('excluded_source_spans'):
                raise ValueError('bootstrap expects single whole-document scopes')
            binding = bindings[next(iter(filenames))]
            row['scope_snapshot_id'] = binding['scope_snapshot_id']
            runtime_bindings[row['question_id']] = binding
        export_bytes(output/'dataset.jsonl', ''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows).encode('utf-8'), resume=resume)
        for source in (dataset_dir/'sources').rglob('*'):
            if source.is_file():
                export_bytes(output/'sources'/source.relative_to(dataset_dir/'sources'), source.read_bytes(), resume=resume)
        export_bytes(output/'split.json', (dataset_dir/'split.json').read_bytes(), resume=resume)
        rubric = dataset_dir/'rubric.md'
        if not rubric.exists(): rubric = dataset_dir.parent/'rubric.md'
        export_bytes(output/'rubric.md', rubric.read_bytes(), resume=resume)
        os.environ['RAG_COLLECTION_PREFIX'] = prefix
        config = dict(dataset='dataset.jsonl',split='split.json',rubric='rubric.md',human_review_status='pending',
            repeats=1,order_schedule='alternating',retries=0, **runtime_configuration(settings),
            profile={'manifest_configuration_hashes':{k:v for b in bindings.values() for k,v in b['manifest_configuration_hashes'].items()}},
            statistics=dict(method='family_descriptive',estimand='full_task_success_difference',weighting='equal_family',
                repeat_aggregation='question_mean',failures='zero',quantile='nearest_rank',seed=20260920),
            gates={k:None for k in ('max_p95_latency_ms','max_mean_cost_per_request','max_b1_latency_ratio','max_b1_cost_ratio')},
            pricing={'currency':'CNY','date':None,'price_table':{}},decision_rule='exploratory_no_adoption',runtime_bindings=runtime_bindings)
        export_bytes(output/'development.json', json.dumps(config,ensure_ascii=False,indent=2).encode('utf-8'), resume=resume)
        setup['status']='ready'
        attempt['status']='ready'
    finally:
        attempt['seconds'] = time.monotonic() - attempt_started
        if attempt['status'] != 'ready':
            attempt.update(status='failed', failure_code='BOOTSTRAP_PREPARATION_FAILED')
        # setup is a progress journal, never a publishable manifest or freeze.
        try:
            journal()
        finally:
            try:
                if client is not None: client.close()
            finally:
                if engine is not None: engine.dispose()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','resume','run-dev'])
    parser.add_argument('--env-file',required=True)
    parser.add_argument('--samples')
    parser.add_argument('--dataset-dir')
    parser.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(args.env_file)
    output=Path(args.output).resolve()
    if args.action in {'prepare','resume'}:
        prepare(args.samples,args.dataset_dir,output,resume=args.action=='resume')
    else:
        setup=json.loads((output/'setup.json').read_text(encoding='utf-8'))
        if setup['status']!='ready': raise ValueError('development workspace is not ready')
        os.environ['DATABASE_URL']=setup['database_url']
        os.environ['RAG_COLLECTION_PREFIX']=setup['collection_prefix']
        frozen=freeze(output/'development.json',output/'freeze.json')
        factory,resolver,engine=configured_runtime(frozen)
        try:
            records=run(output/'freeze.json',output/'dev.jsonl',partition='dev',pipeline_factory=factory,scope_resolver=resolver)
            write_json(output/'report.json',report(output/'freeze.json',output/'dev.jsonl'))
            print(json.dumps({'requests':len(records),'service_successes':sum(r['service_success'] for r in records)}))
        finally: engine.dispose()


if __name__=='__main__':
    main()
