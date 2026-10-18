"""Ordered rolling allocations shared by previews, application and refresh.

Allocations are reservations, not evidence of learning or prerequisite mastery.
Splitting a reservation never splits the stable learning node.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from math import ceil
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..errors import DomainConflict

VERSION = 'budget-ordered-rolling-v1'


def calendar_zone(name='Asia/Shanghai'):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name == 'Asia/Shanghai':
            return timezone(timedelta(hours=8))
        raise DomainConflict('INVALID_TIMEZONE', '未知的日历时区') from None


def local_day(instant, config):
    value = datetime.fromisoformat(instant) if isinstance(instant, str) else instant
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(calendar_zone(config.get('calendar_timezone', 'Asia/Shanghai'))).date()


def _week(day):
    return day - timedelta(days=day.weekday())


def _remaining_work(item):
    context = item.get('context', {})
    forecast, accumulated = context.get('duration_forecast', {}), context.get('accumulated', {})
    study = max(0, ceil(forecast.get('study_minutes', item['estimated_minutes']) - accumulated.get('study_seconds', 0) / 60))
    answer = max(0, ceil(forecast.get('answer_minutes', 0) - accumulated.get('answer_seconds', 0) / 60))
    return (study or (5 if answer == 0 else 0)), answer


def allocate_ordered_tasks(tasks, config, space, start_date, *, spent_minutes=None, protected_ids=(), consumed_by_task=None):
    daily, horizon = config.get('daily_minutes', 30), config.get('horizon_days', 7)
    if type(daily) is not int or not 1 <= daily <= 480 or type(horizon) is not int or not 1 <= horizon <= 30:
        raise DomainConflict('INVALID_BUDGET', '每日预算须为 1—480 分钟，窗口须为 1—30 天')
    calendar_zone(config.get('calendar_timezone', 'Asia/Shanghai'))
    if len(tasks) > 512:
        raise DomainConflict('PLANNING_LIMIT_EXCEEDED', '单轮最多安排 512 个节点，请缩小学习范围')
    start = date.fromisoformat(start_date) if isinstance(start_date, str) else start_date
    deadline = date.fromisoformat(space['target_date']) if space.get('target_date') else None
    days = [start + timedelta(days=i) for i in range(horizon)
            if not deadline or start + timedelta(days=i) <= deadline]
    spent = {date.fromisoformat(k): max(0, float(v)) for k, v in (spent_minutes or {}).items()}
    used = defaultdict(float, spent)
    weekly = defaultdict(float)
    for day, minutes in used.items():
        weekly[_week(day)] += minutes
    week_cap = space.get('weekly_minutes')
    capacity_by_week = defaultdict(float)
    for day in days:
        capacity_by_week[_week(day)] += max(0, daily - used[day])
    capacity = sum(min(value, max(0, week_cap - weekly[week])) if week_cap is not None else value
                   for week, value in capacity_by_week.items())
    result, remaining, blocked = deepcopy(tasks), 0, {}
    cursor = 0
    by_topic = {topic: t for t in result for topic in t['topic_ids']}
    protected = set(protected_ids)
    reservation_ends, protected_allocated, protected_required = {}, defaultdict(float), {}
    # Existing active reservations are capacity claims. Completed time is supplied
    # separately by the caller and must not be charged again.
    for item in result:
        if item['id'] in protected and item['status'] not in {'completed', 'skipped'}:
            context = item.get('context', {})
            consumed = (consumed_by_task or {}).get(item['id'], context.get('accumulated', {}))
            projected = {**item, 'context': {**context, 'accumulated': consumed}}
            protected_required[item['id']] = sum(_remaining_work(projected))
            # A postponed node keeps its old slots as history, never as claims
            # on current capacity or as scheduled remaining work.
            if item['status'] == 'deferred':
                continue
            # These reservations already exclude the accumulated time recorded
            # when they were created. Only later consumption reduces them again.
            accounted = context.get('accumulated', {})
            consumed_minutes = {phase: max(0, (consumed.get(f'{phase}_seconds', 0) - accounted.get(f'{phase}_seconds', 0)) / 60)
                                for phase in ('study', 'answer')}
            for slot in sorted(context.get('allocations', []), key=lambda s: s['date']):
                day = date.fromisoformat(slot['date'])
                amount = max(0, slot.get('minutes', 0))
                phase = slot.get('phase', 'study')
                already_used = min(amount, consumed_minutes.get(phase, 0))
                consumed_minutes[phase] = max(0, consumed_minutes.get(phase, 0) - already_used)
                amount -= already_used
                if amount > 0:
                    reservation_ends[item['id']] = max(day, reservation_ends.get(item['id'], day))
                if day >= start:
                    used[day] += amount
                    weekly[_week(day)] += amount
                    if day in days:
                        protected_allocated[item['id']] += amount
    for item in result:
        context = item.setdefault('context', {})
        if item['status'] in {'completed', 'skipped'} or context.get('historical'):
            continue
        if item['id'] in protected:
            # Keep fixed nodes intact, but subsequent ordered nodes cannot be
            # scheduled earlier than the remaining fixed predecessor slots.
            end = reservation_ends.get(item['id'])
            while end and cursor < len(days) and days[cursor] < end:
                cursor += 1
            if context.get('remaining_unallocated_minutes', 0) > 0 or context.get('allocation_state') in {'partial', 'waiting_capacity', 'blocked'}:
                blocked.update({topic: 'prerequisite_capacity' for topic in item['topic_ids']})
            if item['status'] == 'deferred':
                blocked.update({topic: 'prerequisite_deferred' for topic in item['topic_ids']})
            continue
        context['allocations'] = []
        study, answer = _remaining_work(item)
        context['remaining_unallocated_minutes'] = study + answer
        if item['status'] == 'deferred':
            # Expiry is a domain transition checked against the exact server
            # instant, never against a calendar-date-only capacity bucket.
            context.update(allocation_state='deferred', allocation_reason='user_deferred')
            context.pop('scheduled_date', None)
            blocked.update({topic: 'prerequisite_deferred' for topic in item['topic_ids']})
            remaining += study + answer
            continue
        dependencies = context.get('prerequisites', [])
        if any(p in blocked or p in by_topic and by_topic[p].get('status') == 'deferred' for p in dependencies):
            reason = next((blocked[p] for p in dependencies if p in blocked), 'prerequisite_deferred')
            context.update(allocation_state='blocked', allocation_reason=reason)
            blocked.update({topic: reason for topic in item['topic_ids']})
            remaining += study + answer
            continue
        if context.get('not_required'):
            context.update(allocation_state='not_required', allocation_reason='mastered_not_due')
            context['remaining_unallocated_minutes'] = 0
            continue
        left = study + answer
        phases = [('study', study), ('answer', answer)]
        for phase, minutes in phases:
            while minutes > 0 and cursor < len(days):
                day = days[cursor]
                available = max(0, daily - used[day])
                if week_cap is not None:
                    available = min(available, max(0, week_cap - weekly[_week(day)]))
                amount = min(minutes, int(available))
                if amount <= 0:
                    cursor += 1
                    continue
                context['allocations'].append({'date': day.isoformat(), 'phase': phase, 'minutes': amount})
                used[day] += amount
                weekly[_week(day)] += amount
                minutes -= amount
                left -= amount
        context['allocation_state'] = 'scheduled' if left == 0 else 'partial' if context['allocations'] else 'waiting_capacity'
        context['allocation_reason'] = None if left == 0 else 'deadline_capacity' if deadline else 'window_capacity'
        context['remaining_unallocated_minutes'] = left
        context['scheduled_date'] = context['allocations'][0]['date'] if context['allocations'] else None
        remaining += left
    pending = [t for t in result if t['status'] not in {'completed', 'skipped'}
               and not t.get('context', {}).get('historical') and not t.get('context', {}).get('not_required')]
    date_keys = {day.isoformat() for day in days}
    allocated_by_task = {t['id']: min(protected_allocated[t['id']], protected_required[t['id']]) if t['id'] in protected else
                    sum(a['minutes'] for a in t['context'].get('allocations', []) if a['date'] in date_keys)
                    for t in pending}
    allocated = sum(allocated_by_task.values())
    unallocated_by_task = {t['id']: protected_required[t['id']] - allocated_by_task[t['id']] if t['id'] in protected else
                          t['context'].get('remaining_unallocated_minutes', 0) for t in pending}
    unallocated = sum(unallocated_by_task.values())
    summary = {'selected_minutes': allocated + unallocated, 'capacity_minutes': capacity,
               'allocated_minutes': allocated, 'unallocated_minutes': unallocated,
               'unallocated_count': sum(value > 0 for value in unallocated_by_task.values())}
    return {'tasks': result, 'strategy_version': VERSION, 'remaining_minutes': remaining, 'summary': summary,
            'days': [{'date': day.isoformat(), 'used_minutes': used[day], 'daily_minutes': daily} for day in days],
            'waiting_count': sum(t.get('context', {}).get('allocation_state') in {'waiting_capacity', 'partial', 'blocked'} for t in result),
            'calendar_timezone': config.get('calendar_timezone', 'Asia/Shanghai')}
