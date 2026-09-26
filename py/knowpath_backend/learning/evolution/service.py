"""Bounded observation and correction history with a current effective curve."""
from __future__ import annotations

from copy import deepcopy

from knowpath_backend.learning.assessments.mastery import aggregate
from knowpath_backend.learning.assessments.review_policy import (
    REVIEW_VERSION, REVIEW_CONFIG, LIMITATIONS, effective_evidence,
    independent_evidence, project_review_state, moment, observation_time, enrich_observation_times,
)
from knowpath_backend.learning.assessments.service import revision
from knowpath_backend.learning.errors import DomainConflict
from knowpath_backend.learning.spaces.service import now


class EvolutionService:
    def __init__(self, assessments):
        self.assessments = assessments
        self.repository = assessments.repository

    def get(self, space_id, *, limit=200):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise DomainConflict("INVALID_LIMIT", "limit 必须在 1 到 500 之间")
        with self.repository.transaction():
            space = self.assessments.spaces.repository.get(space_id)
            topics = self.assessments._topics(space)
            evidence = enrich_observation_times(self.repository, self.repository.records("evidence", space_id=space_id))
            resets = self.repository.records("resets", space_id=space_id)
            states = {s["topic_id"]: s for s in self.repository.records("states", space_id=space_id)}
            cutoff = moment(now())
            epochs = {}
            for reset in resets:
                if moment(reset["created_at"]) <= cutoff:
                    for topic_id in reset["topic_ids"]:
                        epochs[topic_id] = max(epochs.get(topic_id, 0), reset["state_version"])
            items = []
            for topic in topics:
                topic_id, revision_id = topic["id"], revision(topic)
                epoch = epochs.get(topic_id, 0)
                rows = [e for e in evidence if e["topic_id"] == topic_id]
                base = states.get(topic_id, {"topic_id": topic_id, "topic_revision_id": revision_id,
                    "epoch": epoch, "mastery_score": None, "status": "unseen", "score_validity": "unverified",
                    "evidence_ids": [], "independent_evidence_count": 0, "evidence_count": 0,
                    "last_assessed_at": None, "error_tags": [], "policy_version": "mastery-v1",
                    "state_version": space["state_version"]})
                current = project_review_state(base, rows, revision_id=revision_id, epoch=epoch, as_of=cutoff,
                                               mastery_policy=self.assessments.mastery_policy)
                effective = effective_evidence(rows, revision_id=revision_id, epoch=epoch, as_of=cutoff)
                curve = self._curve(effective, rows, topic_id, revision_id, epoch, cutoff)
                events = self._events(rows, [r for r in resets if topic_id in r["topic_ids"]], cutoff, revision_id, epoch)
                items.append({"topic_id": topic_id, "topic_name": topic.get("name", topic_id),
                    "topic_revision_id": revision_id, "epoch": epoch, "current": current,
                    "evidence_sufficiency": current["evidence_sufficiency"],
                    "curve": curve, "events": events})
            # Apply one total history budget, retaining the most recent points.
            history = [(entry.get("recorded_at", entry.get("observed_at")), entry["id"], index, key, entry)
                       for index, item in enumerate(items) for key in ("events", "curve") for entry in item[key]]
            history.sort(key=lambda entry: (moment(entry[0]), entry[1], entry[3]), reverse=True)
            for item in items:
                item.update(events=[], curve=[])
            for _, _, index, key, entry in history[:limit]:
                items[index][key].append(entry)
            for item in items:
                for key in ("events", "curve"):
                    item[key].sort(key=lambda entry: (moment(entry.get("recorded_at", entry.get("observed_at"))), entry["id"]))
            return {"space_id": space_id, "state_version": space["state_version"], "as_of": cutoff.isoformat(),
                    "policy_version": REVIEW_VERSION, "mastery_policy_version": self.assessments.mastery_policy.version,
                    "config": deepcopy(REVIEW_CONFIG), "items": items, "limit": limit,
                    "returned_history_count": min(len(history), limit), "total_history_count": len(history),
                    "truncated": len(history) > limit,
                    "limitations": [*LIMITATIONS, "The current curve reinterprets original observations using accepted corrections; observed_mastery_score only uses grades available at the point's recorded_at time, which may follow the answer time.",
                        "Reset and obsolete revision observations remain audit events and cannot contribute to the current curve."]}

    def _curve(self, effective, all_rows, topic_id, revision_id, epoch, cutoff):
        points = []
        times = sorted({observation_time(e).isoformat() for e in effective}, key=moment)
        for timestamp in times:
            past = [e for e in effective if observation_time(e) <= moment(timestamp)]
            selected = independent_evidence(past, window=self.assessments.mastery_policy.window)
            available_at = max((e["created_at"] for e in past), key=moment)
            available = [e for e in all_rows if observation_time(e) <= moment(timestamp)]
            original = independent_evidence(effective_evidence(available, revision_id=revision_id, epoch=epoch, as_of=available_at),
                                             window=self.assessments.mastery_policy.window)
            if not selected:
                continue
            revised_at = max((e["reviewed_at"] for e in selected if e.get("reviewed_at")), default=None, key=moment)
            value = aggregate(topic_id, revision_id, past, {}, 0, cutoff.isoformat(), policy=self.assessments.mastery_policy)
            points.append({"id": "curve:" + topic_id + ":" + timestamp, "observed_at": timestamp,
                "recorded_at": available_at, "last_revised_at": revised_at,
                "interpretation": "current_effective", "mastery_score": value["mastery_score"],
                "observed_mastery_score": sum(e["score"] for e in original) / len(original) if original else None,
                "status": value["status"], "evidence_ids": [e["id"] for e in selected],
                "observation_ids": [e.get("observation_id", e["id"]) for e in selected],
                "independent_evidence_count": len(selected), "next_review_at": value["next_review_at"]})
        return points

    def _events(self, rows, resets, cutoff, revision_id, epoch):
        events = []
        for row in rows:
            observed = observation_time(row)
            recorded = moment(row.get("reviewed_at") or row["created_at"])
            if observed > cutoff or recorded > cutoff:
                continue
            revoked = bool(row.get("revoked_by_review_id") and
                           (not row.get("revoked_at") or moment(row["revoked_at"]) <= cutoff))
            exclusions = []
            if row.get("assisted"):
                exclusions.append("assisted")
            if row.get("topic_revision_id") != revision_id:
                exclusions.append("obsolete_revision")
            if row.get("epoch", 0) != epoch:
                exclusions.append("reset_epoch")
            if revoked:
                exclusions.append("revoked")
            if row.get("score") is None:
                exclusions.append("unverified")
            if not row.get("eligible"):
                exclusions.append("ineligible")
            correction = bool(row.get("reviewed_at") or row.get("replaces_evidence_id"))
            events.append({"id": row["id"], "type": "correction" if correction else "observation",
                "evidence_id": row["id"], "observation_id": row.get("observation_id", row["id"]),
                "replaces_evidence_id": row.get("replaces_evidence_id"), "review_id": row.get("review_id"),
                "assessment_id": row.get("assessment_id"), "family_id": row.get("family_id"),
                "topic_revision_id": row.get("topic_revision_id"), "epoch": row.get("epoch", 0),
                "observed_at": observed.isoformat(), "recorded_at": recorded.isoformat(),
                "observed_at_source": row.get("observed_at_source", "legacy_finalization"),
                "score": row.get("score"), "result": row.get("result"),
                "difficulty": row.get("difficulty", "unknown"), "is_application": row.get("is_application", False),
                "assisted": bool(row.get("assisted")), "exclusion_reasons": exclusions,
                "revoked_at": row.get("revoked_at") if revoked else None,
                "question_review_status": row.get("question_review_status")})
        for reset in resets:
            if moment(reset["created_at"]) <= cutoff:
                events.append({"id": reset["id"], "type": "reset", "recorded_at": reset["created_at"],
                               "observed_at": None, "epoch": reset["state_version"], "reason": reset["reason"]})
        return events
