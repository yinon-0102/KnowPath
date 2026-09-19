"""verify —— 在线验证-重试机制(Online Verify-Retry Loop)。"""
from knowpath_backend.verify.spec import Check, CheckSpec, SpecGenerator
from knowpath_backend.verify.checkers import CheckContext, CheckerRunner, check_one
from knowpath_backend.verify.residual import residual
from knowpath_backend.verify.convergence import (
    Verdict, ConvergenceJudge, Round, fingerprint,
)
from knowpath_backend.verify.loop import VerifyResult, VerifyRetryLoop, Executor
from knowpath_backend.verify.stall import stalled_oracle_ids, apply_demotion

__all__ = [
    "Check", "CheckSpec", "SpecGenerator",
    "CheckContext", "CheckerRunner", "check_one",
    "residual",
    "Verdict", "ConvergenceJudge", "Round", "fingerprint",
    "VerifyResult", "VerifyRetryLoop", "Executor",
    "stalled_oracle_ids", "apply_demotion",
]
