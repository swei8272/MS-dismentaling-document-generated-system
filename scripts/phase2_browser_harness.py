"""Loopback-only synthetic browser acceptance harness, never production startup."""
from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from phase2_validation import PROJECT_ROOT, process_memory_counters
from app import create_app
from database import database_connection
from flask import g, jsonify, redirect, render_template_string, request
from waitress import serve


def create_harness(root: Path):
    root = root.resolve()
    # A dedicated marker prevents accidentally attaching fault injection to a deployment.
    if not (root / 'SYNTHETIC_BROWSER_VALIDATION').is_file():
        raise ValueError('Missing synthetic validation marker')
    app = create_app({'DATABASE_PATH': root / 'data' / 'validation.db',
                      'UPLOAD_DIR': root / 'uploads',
                      'SECRET_KEY': 'synthetic-browser-validation-only'})
    lock = threading.Lock()
    state = {'upload_failures': 0, 'failure_write_failures': 0,
             'image_failures': 0, 'upload_delay': 0, 'image_delay': 0,
             'lose_response': 0, 'active_uploads': 0}
    events = []

    def record(kind, **fields):
        event = {'utc': datetime.now(timezone.utc).isoformat(), 'kind': kind, **fields}
        with lock:
            events.append(event)
            with (root / 'events.jsonl').open('a', encoding='utf-8') as output:
                output.write(json.dumps(event, ensure_ascii=False) + '\n')

    @app.before_request
    def before():
        if request.path.startswith('/validation/'):
            return None
        g.started = time.perf_counter()
        g.upload = request.method == 'POST' and request.path.endswith('/upload')
        if g.upload:
            with lock:
                state['active_uploads'] += 1
                active = state['active_uploads']
            record('upload_start', path=request.path, active=active,
                   content_length=request.content_length)
        key = ('upload_failures' if g.upload else
               'failure_write_failures' if request.method == 'POST' and request.path.endswith('/upload-failures') else
               'image_failures' if request.path.endswith('/images') else None)
        with lock:
            should_fail = bool(key and state[key] > 0)
            if should_fail:
                state[key] -= 1
        if should_fail:
            record('synthetic_503', path=request.path)
            return jsonify(error='合成验收临时错误', retryable=True), 503
        return None

    @app.after_request
    def after(response):
        if not hasattr(g, 'started'):
            return response
        upload = g.upload
        with lock:
            delay = state['upload_delay'] if upload else state['image_delay'] if request.path.endswith('/images') else 0
            lose = upload and response.status_code == 200 and state['lose_response'] > 0
            if lose:
                state['lose_response'] -= 1
        if upload:
            payload = response.get_json(silent=True) or {}
            record('upload_result', status=response.status_code, payload=payload,
                   files=len(request.files.getlist('files')),
                   net_bytes=sum(int(size) for size in request.form.getlist('file_sizes')))
        if delay:
            record('response_delay', path=request.path, seconds=delay)
            time.sleep(delay)
        if lose:
            # Test fixture only: completed evidence must not be queued by replay.
            with database_connection(root / 'data' / 'validation.db') as db:
                db.execute("UPDATE evidence SET processing_status = 'completed' WHERE id IN (SELECT evidence_id FROM batch_images WHERE batch_id = ?)",
                           (int(request.path.split('/')[2]),))
                db.commit()
            response = jsonify(error='合成验收：已提交的成功响应被抑制', retryable=True)
            response.status_code = 503
            record('committed_response_suppressed', path=request.path)
        if upload:
            with lock:
                state['active_uploads'] -= 1
            record('upload_end', path=request.path, status=response.status_code,
                   elapsed_ms=round((time.perf_counter() - g.started) * 1000, 3))
        return response

    @app.route('/validation/control', methods=['GET', 'POST'])
    def control():
        if request.method == 'POST':
            action = request.form.get('action')
            presets = {
                'reset': {}, 'confirm': {'upload_delay': 8},
                'retry': {'upload_failures': 3}, 'list-error': {'image_failures': 100},
                'list-delay': {'image_delay': 5},
                'outbox': {'upload_failures': 100, 'failure_write_failures': 100},
                'response-loss': {'lose_response': 1},
            }
            if action not in presets:
                return 'unknown action', 400
            with lock:
                for key in state:
                    if key != 'active_uploads':
                        state[key] = 0
                state.update(presets[action])
            record('control', action=action)
            return redirect('/validation/control')
        return render_template_string('''<!doctype html><meta charset="utf-8">
            <title>合成浏览器验收控制</title><h1>合成浏览器验收控制</h1>
            <p>仅作用本次隔离数据库和本机回环服务。默认关闭所有注入。</p>
            <form method="post">{% for key,label in actions %}
            <button name="action" value="{{key}}">{{label}}</button>{% endfor %}</form>
            <pre>{{state}}</pre>''', state=json.dumps(state, ensure_ascii=False), actions=[
                ('reset','恢复正常'), ('confirm','上传响应延迟 8 秒'),
                ('retry','接下来 3 次上传返回 503'), ('list-error','图片列表持续 503'),
                ('list-delay','图片列表延迟 5 秒'), ('outbox','上传及失败补写持续 503'),
                ('response-loss','下次提交后抑制成功响应并标记 completed')])

    @app.get('/validation/metrics')
    def metrics():
        return jsonify(**process_memory_counters())

    @app.get('/validation/events')
    def event_list():
        with lock:
            return jsonify(events=list(events))

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('--port', type=int, default=5057)
    args = parser.parse_args()
    serve(create_harness(args.root), host='127.0.0.1', port=args.port, threads=8)
