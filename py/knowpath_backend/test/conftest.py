"""Explicit semantic routing double for frozen command/source fixtures.

Production always uses the configured provider; these legacy command fixtures
exercise source binding and deliberately classify requests as educational material.
Adaptive tests override with configurable SemanticAnswer.classify.
"""
import pytest

@pytest.fixture(autouse=True)
def frozen_semantic_provider(monkeypatch, request):
    # Explicitly scoped frozen message/source fixture modules. Other suites and
    # production never receive this router; provider routing boundary tests undo
    # the fixture before exercising the configured adapter.
    modules = {
        'test_document_scope_api', 'test_document_scope_context',
        'test_document_scope_deletion', 'test_document_scope_persistence',
        'test_learning_api_workflow', 'test_learning_assistance_integrity',
        'test_learning_conversation_context',
        'test_learning_messages', 'test_learning_message_retrieval',
        'test_learning_model_tasks_mysql',
        'test_learning_profile_candidates', 'test_learning_space_deletion',
        'test_message_document_scope', 'test_rag_document_scope', 'test_rag_messages',
        'test_rag_message_failures', 'test_rag_services_integration',
        'test_reading_messages', 'test_study_lessons', 'test_study_lesson_streaming',
        'test_learning_source_access',
    }
    if request.module.__name__.rsplit('.',1)[-1] not in modules:
        return
    from knowpath_backend.learning.conversations import adaptive
    def classify(settings, client, snapshot):
        return {'document_related':True,'educational':True,'topic_ids':[],
                'scope_uncertain':True,'reason':'Explicit frozen material test classifier'}
    monkeypatch.setattr(adaptive,'classify_with_provider',classify)
