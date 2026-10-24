"""Automatic-all and a custom selection of all topics have different scope."""
from knowpath_backend.test.test_reading_messages import _documents, reading


def test_reading_unmapped_chunk_requires_automatic_all_mode(monkeypatch):
    state, space, _, _ = _documents()
    ctx = reading(space)
    version = state.material_service.repository.get_version(ctx['material_version_id'])
    unmapped = version.chunks[-1]
    original = state.space_service.bound_topics

    def mapped_topics(current):
        topics = original(current)
        for topic in topics:
            topic['source_refs'] = [ref for ref in topic['source_refs'] if ref['chunk_id'] != unmapped.id]
        return topics

    monkeypatch.setattr(state.space_service, 'bound_topics', mapped_topics)
    automatic = state.message_service._reading_sources(space, ctx)
    assert unmapped.id in {source['chunk_id'] for source in automatic}
    # Even selecting every mapped topic explicitly cannot authorize other prose.
    state.set_scope(space['id'], {'topic_ids': space['topic_ids'], 'scope_mode': 'custom',
                                 'expected_version': space['space_version']})
    explicit = state.message_service._reading_sources(state.get_space(space['id']), ctx)
    assert unmapped.id not in {source['chunk_id'] for source in explicit}
