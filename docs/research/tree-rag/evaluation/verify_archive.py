"""Verify historical frozen bytes without restoring or executing archived code."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_directory')
    parser.add_argument('--inputs', help='Defaults to this run or its parent frozen-inputs.zip')
    args = parser.parse_args()
    directory = Path(args.run_directory)
    frozen = json.loads((directory / 'freeze.json').read_text(encoding='utf-8'))
    payload = {key: value for key, value in frozen.items() if key != 'freeze_id'}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False,
                         separators=(',', ':')).encode()
    if sha256(encoded).hexdigest() != frozen['freeze_id']:
        raise ValueError('freeze identity mismatch')
    checked = 0
    default_inputs = directory / 'frozen-inputs.zip'
    if not default_inputs.exists():
        default_inputs = directory.parent / 'frozen-inputs.zip'
    with zipfile.ZipFile(directory / 'frozen-code.zip') as code, zipfile.ZipFile(
            args.inputs or default_inputs) as inputs:
        mapping = json.loads(inputs.read('archive-map.json'))
        for original, expected in frozen['file_hashes'].items():
            normalized = original.replace('\\', '/')
            if original in mapping:
                raw = inputs.read(mapping[original]['archive_path'])
                if mapping[original]['sha256'] != expected:
                    raise ValueError('input mapping disagrees with freeze')
            elif '/py/knowpath_backend/' in normalized:
                suffix = normalized.split('/py/knowpath_backend/', 1)[1]
                raw = code.read('py/knowpath_backend/' + suffix)
            elif normalized.rsplit('/', 1)[-1] in {'pyproject.toml', 'uv.lock'}:
                raw = code.read('py/' + normalized.rsplit('/', 1)[-1])
            elif normalized.endswith('/development.json'):
                raw = (directory / 'development.json').read_bytes()
            else:
                raise ValueError('unmapped frozen file: ' + original)
            if sha256(raw).hexdigest() != expected:
                raise ValueError('archived file hash mismatch: ' + original)
            checked += 1
    results = [json.loads(line) for line in (directory / 'dev.jsonl').read_text(
        encoding='utf-8').splitlines() if line.strip()]
    identities = [(row['question_id'], row['plugin'], row['repeat']) for row in results]
    if len(set(identities)) != len(identities) or any(
            row['freeze_id'] != frozen['freeze_id'] for row in results):
        raise ValueError('duplicate or differently frozen result')
    print(json.dumps(dict(freeze_id=frozen['freeze_id'], verified_input_files=checked,
                         result_rows=len(results), assurance='local integrity, not signed attestation')))


if __name__ == '__main__':
    main()
