"""Chinese B4 reports expose missing observations without inventing results."""
from copy import deepcopy

from knowpath_backend.rag_eval.final_tree_analysis import analyze
from knowpath_backend.test.test_rag_b4_eval import MODES, question, record


def test_report_renders_120_schedule_unknown_observations_and_pending_quality():
    from knowpath_backend.rag_eval.final_tree_report import render_markdown
    questions = [question(f'q{i:02}', f'family{i // 2}') for i in range(40)]
    analysis = analyze([], questions, bootstrap_samples=20)
    before = deepcopy(analysis)
    text = render_markdown(analysis)
    assert analysis == before
    assert '40 × 3 × 1 = 120' in text and '360' not in text
    assert '| 指标 | A0 | F | B4 |' in text
    assert '| 生成前必要证据覆盖率 | unknown | unknown | unknown |' in text
    assert '| 最终引用必要证据覆盖率 | 0.00% | 0.00% | 0.00% |' in text
    assert '正确性：unknown' in text
    assert '自动评分不等于人工复核' in text
    assert '不得上线' in text
    assert 'family 等权' in text and '20260924' in text
    assert '未知项不按零填充' in text


def test_report_shows_question_pp_and_family_ci_separately_with_full_rank_tables():
    from knowpath_backend.rag_eval.final_tree_report import render_markdown
    questions = [question('q1', 'large'), question('q2', 'large'), question('q3', 'small')]
    rows = [record(m, q['question_id'], covered=m == 'b4' or q['question_id'] == 'q3')
            for q in questions for m in MODES]
    analysis = analyze(rows, questions, expected_questions=3, bootstrap_samples=20)
    text = render_markdown(analysis)
    assert '+66.67 pp' in text and '+50.00 pp' in text
    for k in (1, 3, 5, 10, 20, 40):
        assert f'Recall@{k}' in text
    assert '候选：原单叶口径' in text and '重排：坐标并集口径' in text
    assert 'NDCG@10' in text and 'MRR' in text
    assert '上下文改变' in text and '上下文相同' in text
    assert '相同输入下的引用差异不能直接归功于树' in text


def test_report_keeps_observed_cost_distinct_from_unknown_total_and_partial_index():
    from knowpath_backend.rag_eval.final_tree_report import render_markdown
    item = record('a0')
    item['cost'] = .125
    analysis = analyze([item], [question(), question('q2')], expected_questions=2,
        bootstrap_samples=20, index_build=dict(status='stopped', summary_calls_success=4,
                                              summary_calls_unknown=1, elapsed_seconds=12.5,
                                              cost=None, retry_approval='pending'))
    text = render_markdown(analysis)
    assert '| 在线金额总计（单位见冻结定价） | unknown | unknown | unknown |' in text
    assert '| 已观察金额小计（非总计） | 0.125000 | unknown | unknown |' in text
    assert 'summary_calls_success' in text and 'summary_calls_unknown' in text
    assert 'pending' in text and 'stopped' in text
    assert 'F 与 B4 共享预处理成本' in text
    assert '价格或 usage 不完整时金额为 unknown' in text


def test_report_escapes_untrusted_table_text_and_does_not_call_automated_review_human():
    from knowpath_backend.rag_eval.final_tree_report import render_markdown
    analysis = analyze([record(m) for m in MODES], [question()], expected_questions=1,
                       bootstrap_samples=20, quality_review=dict(status='completed', reviewer_type='automated',
                       blinded=True, reviewed_question_ids=['q1']))
    analysis['experiment_validity']['reason'] = 'bad|cell\n<script>alert(1)</script>'
    text = render_markdown(analysis)
    assert '<script>' not in text
    assert 'bad\\|cell' in text
    assert '审查类型：自动辅助评审' in text
    assert '正确性：unknown' in text


def test_native_index_usage_is_readable_rows_without_invented_total_or_retry():
    from knowpath_backend.rag_eval.final_tree_report import render_markdown
    build = dict(index_id='idx', plan=dict(summary_calls=5), amount=None, currency=None,
        usage=[dict(kind='summary', call_id='call1', model='model', cached=True, elapsed_seconds=1.5,
                    usage=dict(input_tokens=99, output_tokens=12)),
               dict(kind='embedding', call_id='call2', model='embed', cached=False,
                    elapsed_seconds=2, usage=dict(total_tokens=50))])
    analysis = analyze([], [question()], expected_questions=1, bootstrap_samples=20, index_build=build)
    text = render_markdown(analysis)
    assert '| summary | call1 | model | 是 | 99 | 12 | 1.50 |' in text
    assert '| embedding | call2 | embed | 否 | 50 | unknown | 2.00 |' in text
    assert '缓存命中仍保留原始调用资源' in text
    assert '| usage |' not in text


def test_index_prior_unknown_attempts_prevent_implied_complete_accounting():
    from knowpath_backend.rag_eval.final_tree_report import render_markdown
    analysis = analyze([], [question()], expected_questions=1, bootstrap_samples=20,
        index_build=dict(accounting_complete=False, prior_unresolved_summary_attempts=[
            dict(call_id='old-call', status='unknown')], amount=None))
    text = render_markdown(analysis)
    assert '索引账目完整性：不完整' in text
    assert '历史未解决摘要尝试' in text and 'old-call' in text
    assert '历史 unknown 调用可能已计费' in text
    assert '不能因后续重试成功而清除' in text
