"""The message boundary publishes bounded RAG failure diagnostics safely."""

import json

import pytest

from knowpath_backend.learning.rag.verification import VerificationError
from knowpath_backend.learning.workers.model_tasks import ModelTaskWorker
from knowpath_backend.test.test_rag_messages import build_workspace


SAFE_DETAILS = {
    "stage": "verification", "failure_kind": "finish_reason", "finish_reason": "length",
    "call_index": 2, "prompt_tokens": 120, "completion_tokens": 32, "output_limit": 32,
}
SECRET = "provider-secret-do-not-publish"


@pytest.mark.parametrize('code', ['ANSWER_VERIFICATION_FAILED', 'MODEL_OUTPUT_CAPACITY_EXCEEDED',
                                  'RAG_STAGE_BUDGET_EXCEEDED'])
def test_failed_model_journal_persists_internally_without_answer_or_public_leak(build_workspace, code):
    from knowpath_backend.learning.rag.diagnostics import RequestJournal
    state, _, space, *_ = build_workspace
    class Pipeline:
        def answer(self, *args, **kwargs):
            journal = RequestJournal()
            call = journal.begin_call('generation')
            journal.finish_call(call, status='succeeded', usage={'prompt_tokens':17})
            error = VerificationError(code, details={'stage':'verification'})
            error.call_journal = journal.seal('failed')
            error.call_journal['provider_response'] = SECRET
            raise error
    state.message_service.rag_pipeline = Pipeline()
    response = state.message_service.send(space['id'], {'message':'学校应该告知谁？'}, durable=True)
    event = state.message_service.repository.records('outbox', event_type='message.generate')[0]
    worker = ModelTaskWorker(messages=state.message_service)
    assert worker.run_once(event['id'])
    run = state.get_run(response['run_id'])
    assert_failed_without_answer(run, code, {'stage':'verification'})
    stored = state.message_service.repository.get_record('messages', event['aggregate_id'])
    journal = stored['snapshot']['rag_failure_journal']
    assert journal['calls'][0]['usage']['prompt_tokens'] == 17
    assert SECRET not in json.dumps(journal)
    assert 'call_journal' not in json.dumps(run) and 'prompt_tokens' not in json.dumps(run)
    assert worker.run_once(event['id']) is False


def assert_failed_without_answer(run, code, details):
    assert run["status"] == "failed"
    assert run["error"]["code"] == code
    assert run["error"]["details"] == details
    failed = [event for event in run["events"] if event["event"] == "run.failed"]
    assert len(failed) == 1
    assert failed[0]["data"]["error"] == run["error"]
    assert not any(event["event"] in {"message.delta", "message.completed"} for event in run["events"])
    assert SECRET not in json.dumps(run, ensure_ascii=False)


def test_verification_diagnostics_survive_real_message_publication(build_workspace):
    state, _, space, *_ = build_workspace

    class Pipeline:
        def answer(self, *args, **kwargs):
            raise VerificationError("VERIFICATION_UNAVAILABLE", details={
                **SAFE_DETAILS, "provider_response": SECRET, "api_key": SECRET,
                "input_tokens": True, "total_tokens": -1,
            })

    state.message_service.rag_pipeline = Pipeline()
    response = state.send_message(space["id"], {"message": "学校应该告知谁？"})
    assert_failed_without_answer(state.get_run(response["run_id"]), "VERIFICATION_UNAVAILABLE", SAFE_DETAILS)


@pytest.mark.parametrize("code", ["RAG_COST_BUDGET_EXCEEDED", "RAG_SPEND_CONFIG_INVALID"])
def test_cost_failure_keeps_public_code_and_never_replays_durable_call(build_workspace, code):
    state, _, space, *_ = build_workspace
    calls = []

    class Pipeline:
        def answer(self, *args, **kwargs):
            calls.append("provider")
            raise VerificationError(code, details=SAFE_DETAILS)

    state.message_service.rag_pipeline = Pipeline()
    response = state.message_service.send(space["id"], {"message": "学校应该告知谁？"}, durable=True)
    event = state.message_service.repository.records("outbox", event_type="message.generate")[0]
    worker = ModelTaskWorker(messages=state.message_service)
    assert worker.run_once(event["id"])
    run = state.get_run(response["run_id"])
    assert_failed_without_answer(run, code, SAFE_DETAILS)
    assert run["error"]["message"] != "对话模型暂时不可用"
    assert run["error"]["retryable"] is False
    assert worker.run_once(event["id"]) is False
    assert calls == ["provider"]
    settled = state.message_service.repository.get_record("outbox", event["id"])
    assert settled["status"] == "failed" and settled["attempts"] == 1


@pytest.mark.parametrize("kind", ["unknown_verification", "unexpected_exception"])
def test_unknown_failures_clear_diagnostics_and_provider_text(build_workspace, kind):
    state, _, space, *_ = build_workspace

    class Pipeline:
        def answer(self, *args, **kwargs):
            if kind == "unknown_verification":
                raise VerificationError(SECRET, details=SAFE_DETAILS)
            error = RuntimeError(SECRET)
            error.details = {**SAFE_DETAILS, "provider_response": SECRET}
            raise error

    state.message_service.rag_pipeline = Pipeline()
    response = state.send_message(space["id"], {"message": "学校应该告知谁？"})
    assert_failed_without_answer(state.get_run(response["run_id"]), "MODEL_UNAVAILABLE", {})


@pytest.mark.parametrize("change", ["scope", "session"])
def test_context_failure_overrides_provider_code_and_clears_diagnostics(build_workspace, change):
    state, _, space, *_ = build_workspace
    payload = {"message": "学校应该告知谁？"}
    if change == "session":
        plan = state.create_plan(space["id"], {})
        session = state.start_session(plan["id"], plan["tasks"][0]["id"])
        payload["session_id"] = session["id"]

    class Pipeline:
        def answer(self, *args, **kwargs):
            if change == "scope":
                topic = state.space_service.bound_topics(space)[0]
                state.set_scope(space["id"], {"topic_ids": [topic["id"]], "expected_version": space["space_version"]})
            else:
                state.finish_session(session["id"])
            raise VerificationError("VERIFICATION_UNAVAILABLE", details=SAFE_DETAILS)

    state.message_service.rag_pipeline = Pipeline()
    response = state.send_message(space["id"], payload)
    expected = "STALE_LEARNING_CONTEXT" if change == "scope" else "SESSION_FINISHED"
    assert_failed_without_answer(state.get_run(response["run_id"]), expected, {})
