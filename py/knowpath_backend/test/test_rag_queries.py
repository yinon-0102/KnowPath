from knowpath_backend.learning.rag.queries import prepare_query


def test_standalone_query_is_unchanged_with_trace():
    result = prepare_query('学校应当通知谁？', [])
    assert result['query'] == result['original_query']
    assert result['status'] == 'ready' and not result['history_used']


def test_followup_uses_user_context_not_previous_answer_as_evidence():
    result = prepare_query('那应该通知谁？', [
        {'role': 'user', 'content': '学校发现学生欺凌应当如何处理？'},
        {'role': 'assistant', 'content': '未经证实的额外事实'}])
    assert result['status'] == 'ready'
    assert '学校发现学生欺凌' in result['query']
    assert '未经证实' not in result['query']
    assert result['history_used'] == [0]


def test_unresolved_pronoun_returns_clarification():
    assert prepare_query('那应该通知谁？', [])['status'] == 'clarify'
    assert prepare_query('那应该通知谁？', [{'role':'user','content':'它是什么？'}])['status'] == 'clarify'
