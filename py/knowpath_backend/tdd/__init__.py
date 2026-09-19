"""TDD 模式:独立出题(test-author)+ 硬红门 + 实现转绿,与事后 verify 并存。"""
from knowpath_backend.tdd.orchestrator import run_tdd, TddResult
from knowpath_backend.tdd.classify import classify, TddDecision
from knowpath_backend.tdd.runner import run_pytest, RunResult, RunOutcome
from knowpath_backend.tdd.gates import red_gate, green_gate, RedVerdict, GreenVerdict

__all__ = [
    "run_tdd", "TddResult", "classify", "TddDecision",
    "run_pytest", "RunResult", "RunOutcome",
    "red_gate", "green_gate", "RedVerdict", "GreenVerdict",
]
