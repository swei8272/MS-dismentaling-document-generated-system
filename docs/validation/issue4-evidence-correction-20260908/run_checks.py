"""Capture a NEW run's full stdout/stderr, never reconstruct historical logs."""
import json
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone

evidence = Path(__file__).resolve().parent
repo = evidence.parents[2]
python = repo / '.venv' / 'Scripts' / 'python.exe'
started = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
commands = {
    'pytest': [str(python), '-m', 'pytest', '-p', 'no:cacheprovider', '--basetemp', f'.pytest-tmp-evidence-{started}'],
    'javascript': ['node', '--test', 'tests/js/batch_upload_state.test.js', 'tests/js/batch_images.test.js'],
}
env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1'}
records = []
for name, command in commands.items():
    record = {'name': name, 'command': command, 'started_at_utc': datetime.now(timezone.utc).isoformat(),
              'stdout': f'{name}.stdout.txt', 'stderr': f'{name}.stderr.txt'}
    clock = time.perf_counter()
    with (evidence / record['stdout']).open('xb') as stdout, (evidence / record['stderr']).open('xb') as stderr:
        result = subprocess.run(command, cwd=repo, env=env, stdout=stdout, stderr=stderr, check=False)
    record.update(exit_code=result.returncode, duration_seconds=round(time.perf_counter()-clock, 3),
                  ended_at_utc=datetime.now(timezone.utc).isoformat())
    # Store a repo-relative interpreter path, not a personal machine path.
    if name == 'pytest':
        record['command'][0] = '.venv/Scripts/python.exe'
    records.append(record)
    print(json.dumps(record), flush=True)
(evidence / 'test_run.json').write_text(json.dumps({
    'application_code_sha': '7b71407bed03eda7a65e4e7803267fba956cd213',
    'checkout_sha': subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(),
    'historical_output_status': 'No archived complete stdout/stderr files found; this is a new run, not a historical transcript.',
    'runs': records}, indent=2)+'\n', encoding='utf-8')
raise SystemExit(any(record['exit_code'] for record in records))
