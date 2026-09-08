"""Check new evidence consistency and record fresh delivery checks."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

out = Path(__file__).resolve().parent
repo = out.parents[2]
python = str(repo / '.venv' / 'Scripts' / 'python.exe')
for path in out.glob('*.json'):
    json.loads(path.read_text(encoding='utf-8'))
for path in out.glob('*.jsonl'):
    for line in path.read_text(encoding='utf-8').splitlines():
        json.loads(line)
for item in json.loads((out / 'visual_review.json').read_text())['files']:
    assert hashlib.sha256((out / item['file']).read_bytes()).hexdigest() == item['sha256']
for filename in ('31-failures-page2.dom.json', '36-retained-500-page2.dom.json'):
    data = json.loads((out / filename).read_text(encoding='utf-8'))
    for side in ('before', 'after'):
        state = data[side]
        if filename.startswith('31'):
            assert '第 2 / 2 页' in state['pagination'] and len(state['rows']) == 1
            assert state['rows'][0]['failure_id'] == '1'
        else:
            assert state['page'] == '2' and '第 2 / 10 页' in state['pagination']
            assert [int(row[0]) for row in state['rows']] == list(range(51, 101))
for filename, total in [('33-outbox-replaced-final.dom.json', 1), ('35-mixed-replaced-final.dom.json', 3)]:
    for state in (json.loads((out / filename).read_text(encoding='utf-8'))[side] for side in ('before', 'after')):
        assert state['stats']['total'] == total and state['stats']['failed'] == 0
        assert state['unresolved_failures'] == 0 and len(state['image_rows']) == total
for doc in [repo / 'docs/PHASE_2_VALIDATION_REPORT.md', repo / 'docs/PHASE_2_BROWSER_ACCEPTANCE.md',
            out / 'README.md', out.parent / 'issue4-browser-20260908/README.md']:
    for link in re.findall(r'\]\(([^)]+)\)', doc.read_text(encoding='utf-8')):
        if '://' not in link and not link.startswith('#'):
            assert (doc.parent / link.split('#')[0]).exists(), (doc, link)

commands = [
    [python, '-m', 'compileall', '-q', 'app.py', 'config.py', 'database.py', 'storage.py', 'worker.py', 'scripts', 'tests', str(out)],
    ['node', '--check', 'static/batch_upload.js'],
    ['node', '--check', 'static/batch_upload_state.js'],
    ['node', '--check', 'static/batch_images.js'],
    [python, '-m', 'pip', 'check'],
    ['git', 'diff', '--check'],
    ['git', 'diff', '--exit-code', '7b71407', '--', 'app.py', 'config.py', 'database.py', 'storage.py', 'worker.py', 'static', 'templates', 'scripts', 'tests'],
]
records = []
for command in commands:
    started = datetime.now(timezone.utc).isoformat()
    result = subprocess.run(command, cwd=repo, capture_output=True, text=True, errors='replace')
    records.append({'command': [s.replace(str(repo), '<repo>') for s in command],
        'started_at_utc': started, 'ended_at_utc': datetime.now(timezone.utc).isoformat(),
        'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
with (out / 'delivery_checks.json').open('x', encoding='utf-8') as output:
    json.dump({'evidence_assertions': 'passed', 'runs': records}, output, ensure_ascii=False, indent=2)
print(json.dumps({'evidence_assertions': 'passed', 'exit_codes': [r['exit_code'] for r in records]}))
raise SystemExit(any(r['exit_code'] for r in records))
