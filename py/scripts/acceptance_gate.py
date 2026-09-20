"""Fail-closed pytest profile selection and machine-readable execution audit."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pytest


class AcceptanceGate:
    def __init__(self, profile, matrix, report):
        self.profile, self.matrix, self.path = profile, matrix, report
        self.expected = set()
        self.executed = set()
        self.outcomes = {}
        self.errors = set()
        self.collected = set()
        self.selected = set()

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection_modifyitems(self, config, items):
        self.collected = {item.nodeid for item in items}
        known = set().union(*(set(self.matrix.get(key, ())) for key in ("offline", "store", "model", "not_applicable")))
        if known - self.collected:
            self.errors.add("matrix_drift")
        store = set(self.matrix["store"])
        model = set(self.matrix["model"])
        na = set(self.matrix["not_applicable"])
        # New tests are never silently hidden: default offline, or explicit store
        # marker / MySQL fixture parameter. Any unforeseen skip fails the gate.
        for item in items:
            params = getattr(getattr(item, "callspec", None), "params", {})
            if item.get_closest_marker("acceptance_store") or "mysql" in params.values():
                store.add(item.nodeid)
            if item.get_closest_marker("acceptance_model"):
                model.add(item.nodeid)
        store -= na | model
        intended = {"offline": self.collected - store - model - na, "store": store, "model": model}[self.profile]
        self.expected = intended
        selected, deselected = [], []
        for item in items:
            (selected if item.nodeid in intended else deselected).append(item)
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)

    def pytest_configure(self, config):
        for marker in ("acceptance_store", "acceptance_model"):
            config.addinivalue_line("markers", marker + ": explicit required acceptance profile")

    def pytest_collection_finish(self, session):
        self.selected = {item.nodeid for item in session.items}
        if self.selected != self.expected:
            self.errors.add("selection_mismatch")

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            self.executed.add(report.nodeid)
        if report.skipped or hasattr(report, "wasxfail"):
            self.errors.add("unexpected_skip")
            self.outcomes[report.nodeid] = "unexpected_skip"
        elif report.failed:
            self.errors.add("failed")
            self.outcomes[report.nodeid] = "failed:" + report.when
        elif report.when == "call":
            self.outcomes.setdefault(report.nodeid, "passed")

    def pytest_sessionfinish(self, session, exitstatus):
        if not self.expected:
            self.errors.add("zero_selected")
        if self.expected != self.executed:
            self.errors.add("not_executed")
        if exitstatus:
            self.errors.add("pytest_failed")
        if self.errors:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"profile": self.profile,
            "expected": sorted(self.expected), "collected": sorted(self.collected),
            "selected": sorted(self.selected), "executed": sorted(self.executed),
            "outcomes": self.outcomes, "not_applicable": self.matrix["not_applicable"],
            "missing_from_collection": sorted(set().union(*(set(self.matrix.get(key, ()))
                for key in ("offline", "store", "model", "not_applicable"))) - self.collected),
            "gate_errors": sorted(self.errors), "pytest_exit_code": int(exitstatus),
            "status": "failed" if self.errors else "passed"}, indent=2), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=("offline", "store", "model"))
    parser.add_argument("--matrix", type=Path, default=Path(__file__).with_name("acceptance_matrix.json"))
    parser.add_argument("--report", type=Path, required=True)
    args, pytest_args = parser.parse_known_args(argv)
    if pytest_args[:1] == ["--"]:
        pytest_args.pop(0)
    plugin = AcceptanceGate(args.profile, json.loads(args.matrix.read_text(encoding="utf-8-sig")), args.report)
    return pytest.main(pytest_args, plugins=[plugin])


if __name__ == "__main__":
    sys.exit(main())
