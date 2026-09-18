import pytest

from my_agent_llms.learning.materials import (
    IdempotencyConflict,
    InMemoryMaterialRepository,
    MaterialParser,
    MaterialService,
    UnsupportedMaterial,
)


def test_parser_preserves_markdown_sections_and_source_lines():
    parser = MaterialParser()

    chunks = parser.parse(
        b"# Functions\n\nA function groups reusable behavior.\n\n## Parameters\n\nParameters receive values.",
        filename="python.md",
    )

    assert [chunk.section_path for chunk in chunks] == [("Functions",), ("Functions", "Parameters")]
    assert chunks[0].text == "A function groups reusable behavior."
    assert chunks[0].line_start == 3
    assert chunks[1].line_start == 7


def test_material_service_deduplicates_retries_by_idempotency_key():
    service = MaterialService(InMemoryMaterialRepository())

    first = service.create(
        filename="python.txt",
        content=b"functions and parameters",
        idempotency_key="upload-1",
    )
    retry = service.create(
        filename="python.txt",
        content=b"functions and parameters",
        idempotency_key="upload-1",
    )

    assert retry.material.id == first.material.id
    assert retry.version.id == first.version.id
    assert retry.replayed is True


def test_material_service_rejects_reusing_key_for_different_payload():
    service = MaterialService(InMemoryMaterialRepository())
    service.create(filename="a.txt", content=b"a", idempotency_key="same-key")

    with pytest.raises(IdempotencyConflict):
        service.create(filename="b.txt", content=b"b", idempotency_key="same-key")


def test_material_service_rejects_unsupported_extension():
    service = MaterialService(InMemoryMaterialRepository())

    with pytest.raises(UnsupportedMaterial):
        service.create(filename="notes.docx", content=b"text", idempotency_key="doc-1")


def test_material_service_adds_an_immutable_version_to_existing_material():
    repository = InMemoryMaterialRepository()
    service = MaterialService(repository)
    first = service.create(filename="python.md", content=b"# One\n\nFirst", idempotency_key="v1")

    second = service.create_version(
        material_id=first.material.id,
        filename="python-v2.md",
        content=b"# Two\n\nSecond",
        idempotency_key="v2",
    )

    assert second.material.id == first.material.id
    assert second.version.id != first.version.id
    assert second.material.current_version_id == second.version.id
    assert len(repository.list_versions(first.material.id)) == 2
