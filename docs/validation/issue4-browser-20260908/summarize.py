"""Recompute synthetic browser measurements; refuses an unmarked database root."""
import collections
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

out = Path(__file__).resolve().parent
root = Path(sys.argv[1]).resolve()
assert (root / 'SYNTHETIC_BROWSER_VALIDATION').is_file()
events = [json.loads(line) for line in (root / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
final = len(sys.argv) > 2 and sys.argv[2] in ('--final', '--verified')
batch = 1 if final else 3
prefix = 'verified' if '--verified' in sys.argv else 'final' if final else 'raw'
monitor = json.loads((out / f'{prefix}_monitor.json').read_text(encoding='utf-8'))
browser = json.loads((out / f'{prefix}_browser_memory.json').read_text(encoding='utf-8'))
parse = datetime.fromisoformat
starts = [e for e in events if e['kind'] == 'upload_start' and e['path'] == f'/batches/{batch}/upload']
ends = [e for e in events if e['kind'] == 'upload_end' and e['path'] == f'/batches/{batch}/upload']
results = [e for e in events if e['kind'] == 'upload_result' and e['payload'].get('batch', {}).get('id') == batch]
start, end = parse(starts[0]['utc']), parse(ends[-1]['utc'])
samples = [s for s in monitor['samples'] if start.timestamp() <= s['at'] <= end.timestamp()]
latencies = sorted(s['latency_ms'] for s in samples)
with sqlite3.connect((root / 'data' / 'validation.db').as_uri() + '?mode=ro', uri=True) as db:
    batches = []
    for (batch_id,) in db.execute('SELECT id FROM batches ORDER BY id'):
        batches.append({'batch_id': batch_id,
            'batch_images': db.execute('SELECT count(*) FROM batch_images WHERE batch_id=?', (batch_id,)).fetchone()[0],
            'receipts': db.execute('SELECT count(*) FROM upload_receipts WHERE batch_id=?', (batch_id,)).fetchone()[0],
            'unresolved_failures': db.execute("SELECT count(*) FROM upload_failures WHERE batch_id=? AND status != 'resolved'", (batch_id,)).fetchone()[0],
            'resolved_failures': db.execute("SELECT count(*) FROM upload_failures WHERE batch_id=? AND status = 'resolved'", (batch_id,)).fetchone()[0]})
    database = {'batches': batches,
        'global_evidence': db.execute('SELECT count(*) FROM evidence').fetchone()[0],
        'unique_sha256': db.execute('SELECT count(DISTINCT sha256) FROM evidence').fetchone()[0],
        'evidence_status': dict(db.execute('SELECT processing_status,count(*) FROM evidence GROUP BY processing_status')),
        'max_attempt_count': db.execute('SELECT max(attempt_count) FROM evidence').fetchone()[0],
        'foreign_key_errors': db.execute('PRAGMA foreign_key_check').fetchall()}
files = list((root / 'uploads').rglob('*'))
summary = {
    'validation_commit_sha': monitor['validation_commit_sha'],
    'production_code_sha': monitor['validation_commit_sha'] if prefix == 'verified' else '0acd823e7e67684ae4fa9d50fa63aed5fd7f8629',
    'generated_at_utc': datetime.now(timezone.utc).isoformat(),
    'browser': 'Chrome 152.0.7977.82, real extension file chooser and page buttons',
    'upload_500': {
        'window_definition': 'server observed first request start through last response end; not browser click-to-render timing',
        'started_at_utc': start.isoformat(), 'ended_at_utc': end.isoformat(),
        'duration_seconds': round((end-start).total_seconds(), 6),
        'groups': len(starts), 'max_active_uploads': max(e['active'] for e in starts),
        'files_per_group': [e['files'] for e in results],
        'net_bytes_per_group': [e['net_bytes'] for e in results],
        'total_bytes': sum(e['net_bytes'] for e in results),
        'result_counts': dict(collections.Counter(r['status'] for e in results for r in e['payload']['results'])),
        'monitor_seconds_before_upload': round((start-parse(monitor['sampling_window']['started_at_utc'])).total_seconds(),6),
        'monitor_seconds_after_upload': round((parse(monitor['sampling_window']['ended_at_utc'])-end).total_seconds(),6),
        'upload_window_status_sample_count': len(samples),
        'upload_window_status_p95_ms': latencies[math.ceil(len(latencies)*.95)-1],
        'upload_window_status_max_ms': max(latencies),
        'upload_window_http_failures': sum(s['http_status'] != 200 for s in samples)},
    'browser_memory': {'scope': browser['scope'], 'method': browser['method'],
        'sample_count': len(browser['samples']), 'first': browser['samples'][0], 'last': browser['samples'][-1],
        'sampled_working_set_max_bytes': max(s['total_working_set_bytes'] for s in browser['samples']),
        'sampled_private_max_bytes': max(s['total_private_bytes'] for s in browser['samples'])},
    'database_final': database,
    'disk_final': {'formal_files': sum(p.is_file() and '.incoming' not in p.parts for p in files),
        'incoming_files': sum(p.is_file() and '.incoming' in p.parts for p in files)},
    'boundary_requests': [{'files': e['files'], 'net_bytes': e['net_bytes']} for e in events if e['kind']=='upload_result' and e['payload'].get('batch',{}).get('id')==4],
    'synthetic_503_upload_times_batch2': [e['utc'] for e in events if e['kind']=='synthetic_503' and e['path']=='/batches/2/upload'],
    'limitations': ['Chrome memory includes unrelated existing tabs; no isolated renderer attribution.',
        'Response loss fixture suppresses committed success with 503; it is not a TCP response drop.',
        'Screenshot 11 was captured before pagination settled; use screenshot 12 for the one-row second page.',
        'replacement-actions.json records only the final 18 replacements after browser runtime recovery; server events include all 26.']}
if final:
    timing = json.loads((out / f'{prefix}_browser_timing.json').read_text(encoding='utf-8'))
    timing['click_request_to_confirmed_observation_seconds'] = (parse(timing['confirmed_500_observed_utc'])-parse(timing['click_requested_utc'])).total_seconds()
    summary['browser_timing'] = timing
    summary['limitations'] = ['Browser timing includes control dispatch and up to approximately one second confirmation observation interval.',
        'Chrome memory includes unrelated existing tabs and extension processes; no isolated renderer attribution.',
        'GetProcessMemoryInfo lifetime high-water mark includes process time before this upload. No tracemalloc was enabled.']
(out / f'{prefix}_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
(out / (f'{prefix}_events.jsonl' if final else 'synthetic_events.jsonl')).write_text('\n'.join(json.dumps(e,ensure_ascii=False) for e in events)+'\n',encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
