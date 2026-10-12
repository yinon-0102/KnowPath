import json

from knowpath_backend.learning.plans.activity import new_activity, advance_activity, activity_snapshot, activity_daily_seconds


def test_heartbeat_accounts_server_seconds_and_survives_serialization():
    state = new_activity(1000)
    state = advance_activity(state, 1030)
    restored = json.loads(json.dumps(state))
    assert advance_activity(restored, 1060)["active_seconds"] == 60
    assert state["active_seconds"] == 30


def test_liveness_caps_gap_and_reconnect_does_not_backfill():
    state = new_activity(1000)
    snapshot = activity_snapshot(state, 1600)
    assert snapshot["active_seconds"] == 90
    assert "disconnected" in snapshot["pause_reasons"]
    state = advance_activity(state, 1600)
    assert state["active_seconds"] == 90
    assert advance_activity(state, 1630)["active_seconds"] == 120


def test_automatic_resume_keeps_manual_pause_and_independent_reasons():
    state = advance_activity(new_activity(1000), 1010, "pause")
    state = advance_activity(state, 1020, "hidden")
    state = advance_activity(state, 1030, "visible")
    assert state["pause_reasons"] == ["manual_pause"]
    state = advance_activity(state, 1040, "assessment_start")
    state = advance_activity(state, 1050, "resume")
    assert state["pause_reasons"] == ["assessment"]
    state = advance_activity(state, 1060, "assessment_end")
    assert advance_activity(state, 1070)["active_seconds"] == 20


def test_waiting_finish_and_backwards_clock_do_not_add_time():
    state = advance_activity(new_activity(1000), 1010, "waiting")
    state = advance_activity(state, 1050, "ready")
    state = advance_activity(state, 1060, "finish")
    assert advance_activity(state, 1100)["active_seconds"] == 20
    assert advance_activity(state, 900)["active_seconds"] == 20


def test_legacy_elapsed_is_never_imported_as_reliable_activity():
    state = advance_activity({"elapsed_seconds": 999}, 1000)
    assert state["active_seconds"] == 0
    assert state["reliable"] is False


def test_timeout_marks_sample_quality_without_erasing_real_budget_time():
    state = advance_activity(new_activity(0), 120, 'finish')
    assert state['active_seconds'] == 90
    assert state['quality_flags'] == ['heartbeat_timeout']
    assert state['reliable']  # server accounting remains real; predictor excludes gaps


def test_active_intervals_split_at_local_midnight_and_can_change_timezone():
    state = new_activity("2026-10-11T15:59:30Z")
    state = advance_activity(state, "2026-10-11T16:00:30Z")
    assert state['daily_active_seconds'] == {'2026-10-11': 30, '2026-10-12': 30}
    assert activity_daily_seconds(state, 'UTC') == {'2026-10-11': 60}
    assert sum(state['daily_active_seconds'].values()) == state['active_seconds']


def test_pause_and_liveness_gap_never_backfill_active_day_buckets():
    state = new_activity("2026-10-11T15:59:30Z")
    state = advance_activity(state, "2026-10-11T16:04:30Z")
    assert state['active_seconds'] == 90
    assert state['paused_seconds'] == 210
    assert state['daily_active_seconds'] == {'2026-10-11': 30, '2026-10-12': 60}
    state = advance_activity(state, "2026-10-11T16:05:00Z", 'pause')
    state = advance_activity(state, "2026-10-11T16:05:30Z", 'finish')
    assert state['active_seconds'] == 120
    assert state['paused_seconds'] == 240
    assert advance_activity(state, "2026-10-12T16:05:30Z")['paused_seconds'] == 240


def test_contiguous_intervals_merge_and_reads_leave_stored_segments_untouched():
    state = advance_activity(new_activity(1000), 1030)
    state = advance_activity(state, 1060)
    assert state['active_segments'] == [{'start': 1000, 'end': 1060}]
    activity_snapshot(state, 1090)
    assert state['active_segments'] == [{'start': 1000, 'end': 1060}]
