"""Plain-language logs must remain truthful, safe and useful for diagnosis."""
import io
import json
import logging

import pytest

from knowpath_backend.observability.configuration import (
    JsonFormatter, TextFormatter, LogSettings, configure_logging, shutdown_logging,
)
from knowpath_backend.observability import log_event


def record(event, **fields):
    item = logging.LogRecord('knowpath_backend.test', logging.INFO, __file__, 1, event, (), None)
    item.event, item.fields = event, fields
    return item


def render(event, **fields):
    return TextFormatter(service='api').format(record(event, **fields))


def test_default_text_explains_activity_without_dumping_system_parameters():
    line = render('model.call.completed', model='qwen-plus', duration_ms=1250,
                  request_id='request-one', model_call_id='call-one', span_id='internal-span',
                  parent_span_id='internal-parent', usage_status='unknown', usage={})
    assert '模型已返回响应' in line and '尚未校验答案' in line
    assert '模型：qwen-plus' in line and '耗时：1.25 秒' in line
    assert '用量：未提供' in line
    assert 'request-one' not in line and 'call-one' not in line
    assert '模型调用编号：call-one' not in line
    assert all(value not in line for value in ('{', 'thread', 'logger', 'span_id', 'internal-span'))


@pytest.mark.parametrize('status,expected', [
    ('completed', '后台任务已完成'), ('pending', '等待重试'),
    ('processing', '仍在处理中'), ('superseded', '已由新的执行尝试接管'),
    ('unavailable', '暂时无法确认'), ('failed', '后台任务失败'),
    ('cancelled', '后台任务已取消'),
    ('new-state', '尚未识别'),
])
def test_worker_completion_is_based_on_observed_status(status, expected):
    line = render('worker.attempt.finished', observed_status=status, attempt=2)
    assert expected in line and '第 2 次尝试' in line
    if status != 'completed':
        assert '后台任务已完成' not in line


@pytest.mark.parametrize('event,fields,expected', [
    ('http.request.completed', {'status_code':401}, '身份验证未通过'),
    ('http.request.completed', {'status_code':422}, '提交的数据未通过检查'),
    ('http.request.completed', {'status_code':404}, '未找到'),
    ('http.request.failed', {'status_code':200, 'streaming':True}, '请求处理失败'),
    ('http.request.disconnected', {}, '连接已断开'),
    ('model.http.retry', {'attempt':1, 'status_code':429}, '准备重试'),
    ('model.call.unknown', {}, '模型调用结果尚无法确认'),
    ('model.call.validation', {'validation':'content_passed'}, '响应格式检查通过'),
    ('tool.call.completed', {'result_status':'error_reported'}, '工具返回了错误'),
    ('tool.call.completed', {'result_status':'returned'}, '工具已返回结果'),
    ('tool.call.timed_out', {'execution_may_continue':True}, '后台执行可能仍在继续'),
    ('tool.call.approval', {'decision':'denied'}, '未获批准'),
    ('worker.failure.requested', {'terminal':False}, '申请'),
    ('worker.retry.requested', {'backoff_seconds':8}, '8 秒'),
    ('rag.result', {'status':'insufficient', 'citation_count':0}, '资料依据不足'),
    ('rag.answer.completed', {}, '问答流程已结束'),
    ('rag.stage', {'stage':'candidate', 'candidate_count':12}, '12'),
    ('logging.file.unavailable', {}, '仍继续输出'),
])
def test_common_events_have_truthful_plain_language(event, fields, expected):
    line = render(event, **fields)
    assert expected in line


def test_json_keeps_existing_diagnostic_fields_and_adds_explanation():
    fields = dict(request_id='req', span_id='span', parent_span_id='parent',
                  usage_status='observed', usage={'input_tokens':23}, duration_ms=12)
    item = record('model.call.completed', **fields)
    result = json.loads(JsonFormatter(service='api').format(item))
    assert '模型已返回响应' in result['message']
    assert all(result[key] == value for key, value in fields.items())
    assert result['event'] == 'model.call.completed' and result['logger'] == item.name
    assert '23' in result['message'] and '总量未知' in result['message']
    assert item.fields == fields


def test_missing_and_zero_usage_are_distinct():
    assert '用量：未提供' in render('model.call.completed', usage_status='unknown', usage={})
    assert '合计 0' in render('model.call.completed', usage_status='observed', usage={'total_tokens':0})
    partial = render('model.call.completed', usage_status='observed', usage={'input_tokens':15})
    assert '输入 15' in partial and '总量未知' in partial and '合计 15' not in partial


def test_summaries_use_sanitized_metadata_and_do_not_print_payloads(monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'PRIVATE_PROVIDER_KEY')
    item = record('model.call.failed', model='PRIVATE_PROVIDER_KEY', prompt='PRIVATE_PROMPT',
                  messages=['PRIVATE_MESSAGE'], error_code='MODEL_UNAVAILABLE',
                  exception={'type':'RuntimeError', 'frames':[]})
    for formatter in (TextFormatter(service='api'), JsonFormatter(service='api')):
        value = formatter.format(item)
        assert all(secret not in value for secret in ('PRIVATE_PROVIDER_KEY', 'PRIVATE_PROMPT', 'PRIVATE_MESSAGE'))
        assert '模型服务暂不可用' in value


def test_unknown_event_is_neutral_and_preserves_event_identifier():
    line = render('future.workflow.completed', mystery={'value':'internal-data'})
    assert '未翻译的事件' in line and 'future.workflow.completed' in line
    assert '成功' not in line and 'internal-data' not in line


def test_legacy_message_stays_available_and_dependency_content_is_omitted():
    item = logging.LogRecord('legacy', logging.WARNING, __file__, 1, '已恢复 password=HIDDEN', (), None)
    assert '已恢复' in TextFormatter(service='api').format(item)
    assert 'HIDDEN' not in TextFormatter(service='api').format(item)
    item.name = 'uvicorn.error'
    assert '第三方组件' in TextFormatter(service='api').format(item)
    assert '已恢复' not in TextFormatter(service='api').format(item)


def test_text_is_one_line_even_when_safe_metadata_contains_newlines():
    line = render('model.call.completed', model='model\nFORGED\r\nLINE\t\x1b[31m', request_id='one\ntwo')
    assert len(line.splitlines()) == 1 and '\x1b' not in line and '\t' not in line


def test_default_console_and_json_file_share_readable_message(tmp_path):
    stream = io.StringIO()
    try:
        configure_logging('api', settings=LogSettings(directory=tmp_path), stream=stream)
        log_event(logging.getLogger('knowpath_backend.test'), 'worker.attempt.finished',
                  observed_status='pending', request_id='req', run_id='run', attempt=1)
        line, = stream.getvalue().splitlines()
        assert '等待重试' in line and 'req' not in line
        path, = tmp_path.glob('*.jsonl')
        document = json.loads(path.read_text(encoding='utf-8'))
        assert document['message'] in line and document['observed_status'] == 'pending'
        assert document['run_id'] == 'run'
    finally:
        shutdown_logging()


@pytest.mark.parametrize('event,fields,expected', [
    ('worker.attempt.started', {'job_type':'material.parse'}, '任务内容：解析上传资料'),
    ('worker.attempt.started', {'job_type':'graph.prepare'}, '任务内容：整理知识图谱'),
    ('worker.attempt.started', {'job_type':'message.generate'}, '任务内容：生成问答回复'),
    ('worker.attempt.started', {'job_type':'assessment.generate'}, '任务内容：生成测评题目'),
    ('agent.turn.completed', {}, '本轮智能助手处理：已完成'),
    ('tool.call.executing', {'tool_name':'Read'}, '读取文件（Read）'),
    ('model.call.completed', {'finish_reason':'length'}, '达到长度上限'),
    ('model.call.completed', {'finish_reason':'content_filter'}, '内容限制'),
    ('model.call.completed', {'finish_reason':'tool_calls'}, '请求使用工具'),
    ('model.call.started', {'retry_reason':'empty_length_limit'}, '未得到正文'),
    ('model.call.failed', {'error_code':'RATE_LIMITED'}, '请求过于频繁或服务额度受限'),
    ('model.call.started', {'revision_index':0}, '生成初稿'),
    ('model.call.started', {'revision_index':1}, '第 1 次修订'),
])
def test_explain_task_purpose_and_model_stopping_conditions(event, fields, expected):
    assert expected in render(event, **fields)


@pytest.mark.parametrize('fields', [
    {}, {'observed_status':None}, {'observed_status':[]},
    {'duration_ms':float('nan'), 'attempt':True, 'backoff_seconds':-1},
])
def test_missing_or_unexpected_metadata_never_implies_success(fields):
    line = render('worker.attempt.finished', **fields)
    assert '已完成' not in line and '尚未识别' in line
    assert 'nan' not in line and 'True' not in line


@pytest.mark.parametrize('event,fields,expected', [
    ('command.completed', {'operation':'message.send'}, '提交提问：已完成'),
    ('material.parse.completed', {'chunk_count':0}, '解析片段：0'),
    ('api.configuration', {'persistence':'memory', 'chat_model':'example-model'}, '内存（重启后不保留）'),
    ('model.call.validation', {'validation':'failed'}, '未通过本阶段校验'),
    ('tool.call.approval', {'decision':'callback_unavailable'}, '审批回调不可用'),
    ('rag.result', {'status':'partial'}, '仍有问题缺少资料依据'),
    ('rag.result', {'status':'clarify'}, '需要进一步明确问题'),
    ('http.request.failed', {'status_code':None}, '请求处理失败'),
])
def test_existing_operation_and_result_metadata_is_readable(event, fields, expected):
    assert expected in render(event, **fields)
