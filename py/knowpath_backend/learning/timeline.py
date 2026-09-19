"""Read-only timeline derived from immutable domain history, not runtime caches."""
from copy import deepcopy


def changes_for(state, space_id):
    repository = state.assessment_service.repository
    space = state.space_service.get(space_id)
    events = state.assessment_service.changes(space_id)
    events.extend(deepcopy(event["payload"]) for event in repository.records(
        "outbox", event_type="knowledge.updated", aggregate_id=space_id, lock=False))
    material_ids = {b["material_id"] for b in space["bindings"]}
    for material_id in sorted(material_ids):
        for version in state.material_repository.list_versions(material_id):
            events.append({"id": "material-version:" + version.id, "kind": "material_version", "space_id": space_id,
                           "material_id": material_id, "material_version_id": version.id, "status": version.status,
                           "created_at": version.created_at.isoformat()})
        for revision in state.graph_service._history(material_id):
            if not revision.get("publication"):
                continue
            events.append({"id": "graph-published:" + revision["id"], "kind": "graph_published", "space_id": space_id,
                           "material_id": material_id, "revision_id": revision["id"],
                           "material_version_id": revision["material_version_id"], "graph_version": revision["graph_version"],
                           "created_at": revision["publication"].get("published_at") or revision["created_at"]})
    for assessment in repository.records("assessments", space_id=space_id, lock=False):
        result = assessment.get("snapshot", {}).get("original_graded_result") or assessment.get("result")
        if not result:
            continue
        events.append({"id": "assessment-graded:" + assessment["id"], "kind": "mastery_updated", "space_id": space_id,
                       "assessment_id": assessment["id"], "state_version": result["state_version"],
                       "topic_results": deepcopy(result["topic_results"]), "created_at": result["graded_at"]})
        for review in assessment.get("grade_reviews", []):
            events.append({"id": "grade-review:" + review["id"], "kind": "grade_review", "space_id": space_id,
                           "assessment_id": assessment["id"], "state_version": review.get("state_version"),
                           "topic_results": deepcopy(review.get("topic_results", [])), "created_at": review["created_at"]})
    for plan in repository.records("plans", space_id=space_id, lock=False):
        events.append({"id": "plan-created:" + plan["id"],
                       "kind": "plan_replanned" if plan.get("config", {}).get("rebuild_mode") == "local_replan" else "plan_created",
                       "space_id": space_id, "plan_id": plan["id"], "created_at": plan["created_at"]})
    return events
