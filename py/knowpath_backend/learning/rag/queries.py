"""Conservative shared follow-up context; never promote old answers to evidence."""
import re


FOLLOWUP = re.compile(r'^(?:那(?:么|个|些|这)?|这(?:个|些|种)|它(?:们)?|他们|她们|上述|前者|后者|继续|为什么会这样|what about|and |it\b|they\b|that\b)', re.I)


def prepare_query(question, history=()):
    result = dict(original_query=question, query=question, status='ready', history_used=[],
                  method='explicit-user-context-v1')
    if not FOLLOWUP.search(question.strip()):
        return result
    users = [(i, row['content']) for i, row in enumerate(history)
             if row.get('role') == 'user' and isinstance(row.get('content'), str)]
    if not users:
        return {**result, 'status': 'clarify'}
    index, previous = users[-1]
    # Chained pronouns or competing subjects are deliberately left unresolved.
    if (FOLLOWUP.search(previous.strip()) or len(previous.encode('utf-8')) > 1500
            or any(word in previous for word in ('分别', '前者', '后者', '对比', '比较', '还是', '或者'))):
        return {**result, 'status': 'clarify'}
    return {**result, 'query': f'上下文问题：{previous}\n本轮追问：{question}', 'history_used': [index]}
