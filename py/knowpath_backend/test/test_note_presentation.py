from knowpath_backend.learning.notes.presentation import readable_markdown, source_label


def test_legacy_machine_sections_become_readable_without_changing_personal_text():
    refs = [{'material_id': 'm', 'material_version_id': 'v', 'chunk_id': 'chunk', 'material_name': 'Python 入门', 'section_path': ['第一章'], 'line_start': 3, 'line_end': 5}]
    questions = [{'id': 'q1', 'prompt': '为什么转换无效输入会失败？'}]
    facts = {'question_results': [{'question_id': 'q1', 'verdict': 'incorrect', 'score': 0, 'assisted': True}]}
    text = '## 测评结果\n- q1：incorrect；分数 0\n\n## 理解纠偏\n- 纠偏记录：a:q1\n\n## 待巩固内容\n- q1\n\n## 来源\n- [chunk](/api/v1/materials/m/versions/v/chunks/chunk?space_id=s)\n- [chunk](/api/v1/materials/m/versions/v/chunks/chunk?space_id=s)\n\n## 我的补充\n保留 q1 和个人文字'
    result = readable_markdown(text, facts, questions, refs, 'a')
    assert '第 1 题：为什么转换无效输入会失败？' in result
    assert '错误；得分 0；有辅助记录' in result
    assert '需要巩固' in result
    assert result.count('Python 入门 · 第一章 · 第 3—5 行') == 1
    assert '保留 q1 和个人文字' in result
    assert 'incorrect' not in result and '纠偏记录：a:q1' not in result


def test_manual_sections_and_unknown_references_are_not_rewritten():
    text = '## 测评结果\n我对 q1 的个人分析\n\n## 来源\n自己的链接\n'
    assert readable_markdown(text, {}, [], [], 'a') == text
    assert source_label({'chunk_id': 'opaque'}) == '原始资料 · 位置未记录'


def test_question_summary_and_source_labels_never_include_answer_keys():
    from knowpath_backend.learning.notes.presentation import question_summaries
    questions = [{'id': 'q', 'prompt': '**类型转换**', 'answer': 'secret', 'rubric': 'hidden'}]
    assert question_summaries(questions) == [{'id': 'q', 'number': 1, 'summary': '类型转换'}]
    assert source_label({'material_name': '法律', 'page': 2}) == '法律 · 第 2 页'
