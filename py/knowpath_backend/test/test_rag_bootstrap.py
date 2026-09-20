from copy import deepcopy
from hashlib import sha256
import json

import pytest
from qdrant_client import QdrantClient

from knowpath_backend.learning.config import LearningSettings
from knowpath_backend.learning.materials.service import MaterialParser
from knowpath_backend.rag_eval import bootstrap


class LocalEmbeddings:
    dimension = 4
    model_version = "bootstrap-local-test"

    def __init__(self):
        self.calls = 0
        self.fail = False
        self.last_usage = None

    def embed(self, texts, *, query=False):
        self.calls += 1
        self.last_usage = None
        if self.fail:
            raise RuntimeError("private-key-provider-message")
        self.last_usage = {"model": self.model_version, "input_tokens": 17, "calls": 1, "complete": True}
        return [[float(x+1) for x in sha256(t.encode()).digest()[:4]] for t in texts]


@pytest.fixture
def local_setup(tmp_path, monkeypatch):
    samples, dataset, output = tmp_path / "samples", tmp_path / "draft", tmp_path / "output"
    samples.mkdir()
    (dataset / "sources").mkdir(parents=True)
    content = b"# Alpha\nFirst paragraph.\n\n# Beta\nSecond paragraph."
    (samples / "sample.md").write_bytes(content)
    original = MaterialParser().parse(content, filename="sample.md")
    spans = []
    for ordinal, chunk in enumerate(original):
        relative = f"sources/{ordinal}.txt"
        (dataset / relative).write_bytes(chunk.text.encode())
        spans.append(dict(source_filename="sample.md", source_ordinal=ordinal, raw_source_sha256=sha256(content).hexdigest(),
            artifact_hash=chunk.content_hash, artifact_path=relative, page=chunk.page, block=chunk.id,
            material_version_id="draft-version", material_id="draft-material", start=0, end=len(chunk.text)))
    row = dict(question_id="q1", family_id="family", question="What comes first?", source_filename="sample.md",
        allowed_source_spans=spans, excluded_source_spans=[], necessary_evidence=[dict(spans[0], quote=original[0].text)],
        scope_snapshot_id="draft-scope", human_review_status="pending")
    (dataset / "dataset.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (dataset / "split.json").write_text(json.dumps({"dev": {"question_ids": ["q1"]}, "heldout": {"question_ids": []}}), encoding="utf-8")
    (dataset / "rubric.md").write_text("Human review required.", encoding="utf-8")
    embedder, configurations = LocalEmbeddings(), []
    monkeypatch.setattr(LearningSettings, "from_env", classmethod(lambda cls: LearningSettings(embedding_dimension=4)))
    monkeypatch.setattr("knowpath_backend.learning.providers.models.embedding_model", lambda settings: embedder)
    def qdrant(**configuration):
        configurations.append(configuration)
        return QdrantClient(path=str(tmp_path / "local-qdrant"))
    monkeypatch.setattr(bootstrap, "QdrantClient", qdrant)
    monkeypatch.setenv("QDRANT_API_KEY", "private-qdrant-key")
    return samples, dataset, output, row, embedder, configurations


@pytest.mark.parametrize("change", ["missing_artifact", "gap", "bad_page", "extra_ordinal"])
def test_whole_document_scope_rejected_before_embedding(local_setup, change):
    samples, dataset, output, row, embedder, _ = local_setup
    row = deepcopy(row)
    if change == "missing_artifact":
        row["allowed_source_spans"].pop()
    elif change == "gap":
        row["allowed_source_spans"][0]["start"] = 1
    elif change == "bad_page":
        row["allowed_source_spans"][0]["page"] = 2
    else:
        row["allowed_source_spans"][0]["source_ordinal"] = -1
    (dataset / "dataset.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="whole-document"):
        bootstrap.prepare(samples, dataset, output)
    assert embedder.calls == 0
    setup = json.loads((output / "setup.json").read_text(encoding="utf-8"))
    assert setup["status"] == "building"
    assert setup["attempts"][-1]["status"] == "failed"
    assert setup["build_records"][-1]["status"] == "failed"


def test_partial_export_resume_keeps_history_and_reused_usage(local_setup, monkeypatch):
    samples, dataset, output, row, embedder, configurations = local_setup
    original_export = bootstrap.export_bytes
    interrupted = False
    def fail_once(path, content, *, resume):
        nonlocal interrupted
        if path.name == "split.json" and not interrupted:
            interrupted = True
            raise OSError("private-export-error")
        return original_export(path, content, resume=resume)
    monkeypatch.setattr(bootstrap, "export_bytes", fail_once)
    with pytest.raises(OSError):
        bootstrap.prepare(samples, dataset, output)
    before = json.loads((output / "setup.json").read_text(encoding="utf-8"))
    assert before["attempts"][-1]["status"] == "failed"
    assert before["build_records"][0]["embedding_usage"][0]["input_tokens"] == 17
    assert before["build_records"][0]["reused_index"] is False
    assert configurations[0]["api_key"] == "private-qdrant-key"
    assert "private-export-error" not in json.dumps(before) and "private-qdrant-key" not in json.dumps(before)
    calls = embedder.calls
    bootstrap.prepare(samples, dataset, output, resume=True)
    after = json.loads((output / "setup.json").read_text(encoding="utf-8"))
    assert after["status"] == "ready" and len(after["attempts"]) == 2
    assert after["build_records"][:len(before["build_records"])] == before["build_records"]
    assert after["build_records"][-1]["reused_index"] is True
    assert after["build_records"][-1]["embedding_usage"] == before["build_records"][0]["embedding_usage"]
    assert after["build_records"][-1]["embedding_attempts"] == []
    assert embedder.calls == calls
    assert (output / "development.json").exists() and (output / "split.json").exists()


def test_failed_embedding_attempt_is_journaled_with_unknown_bill(local_setup):
    samples, dataset, output, _, embedder, _ = local_setup
    embedder.fail = True
    with pytest.raises(Exception):
        bootstrap.prepare(samples, dataset, output)
    setup = json.loads((output / "setup.json").read_text(encoding="utf-8"))
    record = setup["build_records"][-1]
    assert record["status"] == "failed" and record["seconds"] >= 0
    failed = record["embedding_attempts"][-1]
    assert failed["status"] == "failed" and failed["billing_status"] == "unknown"
    assert failed["bill_amount"] is None
    assert "private-key-provider-message" not in json.dumps(setup)


def test_ready_workspace_rejection_does_not_open_db_or_client(local_setup, monkeypatch):
    samples, dataset, output, _, _, configurations = local_setup
    bootstrap.prepare(samples, dataset, output)
    before = {p.name: p.read_bytes() for p in output.iterdir() if p.is_file()}
    count = len(configurations)
    monkeypatch.setattr(bootstrap.sa, "create_engine", lambda *a, **k: pytest.fail("ready workspace opened"))
    with pytest.raises(ValueError, match="unfinished"):
        bootstrap.prepare(samples, dataset, output, resume=True)
    assert len(configurations) == count
    assert {p.name: p.read_bytes() for p in output.iterdir() if p.is_file()} == before


def test_initial_client_failure_can_resume_without_losing_attempt(local_setup, monkeypatch):
    samples, dataset, output, _, _, _ = local_setup
    original_client = bootstrap.QdrantClient
    def fail_client(**kwargs):
        raise OSError("private-client-error")
    monkeypatch.setattr(bootstrap, "QdrantClient", fail_client)
    with pytest.raises(OSError):
        bootstrap.prepare(samples, dataset, output)
    before = json.loads((output / "setup.json").read_text(encoding="utf-8"))
    assert before["attempts"][-1]["status"] == "failed"
    assert "private-client-error" not in json.dumps(before)
    monkeypatch.setattr(bootstrap, "QdrantClient", original_client)
    bootstrap.prepare(samples, dataset, output, resume=True)
    after = json.loads((output / "setup.json").read_text(encoding="utf-8"))
    assert after["status"] == "ready" and after["attempts"][0] == before["attempts"][0]
    assert len(after["attempts"]) == 2


def test_adjacent_spans_covering_whole_document_are_accepted(local_setup):
    samples, dataset, output, row, _, _ = local_setup
    whole = row["allowed_source_spans"].pop(0)
    midpoint = whole["end"] // 2
    row["allowed_source_spans"].extend([dict(whole, end=midpoint), dict(whole, start=midpoint)])
    (dataset / "dataset.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    bootstrap.prepare(samples, dataset, output)
    assert json.loads((output / "setup.json").read_text(encoding="utf-8"))["status"] == "ready"
