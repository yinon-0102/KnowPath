"""Plain-language views of sanitized metadata, never of prompts or results.

Descriptions explain observation boundaries: returned is not validated, requested
is not committed, and an absent measurement is not zero. Keep original event
names/fields in JSON; unknown events get a neutral, forward-compatible summary.
"""
import math
import unicodedata


SERVICES = {'api': '接口服务', 'model-worker': '模型任务服务', 'graph-worker': '知识图谱服务'}
LEVELS = {'DEBUG': '调试', 'INFO': '信息', 'WARNING': '注意', 'ERROR': '错误', 'CRITICAL': '严重错误'}

_ACTIVITIES = {
    'agent.turn': '本轮智能助手处理',
    'api.build': '初始化接口服务', 'api.shutdown': '关闭接口服务',
    'message.retrieve': '查找与问题相关的资料', 'assessment.generate': '生成测评题目',
    'model.http': '请求模型服务', 'model.generate': '生成模型回复',
    'model.generate_json': '生成结构化回复', 'model.stream': '接收模型的逐步回复',
    'model.operation': '处理模型输入与输出', 'model.embedding': '把资料转换为可检索的表示',
    'model.reranking': '用模型筛选更相关的资料', 'material.parse': '读取并解析资料',
    'graph.prepare': '整理资料中的知识关系', 'rag.retrieve': '查找相关资料',
    'rag.rerank': '按相关程度筛选资料', 'rag.verify': '生成回答并核对资料依据',
    'rag.answer': '基于资料回答问题',
}
_OPERATIONS = {
    'space.create': '创建学习空间', 'space.update': '更新学习空间', 'space.delete': '删除学习空间',
    'space.scope': '调整学习范围', 'space.profile': '更新学习偏好',
    'message.send': '提交提问', 'message.replay': '读取已有回复', 'message.publish': '发布回复',
    'material.ingest': '提交资料处理任务', 'material.delete': '删除资料',
    'graph.reconcile': '更新知识关系', 'graph.publish': '发布知识图谱',
    'correction.create': '提交纠错', 'correction.confirm': '确认纠错',
    'knowledge_updates.apply': '应用知识更新', 'source.consult': '查看引用资料',
    'plan.create': '创建学习计划', 'plan.compare': '比较学习计划',
    'session.start': '开始学习', 'session.event': '记录学习进展',
    'policy.replay': '回放学习策略', 'assessment.create': '提交测评生成任务',
    'assessment.attempt': '提交测评答案', 'assessment.finalize': '完成测评',
    'assessment.question_report': '反馈题目问题', 'assessment.question_resolution': '处理题目反馈',
    'assessment.grade_review': '复核评分', 'state.reset': '重置学习状态', 'export.create': '导出学习数据',
}
_STAGES = {
    'candidate': '查找候选资料', 'rerank': '筛选相关资料', 'context': '准备回答所需资料',
    'generate': '生成回答', 'generation': '生成回答', 'verify': '核对回答依据',
    'verification': '核对回答依据', 'embedding': '建立检索表示', 'chat': '生成对话回复',
}
_JOBS = {
    'material.parse': '解析上传资料', 'graph.prepare': '整理知识图谱',
    'message.generate': '生成问答回复', 'assessment.generate': '生成测评题目',
}
_TOOLS = {
    'Read': '读取文件', 'Write': '写入文件', 'Edit': '修改文件', 'LS': '查看目录',
    'Glob': '查找文件', 'Grep': '搜索文件内容', 'Bash': '执行命令',
    'calculator': '计算', 'recall': '查找已保存的记忆', 'remember': '保存记忆', 'search': '搜索信息',
}
_FINISH_REASONS = {
    'length': '达到长度上限，输出可能不完整', 'max_tokens': '达到长度上限，输出可能不完整',
    'content_filter': '触发模型服务的内容限制',
    'tool_calls': '模型请求使用工具', 'function_call': '模型请求使用工具', 'tool_use': '模型请求使用工具',
}
_EVENTS = {
    'api.started': '接口服务已启动', 'api.stopped': '接口服务已停止',
    'api.configuration': '已读取服务配置',
    'worker.started': '后台任务服务已启动，开始等待任务',
    'worker.stopped': '后台任务服务已停止', 'worker.idle': '暂时没有待处理的后台任务',
    'worker.poll.failed': '读取待处理任务失败', 'worker.poll.recovered': '已恢复读取待处理任务',
    'worker.start.failed': '后台任务服务启动失败', 'worker.runtime.failed': '后台任务服务运行中发生异常',
    'worker.shutdown.failed': '关闭后台任务服务时发生异常',
    'worker.attempt.started': '开始处理后台任务', 'worker.attempt.failed': '本次后台任务执行发生异常',
    'worker.processing.failed': '处理后台任务时发生异常，最终状态以任务记录为准',
    'worker.status.unavailable': '暂时无法读取后台任务状态，不能据此判断成功或失败',
    'worker.retry.requested': '已申请稍后重试，是否生效以任务记录为准',
    'worker.retry.exhausted': '已达到重试次数上限',
    'worker.lease.renewal_failed': '无法续期任务执行权，需要核对任务是否已由其他进程接管',
    'material.cleanup.failed': '资料文件清理未完成，需要检查待清理记录',
    'message.generation.failed': '未能生成本次回复',
    'command.replayed': '检测到重复提交，使用已有处理结果',
    'command.database.retry': '数据库操作发生并发冲突，准备重试',
    'model.http.retry': '本次模型请求未完成，准备重试',
    'model.call.started': '正在调用模型',
    'model.call.completed': '模型已返回响应（尚未校验答案）',
    'model.call.failed': '模型调用失败', 'model.call.cancelled': '模型调用已取消',
    'model.call.unknown': '模型调用结果尚无法确认',
    'tool.call.started': '已创建工具调用，等待执行', 'tool.call.executing': '正在执行工具',
    'tool.call.failed': '工具执行发生异常', 'tool.call.rejected': '工具调用被拒绝，未执行',
    'tool.call.cancelled': '工具调用已取消', 'tool.call.unknown': '工具执行结果尚无法确认',
    'tool.call.timed_out': '等待工具结果超时',
    'logging.file.unavailable': '无法写入日志文件，控制台仍继续输出；请检查日志目录权限和磁盘空间',
}
_ERRORS = {
    'RATE_LIMITED': '请求过于频繁或服务额度受限，可稍后重试并检查服务额度',
    'MODEL_UNAVAILABLE': '模型服务暂不可用，可检查服务连接与配置',
    'MODEL_TIMEOUT': '等待模型响应超时', 'MODEL_INVALID_RESPONSE': '模型返回的数据不符合要求',
    'LOCAL_TOKEN_REQUIRED': '身份验证未通过，请检查本地访问凭据',
    'INTERNAL_ERROR': '服务内部发生异常，可用请求编号查看详细日志',
    'MATERIAL_REQUIRED': '尚未选择资料', 'MATERIAL_NOT_READY': '资料还没有准备好',
    'MATERIAL_ARCHIVED': '资料已归档', 'MATERIAL_PARSE_FAILED': '资料解析失败',
    'STALE_LEARNING_CONTEXT': '学习资料范围已变化，需要刷新后重试',
    'RAG_SOURCE_INVALID': '回答引用的资料未通过检查',
}
_HTTP_ERRORS = {
    400: '请求内容不符合要求', 401: '身份验证未通过', 403: '没有执行该操作的权限',
    404: '未找到请求的资源', 409: '当前状态不允许该操作', 422: '提交的数据未通过检查',
    429: '请求过于频繁或服务额度受限', 500: '服务内部发生异常',
    502: '上游服务返回异常', 503: '服务暂不可用', 504: '等待上游服务响应超时',
}
_COUNTS = {
    'candidate_count': '找到候选资料', 'reranked_count': '筛选后资料',
    'context_count': '用于回答的资料', 'citation_count': '引用资料',
    'chunk_count': '解析片段', 'question_count': '生成题目',
    'consecutive_failures': '连续失败次数',
}
_IDS = {
    'request_id': '请求编号', 'run_id': '任务编号', 'job_id': '后台作业编号',
    'model_call_id': '模型调用编号', 'tool_call_id': '工具调用编号',
}


def one_line(value):
    """Prevent newlines/control characters from creating misleading console rows."""
    return ''.join(ch if ch.isprintable() and unicodedata.category(ch) != 'Cf' else ' ' for ch in str(value))


def _label(labels, value, default):
    return labels.get(value, default) if isinstance(value, (str, int)) else default


def _number(value):
    return type(value) in (int, float) and value >= 0 and math.isfinite(value)


def _count(value):
    return type(value) is int and value >= 0


def _headline(item):
    event = item.get('event', '')
    if not isinstance(event, str):
        return '系统事件'
    if event == 'legacy.log':
        message = item.get('message', '')
        return '第三方组件发来日志（原文已省略）' if message == '[dependency message omitted]' else str(message)
    if event.startswith('http.request.'):
        if event.endswith('.failed'):
            return '请求处理失败' + ('（已发送响应头，后续处理仍发生异常）' if item.get('status_code') == 200 else '')
        if event.endswith('.disconnected'):
            return '请求连接已断开，响应可能未完整送达'
        code = item.get('status_code')
        if _number(code) and code >= 400:
            return '请求未成功处理：' + _HTTP_ERRORS.get(code, '请求被拒绝或处理失败')
        return '请求处理已结束'
    if event == 'worker.attempt.finished':
        return _label({
            'completed': '后台任务已完成', 'pending': '本次未完成，等待重试',
            'processing': '后台任务仍在处理中', 'superseded': '任务已由新的执行尝试接管',
            'unavailable': '本次执行已返回，但暂时无法确认最终状态', 'failed': '后台任务失败',
            'cancelled': '后台任务已取消',
        }, item.get('observed_status'), '本次执行已返回，任务状态尚未识别')
    if event == 'worker.failure.requested':
        return ('已申请将任务标记为失败' if item.get('terminal') is True else '已申请稍后重试'
                if item.get('terminal') is False else '已申请更新任务失败状态') + '，是否生效以任务记录为准'
    if event == 'model.call.validation':
        return _label({
            'content_passed': '模型响应格式检查通过（尚未完成后续校验）',
            'passed': '模型输出通过本阶段校验', 'failed': '模型输出未通过本阶段校验',
        }, item.get('validation'), '模型输出校验状态尚无法确认')
    if event == 'tool.call.completed':
        return '工具返回了错误' if item.get('result_status') == 'error_reported' else '工具已返回结果（不代表业务一定成功）'
    if event == 'tool.call.approval':
        return _label({
            'approved': '工具执行已获批准', 'denied': '工具执行未获批准',
            'not_required': '该工具操作无需审批',
            'callback_unavailable': '该工具需要审批，但审批回调不可用（是否执行以执行事件为准）',
        }, item.get('decision'), '工具审批结果尚无法确认')
    if event == 'rag.stage':
        return _label(_STAGES, item.get('stage'), '资料检索阶段进展')
    if event == 'rag.result':
        return _label({
            'answered': '已依据资料生成回答', 'partial': '已生成部分回答，仍有问题缺少资料依据',
            'insufficient': '资料依据不足，无法给出完整回答', 'clarify': '需要进一步明确问题',
        }, item.get('status'), '问答结果状态尚无法确认')
    if event == 'rag.answer.completed':
        return '问答流程已结束，回答是否完整请看问答结果'
    if event in _EVENTS:
        return _EVENTS[event]
    base, _, state = event.rpartition('.')
    activity = _label(_OPERATIONS, item.get('operation'), '处理业务操作') if base == 'command' else _ACTIVITIES.get(base)
    if activity and state in {'started', 'completed', 'failed', 'cancelled'}:
        return activity + '：' + {'started': '开始', 'completed': '已完成', 'failed': '失败', 'cancelled': '已取消'}[state]
    return '系统事件'


def _usage(item):
    usage = item.get('usage')
    if not isinstance(usage, dict) or not any(_count(value) for value in usage.values()):
        return '用量：未提供（不代表零消耗）'
    parts = []
    for keys, label in ((('input_tokens', 'prompt_tokens'), '输入'), (('output_tokens', 'completion_tokens'), '输出'), (('total_tokens',), '合计')):
        count = next((usage[key] for key in keys if _count(usage.get(key))), None)
        if count is not None:
            parts.append(f'{label} {count}')
    if not _count(usage.get('total_tokens')):
        parts.append('总量未知')
    return '用量：' + '、'.join(parts) + '（模型计量单位 token）'


def describe(item):
    """Render sanitized document fields without modifying the document or record."""
    parts = [_headline(item)]
    event = item.get('event', '')
    if isinstance(event, str) and event.startswith('http.request.'):
        route = item.get('route')
        if isinstance(route, str) and route:
            parts.append('接口：' + ('未匹配到具体接口' if route == '<unmatched>' else route))
    for key, label in (('model', '模型'), ('tool_name', '工具')):
        value = item.get(key)
        if isinstance(value, str) and value:
            if key == 'tool_name' and value in _TOOLS:
                value = f'{_TOOLS[value]}（{value}）'
            parts.append(f'{label}：{value}')
    if 'job_type' in item:
        parts.append('任务内容：' + _label(_JOBS, item['job_type'], '未识别的后台任务'))
    if event != 'rag.stage' and isinstance(item.get('stage'), str) and item['stage'] in _STAGES:
        parts.append('阶段：' + _STAGES[item['stage']])
    if _count(item.get('revision_index')):
        parts.append('轮次：生成初稿' if item['revision_index'] == 0 else f"轮次：第 {item['revision_index']} 次修订")
    finish = _label(_FINISH_REASONS, item.get('finish_reason'), None)
    if finish:
        parts.append('停止原因：' + finish)
    if item.get('retry_reason') == 'empty_length_limit':
        parts.append('重试原因：上次达到长度上限且未得到正文')
    if event == 'api.configuration':
        parts.append('数据存储：' + _label({'sql': '数据库', 'memory': '内存（重启后不保留）'}, item.get('persistence'), '未知'))
        for key, label in (('chat_model', '对话模型'), ('embedding_model', '检索模型')):
            if isinstance(item.get(key), str) and item[key]:
                parts.append(f'{label}：{item[key]}')
    if _count(item.get('attempt')):
        attempt = f"第 {item['attempt']} 次尝试"
        if _count(item.get('max_attempts')):
            attempt += f"（最多 {item['max_attempts']} 次）"
        parts.append(attempt)
    if _number(item.get('backoff_seconds')):
        parts.append(f"计划等待：{item['backoff_seconds']:g} 秒")
    for key, label in _COUNTS.items():
        if _count(item.get(key)):
            parts.append(f'{label}：{item[key]}')
    duration = item.get('duration_ms')
    if _number(duration):
        parts.append('耗时：' + (f'{duration:g} 毫秒' if duration < 1000 else f'{duration / 1000:.2f} 秒'))
    code = item.get('status_code')
    if _number(code):
        parts.append(f'响应状态：{code}' + ('（' + _HTTP_ERRORS.get(code, '请求未成功') + '）' if code >= 400 else ''))
    error = item.get('error_code')
    if isinstance(error, (str, int)) and not isinstance(error, bool) and error != '':
        parts.append('原因：' + _label(_ERRORS, error, '未分类错误，请结合详细日志排查') + f'（{error}）')
    if isinstance(event, str) and event.startswith('model.') and ('usage_status' in item or 'usage' in item) and not event.endswith('.started'):
        parts.append(_usage(item))
    if item.get('execution_may_continue') is True:
        parts.append('后台执行可能仍在继续，请先核对结果，避免重复操作')
    exception = item.get('exception')
    if isinstance(exception, dict) and isinstance(exception.get('type'), str):
        parts.append('异常类型：' + exception['type'])
    return one_line('；'.join(parts))


def correlation(item):
    """Keep complete searchable IDs, omitting redundant span/thread metadata."""
    parts = []
    for key, label in _IDS.items():
        value = item.get(key)
        if key == 'job_id' and item.get('run_id'):
            continue
        if isinstance(value, str) and value:
            parts.append(f'{label}：{value}')
    if not parts:
        for key, label in (('material_id', '资料编号'), ('space_id', '学习空间编号'), ('resource_id', '资源编号')):
            if isinstance(item.get(key), str) and item[key]:
                parts.append(f'{label}：{item[key]}')
                break
    parts.append('事件：' + str(item.get('event', 'legacy.log')))
    return one_line('；'.join(parts))
