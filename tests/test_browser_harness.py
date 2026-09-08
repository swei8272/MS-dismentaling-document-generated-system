import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from phase2_browser_harness import create_harness


def test_browser_harness_requires_explicit_synthetic_marker(tmp_path):
    with pytest.raises(ValueError, match='marker'):
        create_harness(tmp_path)
    assert not (tmp_path / 'data').exists()


def test_faults_are_bounded_and_can_be_reset(tmp_path):
    (tmp_path / 'SYNTHETIC_BROWSER_VALIDATION').touch()
    app = create_harness(tmp_path)
    client = app.test_client()
    assert client.post('/validation/control', data={'action': 'retry'}).status_code == 302
    for _ in range(3):
        assert client.post('/batches/1/upload').status_code == 503
    assert client.post('/batches/1/upload').status_code != 503
    client.post('/validation/control', data={'action': 'list-error'})
    assert client.get('/batches/1/images').status_code == 503
    client.post('/validation/control', data={'action': 'reset'})
    assert client.get('/batches/1/images').status_code != 503
    events = client.get('/validation/events').json['events']
    assert sum(event['kind'] == 'synthetic_503' for event in events) == 4
    assert all(event['active'] == 1 for event in events if event['kind'] == 'upload_start')


def test_response_suppression_occurs_after_commit_and_replay_preserves_completed(tmp_path):
    from database import create_batch, database_connection
    from tests.conftest import image_bytes
    from tests.test_phase2_uploads import json_upload

    (tmp_path / 'SYNTHETIC_BROWSER_VALIDATION').touch()
    app = create_harness(tmp_path)
    client = app.test_client()
    db_path = tmp_path / 'data' / 'validation.db'
    batch = create_batch(db_path)
    client.post('/validation/control', data={'action': 'response-loss'})
    files = [(image_bytes(), 'synthetic.png')]
    assert json_upload(client, batch['id'], files, ['one']).status_code == 503
    replay = json_upload(client, batch['id'], files, ['one'])
    assert replay.status_code == 200
    assert replay.json['results'][0]['status'] == 'already_in_batch'
    with database_connection(db_path) as db:
        assert db.execute('SELECT COUNT(*) FROM evidence').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM batch_images').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM upload_receipts').fetchone()[0] == 1
        assert tuple(db.execute('SELECT processing_status, attempt_count FROM evidence').fetchone()) == ('completed', 0)
