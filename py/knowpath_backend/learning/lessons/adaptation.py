"""Small, source-independent teaching hints from current independent evidence.

The returned context is an explicit allowlist, never raw questions, answers,
rubrics, cached grading objects, or unrelated node performance.
"""
from knowpath_backend.learning.assessments.review_policy import effective_evidence, independent_evidence
from knowpath_backend.learning.assessments.service import revision

ADAPTATION_VERSION = "effective-lesson-v1"


def build_adaptation_context(*, topics, topic_ids, prerequisite_ids=(), states=None,
                             evidence=(), epochs=None, as_of, enabled=True):
    if not enabled:
        return {"version": ADAPTATION_VERSION, "enabled": False, "topics": [],
                "prerequisites": [], "reason": "adaptation_disabled"}
    states, epochs = states or {}, epochs or {}
    by_id = {topic['id']: topic for topic in topics}

    def summarize(topic_id, prerequisite=False):
        topic = by_id[topic_id]
        revision_id, epoch = revision(topic), epochs.get(topic_id, 0)
        effective = effective_evidence([row for row in evidence if row.get('topic_id') == topic_id],
            revision_id=revision_id, epoch=epoch, as_of=as_of)
        selected = independent_evidence(effective, window=20)
        failures = [row for row in selected if row['score'] < 1]
        state = states.get(topic_id, {})
        state_valid = (state.get('topic_revision_id') == revision_id
                       and state.get('epoch', 0) == epoch and state.get('score_validity') == 'current')
        tags = sorted({tag[:120] for row in failures for tag in row.get('error_tags', [])
                       if isinstance(tag, str)})[:10]
        strategy = ('scaffold_prerequisite' if prerequisite else 'explain_errors') if failures else (
            'concise_review' if selected and state_valid and state.get('status') == 'mastered' else 'neutral')
        return {'topic_id': topic_id, 'name': topic.get('name', ''),
                'topic_revision_id': revision_id, 'epoch': epoch,
                'strategy': strategy, 'evidence_count': len(selected),
                'independent_error_count': len(failures), 'error_tags': tags,
                'evidence_ids': [row['id'] for row in selected],
                'reason': 'effective_independent_errors' if failures else (
                    'current_mastery' if strategy == 'concise_review' else 'no_effective_difficulty')}

    primary = [summarize(identifier) for identifier in dict.fromkeys(topic_ids) if identifier in by_id]
    prerequisite = [summarize(identifier, True) for identifier in dict.fromkeys(prerequisite_ids)
                    if identifier in by_id and identifier not in topic_ids]
    return {'version': ADAPTATION_VERSION, 'enabled': True, 'topics': primary,
            'prerequisites': prerequisite,
            'reason': 'effective_topic_and_required_prerequisite_evidence'}
