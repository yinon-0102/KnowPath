"""Session permission decisions and grants with caller-provided approval."""
from __future__ import annotations

import enum
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Set, Tuple

class PermissionDecision(enum.Enum):
    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    DENY = "deny"

PromptFn = Callable[[str, Dict[str, Any], str], PermissionDecision]


class PermissionGrants:
    def __init__(self) -> None:
        # (name, prefix) — prefix == "" 表示整工具授权
        self._grants: Set[Tuple[str, str]] = set()

    @staticmethod
    def _prefix(args: Dict[str, Any]) -> str:
        path = str(args.get("path") or "").strip()
        if not path:
            return ""
        return str(PurePosixPath(path).parent)   # "src/a.py" → "src"; "a.py" → "."

    def is_granted(self, name: str, args: Dict[str, Any]) -> bool:
        prefix = self._prefix(args)
        for gn, gp in self._grants:
            if gn != name:
                continue
            if gp == "":
                return True
            if prefix == gp or prefix.startswith(gp + "/"):
                return True
        return False

    def grant(self, name: str, args: Dict[str, Any]) -> None:
        self._grants.add((name, self._prefix(args)))


def decide(
    grants: PermissionGrants,
    prompt_fn: PromptFn,
    name: str,
    args: Dict[str, Any],
    preview: str,
) -> bool:
    """返回最终是否允许(bool,给 agent)。命中授权直接放行;否则弹三态框,
    ALLOW_ALWAYS 记进 grants。"""
    if grants.is_granted(name, args):
        return True
    decision = prompt_fn(name, args, preview)
    if decision is PermissionDecision.DENY:
        return False
    if decision is PermissionDecision.ALLOW_ALWAYS:
        grants.grant(name, args)
    return True
