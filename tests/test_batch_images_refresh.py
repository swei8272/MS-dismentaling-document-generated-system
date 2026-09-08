import io

from conftest import image_bytes
from database import create_batch


def test_batch_status_badge_exposes_server_label_map(client, database_path):
    from html import unescape
    import json
    import re

    batch = create_batch(database_path)
    page = client.get(f"/batches/{batch['id']}").get_data(as_text=True)
    labels = json.loads(unescape(re.search(r"data-status-labels='([^']+)'", page).group(1)))
    assert labels['created'] == '已创建'
    assert labels['queued'] == '排队中'
    assert labels['completed'] == '已完成'
    assert 'id="batch-status"' in page


def upload(client, batch_id, start, count):
    for index in range(start, start + count):
        response = client.post(
            f"/batches/{batch_id}/upload",
            data={"files": (io.BytesIO(image_bytes((index, 80, 90))), f"image-{index}.png")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 303


def test_live_table_leaves_empty_state_and_adds_page_without_reload(client, database_path):
    batch_id = create_batch(database_path)["id"]
    url = f"/batches/{batch_id}/images?view=table"
    initial = client.get(url).get_json()
    assert initial["total"] == 0
    assert "这个批次还没有图片" in initial["html"]
    upload(client, batch_id, 0, 50)
    first = client.get(url).get_json()
    assert first["total"] == 50 and first["page_count"] == 1
    assert "这个批次还没有图片" not in first["html"]
    upload(client, batch_id, 50, 1)
    first = client.get(url).get_json()
    assert first["total"] == 51 and first["page_count"] == 2
    assert "下一页" in first["html"]
    second = client.get(url + "&page=2").get_json()
    assert len(second["items"]) == 1
    assert "image-50.png" in second["html"]
    assert "第 2 / 2 页" in second["html"]
    assert "batch-upload-form" not in second["html"]
    assert "type=\"file\"" not in second["html"]


def test_live_table_clamps_page_bounds_and_escapes_filenames(client, database_path):
    batch_id = create_batch(database_path)["id"]
    client.post(f"/batches/{batch_id}/upload", data={
        "files": (io.BytesIO(image_bytes((1, 2, 3))), '<img onerror=alert(1)>.png'),
    }, content_type="multipart/form-data")
    payload = client.get(f"/batches/{batch_id}/images?view=table&page=999&per_page=999").get_json()
    assert payload["page"] == 1 and payload["per_page"] == 100
    assert "<img" not in payload["html"]
    assert "&lt;img" in payload["html"]
    assert client.get("/batches/999999/images?view=table").status_code == 404
