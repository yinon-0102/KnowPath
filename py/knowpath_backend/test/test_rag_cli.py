"""CLI failures stay structured and do not echo connection secrets."""
import json

from knowpath_backend.learning.rag.cli import main


def test_invalid_config_returns_nonzero_without_opening_services(tmp_path, capsys):
    config = tmp_path / "bad.json"
    config.write_text(json.dumps({"space_id": "s", "secret": "do-not-echo"}), encoding="utf-8")
    assert main(["build", "--config", str(config), "--output", str(tmp_path / "manifest.json")]) == 1
    output = capsys.readouterr()
    assert "RAG_CONFIG_INVALID" in output.err and "do-not-echo" not in output.err
    assert not (tmp_path / "manifest.json").exists()


def test_publish_requires_explicit_expected_generation(capsys):
    try:
        main(["publish", "--manifest", "anything.json"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("publication cannot bypass CAS")
