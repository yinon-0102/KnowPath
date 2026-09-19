"""Narrow, non-sensitive learning preference suggestions with explicit confirmation."""
from copy import deepcopy
import re

_PATTERNS = {
    "example_first": r"^(?:请|我希望|我想要|我更喜欢)(?:你)?先(?:举例|给(?:我)?例子)",
    "concise_explanations": r"^(?:请|我希望|我更喜欢)(?:你)?(?:简短|简洁)(?:地)?(?:解释|回答|说明)",
}


def infer_candidates(message):
    # Only an anchored, direct request is considered. Source documents, model
    # output and quoted sentences are never inputs to this inference.
    return [{"id": message["id"] + ":" + field, "field": "preferences." + field,
             "value": True, "source": "inferred", "confidence": 0.9,
             "confirmation": "pending", "updated_at": message["created_at"],
             "source_ref": {"message_id": message["id"], "run_id": message["run_id"]}}
            for field, pattern in _PATTERNS.items() if re.search(pattern, message["message"].strip())]


def candidates_for(repository, space_id):
    return [deepcopy(candidate) for message in repository.records("messages", space_id=space_id, lock=False)
            if message["status"] == "completed" for candidate in message["snapshot"].get("profile_candidates", [])]


def confirm_candidates(repository, space_id, preferences, timestamp):
    if not isinstance(preferences, dict):
        return
    for message in repository.records("messages", space_id=space_id):
        changed = False
        for candidate in message["snapshot"].get("profile_candidates", []):
            field = candidate["field"].removeprefix("preferences.")
            if candidate["confirmation"] == "pending" and field in preferences:
                candidate["confirmation"] = "confirmed" if preferences[field] == candidate["value"] else "rejected"
                candidate["updated_at"] = timestamp
                changed = True
        if changed:
            repository.put_record("messages", message)
