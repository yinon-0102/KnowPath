"""The opt-in acceptance runner cleans up even a partially started container."""
import importlib.util
from pathlib import Path

import pytest


def runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "accept_learning_backend.py"
    spec = importlib.util.spec_from_file_location("acceptance_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failed_container_start_is_still_owned_and_cleaned(monkeypatch):
    module = runner()
    calls = []
    def docker(*args):
        calls.append(args)
        if args[0] == "run":
            raise RuntimeError("DOCKER_COMMAND_FAILED:run")
        if args[0] == "ps":
            return "owned-id"
        if args[0] == "inspect":
            return "disposable"
        return ""
    monkeypatch.setattr(module, "docker", docker)
    acceptance = module.Acceptance()
    with pytest.raises(RuntimeError):
        acceptance.container("knowpath-accept-owned", "mysql:8.4", 3306, {})
    acceptance.cleanup()
    assert ("rm", "-f", "-v", "knowpath-accept-owned") in calls
    assert acceptance.report["cleanup"]["remaining_containers"] == []


def test_cleanup_refuses_a_container_without_owner_label(monkeypatch):
    module = runner()
    calls = []
    def docker(*args):
        calls.append(args)
        return "foreign-id" if args[0] == "ps" else "unrelated"
    monkeypatch.setattr(module, "docker", docker)
    acceptance = module.Acceptance()
    acceptance.containers = ["unexpected-name"]
    acceptance.cleanup()
    assert not any(args[0] == "rm" for args in calls)
    assert acceptance.report["cleanup"]["remaining_containers"] == ["unexpected-name"]
