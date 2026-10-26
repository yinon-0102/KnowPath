"""Read-only R0 compatibility metadata and fail-closed projection adapter.

R0 is deliberately independent of the Task1-3 memory implementation. It reads
only the legacy projection columns and never registers or imports new memory
tables. The manifest is emitted by the release process and is auditable.
"""
from dataclasses import dataclass
from hashlib import sha256
import json


@dataclass(frozen=True)
class R0Manifest:
    baseline_commit: str
    patch_commit: str
    artifact_sha256: str
    schema_range: str

    def to_dict(self): return self.__dict__.copy()


def manifest(*, baseline_commit, patch_commit, artifact_bytes, schema_range):
    if not all(isinstance(v, str) and v for v in (baseline_commit, patch_commit, schema_range)):
        raise ValueError('R0 manifest identities are required')
    return R0Manifest(baseline_commit, patch_commit,
        sha256(bytes(artifact_bytes)).hexdigest(), schema_range)


def read_legacy_projection(row, *, current_scope, source_exists, tombstoned):
    """Return a detached candidate only when complete legacy provenance survives."""
    try:
        if not isinstance(row, dict) or row.get('space_id') != current_scope['space_id']:
            return None
        provenance = row.get('projection_provenance')
        if not isinstance(provenance, dict) or provenance.get('quarantined'):
            return None
        dependencies = provenance.get('source_dependencies')
        if not isinstance(dependencies, list) or not dependencies:
            return None
        for dependency in dependencies:
            if not isinstance(dependency, dict) or not source_exists(dependency) or tombstoned(dependency):
                return None
        if row.get('scope_fingerprint') != current_scope.get('fingerprint'):
            return None
        return json.loads(json.dumps(row, ensure_ascii=False))
    except Exception:
        return None


def incremental_backfill_boundary(*, manifest_record, expected_baseline):
    """Validate an R0 artifact before Task11 supplies full incremental backfill."""
    if not isinstance(manifest_record, dict) or manifest_record.get('baseline_commit') != expected_baseline:
        raise ValueError('R0 artifact baseline mismatch')
    required = {'baseline_commit', 'patch_commit', 'artifact_sha256', 'schema_range'}
    if not required <= manifest_record.keys(): raise ValueError('Incomplete R0 manifest')
    return dict(manifest_record)
