"""Auditable phase-specific forecasts from reliable active-time observations."""
from __future__ import annotations

import math
from datetime import datetime, timezone

DURATION_VERSION = "active-duration-v1"
STUDY_BASELINES = {"learn": 10, "practice": 15, "targeted_practice": 15, "review": 10, "diagnostic": 0}
PRIOR_WEIGHT = 5.0
SAMPLE_LIMIT = 20


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _completed_at(row):
    value = row.get("completed_at")
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime):
            value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
            return value.timestamp()
        value = float(value or 0)
        return value if math.isfinite(value) else 0
    except (ValueError, TypeError):
        return 0


def _calibration(samples, kind, phase, space_id, material_id):
    selected = []
    for row in samples:
        if (row.get("reliable") is not True or row.get("space_id") != space_id
                or space_id is None or row.get("kind") != kind or row.get("phase") != phase):
            continue
        seconds, baseline = _positive(row.get("active_seconds")), _positive(row.get("baseline_minutes"))
        if seconds is None or baseline is None:
            continue
        sample_log_ratio = math.log(seconds) - math.log(60) - math.log(baseline)
        selected.append((row, max(math.log(0.25), min(math.log(4), sample_log_ratio))))
    selected.sort(key=lambda item: (
        material_id is not None and item[0].get("material_id") == material_id,
        _completed_at(item[0])), reverse=True)
    selected = selected[:SAMPLE_LIMIT]
    weights = [1.0 if material_id is not None and row.get("material_id") == material_id else 0.5
               for row, _ in selected]
    weight = sum(weights)
    log_ratio = sum(w * ratio for w, (_, ratio) in zip(weights, selected)) / (PRIOR_WEIGHT + weight)
    ratio = math.exp(max(math.log(0.5), min(math.log(2.0), log_ratio)))
    return {"ratio": ratio, "sample_count": len(selected), "sample_weight": weight,
            "sample_ids": [row["id"] for row, _ in selected if row.get("id")],
            "prior_weight": PRIOR_WEIGHT,
            "reason": "reliable_recent_activity" if selected else "baseline_no_reliable_samples"}


def predict_duration(kind, *, space_id=None, material_id=None, questions=None, samples=(), prerequisites=()):
    """Predict study and answering separately; all duration values are minutes.

    Missing questions means the standard five single-choice assessment. An empty
    question list means no assessment. Callers pass only necessary prerequisites,
    with projected effective difficulty; stale or unrelated rows are excluded.
    """
    if kind not in STUDY_BASELINES:
        raise ValueError(f"Unknown study task kind: {kind}")
    study_baseline = STUDY_BASELINES[kind]
    answer_baseline = 10 if questions is None else sum(
        5 if question.get("type") == "short_answer" else 2 for question in questions)
    samples = tuple(samples)
    study = _calibration(samples, kind, "study", space_id, material_id)
    answer = _calibration(samples, kind, "answer", space_id, material_id)
    difficulties = []
    for row in prerequisites:
        if any(row.get(flag) is not True for flag in ("relevant", "required", "effective")):
            continue
        difficulty = _positive(row.get("effective_difficulty"))
        if difficulty is not None:
            difficulties.append(min(difficulty, 1.0))
    # The prerequisite adjustment is applied once, never again as a weak-topic factor.
    factor = 1.0 + min(0.4, 0.4 * sum(difficulties))
    study["prerequisite_factor"] = factor
    study["baseline_minutes"] = study_baseline
    answer["baseline_minutes"] = answer_baseline
    study_minutes = round(study_baseline * study["ratio"] * factor, 3)
    answer_minutes = round(answer_baseline * answer["ratio"], 3)
    return {"version": DURATION_VERSION, "study_minutes": study_minutes,
            "answer_minutes": answer_minutes, "total_minutes": round(study_minutes + answer_minutes, 3),
            "baseline": {"study_minutes": study_baseline, "answer_minutes": answer_baseline},
            "study": study, "answer": answer}


def evaluate_duration_predictions(samples, *, train_fraction=0.7, minimum_test_samples=5):
    """Chronological holdout comparison against each sample's frozen baseline.

    Fits only the initial training partition; equal timestamps at the split are
    excluded from training. No future holdout is recycled as training. This is
    descriptive replay, not evidence that personalization causes improvement.
    """
    if not 0 < train_fraction < 1 or minimum_test_samples < 1:
        raise ValueError('Invalid duration evaluation split')
    rows = list(samples)
    usable = [row for row in rows if row.get('reliable') is True
              and row.get('kind') in STUDY_BASELINES and row.get('phase') in {'study', 'answer'}
              and row.get('space_id') is not None and row.get('completed_at') is not None
              and _completed_at(row) > 0 and _positive(row.get('active_seconds')) is not None
              and _positive(row.get('baseline_minutes')) is not None]
    usable.sort(key=_completed_at)
    split = max(1, min(len(usable) - 1, int(len(usable) * train_fraction))) if len(usable) > 1 else len(usable)
    training, test = usable[:split], usable[split:]
    predictions = []
    for row in test:
        prior = [sample for sample in training if _completed_at(sample) < _completed_at(row)]
        calibration = _calibration(prior, row['kind'], row['phase'], row['space_id'], row.get('material_id'))
        baseline = float(row['baseline_minutes'])
        actual = float(row['active_seconds']) / 60
        predicted = baseline * calibration['ratio']
        predictions.append({'id': row.get('id'), 'phase': row['phase'],
            'completed_at': row['completed_at'], 'baseline_minutes': baseline,
            'forecast_minutes': predicted, 'actual_minutes': actual,
            'baseline_absolute_error': abs(actual - baseline),
            'forecast_absolute_error': abs(actual - predicted),
            'training_sample_count': calibration['sample_count'],
            'training_sample_ids': calibration['sample_ids']})

    def metrics(items):
        count = len(items)
        return {'sample_count': count,
                'forecast_mae_minutes': sum(p['forecast_absolute_error'] for p in items) / count if count else None,
                'baseline_mae_minutes': sum(p['baseline_absolute_error'] for p in items) / count if count else None}

    result = metrics(predictions)
    calibrated_count = sum(p['training_sample_count'] > 0 for p in predictions)
    reasons = []
    if len(predictions) < minimum_test_samples:
        reasons.append('insufficient_time_ordered_reliable_holdout')
    if predictions and not calibrated_count:
        reasons.append('no_scoped_training_samples')
    enough = not reasons
    result.update(version=DURATION_VERSION, method='chronological_fixed_training_holdout',
        sample_count=len(rows), reliable_sample_count=len(usable), train_sample_count=len(training),
        test_sample_count=len(predictions), coverage=len(usable) / len(rows) if rows else 0.0,
        calibrated_coverage=calibrated_count / len(predictions) if predictions else 0.0,
        status='evaluated' if enough else 'insufficient_data',
        insufficient_reasons=reasons,
        improvement_observed=(result['forecast_mae_minutes'] < result['baseline_mae_minutes']) if enough else None,
        phases={phase: metrics([p for p in predictions if p['phase'] == phase]) for phase in ('study', 'answer')},
        predictions=predictions,
        limitations=['Descriptive time-split replay; not a causal improvement claim.',
                     'Only reliable server activity with known completion time and baseline is evaluated.',
                     'Prerequisite effects cannot be evaluated without their frozen prediction-time inputs.'])
    return result
