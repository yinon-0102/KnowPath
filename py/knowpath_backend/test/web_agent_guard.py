"""Offline pytest plugin, loaded before application collection by the launcher."""
import json
import os
from pathlib import Path
import socket
import threading

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_socketpair = socket.socketpair
_internal_pair = threading.local()
_collected = []
_outcomes = []
_collection_errors = []


def check_address(address, *, allowed):
    if not isinstance(address, tuple) or tuple(address[:2]) not in allowed:
        raise RuntimeError("Network access blocked by offline Agent checks; inject a fake model or opt in to isolated MySQL")


def pytest_configure(config):
    allowed = tuple(tuple(value) for value in json.loads(os.getenv("KNOWPATH_AGENT_ALLOWED_ADDRESSES", "[]")))

    def connect(sock, address):
        if not getattr(_internal_pair, "active", False):
            check_address(address, allowed=allowed)
        return _original_connect(sock, address)

    def connect_ex(sock, address):
        check_address(address, allowed=allowed)
        return _original_connect_ex(sock, address)

    def socketpair(*args, **kwargs):
        # Windows' stdlib fallback connects two fresh loopback sockets. Only
        # that trusted operation on this thread is exempt; localhost remains
        # blocked for ordinary model/database connections.
        previous = getattr(_internal_pair, "active", False)
        _internal_pair.active = True
        try:
            return _original_socketpair(*args, **kwargs)
        finally:
            _internal_pair.active = previous

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socketpair = socketpair
    _collected.clear()
    _outcomes.clear()
    _collection_errors.clear()


def pytest_collection_finish(session):
    _collected.extend(item.nodeid for item in session.items)


def pytest_collectreport(report):
    if report.failed:
        _collection_errors.append(report.nodeid)


def pytest_runtest_logreport(report):
    if report.when == "call" or report.failed or report.skipped:
        item = {"nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome}
        if report.skipped:
            item["reason"] = str(report.longrepr[-1]) if isinstance(report.longrepr, tuple) else str(report.longrepr)
        _outcomes.append(item)


def pytest_sessionfinish(session, exitstatus):
    target = os.getenv("KNOWPATH_AGENT_CHECK_REPORT")
    if target:
        Path(target).write_text(json.dumps({
            "collected": _collected, "collection_errors": _collection_errors,
            "outcomes": _outcomes, "exitstatus": int(exitstatus),
            "collect_only": session.config.option.collectonly,
        }, indent=2, ensure_ascii=False), encoding="utf-8")


def pytest_unconfigure(config):
    socket.socket.connect = _original_connect
    socket.socket.connect_ex = _original_connect_ex
    socket.socketpair = _original_socketpair
