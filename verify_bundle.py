"""Verify the transferred source bundle without importing app dependencies."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent
MANIFEST=ROOT/'MANIFEST.json'

def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def main():
    expected=json.loads(MANIFEST.read_text(encoding='utf-8'))['files']
    changed=[name for name,digest in expected.items() if not (ROOT/name).is_file() or sha(ROOT/name)!=digest]
    forbidden=[str(ROOT/name) for name in ('.env','.venv','pdf','.heavy','recordings','outputs','data/classroom_records','frontend/node_modules') if (ROOT/name).exists()]
    print(json.dumps({'checked_files':len(expected),'changed_files':changed,'extra_local_paths_ignored':forbidden},ensure_ascii=False))
    return 1 if changed else 0

if __name__=='__main__':raise SystemExit(main())
