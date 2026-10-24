import json
import re
from types import SimpleNamespace

import pytest

import engine


def test_database_load_save_and_corrupt_json_safety(tmp_path, monkeypatch):
    database_file = tmp_path / "video_db.json"
    monkeypatch.setattr(engine, "DB_FILE", str(database_file))
    records = {"video": {"title": "Original", "polished": False}}

    engine.save_db(records)
    assert engine.load_db() == records

    corrupt_contents = '{"video":'
    database_file.write_text(corrupt_contents, encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        engine.load_db()
    assert database_file.read_text(encoding="utf-8") == corrupt_contents
    assert list(tmp_path.iterdir()) == [database_file]


def test_polished_batch_requires_matching_ids_and_valid_titles():
    result = engine.parse_polished_response(
        '[{"id":"a","title":"Bike start problem fix"},{"id":"b","title":"Family funny moment"}]',
        ["a", "b"],
    )
    assert [item["id"] for item in result] == ["a", "b"]
    assert result[0]["title"] == "Bike start problem fix"

    with pytest.raises(ValueError, match="IDs"):
        engine.parse_polished_response('[{"id":"a","title":"Safe title"}]', ["b"])
    with pytest.raises(ValueError, match="count"):
        engine.parse_polished_response("[]", ["a"])


def test_title_batching_uses_one_structured_request_per_forty(monkeypatch):
    requests = []

    class FakeModels:
        def generate_content(self, **kwargs):
            requests.append(kwargs)
            input_json = kwargs["contents"].split("Input JSON:\n", 1)[1]
            items = json.loads(input_json)
            return SimpleNamespace(text=json.dumps([
                {"id": item["id"], "title": f"SEO {item['title']}"}
                for item in items
            ]))

    class FakeLimiter:
        def wait(self):
            pass

    class FakeClient:
        def __init__(self, api_key):
            assert api_key == "test-key"
            self.models = FakeModels()

    monkeypatch.setattr(engine, "BATCH_SIZE", 40)
    monkeypatch.setattr(engine.genai, "Client", FakeClient)
    monkeypatch.setattr(engine, "GeminiRateLimiter", FakeLimiter)
    monkeypatch.setattr(engine, "save_db", lambda _db: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    database = {
        f"video-{index}": {
            "original_title": f"Original {index}",
            "title": f"Original {index}",
            "polished": False,
        }
        for index in range(41)
    }

    assert engine._polish_unpolished_videos(database) == 41
    assert [len(json.loads(request["contents"].split("Input JSON:\n", 1)[1])) for request in requests] == [40, 1]
    assert all(request["config"]["response_mime_type"] == "application/json" for request in requests)
    assert all(video["polished"] for video in database.values())


def test_html_escapes_untrusted_content_and_keeps_valid_json_ld():
    videos = [(
        "video-id",
        {
            "title": '<img src=x onerror="alert(1)">',
            "description": "Description </script><script>alert(1)</script>",
            "published_at": "2025-03-04T12:30:00+05:00",
            "duration": "PT1M2S",
        },
    )]
    page = engine.build_html(videos)

    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in page
    assert "<script>alert(1)</script>" not in page
    assert "<h1>Hassan Paracha Videos</h1>" in page
    json_ld = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>',
        page,
        flags=re.DOTALL,
    )
    assert len(json_ld) == 1
    video_object = json.loads(json_ld[0])
    assert video_object["name"] == '<img src=x onerror="alert(1)">'
    assert video_object["uploadDate"] == "2025-03-04T07:30:00Z"
    assert video_object["description"] == "Description </script><script>alert(1)</script>"
    assert video_object["embedUrl"] == "https://www.youtube.com/embed/video-id"
    assert video_object["thumbnailUrl"].endswith("/video-id/hqdefault.jpg")
    assert video_object["duration"] == "PT1M2S"


def test_paginated_navigation_uses_valid_relative_paths():
    second_page = engine.build_html(
        [], page_number=2, total_pages=3, page_path="page/2/"
    )
    third_page = engine.build_html(
        [], page_number=3, total_pages=3, page_path="page/3/"
    )

    assert 'href="../../" rel="prev"' in second_page
    assert 'href="../3/" rel="next"' in second_page
    assert 'href="../2/" rel="prev"' in third_page
    assert 'rel="next"' not in third_page


def test_database_sync_resets_polish_only_when_original_changes():
    db = {
        "a": {
            "original_title": "Old original",
            "title": "Polished version",
            "polished": True,
            "polished_at": "2025-01-01T00:00:00Z",
        },
    }

    engine._sync_database(db, [{
        "id": "a",
        "title": "Updated YouTube title",
        "published_at": "2025-03-04T12:30:00+05:00",
        "description": "Updated description",
        "duration": "PT4S",
    }])

    assert db["a"]["original_title"] == "Updated YouTube title"
    assert db["a"]["title"] == "Updated YouTube title"
    assert db["a"]["polished"] is False
    assert db["a"]["polished_at"] is None
    assert db["a"]["published_at"] == "2025-03-04T07:30:00Z"
    assert db["a"]["description"] == "Updated description"
