"""Archive only marked synthetic fixtures, read SQLite in read-only mode."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

out = Path(__file__).resolve().parent
base = Path(sys.argv[1]).resolve()
roots = ['server-evidence-correction-20260908', 'server-500-final-20260908']
summary = {'application_code_sha': '7b71407bed03eda7a65e4e7803267fba956cd213',
           'checkout_sha': '49a85633ffc454b7b2ed4bf1cc60c3c98799342f',
           'collected_at_utc': datetime.now(timezone.utc).isoformat(), 'roots': []}
for name in roots:
    root = base / name
    if not (root / 'SYNTHETIC_BROWSER_VALIDATION').is_file():
        raise ValueError('Refusing an unmarked root')
    events = [json.loads(line) for line in (root / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    record = {'root': name, 'batches': []}
    with sqlite3.connect((root / 'data' / 'validation.db').as_uri() + '?mode=ro', uri=True) as db:
        for (batch_id,) in db.execute('SELECT id FROM batches ORDER BY id'):
            record['batches'].append({'id': batch_id,
                'images': db.execute('SELECT count(*) FROM batch_images WHERE batch_id=?', (batch_id,)).fetchone()[0],
                'unresolved': db.execute("SELECT count(*) FROM upload_failures WHERE batch_id=? AND status != 'resolved'", (batch_id,)).fetchone()[0],
                'resolved': db.execute("SELECT count(*) FROM upload_failures WHERE batch_id=? AND status = 'resolved'", (batch_id,)).fetchone()[0],
                'failure_ids_and_status': db.execute('SELECT id,status,resolved_evidence_id FROM upload_failures WHERE batch_id=? ORDER BY id', (batch_id,)).fetchall()})
        record['evidence'] = db.execute('SELECT count(*) FROM evidence').fetchone()[0]
        record['sha256_count'] = db.execute('SELECT count(DISTINCT sha256) FROM evidence').fetchone()[0]
        record['receipts'] = db.execute('SELECT count(*) FROM upload_receipts').fetchone()[0]
        record['ocr_states'] = dict(db.execute('SELECT processing_status,count(*) FROM evidence GROUP BY processing_status'))
        record['max_attempt_count'] = db.execute('SELECT max(attempt_count) FROM evidence').fetchone()[0]
        record['foreign_key_errors'] = db.execute('PRAGMA foreign_key_check').fetchall()
    record['formal_disk_files'] = sum(p.is_file() and '.incoming' not in p.parts for p in (root / 'uploads').rglob('*'))
    starts = [e for e in events if e['kind'] == 'upload_start']
    record['upload_starts_all_time'] = len(starts)
    record['upload_starts_since_20260908_utc'] = sum(e['utc'] >= '2026-09-08' for e in starts)
    if name == roots[0]:
        with (out / 'correction_events.jsonl').open('xb') as output:
            output.write((root / 'events.jsonl').read_bytes())
    summary['roots'].append(record)
with (out / 'isolated_state.json').open('x', encoding='utf-8') as output:
    json.dump(summary, output, ensure_ascii=False, indent=2)

# This records the actual manual visual review performed in this turn, not a
# machine inference from filenames. Each image was reopened using view_image.
findings = {
    '31-failures-page2.png': 'Visible page 2/2, exactly one failure row, ID 1; total 26.',
    '32-outbox-saved-unresolved.png': 'Saved metadata badge; saved images 0, one unresolved failure, ID 27.',
    '33-outbox-replaced-final.png': 'Saved 1; local failed/unconfirmed 0; one image row; unresolved panel hidden (DOM 0).',
    '34-mixed-saved2-failed1.png': 'Saved 2, local failed 1, failure ID 28, OCR failed 0, two image rows.',
    '35-mixed-replaced-final.png': 'Saved 3, confirmed 3, local failed 0, OCR failed 0, three image rows; unresolved panel hidden (DOM 0).',
    '36-retained-500-page2-full.png': 'Saved 500; full table contains 50 rows, sequence 51 through 100; footer page 2/10.'}
review = {'review_recorded_at_utc': datetime.now(timezone.utc).isoformat(),
          'method': 'All six saved PNG files were reopened with view_image and visually checked by the agent in this turn; timestamp is review record creation, not historical capture time.',
          'files': [{'file': name, 'sha256': hashlib.sha256((out / name).read_bytes()).hexdigest(),
                     'size_bytes': (out / name).stat().st_size, 'visual_result': result}
                    for name, result in findings.items()]}
with (out / 'visual_review.json').open('x', encoding='utf-8') as output:
    json.dump(review, output, indent=2)
print(json.dumps(summary, ensure_ascii=False, indent=2))
