"""Append-only paired execution with allowlisted runtime inputs and no retries."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from .dataset import authorize_partition, digest, evaluation_modes, mode_spec, numeric, verify_freeze


def _bindings(values):
    return sorted((item["material_id"], item["material_version_id"], item["graph_version"]) for item in values)


def runtime_binding(config, row):
    mapping = config["runtime_bindings"]
    value = mapping.get(row["question_id"], mapping.get(row["scope_snapshot_id"]))
    required = {"space_id", "scope_snapshot_id", "expected_scope_version", "expected_bindings",
                "expected_manifest_ids", "expected_retrieval_versions", "manifest_configuration_hashes"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("each question requires an explicit real runtime scope/manifest mapping")
    if (not value["expected_manifest_ids"] or not value["expected_retrieval_versions"]
            or set(value["manifest_configuration_hashes"]) != set(value["expected_manifest_ids"])):
        raise ValueError("runtime mapping must freeze manifest and retrieval identities plus configuration hashes")
    return deepcopy(value)


def _check_scope(binding, actual):
    if hasattr(actual, "model_dump"):
        actual = actual.model_dump(mode="json")
    if (actual["space_id"] != binding["space_id"] or actual["scope_snapshot_id"] != binding["scope_snapshot_id"]
            or actual["scope_version"] != binding["expected_scope_version"]
            or _bindings(actual["bindings"]) != _bindings(binding["expected_bindings"])):
        raise ValueError("runtime scope changed")
    manifests = actual["manifests"]
    if (sorted(m["manifest_id"] for m in manifests) != sorted(binding["expected_manifest_ids"])
            or sorted(m["retrieval_version_id"] for m in manifests) != sorted(binding["expected_retrieval_versions"])
            or any(digest(m["configuration"]) != binding["manifest_configuration_hashes"][m["manifest_id"]] for m in manifests)):
        raise ValueError("runtime manifests/profile changed")


def _check_trace(binding, trace):
    if (trace.get("scope_snapshot_id") != binding["scope_snapshot_id"]
            or sorted(trace.get("manifest_ids", [])) != sorted(binding["expected_manifest_ids"])
            or sorted(trace.get("retrieval_versions", [])) != sorted(binding["expected_retrieval_versions"])):
        raise ValueError("runtime response did not use the frozen manifest set")


def _key(record):
    return [record['question_id'], record['plugin'], record['repeat']]


def request_schedule(rows, modes, repeats):
    """The frozen complete sequence; three-mode order rotates once per question."""
    schedule = []
    for index, row in enumerate(rows):
        for repeat in range(repeats):
            offset = (index * repeats + repeat) % len(modes)
            order = tuple(reversed(modes)) if len(modes) == 2 and offset else modes[offset:] + modes[:offset]
            schedule.extend([row['question_id'], mode, repeat] for mode in order)
    return schedule


def _read_lines(path):
    if not Path(path).exists():
        return []
    raw = Path(path).read_text(encoding='utf-8')
    if raw and not raw.endswith('\n'):
        raise ValueError('incomplete journal/record prefix; explicit recovery required')
    try:
        return [json.loads(line) for line in raw.splitlines()]
    except (ValueError, TypeError) as error:
        raise ValueError('invalid journal/record prefix') from error


def _flush(stream):
    stream.flush()
    os.fsync(stream.fileno())


class _AttemptJournal:
    """Durable hash-chained write-ahead attempts with conservative crash recovery."""
    def __init__(self, path, output_path, frozen, partition, schedule, *, resume, snapshot_path=None):
        self.path, self.output_path = Path(path), Path(output_path)
        self.snapshot_path = Path(snapshot_path) if snapshot_path else None
        paths = [self.path.resolve(), self.output_path.resolve()]
        if self.snapshot_path:
            paths.append(self.snapshot_path.resolve())
        if len(set(paths)) != len(paths):
            raise ValueError('journal, output and snapshot paths must be distinct')
        self.previous = None
        self.sequence = 0
        self.records = []
        self.pending = None
        self.snapshot = None
        self.record_previous = None
        self.header = dict(freeze_id=frozen['freeze_id'], partition=partition, schedule_hash=digest(schedule))
        events = _read_lines(self.path) if resume else []
        if not resume:
            if self.output_path.exists() or (self.snapshot_path and self.snapshot_path.exists()):
                raise FileExistsError('evaluation output already exists')
            with self.path.open('x', encoding='utf-8'):
                pass
            self.append('header', **self.header)
            return
        if not self.path.exists() or not events or events[0].get('event') != 'header':
            raise ValueError('resume requires an existing journal header')
        records = _read_lines(self.output_path)
        for index, record in enumerate(records):
            if (index >= len(schedule) or _key(record) != schedule[index]
                    or record.get('freeze_id') != frozen['freeze_id'] or record.get('partition') != partition
                    or record.get('previous_record_hash') != self.record_previous
                    or record.get('record_hash') != digest({k: v for k, v in record.items() if k != 'record_hash'})):
                raise ValueError('record hash or schedule prefix mismatch')
            self.record_previous = record['record_hash']
        completed = 0
        for event in events:
            if (event.get('sequence') != self.sequence or event.get('previous_hash') != self.previous
                    or event.get('event_hash') != digest({k: v for k, v in event.items() if k != 'event_hash'})):
                raise ValueError('journal hash prefix mismatch')
            kind = event.get('event')
            if kind == 'header':
                if self.sequence != 0 or any(event.get(k) != v for k, v in self.header.items()):
                    raise ValueError('journal freeze/schedule prefix mismatch')
            elif kind == 'started':
                if self.pending is not None or completed >= len(schedule) or event.get('key') != schedule[completed]:
                    raise ValueError('attempt schedule prefix mismatch')
                self.pending, self.snapshot = event['record'], None
                if _key(self.pending) != event['key']:
                    raise ValueError('started record key mismatch')
            elif kind == 'snapshot':
                if self.pending is None or event.get('key') != _key(self.pending):
                    raise ValueError('snapshot without matching started attempt')
                self.snapshot = event['snapshot']
            elif kind == 'terminal':
                if (self.pending is None or completed >= len(records)
                        or event.get('key') != _key(self.pending)
                        or event.get('record_hash') != records[completed]['record_hash']):
                    raise ValueError('terminal record prefix mismatch')
                completed += 1
                self.pending, self.snapshot = None, None
            else:
                raise ValueError('unknown journal event')
            self.previous, self.sequence = event['event_hash'], self.sequence + 1
        if len(records) != completed + (1 if self.pending is not None and len(records) > completed else 0):
            raise ValueError('records exist without a durable started attempt')
        # A terminal record may have reached disk immediately before a crash.
        # Its exact hash is validated above; complete its acknowledgement only.
        if len(records) > completed:
            if _key(records[-1]) != _key(self.pending):
                raise ValueError('unacknowledged record key mismatch')
            self.append('terminal', key=_key(records[-1]), record_hash=records[-1]['record_hash'], recovered=True)
            self.pending, self.snapshot = None, None
        self.records = records

    def append(self, event, **data):
        entry = dict(event=event, sequence=self.sequence, previous_hash=self.previous,
                     timestamp=datetime.now(timezone.utc).isoformat(), **data)
        entry['event_hash'] = digest(entry)
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + '\n')
            _flush(stream)
        self.previous, self.sequence = entry['event_hash'], self.sequence + 1

    def persist_snapshot(self, record, snapshot):
        value = deepcopy(snapshot)
        self.append('snapshot', key=_key(record), snapshot=value)
        self.snapshot = value
        if self.snapshot_path:
            with self.snapshot_path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(dict(key=_key(record), snapshot=value), ensure_ascii=False, allow_nan=False) + '\n')
                _flush(stream)

    def write_record(self, stream, record):
        record['previous_record_hash'] = self.record_previous
        record['record_hash'] = digest(record)
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
        _flush(stream)
        self.append('terminal', key=_key(record), record_hash=record['record_hash'])
        self.record_previous = record['record_hash']
        self.pending, self.snapshot = None, None


def run(freeze_path, output_path, *, partition="dev", pipeline_factory, scope_resolver,
        before_request=None, after_record=None, journal_path=None, resume=False, snapshot_path=None):
    frozen, rows, split = verify_freeze(freeze_path)
    config = frozen["config"]
    modes = evaluation_modes(config)
    specs = {mode: mode_spec(mode, config) for mode in modes}
    selected = authorize_partition(frozen, rows, split, partition)
    bindings = {row["question_id"]: runtime_binding(config, row) for row in selected}
    if (resume or snapshot_path) and journal_path is None:
        raise ValueError('resume/snapshots require an explicit journal_path')
    journal = (_AttemptJournal(journal_path, output_path, frozen, partition,
        request_schedule(selected, modes, config['repeats']), resume=resume, snapshot_path=snapshot_path)
        if journal_path else None)
    records = list(journal.records) if journal else []
    # Never overwrite or silently resume an earlier experiment; interruptions
    # leave their rows intact and scoring supplies missing scheduled denominators.
    with Path(output_path).open("a" if resume else "x", encoding="utf-8") as stream:
        if journal and journal.pending is not None:
            unknown = deepcopy(journal.pending)
            unknown.update(attempt_status='unknown', service_success=False,
                error_code='EVALUATION_ATTEMPT_UNKNOWN', response=None, latency_ms=None,
                completed_at=datetime.now(timezone.utc).isoformat(), retrieval_snapshot=journal.snapshot)
            journal.write_record(stream, unknown)
            records.append(unknown)
            if after_record is not None:
                after_record(deepcopy(unknown))
        completed_keys = {tuple(_key(record)) for record in records}
        for question_index, row in enumerate(selected):
            binding = bindings[row["question_id"]]
            for repeat in range(config["repeats"]):
                request_index = question_index * config["repeats"] + repeat
                if len(modes) == 2:
                    # Preserve the historical A/B1 alternation (and the declared
                    # order for a custom two-mode comparison).
                    order = modes if request_index % 2 == 0 else tuple(reversed(modes))
                else:
                    # Rotating the complete mode list gives every mode each
                    # position over a cycle, while retaining a deterministic,
                    # append-only schedule for interrupted runs.
                    offset = request_index % len(modes)
                    order = modes[offset:] + modes[:offset]
                for position, mode in enumerate(order):
                    if (row['question_id'], mode, repeat) in completed_keys:
                        continue
                    if before_request is not None:
                        before_request()
                    started = time.monotonic()
                    record = dict(freeze_id=frozen["freeze_id"], partition=partition,
                        question_id=row["question_id"], family_id=row["family_id"], plugin=mode, repeat=repeat,
                        order=position, service_success=False, error_code=None, error_details={}, answer_status=None,
                        response=None, cost=None, currency=config["pricing"].get("currency"),
                        mode_execution=specs[mode]["execution"], mode_budget=specs[mode]["budget"],
                        mode_fallback_policy=specs[mode].get("fallback"),
                        runtime_binding=binding, evidence_kind="injected_harness" if not getattr(pipeline_factory, "real_runtime", False) else "service_integration")
                    pipeline = None
                    if journal:
                        record.update(attempt_status='started', started_at=datetime.now(timezone.utc).isoformat())
                        journal.append('started', key=_key(record), record=record)
                    try:
                        if (specs[mode]["execution"] == "injected_offline"
                                and getattr(pipeline_factory, "real_runtime", False)):
                            raise ValueError("EVALUATION_MODE_OFFLINE_ONLY")
                        _check_scope(binding, scope_resolver(binding["space_id"]))
                        pipeline = pipeline_factory(mode)
                        if journal:
                            pipeline.retrieval_snapshot_callback = lambda snapshot: journal.persist_snapshot(record, snapshot)
                        # Gold labels, expected statuses, evidence and family metadata
                        # cannot cross this explicit allowlist.
                        history = [{"role": item["role"], "content": item["content"]}
                                   for item in row.get("conversation_history", [])]
                        result = pipeline.answer(row["question"], space_id=binding["space_id"],
                            expected_scope_version=binding["expected_scope_version"],
                            expected_bindings=deepcopy(binding["expected_bindings"]), history=history)
                        _check_scope(binding, scope_resolver(binding["space_id"]))
                        _check_trace(binding, result["trace"])
                        # Keep the ablation contract explicit in injected runs;
                        # this metadata is not a gold label and never reaches
                        # the model request.
                        result.setdefault("trace", {}).setdefault("evaluation_mode", mode)
                        result["trace"].setdefault("mode_execution", specs[mode]["execution"])
                        result["trace"].setdefault("mode_budget", specs[mode]["budget"])
                        if specs[mode]["execution"] == "injected_offline":
                            result["trace"].setdefault("ablation", {
                                "mode": mode, "trace_status": "missing",
                                "fallback": "relation_unavailable",
                            })
                        status = result.get("status")
                        if status not in {"answered", "partial", "insufficient", "clarify", "failed"}:
                            raise ValueError("unknown answer status")
                        record.update(service_success=status != "failed", answer_status=status, response=result)
                        if status == "failed":
                            record["error_code"] = "PIPELINE_FAILED"
                        usage_cost = result["trace"].get("cost")
                        if usage_cost is None:
                            from .costing import estimate
                            usage_cost=estimate(result['trace'],config['pricing'])
                            if usage_cost is not None: result['trace']['cost']=usage_cost
                        if (isinstance(usage_cost, dict) and usage_cost.get("complete") is True
                                and usage_cost.get("currency") == record["currency"]
                                and numeric(usage_cost.get("amount"))):
                            record["cost"] = usage_cost["amount"]
                        # Ensure invalid provider objects cannot break append logging.
                        json.dumps(record, allow_nan=False)
                    except Exception as error:
                        from knowpath_backend.learning.rag.verification import VerificationError, safe_error_details
                        from knowpath_backend.learning.rag.diagnostics import safe_journal_snapshot
                        from knowpath_backend.learning.rag.retrieval import RetrievalError
                        code = error.code if isinstance(error, (VerificationError, RetrievalError)) else (
                            'EVALUATION_MODE_OFFLINE_ONLY' if str(error) == 'EVALUATION_MODE_OFFLINE_ONLY'
                            else 'EVALUATION_REQUEST_FAILED')
                        details = safe_error_details(error.details) if isinstance(error,VerificationError) else {}
                        record.update(service_success=False, error_code=code, error_details=details, response=None)
                        record['call_journal'] = safe_journal_snapshot(getattr(error, 'call_journal', None))
                    finally:
                        if pipeline is not None:
                            snapshot = getattr(pipeline, 'last_retrieval_snapshot', None)
                            if snapshot is not None:
                                record['retrieval_snapshot'] = deepcopy(snapshot)
                        if pipeline is not None and hasattr(pipeline, "close"):
                            try: pipeline.close()
                            except Exception: pass
                    record["latency_ms"] = (time.monotonic() - started) * 1000
                    if journal:
                        record.update(attempt_status='completed', completed_at=datetime.now(timezone.utc).isoformat())
                        journal.write_record(stream, record)
                    else:
                        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                        stream.flush()
                    records.append(record)
                    # Hooks run outside request error conversion. Experiment
                    # stops must preserve the appended attempt and propagate.
                    if after_record is not None:
                        after_record(deepcopy(record))
    return records
