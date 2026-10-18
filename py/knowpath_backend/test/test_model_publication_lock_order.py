"""Publication must hold source parents before fencing its outbox event."""
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import mysql

from knowpath_backend.learning.persistence.db import init_db
from knowpath_backend.learning.persistence.material_repository import SqlAlchemyMaterialRepository
from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
from knowpath_backend.test.test_study_lessons import setup_lesson, Generator
from knowpath_backend.test.test_learning_note_integration import completed, DeterministicNotes


@pytest.mark.parametrize('kind', ['lesson', 'streaming_lesson', 'note', 'reading_message', 'lesson_message'])
def test_model_publication_does_not_acquire_source_parents_after_event(tmp_path, kind):
    engine = create_engine(f"sqlite:///{tmp_path / 'publication.db'}")
    init_db(engine)
    state, sid, node, payload = setup_lesson(SqlAlchemyMaterialRepository(engine))
    if kind.endswith('_message'):
        from knowpath_backend.test.test_learning_messages import FixedAnswer
        from knowpath_backend.test.test_reading_messages import reading
        from knowpath_backend.learning.rag.retrieval import KeywordRetriever
        if kind == 'lesson_message':
            lesson = state.lesson_service.create(sid, node, payload)
            state.lesson_service.generator = Generator()
            assert ModelTaskWorker(lessons=state.lesson_service).run_once()
            context = {'lesson_id': lesson['lesson_id']}
        else:
            context = {'reading_context': reading(state.get_space(sid))}
        state.message_service.generator = FixedAnswer()
        state.message_service.retriever = KeywordRetriever()
        sent = state.send_message(sid, {'message': 'Explain functions', **context}, durable=True,
            dispatch=lambda *args: None)
        service = state.message_service
        worker = ModelTaskWorker(messages=service)
    elif kind == 'note':
        completed(state, sid)
        service = state.notes_service
        service.generator = DeterministicNotes()
        worker = ModelTaskWorker(notes=service)
    else:
        service = state.lesson_service
        created = service.create(sid, node, payload)
        if kind == 'streaming_lesson':
            class Streaming(Generator):
                def stream(self, messages):
                    yield json.dumps(self.generate_json(messages))
            service.generator = Streaming()
        else:
            service.generator = Generator()
        worker = ModelTaskWorker(lessons=service)
    inversions, observed = [], []
    def begin(connection):
        connection.info['publication_locks'] = set()
    def capture(connection, statement, multiparams, params, options):
        if not statement.is_select:
            return
        compiled = statement.compile(dialect=mysql.dialect())
        sql = str(compiled)
        if 'FOR UPDATE' not in sql:
            return
        locked = connection.info.setdefault('publication_locks', set())
        for table in ('materials', 'material_versions', 'outbox_events'):
            if f'FROM {table} ' in sql or f'FROM {table}\n' in sql:
                identity = (table, repr(compiled.params))
                if table != 'outbox_events' and identity not in locked and 'outbox_events' in locked:
                    inversions.append(identity)
                locked.add(identity)
                if table == 'outbox_events':
                    locked.add(table)
                observed.append(table)
    event.listen(engine, 'begin', begin)
    event.listen(engine, 'before_execute', capture)
    try:
        assert worker.run_once()
    finally:
        event.remove(engine, 'begin', begin)
        event.remove(engine, 'before_execute', capture)
    assert 'outbox_events' in observed and 'material_versions' in observed
    assert inversions == [], f'source parents first acquired after event: {inversions}'
    if kind.endswith('_message'):
        assert service.repository.records('messages', run_id=sent['run_id'])[0]['status'] == 'completed'
    elif kind != 'note':
        assert service.repository.get_record('study_lessons', created['lesson_id'])['status'] == 'completed'
    else:
        assert service.repository.records('note_generations', space_id=sid)[0]['status'] == 'completed'
    engine.dispose()


def test_bounded_lesson_locks_old_bindings_before_publication_fence(tmp_path):
    from knowpath_backend.learning.errors import DomainConflict
    engine = create_engine(f"sqlite:///{tmp_path / 'old-bindings.db'}")
    init_db(engine)
    state, sid, node, payload = setup_lesson(SqlAlchemyMaterialRepository(engine))
    lessons = state.lesson_service
    created = lessons.create(sid, node, payload)
    extra = state.material_service.create(filename='extra.md', content=b'# Old source\n\nOld dependency.',
        idempotency_key='old-extra')
    row = lessons.repository.get_record('study_lessons', created['lesson_id'], lock=False)
    original = dict(row['snapshot']['bindings'][0])
    old = {'material_id': extra.material.id, 'material_version_id': extra.version.id, 'graph_version': 1}
    # The immutable binding survives source-budget trimming. The live space
    # no longer includes that old document when the provider result returns.
    row['snapshot']['bindings'].append(old)
    lessons.repository.put_record('study_lessons', row)
    assert old['material_id'] not in {s['material_id'] for s in row['snapshot']['sources']}
    acquired = set()
    material_get, version_get = lessons.spaces.materials.get_material, lessons.spaces.materials.get_version
    def material(identifier):
        acquired.add(('material', identifier))
        return material_get(identifier)
    def version(identifier):
        acquired.add(('version', identifier))
        return version_get(identifier)
    lessons.spaces.materials.get_material = material
    lessons.spaces.materials.get_version = version
    class Fence:
        def check(self):
            assert acquired >= {('material', b['material_id']) for b in (original, old)} | {
                ('version', b['material_version_id']) for b in (original, old)}
    def stale(*args, **kwargs):
        raise DomainConflict('STALE_LEARNING_CONTEXT', 'old binding')
    lessons._context = stale
    with pytest.raises(DomainConflict):
        lessons._publish_sections(row, [], Fence())
    engine.dispose()


def test_source_dependencies_lock_every_material_before_any_version():
    from types import SimpleNamespace
    from knowpath_backend.learning.materials.source_access import lock_source_dependencies
    locks = []
    materials = SimpleNamespace(unit_of_work=SimpleNamespace(active=True),
        get_material=lambda identifier: locks.append(('material', identifier)),
        get_version=lambda identifier: locks.append(('version', identifier)))
    refs = [{'material_id': 'b', 'material_version_id': '2'},
            {'material_id': 'a', 'material_version_id': '1'},
            {'material_id': 'b', 'material_version_id': '2'}]
    lock_source_dependencies(materials, refs)
    assert locks == [('material', 'a'), ('material', 'b'), ('version', '1'), ('version', '2')]


@pytest.mark.parametrize('kind', ['graph', 'parse'])
@pytest.mark.parametrize('status', ['completed', 'cancelled', 'failed'])
def test_terminal_ingest_events_do_not_relock_source_graph(kind, status):
    from types import SimpleNamespace
    from knowpath_backend.learning.workers.graph import GraphWorker
    from knowpath_backend.learning.workers.material_ingest import MaterialParseWorker
    repository = SimpleNamespace(get_record=lambda *args, **kwargs:
        {'event_type': 'graph.prepare' if kind == 'graph' else 'material.parse', 'status': status})
    graph = SimpleNamespace(repository=repository)
    worker = GraphWorker(graph, None) if kind == 'graph' else MaterialParseWorker(graph)
    def unexpected(*args):
        pytest.fail('terminal event must not lock material versions, revisions or outbox history')
    worker._locked = unexpected
    assert worker.claim('terminal-event') is None
