import math

import pytest

from knowpath_backend.learning.plans.duration import predict_duration, evaluate_duration_predictions


@pytest.mark.parametrize("kind,study", [("learn", 10), ("practice", 15), ("targeted_practice", 15), ("review", 10), ("diagnostic", 0)])
def test_baselines_separate_learning_from_answers(kind, study):
    result = predict_duration(kind)
    assert result["study_minutes"] == study
    assert result["answer_minutes"] == 10
    assert result["total_minutes"] == study + 10
    assert result["version"]


def test_questions_use_actual_types_and_empty_assessment_has_zero_time():
    assert predict_duration("learn", questions=[{"type": "single_choice"}, {"type": "short_answer"}])["answer_minutes"] == 7
    assert predict_duration("learn", questions=[])["answer_minutes"] == 0


def sample(**kwargs):
    return {"space_id": "s", "material_id": "m", "kind": "learn", "phase": "study",
            "active_seconds": 1200, "baseline_minutes": 10, "reliable": True,
            "completed_at": "2026-01-01T00:00:00Z", **kwargs}


def test_samples_are_scoped_weighted_geometrically_and_shrunk_with_five_priors():
    result = predict_duration("learn", space_id="s", material_id="m", samples=[sample(), sample(material_id="other"), sample(space_id="elsewhere"), sample(reliable=False)])
    assert result["study"]["sample_count"] == 2
    assert result["study"]["sample_weight"] == 1.5
    assert result["study"]["ratio"] == pytest.approx(math.exp(1.5 * math.log(2) / 6.5))


def test_only_recent_twenty_reliable_samples_and_clamped_ratios():
    samples = [sample(active_seconds=600000, completed_at=f"2026-01-{day:02d}T00:00:00Z") for day in range(1, 26)]
    result = predict_duration("learn", space_id="s", material_id="m", samples=samples)
    assert result["study"]["sample_count"] == 20
    assert result["study"]["ratio"] <= 2
    assert result["study"]["sample_ids"] == []


def test_prerequisite_difficulty_is_relevant_effective_required_and_capped():
    prerequisites = [{"effective_difficulty": 1, "relevant": True, "required": True, "effective": True}, {"effective_difficulty": 1, "relevant": False}, {"effective_difficulty": 1, "effective": False}]
    result = predict_duration("learn", prerequisites=prerequisites)
    assert result["study_minutes"] == 14
    assert result["answer_minutes"] == 10
    assert result["study"]["prerequisite_factor"] == 1.4


def test_answer_calibration_uses_only_answer_samples_and_ignores_invalid_data():
    result = predict_duration("learn", space_id="s", material_id="m", samples=[sample(phase="answer", active_seconds=1200), sample(active_seconds=float("nan")), sample(baseline_minutes=0)])
    assert result["study_minutes"] == 10
    assert result["answer_minutes"] > 10
    assert result["study"]["sample_count"] == 0


def test_same_material_samples_have_priority_within_recent_twenty_pool():
    samples = [sample(id=f"other-{day}", material_id="other", completed_at=f"2026-01-{day:02d}T00:00:00Z") for day in range(2, 23)]
    samples.append(sample(id="same-material", completed_at="2026-01-01T00:00:00Z"))
    result = predict_duration("learn", space_id="s", material_id="m", samples=samples)
    assert result["study"]["sample_ids"][0] == "same-material"
    assert result["study"]["sample_count"] == 20


def test_extreme_finite_samples_remain_bounded_and_never_crash():
    result = predict_duration("learn", space_id="s", material_id="m", samples=[sample(active_seconds=1e-300, baseline_minutes=1e300)])
    assert result["study"]["ratio"] == pytest.approx(math.exp(math.log(0.25) / 6))


def test_unknown_prerequisite_flags_never_increase_estimate():
    assert predict_duration("learn", prerequisites=[{"effective_difficulty": 1}])["study_minutes"] == 10


def test_time_split_evaluation_reports_sparse_data_without_claiming_improvement():
    result = evaluate_duration_predictions([sample(completed_at=None), sample(reliable=False)])
    assert result['sample_count'] == 2
    assert result['reliable_sample_count'] == 0
    assert result['coverage'] == 0
    assert result['status'] == 'insufficient_data'
    assert result['improvement_observed'] is None
    assert result['forecast_mae_minutes'] is None


def test_time_split_evaluation_never_uses_future_or_same_timestamp_samples():
    rows = [sample(id='past', active_seconds=600, completed_at='2026-01-01T00:00:00Z'),
            sample(id='future-a', active_seconds=1200, completed_at='2026-01-02T00:00:00Z'),
            sample(id='future-b', active_seconds=1200, completed_at='2026-01-02T00:00:00Z')]
    result = evaluate_duration_predictions(rows, train_fraction=1/3, minimum_test_samples=1)
    assert result['train_sample_count'] == 1 and result['test_sample_count'] == 2
    assert result['forecast_mae_minutes'] == 10
    assert result['baseline_mae_minutes'] == 10
    assert result['improvement_observed'] is False
    assert result['predictions'][0]['training_sample_ids'] == ['past']
    assert result['predictions'][1]['training_sample_ids'] == ['past']


def test_time_split_evaluation_calibrates_answer_baseline_per_question():
    rows = [sample(id=f'answer-{day}', phase='answer', active_seconds=240, baseline_minutes=2,
                   completed_at=f'2026-01-{day:02d}T00:00:00Z') for day in range(1, 11)]
    result = evaluate_duration_predictions(rows, train_fraction=0.5)
    assert result['status'] == 'evaluated'
    assert result['baseline_mae_minutes'] == 2
    assert result['forecast_mae_minutes'] < 2
    assert result['calibrated_coverage'] == 1
    assert result['phases']['answer']['sample_count'] == 5
    assert result['predictions'][0]['baseline_minutes'] == 2


def test_time_split_reports_absent_calibration_and_invalid_completion_times():
    rows = [sample(id=f's{day}', space_id=f's{day}', completed_at=f'2026-01-{day:02d}T00:00:00Z') for day in range(1, 11)]
    rows.append(sample(completed_at=float('inf')))
    result = evaluate_duration_predictions(rows, train_fraction=0.5)
    assert result['reliable_sample_count'] == 10
    assert result['calibrated_coverage'] == 0
    assert result['status'] == 'insufficient_data'
    assert 'no_scoped_training_samples' in result['insufficient_reasons']
    assert result['improvement_observed'] is None
