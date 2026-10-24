"""Run Web Agent checks in an offline, explicitly configured subprocess.

Only this standard-library bootstrap runs in the caller's environment. Reports
and temporary databases are kept under .runtime.local/agent-checks/<run-id>.
No application module or dotenv file is loaded before sanitizing the environment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

BACKEND = Path(__file__).resolve().parents[1]
TESTS = BACKEND / "knowpath_backend/test"
BASELINE = (
    "test_learning_messages.py", "test_learning_conversation_context.py",
    "test_adaptive_messages.py", "test_reading_messages.py", "test_study_lessons.py",
    "test_learning_source_access.py", "test_learning_model_task_deletion.py",
    "test_document_scope_deletion.py",
)
COMPATIBILITY_PATTERNS = (
    "test_agent_runtime.py", "test_function_call_*.py", "test_memory_*.py",
    "test_l[0-5]_*.py", "test_context_engine.py", "test_learning_source_access.py",
    "test_*deletion*.py", "test_rag_*.py", "test_learning_model_task*.py",
    "test_learning_graph_worker.py", "test_study_lesson_streaming.py",
)


def isolated_environment(directory: Path, *, mysql_url: str | None = None) -> dict[str, str]:
    """Allowlist OS requirements; never inherit application or provider settings."""
    names = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "SYSTEMDRIVE",
             "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "LANG", "LC_ALL",
             "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}
    env = {key: value for key, value in os.environ.items() if key.upper() in names}
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    env.update({
        "PYTHON_DOTENV_DISABLED": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": str(BACKEND), "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1", "TEMP": str(directory), "TMP": str(directory),
        "DATABASE_URL": "sqlite+pysqlite:///" + (directory / "runtime.db").as_posix(),
        "LEARNING_PERSISTENCE": "memory", "LEARNING_RETRIEVAL_BACKEND": "keyword",
        "LEARNING_RAG_PLUGIN": "legacy", "MY_CHAT_STORAGE": str(directory / "memory"),
        "KNOWPATH_AGENT_CHECK_REPORT": str(directory / "results.json"),
        "KNOWPATH_AGENT_ALLOWED_ADDRESSES": "[]",
    })
    if mysql_url:
        parsed = urlsplit(mysql_url)
        name = parsed.path.strip("/")
        if (parsed.scheme not in {"mysql", "mysql+pymysql"} or not parsed.hostname
                or not re.fullmatch(r"[a-zA-Z0-9_]+", name)
                or not re.search(r"(?:^|_)(?:test|tests|acceptance)(?:_|$)", name, re.I)):
            raise ValueError("An explicitly isolated test MySQL database is required")
        env["LEARNING_TEST_MYSQL_URL"] = mysql_url
        port = parsed.port or 3306
        try:
            addresses = {(entry[4][0], entry[4][1]) for entry in
                         socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)}
        except OSError:
            raise ValueError("Cannot resolve the explicitly isolated test MySQL host") from None
        addresses.add((parsed.hostname, port))
        env["KNOWPATH_AGENT_ALLOWED_ADDRESSES"] = json.dumps(sorted(addresses))
    return env


def select_tests(suite: str) -> list[Path]:
    if suite == "full":
        return sorted(TESTS.rglob("test_*.py"))
    if suite == "agent":
        return sorted(TESTS.glob("test_web_agent_*.py"))
    selected = {TESTS / name for name in BASELINE}
    if suite == "compatibility":
        for pattern in COMPATIBILITY_PATTERNS:
            selected.update(TESTS.glob(pattern))
    elif suite != "baseline":
        raise ValueError("Unknown Agent check suite")
    if any(not path.is_file() for path in selected):
        raise ValueError("A required baseline test file is missing")
    return sorted(selected)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--baseline", action="store_true")
    group.add_argument("--suite", choices=("agent", "compatibility", "full"), default="agent")
    parser.add_argument("--test", action="append", help="Test path or node, relative to py/")
    parser.add_argument("--mysql-url", help="Explicit isolated MySQL test URL; never inherited")
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args(argv)
    suite = "baseline" if args.baseline else args.suite
    try:
        targets = args.test or [str(path.relative_to(BACKEND)) for path in select_tests(suite)]
        for target in targets:
            path = (BACKEND / target.split("::", 1)[0]).resolve()
            if not path.is_relative_to(TESTS) or not path.is_file():
                raise ValueError("Test targets must be existing files under knowpath_backend/test")
        root = BACKEND.parent / ".runtime.local/agent-checks"
        root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix=suite + "-", dir=root))
        environment = isolated_environment(directory, mysql_url=args.mysql_url)
    except ValueError as exc:
        parser.error(str(exc))
    (directory / "selection.json").write_text(json.dumps({
        "suite": suite, "tests": targets, "collect_only": args.collect_only,
        "python": sys.version, "mysql_opt_in": bool(args.mysql_url),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Agent checks: {suite}; isolated report directory: {directory}", flush=True)
    command = [sys.executable, "-m", "pytest", "-p", "knowpath_backend.test.web_agent_guard",
               "--basetemp", str(directory / "pytest"), "-q", "-ra", "--tb=short", *targets]
    if args.collect_only:
        command.append("--collect-only")
    return subprocess.run(command, cwd=BACKEND, env=environment).returncode


if __name__ == "__main__":
    raise SystemExit(main())
