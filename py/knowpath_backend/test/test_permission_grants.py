"""Permission decisions and session grants without a terminal prompt."""
from knowpath_backend.core.permissions import PermissionDecision, PermissionGrants, decide


def test_decision_enum_has_three_states():
    assert {d.name for d in PermissionDecision} == {
        "ALLOW_ONCE", "ALLOW_ALWAYS", "DENY"
    }


def test_grant_by_tool_and_path_prefix():
    g = PermissionGrants()
    assert g.is_granted("Edit", {"path": "src/a.py"}) is False
    g.grant("Edit", {"path": "src/a.py"})              # 记 (Edit, "src")
    assert g.is_granted("Edit", {"path": "src/a.py"}) is True      # 同目录
    assert g.is_granted("Edit", {"path": "src/sub/b.py"}) is True  # 子目录前缀命中
    assert g.is_granted("Edit", {"path": "tests/c.py"}) is False   # 别的目录不命中
    assert g.is_granted("Write", {"path": "src/a.py"}) is False


def test_grant_without_path_is_whole_tool():
    g = PermissionGrants()
    g.grant("Calc", {})                       # 无 path → 整工具授权
    assert g.is_granted("Calc", {"x": 1}) is True


def test_decide_granted_skips_prompt():
    g = PermissionGrants()
    g.grant("Edit", {"path": "src/a.py"})
    calls = []
    def fake_prompt(n, a, p):
        calls.append(n); return PermissionDecision.DENY
    assert decide(g, fake_prompt, "Edit", {"path": "src/x.py"}, "diff") is True
    assert calls == []


def test_decide_allow_once_does_not_persist():
    g = PermissionGrants()
    assert decide(g, lambda *a: PermissionDecision.ALLOW_ONCE,
                  "Edit", {"path": "src/a.py"}, "d") is True
    assert g.is_granted("Edit", {"path": "src/a.py"}) is False


def test_decide_allow_always_persists():
    g = PermissionGrants()
    assert decide(g, lambda *a: PermissionDecision.ALLOW_ALWAYS,
                  "Edit", {"path": "src/a.py"}, "d") is True
    assert g.is_granted("Edit", {"path": "src/b.py"}) is True


def test_decide_deny_returns_false():
    g = PermissionGrants()
    assert decide(g, lambda *a: PermissionDecision.DENY,
                  "Edit", {"path": "src/a.py"}, "d") is False
