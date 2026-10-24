import html
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import urlopen
from xml.sax.saxutils import escape as xml_escape

import yt_dlp
from google import genai

CHANNEL_HANDLE = "@hassanparachaoffical"
DB_FILE = "video_db.json"
SITE_DIR = Path("site")
DEFAULT_SITE_BASE_URL = "https://hassan-paracha.github.io/my-channel-seo/"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
BATCH_SIZE = 40
PAGE_SIZE = 24
MAX_DAILY_GEMINI_REQUESTS = 250
GEMINI_REQUEST_INTERVAL = 6.1
GOOGLE_VERIFICATION_TAG = '<meta name="google-site-verification" content="QyDSUhm4hfRGneKuDBC1rA-p19E7Y2lI_Eh9krE5nVY" />'

def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)
    if not isinstance(db, dict):
        raise ValueError(f"{DB_FILE} must contain a JSON object")
    return db

def save_db(db):
    directory = os.path.dirname(os.path.abspath(DB_FILE))
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, delete=False
        ) as f:
            temp_path = f.name
            json.dump(db, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, DB_FILE)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)

def _youtube_api_get(resource, params):
    query = urlencode({**params, "key": os.environ["YOUTUBE_API_KEY"]})
    try:
        with urlopen(f"https://www.googleapis.com/youtube/v3/{resource}?{query}", timeout=30) as response:
            payload = json.load(response)
    except HTTPError as error:
        payload = json.loads(error.read().decode("utf-8"))
        message = payload.get("error", {}).get("message", "unknown error")
        raise RuntimeError(f"YouTube Data API request failed: {message}") from None
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"YouTube Data API network request failed: {error.reason if isinstance(error, URLError) else 'timeout'}") from None
    if "error" in payload:
        raise RuntimeError(f"YouTube Data API request failed: {payload['error'].get('message', 'unknown error')}")
    return payload


def _get_videos_from_api():
    channel = _youtube_api_get("channels", {
        "part": "contentDetails",
        "forHandle": CHANNEL_HANDLE,
    })
    items = channel.get("items", [])
    if not items:
        raise RuntimeError(f"YouTube channel not found for {CHANNEL_HANDLE}")
    uploads_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    playlist_items = []
    page_token = None
    while True:
        params = {"part": "snippet,contentDetails", "playlistId": uploads_id, "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        page = _youtube_api_get("playlistItems", params)
        playlist_items.extend(page.get("items", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            break

    videos = []
    video_ids = [
        item.get("contentDetails", {}).get("videoId")
        for item in playlist_items
    ]
    for offset in range(0, len(video_ids), 50):
        ids = [video_id for video_id in video_ids[offset:offset + 50] if video_id]
        if not ids:
            continue
        payload = _youtube_api_get("videos", {
            "part": "snippet,contentDetails",
            "id": ",".join(ids),
            "maxResults": 50,
        })
        for item in payload.get("items", []):
            snippet = item.get("snippet", {})
            videos.append({
                "id": item["id"],
                "title": snippet.get("title", "YouTube Video"),
                "description": snippet.get("description", ""),
                "published_at": snippet.get("publishedAt"),
                "duration": item.get("contentDetails", {}).get("duration"),
            })
    return videos


def _get_videos_with_ytdlp():
    base_url = f"https://www.youtube.com/{CHANNEL_HANDLE}/videos"
    with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True}) as ydl:
        channel_info = ydl.extract_info(base_url, download=False)
        channel_id = channel_info.get("id")
        if channel_id and channel_id.startswith("UC"):
            uploads_id = "UU" + channel_id[2:]
            playlist_url = f"https://www.youtube.com/playlist?list={uploads_id}"
            entries = ydl.extract_info(playlist_url, download=False).get("entries", [])
        else:
            entries = channel_info.get("entries", [])
    videos = []
    for entry in entries:
        if not entry or not entry.get("id"):
            continue
        upload_date = entry.get("upload_date")
        videos.append({
            "id": entry["id"],
            "title": entry.get("title") or "YouTube Video",
            "description": entry.get("description") or "",
            "published_at": f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}T00:00:00Z" if upload_date else None,
            "duration": entry.get("duration"),
        })
    return videos


def get_channel_videos():
    """Read the complete uploads playlist from the API, falling back to yt-dlp."""
    if os.getenv("YOUTUBE_API_KEY"):
        return _get_videos_from_api()
    return _get_videos_with_ytdlp()

class GeminiRateLimiter:
    def __init__(self):
        self.last_request = None
        self.request_count = 0

    def wait(self):
        if self.request_count >= MAX_DAILY_GEMINI_REQUESTS:
            raise RuntimeError("Gemini daily request limit reached")
        if self.last_request is not None:
            delay = GEMINI_REQUEST_INTERVAL - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
        self.last_request = time.monotonic()
        self.request_count += 1


def parse_polished_response(response_text, expected_ids):
    """Parse a structured Gemini response and require an exact ID match."""
    try:
        results = json.loads(response_text)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Gemini returned invalid JSON") from error
    if not isinstance(results, list) or len(results) != len(expected_ids):
        raise ValueError("Gemini response count does not match the requested batch")
    by_id = {}
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("Gemini response entries must be JSON objects")
        video_id, title = result.get("id"), result.get("title")
        if not isinstance(video_id, str) or not isinstance(title, str) or not title.strip():
            raise ValueError("Gemini response entries need a video id and title")
        if video_id in by_id:
            raise ValueError(f"Gemini returned duplicate video id: {video_id}")
        if len(title.strip()) > 70 or '"' in title or "'" in title:
            raise ValueError(f"Gemini returned an invalid title for video {video_id}")
        if len(re.findall(r"#\w+", title)) > 2:
            raise ValueError(f"Gemini returned too many hashtags for video {video_id}")
        by_id[video_id] = title.strip()
    if set(by_id) != set(expected_ids):
        raise ValueError("Gemini response IDs do not match the requested batch")
    return [{"id": video_id, "title": by_id[video_id]} for video_id in expected_ids]


def _retryable_gemini_error(error):
    status = getattr(error, "status_code", None) or getattr(error, "code", None)
    response = getattr(error, "response", None)
    status = status or getattr(response, "status_code", None)
    if isinstance(status, str) and status.isdigit():
        status = int(status)
    if status in (429, 500, 502, 503, 504):
        return True
    return "429" in type(error).__name__ or "5xx" in type(error).__name__.lower()


def polish_batch(client, batch, limiter):
    expected_ids = [video_id for video_id, _ in batch]
    items = [{"id": video_id, "title": video["original_title"]} for video_id, video in batch]
    prompt = (
        "Polish these YouTube titles for a Pakistani and Indian audience. Keep each title's "
        "meaning, keep Hinglish where present, stay under 70 characters, add one clear "
        "searchable keyword phrase, use at most 2 hashtags, and use no quotation marks. "
        "Never invent facts. Return exactly one object per input with the same id and its "
        "polished title. Input JSON:\n"
        f"{json.dumps(items, ensure_ascii=False)}"
    )
    schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "id": {"type": "STRING"},
                "title": {"type": "STRING"},
            },
            "required": ["id", "title"],
        },
    }
    for attempt in range(4):
        limiter.wait()
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": schema,
                },
            )
            if not response.text:
                raise ValueError("Gemini returned an empty response")
            return parse_polished_response(response.text, expected_ids)
        except Exception as error:
            if attempt == 3 or not _retryable_gemini_error(error):
                raise
            delay = 2 ** (attempt + 1)
            print(f"Gemini request failed ({error}); retrying in {delay}s")
            time.sleep(delay)


def _normalise_published_at(value):
    if not value:
        return None
    if re.fullmatch(r"\d{8}", str(value)):
        value = f"{value[:4]}-{value[4:6]}-{value[6:8]}T00:00:00Z"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sync_database(db, channel_videos):
    active_ids = set()
    for source in channel_videos:
        video_id = source.get("id")
        if not video_id:
            continue
        active_ids.add(video_id)
        source_title = source.get("title") or "YouTube Video"
        video = db.get(video_id)
        if video is None:
            video = {
                "original_title": source_title,
                "title": source_title,
                "polished": False,
                "polished_at": None,
                "published_at": None,
                "description": "",
                "duration": None,
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
            db[video_id] = video
        elif video.get("original_title", video.get("title")) != source_title:
            video["original_title"] = source_title
            video["title"] = source_title
            video["polished"] = False
            video["polished_at"] = None
            video["last_refreshed"] = None
        video.setdefault("original_title", source_title)
        video.setdefault("title", source_title)
        video.setdefault("polished", False)
        video.setdefault("polished_at", None)
        video["description"] = source.get("description", video.get("description", "")) or ""
        video["published_at"] = _normalise_published_at(
            source.get("published_at") or video.get("published_at")
        )
        if source.get("duration") is not None:
            video["duration"] = source["duration"]
        video["url"] = f"https://www.youtube.com/watch?v={video_id}"
        video["removed"] = False
    for video_id, video in db.items():
        if video_id not in active_ids:
            video["removed"] = True


def _polish_unpolished_videos(db):
    candidates = [
        (video_id, video)
        for video_id, video in db.items()
        if not video.get("removed") and not video.get("polished")
    ]
    if not candidates:
        return 0
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required to polish unpolished videos")
    client = genai.Client(api_key=api_key)
    limiter = GeminiRateLimiter()
    failed_batches = 0
    failed_videos = 0
    total_batches = (len(candidates) + BATCH_SIZE - 1) // BATCH_SIZE
    for offset in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[offset:offset + BATCH_SIZE]
        try:
            polished_titles = polish_batch(client, batch, limiter)
        except Exception as error:
            failed_batches += 1
            failed_videos += len(batch)
            print(
                f"Gemini batch {offset // BATCH_SIZE + 1}/{total_batches} failed: "
                f"{type(error).__name__}: {error}"
            )
            continue
        polished_by_id = {item["id"]: item["title"] for item in polished_titles}
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for video_id, video in batch:
            video["title"] = polished_by_id[video_id]
            video["polished"] = True
            video["polished_at"] = now
            video["last_refreshed"] = now[:10]
        save_db(db)
    if failed_batches / total_batches > 0.5:
        raise RuntimeError(
            f"{failed_batches} of {total_batches} Gemini batches failed; "
            "more than half failed"
        )
    return len(candidates) - failed_videos


def _duration_iso8601(duration):
    if duration is None:
        return None
    if isinstance(duration, str) and duration.startswith("P"):
        return duration
    seconds = int(float(duration))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}H")
    if minutes:
        parts.append(f"{minutes}M")
    if seconds or not parts:
        parts.append(f"{seconds}S")
    return "PT" + "".join(parts)


def _json_ld(value):
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def _video_card(video_id, video):
    title = html.escape(str(video.get("title") or "YouTube Video"), quote=True)
    description = html.escape(str(video.get("description") or "")[:240], quote=True)
    video_url = f"https://www.youtube.com/watch?v={quote(video_id, safe='')}"
    thumbnail = f"https://i.ytimg.com/vi/{quote(video_id, safe='')}/hqdefault.jpg"
    embed_url = f"https://www.youtube.com/embed/{quote(video_id, safe='')}"
    video_object = {
        "@context": "https://schema.org",
        "@type": "VideoObject",
        "name": str(video.get("title") or "YouTube Video"),
        "description": str(video.get("description") or ""),
        "thumbnailUrl": thumbnail,
        "embedUrl": embed_url,
    }
    published_at = _normalise_published_at(video.get("published_at"))
    duration = _duration_iso8601(video.get("duration"))
    if published_at:
        video_object["uploadDate"] = published_at
    if duration:
        video_object["duration"] = duration
    return f"""
        <article class="card">
            <script type="application/ld+json">{_json_ld(video_object)}</script>
            <button class="video-facade" type="button" data-video-id="{html.escape(video_id, quote=True)}" aria-label="Play {title}">
                <img src="{html.escape(thumbnail, quote=True)}" alt="{title}" loading="lazy">
                <span aria-hidden="true">▶</span>
            </button>
            <h2>{title}</h2>
            <p class="description">{description}</p>
            <a class="btn" href="{html.escape(video_url, quote=True)}" target="_blank" rel="noopener noreferrer">Watch Video</a>
        </article>"""


def build_html(videos_to_display, *, page_number=1, total_pages=1, page_path="", intro=None, page_title=None):
    base_url = os.getenv("SITE_BASE_URL", DEFAULT_SITE_BASE_URL).rstrip("/") + "/"
    canonical_url = base_url + page_path.lstrip("/")
    heading = page_title or "Hassan Paracha Videos"
    meta_description = intro or "Watch Hassan Paracha's YouTube videos, shorts, family moments, and motorcycle stories."
    cards_html = "".join(_video_card(video_id, video) for video_id, video in videos_to_display)
    if not cards_html:
        cards_html = '<p class="empty">No videos yet — check back soon.</p>'
    pagination = ""
    if total_pages > 1:
        previous_page_path = "../../" if page_number == 2 else f"../{page_number - 1}/"
        previous_page = (
            f'<a href="{previous_page_path}" rel="prev">Previous</a>'
            if page_number > 1 else ""
        )
        next_page_path = f"page/{page_number + 1}/" if page_number == 1 else f"../{page_number + 1}/"
        next_page = (
            f'<a href="{next_page_path}" rel="next">Next</a>'
            if page_number < total_pages else ""
        )
        pagination = f'<nav class="pagination" aria-label="Video pages">{previous_page}<span>Page {page_number} of {total_pages}</span>{next_page}</nav>'
    safe_title = html.escape(f"{heading} | Hassan Paracha", quote=True)
    safe_description = html.escape(meta_description, quote=True)
    safe_canonical = html.escape(canonical_url, quote=True)
    first_thumbnail = ""
    if videos_to_display:
        first_thumbnail = f"https://i.ytimg.com/vi/{quote(videos_to_display[0][0], safe='')}/hqdefault.jpg"
    og_image = f'<meta property="og:image" content="{html.escape(first_thumbnail, quote=True)}">' if first_thumbnail else ""
    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    {GOOGLE_VERIFICATION_TAG}
    <title>{safe_title}</title>
    <meta name="description" content="{safe_description}">
    <link rel="canonical" href="{safe_canonical}">
    <meta property="og:title" content="{safe_title}">
    <meta property="og:description" content="{safe_description}">
    <meta property="og:url" content="{safe_canonical}">
    <meta property="og:type" content="website">
    {og_image}
    <style>
        body {{ font-family: system-ui, sans-serif; background: #f0f2f5; margin: 0; color: #222; }}
        main {{ max-width: 1280px; margin: auto; padding: 24px; }}
        .videos {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 20px; }}
        .card {{ background: #fff; padding: 15px; border-radius: 15px; box-shadow: 0 5px 15px rgba(0,0,0,.05); }}
        .video-facade {{ width: 100%; aspect-ratio: 16 / 9; position: relative; border: 0; padding: 0; cursor: pointer; background: #111; }}
        .video-facade img, .video-facade iframe {{ width: 100%; height: 100%; object-fit: cover; border: 0; border-radius: 10px; }}
        .video-facade span {{ position: absolute; inset: 0; display: grid; place-items: center; color: #fff; font-size: 48px; text-shadow: 0 1px 8px #000; }}
        h1 {{ margin-top: 0; }} h2 {{ font-size: 17px; }}
        .description {{ color: #555; font-size: 14px; }}
        .btn {{ display: block; text-align: center; background: #c00; color: #fff; text-decoration: none; padding: 12px; margin-top: 10px; border-radius: 8px; font-weight: bold; }}
        .pagination {{ display: flex; justify-content: space-between; padding: 24px 0; }}
        .empty {{ padding: 60px; text-align: center; color: #666; }}
    </style>
</head>
<body>
    <main>
        <h1>{html.escape(heading, quote=True)}</h1>
        <p>{html.escape(meta_description, quote=True)}</p>
        <section class="videos" aria-label="Videos">{cards_html}</section>
        {pagination}
    </main>
    <script>
        document.querySelectorAll(".video-facade").forEach(function (button) {{
            button.addEventListener("click", function () {{
                var frame = document.createElement("iframe");
                frame.src = "https://www.youtube-nocookie.com/embed/" + encodeURIComponent(button.dataset.videoId) + "?autoplay=1";
                frame.title = button.getAttribute("aria-label");
                frame.allow = "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share";
                frame.allowFullscreen = true;
                button.replaceChildren(frame);
            }});
        }});
    </script>
</body>
</html>"""


TOPIC_CLUSTERS = (
    ("bike-start", "Motorcycle & Bike Stories", "Motorcycle and bike stories, repairs, and moments when a bike will not start.", re.compile(r"\b(bike|motorcycle|start|chabi|key)\b", re.I)),
    ("family-funny", "Family & Funny Moments", "Funny family, kids, and everyday moments from Hassan's videos.", re.compile(r"\b(family|fun|funny|bach|bache|kids|park|enjoy)\b", re.I)),
    ("celebrations", "Celebrations", "Celebrations, special occasions, and happy moments.", re.compile(r"\b(birthday|eid|wedding|celebration|anniversary|party)\b", re.I)),
)


def _topic_for_title(title):
    for slug, label, intro, pattern in TOPIC_CLUSTERS:
        if pattern.search(title):
            return slug, label, intro
    return None


def generate_site(db):
    videos = [
        (video_id, video)
        for video_id, video in db.items()
        if not video.get("removed")
    ]
    videos.sort(
        key=lambda item: (
            bool(_normalise_published_at(item[1].get("published_at"))),
            _normalise_published_at(item[1].get("published_at")) or "",
        ),
        reverse=True,
    )
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    page_count = max(1, (len(videos) + PAGE_SIZE - 1) // PAGE_SIZE)
    pages = []
    for page_index in range(page_count):
        page_number = page_index + 1
        page_path = "" if page_number == 1 else f"page/{page_number}/"
        output = SITE_DIR / "index.html" if page_number == 1 else SITE_DIR / page_path / "index.html"
        output.parent.mkdir(parents=True, exist_ok=True)
        page_videos = videos[page_index * PAGE_SIZE:(page_index + 1) * PAGE_SIZE]
        heading = "Hassan Paracha Videos" if page_number == 1 else f"Hassan Paracha Videos - Page {page_number}"
        output.write_text(
            build_html(
                page_videos,
                page_number=page_number,
                total_pages=page_count,
                page_path=page_path,
                page_title=heading,
            ),
            encoding="utf-8",
        )
        pages.append(page_path)

    topics = {}
    for video_id, video in videos:
        topic = _topic_for_title(video.get("title") or "")
        if topic:
            topics.setdefault(topic[0], []).append((video_id, video))
    for slug, label, intro, _pattern in TOPIC_CLUSTERS:
        topic_videos = topics.get(slug, [])
        if not topic_videos:
            stale_topic_page = SITE_DIR / "topics" / slug / "index.html"
            if stale_topic_page.exists():
                stale_topic_page.unlink()
            continue
        page_path = f"topics/{slug}/"
        output = SITE_DIR / page_path / "index.html"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            build_html(
                topic_videos,
                page_path=page_path,
                intro=intro,
                page_title=label,
            ),
            encoding="utf-8",
        )
        pages.append(page_path)

    page_directory = SITE_DIR / "page"
    if page_directory.exists():
        for stale_page in page_directory.glob("*/index.html"):
            if stale_page.parent.name.isdigit() and int(stale_page.parent.name) > page_count:
                stale_page.unlink()

    base_url = os.getenv("SITE_BASE_URL", DEFAULT_SITE_BASE_URL).rstrip("/") + "/"
    locations = "".join(
        f"<url><loc>{xml_escape(base_url + path)}</loc></url>"
        for path in pages
    )
    (SITE_DIR / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locations}</urlset>\n",
        encoding="utf-8",
    )
    (SITE_DIR / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {base_url}sitemap.xml\n",
        encoding="utf-8",
    )
    return len(videos), len(pages)


def run_rolling_cycle():
    db = load_db()
    dry_run = os.getenv("DRY_RUN") == "1"
    if not dry_run:
        channel_videos = get_channel_videos()
        for video in db.values():
            if not isinstance(video, dict):
                raise ValueError("Each video database entry must be a JSON object")
            if "original_title" not in video:
                video["last_refreshed"] = None
            video.setdefault("original_title", video.get("title", "YouTube Video"))
            video.setdefault("title", video["original_title"])
            video.setdefault("polished", False)
            video.setdefault("polished_at", None)
        _sync_database(db, channel_videos)
        save_db(db)
        polished_count = _polish_unpolished_videos(db)
        save_db(db)
        print(f"Newly polished videos: {polished_count}")
    else:
        print("DRY_RUN=1: skipping YouTube sync, Gemini calls, and database writes")
    video_count, page_count = generate_site(db)
    print(f"Built site/ with {video_count} videos across {page_count} pages.")


if __name__ == "__main__":
    run_rolling_cycle()
