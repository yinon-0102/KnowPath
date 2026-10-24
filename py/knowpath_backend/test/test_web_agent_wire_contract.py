"""Freeze actual Web events/history for every entrance and requested answer mode."""
import json
from pathlib import Path

import pytest

from knowpath_backend.test.web_agent_support import AgentHarness, SyntheticAnswer

CONTRACT = json.loads((Path(__file__).parent / "fixtures/web_agent_wire_contract.json").read_text())


class ModeAnswer(SyntheticAnswer):
    def generate(self, snapshot):
        if snapshot['answer_mode'] == 'general':
            return {'text': 'A synthetic general explanation.', 'citation_ids': []}
        return super().generate(snapshot)


@pytest.mark.parametrize('entrance', CONTRACT['entrances'])
@pytest.mark.parametrize('answer_mode', CONTRACT['answer_modes'])
def test_all_entrances_preserve_web_wire_contract(tmp_path, entrance, answer_mode):
    with AgentHarness(tmp_path, answer_generator=ModeAnswer()) as harness:
        state, sid = harness.state, harness.space_id
        extra = {}
        if entrance == 'reading':
            binding = state.get_space(sid)['bindings'][0]
            extra['reading_context'] = {key: binding[key] for key in ('material_id', 'material_version_id')}
        if entrance == 'lesson':
            from knowpath_backend.test.test_study_lessons import setup_lesson, Generator
            from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
            state, sid, node, payload = setup_lesson(state.material_repository)
            state.message_service.generator = ModeAnswer()
            lesson = state.lesson_service.create(sid, node, payload)
            state.lesson_service.generator = Generator()
            assert ModelTaskWorker(lessons=state.lesson_service).run_once()
            extra['lesson_id'] = lesson['lesson_id']
        sent = state.send_message(sid, {'message': 'Explain functions', 'answer_mode': answer_mode, **extra})
        assert set(CONTRACT['send_required']) <= sent.keys()
        run = state.get_run(sent['run_id'])
        assert run['status'] == 'succeeded', run.get('error')
        events = run['events']
        names = [event['event'] for event in events]
        positions = [names.index(name) for name in CONTRACT['event_order']]
        assert positions == sorted(positions)
        completed = next(event['data'] for event in events if event['event'] == 'message.completed')
        assert set(CONTRACT['completed_required']) <= completed.keys()
        assert completed['answer_mode'] == ('general' if answer_mode == 'general' else 'material')
        assert bool(completed['citations']) == (answer_mode != 'general')
        for citation in completed['citations']:
            assert set(CONTRACT['citation_required']) <= citation.keys()
        history = state.message_service.history(sid, sent['session_id'])['items']
        assert len(history) == 1 and set(CONTRACT['history_required']) <= history[0].keys()
        assert history[0]['reading_context'] == extra.get('reading_context')
        assert history[0]['lesson_id'] == extra.get('lesson_id')
        assert history[0]['response']['text'] == completed['text']
