from pathlib import Path
import json
import shutil
import subprocess
import sys
import time
import uuid

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root))
from tests.fixtures.file_view import CommandClient
from redpanda.filesystem.file_view.client import native_executable
from redpanda.filesystem.file_view import publication as pub

destination = root / '.tools' / ('checkpoint-git-' + uuid.uuid4().hex[:8])
destination.mkdir()
results = []
powershell = shutil.which('pwsh') or shutil.which('powershell')
git = shutil.which('git')
if '--normal' in sys.argv:
    original_popen = subprocess.Popen
    def without_extended_store(argv, *args, **kwargs):
        if len(argv) == 3 and str(argv[0]) == str(native_executable()):
            argv = [argv[0], str(argv[1]).removeprefix('\\\\?\\'), argv[2]]
        return original_popen(argv, *args, **kwargs)
    subprocess.Popen = without_extended_store
print(destination, flush=True)
for i in range(int(sys.argv[1]) if len(sys.argv)>1 else 60):
    base = destination / str(i) / 'base'; base.mkdir(parents=True)
    store = base.parent / 'store'
    started = time.monotonic()
    row = {'iteration': i, 'store': str(store)}
    try:
        with CommandClient(native_executable(), store, base) as client:
            for name, argv in (
                ('ps', [powershell, '-NoProfile', '-NonInteractive', '-Command', "Set-Content -LiteralPath hello.py -Value 'print(42)' -Encoding utf8"]),
                ('compile', [sys.executable, '-m', 'compileall', '-q', '.']),
                ('git', [git, 'init', '-q']),
            ):
                row['phase'] = name
                result = client.run(name, argv)
                assert result['execution']['exit_code'] == 0, result
                assert result['files']['status'] == 'sealed', result
                client.request('accept', command_id=name)
            row['phase'] = 'publish'
            assert pub.publish(client)['state'] == 'complete'
        row['status'] = 'passed'
    except Exception as error:
        row['status'] = 'failed'; row['error'] = repr(error)
        execution = store/'commands'/row['phase']/'execution.json'
        if execution.is_file(): row['execution'] = json.loads(execution.read_text(encoding='utf-8'))
    row['seconds'] = time.monotonic() - started
    results.append(row)
    (destination/'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(row, ensure_ascii=False), flush=True)
    if row['status'] == 'failed': break
