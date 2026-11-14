"""
engine.py - Hassan Paracha SEO rolling cycle
============================================

Daily, read-only YouTube metadata sync for @hassanparachaoffical:

  1. list the channel's uploads with yt-dlp (no API key, no cookies)
  2. refresh a rolling slice of video details with yt-dlp
  3. refresh SEO metadata with Gemini (new / stale / changed / oldest first)
  4. generate the static GitHub Pages site (listing, topic and /v/<id>/ pages)

The behaviour here is the contract defined by tests/test_engine.py and the
README. Environment variables are read at call time (not import time) so the
workflow inputs, local runs and the tests all behave the same way.
"""

import html as _html
import json
import math
import os
import random
import re
import tempfile
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

import yt_dlp
from google import genai

# -- CONFIGURATION -----------------------------------------------------------

CHANNEL_HANDLE = "@hassanparachaoffical"
DB_FILE = "video_db.json"
SITE_DIR = "site"
SITE_BASE_URL = (
    os.getenv("SITE_BASE_URL") or "https://hassan-paracha.github.io/my-channel-seo/"
).rstrip("/") + "/"
BATCH_SIZE = int(os.getenv("BATCH_SIZE") or "40")
VIDEOS_PER_PAGE = 48
# Bump when the prompt changes enough that every video should be regenerated.
# 3 = trend-aware prompt; makes the whole library eligible for one full pass.
PROMPT_VERSION = 3

DEFAULT_GEMINI_MODELS = "gemini-3.5-flash-lite,gemini-3.6-flash,gemini-3.5-flash"

_FALLBACK_KEYWORD_LIST = [
    "youtube shorts", "viral shorts", "trending pakistan", "funny video",
    "vlog", "hindi", "urdu", "desi", "reels",
]
# Returned as a ready-to-use comma separated string (same type as live results).
TRENDING_KEYWORDS_FALLBACK = ", ".join(_FALLBACK_KEYWORD_LIST)

TRENDING_TOPICS = ["bike start", "funny", "family", "vlog", "comedy"]

TOPICS = {
    "bike-start":         ["motorcycle", "bike start", "khrab", "start nahi"],
    "celebrations":       ["celebration", "mubarak", "independence", "eid", "birthday"],
    "family-funny":       ["family", "bacho", "kids", "ghar", "bachon"],
    "park-vlogs":         ["park", "mela", "outing", "bahar"],
    "jungle-adventure":   ["jungle", "forest", "bhoot", "adventure"],
    "desi-comedy":        ["funny", "comedy", "masti", "mazak", "prank"],
    "street-food":        ["food", "khana", "chai", "desi food"],
    "travel-vlogs":       ["travel", "safar", "road trip", "tour"],
    "emotional":          ["emotional", "dil", "yaad", "motivational"],
    "reaction":           ["reaction", "dekhiye", "shocking", "amazing"],
    "daily-vlog":         ["vlog", "routine", "aaj ka din", "daily"],
    "challenges":         ["challenge", "try", "attempt", "koshish"],
    "unboxing":           ["unbox", "new", "product", "review"],
    "shorts-compilation": ["shorts", "viral", "trending", "reels"],
    "special-events":     ["wedding", "shadi", "party", "event"],
}


# -- SMALL UTILITIES ---------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _dry_run() -> bool:
    return os.getenv("DRY_RUN", "0") == "1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value) -> datetime | None:
    """Parse ISO dates/datetimes (and YYYYMMDD) into aware UTC datetimes."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if re.fullmatch(r"\d{8}", text):
            text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_published(value) -> str | None:
    """
    Normalise a publish date. Date-only values stay dates (YYYY-MM-DD);
    datetimes are converted to UTC (YYYY-MM-DDTHH:MM:SSZ). Unknown -> None.
    """
    if not value:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    dt = _parse_dt(text)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _normalize_timestamp(value) -> str | None:
    """Like _normalize_published but always returns a full UTC datetime."""
    dt = _parse_dt(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


_ISO_DURATION = re.compile(
    r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)


def _parse_duration(value) -> int | None:
    """Seconds from an int/float, a numeric string or an ISO-8601 duration."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return int(float(text))
    match = _ISO_DURATION.match(text)
    if not match or text in ("P", "PT"):
        return None
    days, hours, minutes, seconds = match.groups()
    return (
        int(days or 0) * 86400 + int(hours or 0) * 3600
        + int(minutes or 0) * 60 + int(float(seconds or 0))
    )


def _iso_duration(seconds) -> str | None:
    if not seconds or seconds <= 0:
        return None
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return "PT" + (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "") \
        + (f"{secs}S" if secs else "")


# -- DATABASE ----------------------------------------------------------------

def load_db() -> dict:
    """Load the database. Corrupt JSON raises (and the file is left untouched)."""
    if os.path.exists(DB_FILE):
        with open(DB_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def save_db(db: dict) -> None:
    """Atomic write: a crash mid-write can never corrupt the database."""
    path = Path(DB_FILE)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".video_db.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(db, handle, indent=2, ensure_ascii=False)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def migrate_database(db: dict) -> dict:
    """Bring legacy records up to the current schema (in place)."""
    for rec in db.values():
        rec.setdefault("status", "active")
        rec.setdefault("missing_count", 0)
        if "original_title" not in rec:
            rec["original_title"] = rec.get("title", "")
        if "seo" not in rec and rec.get("polished"):
            rec["seo"] = {
                "title": rec.get("title", ""),
                "description": rec.get("description", ""),
                "keywords": rec.get("tags", []),
                "prompt_version": 0,
                "updated_at": rec.get("polished_at") or rec.get("last_refreshed", ""),
            }
    return db


def _sync_database(db: dict, listing: list) -> bool:
    """
    Merge a channel listing into the database. Returns True if anything changed.

    A changed YouTube title updates ``original_title``, resets any legacy
    "polished" state and flags the record for an SEO refresh. Existing SEO is
    kept (the page stays live) until the refresh succeeds.
    """
    changed = False
    for entry in listing or []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        vid = entry["id"]
        title = (entry.get("title") or "").strip()
        published = _normalize_published(
            entry.get("published_at") or entry.get("upload_date")
        )
        description = entry.get("description") or ""
        seconds = _parse_duration(
            entry["duration_seconds"] if entry.get("duration_seconds") is not None
            else entry.get("duration")
        )

        rec = db.get(vid)
        if rec is None:
            db[vid] = {
                "original_title": title,
                "status": "active",
                "missing_count": 0,
                "last_synced": None,
                "published_at": published,
                "description": description,
                "tags": [],
                "duration_seconds": seconds or 0,
            }
            changed = True
            continue

        if rec.get("status") == "removed":
            rec["status"] = "active"
            rec["missing_count"] = 0
            changed = True
        if title and title != rec.get("original_title"):
            rec["original_title"] = title
            if "title" in rec or "polished" in rec:      # legacy polished fields
                rec["title"] = title
                rec["polished"] = False
                rec["polished_at"] = None
            rec["seo_refresh_pending"] = True
            changed = True
        if published and published != rec.get("published_at"):
            rec["published_at"] = published
            changed = True
        if description and description != rec.get("description"):
            rec["description"] = description
            changed = True
        if seconds and seconds != rec.get("duration_seconds"):
            rec["duration_seconds"] = seconds
            changed = True
    return changed


def _apply_listing_sync(db: dict, listing: list) -> bool:
    """
    Count consecutive misses for videos absent from the listing and mark them
    removed after three. Skipped entirely unless the listing contains at least
    90% of the known active videos (guards against partial/failed scrapes).
    """
    listed = {e["id"] for e in listing if isinstance(e, dict) and e.get("id")}
    active_total = sum(1 for r in db.values() if r.get("status") == "active")
    if active_total == 0 or len(listed) < 0.9 * active_total:
        return False

    changed = False
    for vid, rec in db.items():
        if rec.get("status") != "active":
            continue
        if vid not in listed:
            rec["missing_count"] = rec.get("missing_count", 0) + 1
            if rec["missing_count"] >= 3:
                rec["status"] = "removed"
            changed = True
        elif rec.get("missing_count", 0) > 0:
            rec["missing_count"] = 0
            changed = True
    return changed


def _seo_queue(db: dict, limit: int, now: datetime | None = None,
               force_ids=None) -> list:
    """
    Videos to (re)generate SEO for, as ``[(video_id, record), ...]``, in order:
    forced -> source title changed -> no SEO -> stale prompt version ->
    otherwise-current SEO older than MIN_REFRESH_DAYS (oldest first).
    """
    now = now or _now()
    min_age = timedelta(days=_env_int("MIN_REFRESH_DAYS", 21))
    force_ids = set(force_ids or ())
    forced, pending, missing, stale, aged = [], [], [], [], []
    floor = datetime.min.replace(tzinfo=timezone.utc)

    for vid, rec in db.items():
        if rec.get("status") == "removed":
            continue
        seo = rec.get("seo")
        if vid in force_ids:
            forced.append((vid, rec))
        elif rec.get("seo_refresh_pending"):
            pending.append((vid, rec))
        elif not seo:
            missing.append((vid, rec))
        elif seo.get("prompt_version", 0) < PROMPT_VERSION:
            stale.append((_parse_dt(seo.get("updated_at")) or floor, vid, rec))
        else:
            updated = _parse_dt(seo.get("updated_at"))
            if updated is None or now - updated >= min_age:
                aged.append((updated or floor, vid, rec))

    stale.sort(key=lambda item: item[0])
    aged.sort(key=lambda item: item[0])
    ordered = (
        forced + pending + missing
        + [(v, r) for _, v, r in stale]
        + [(v, r) for _, v, r in aged]
    )
    return ordered[:max(0, limit)]


def _record_seo(record: dict, seo_obj: dict, model, updated_at: str) -> None:
    """Store new SEO, pushing the previous object onto a 3-entry history."""
    current = record.get("seo")
    if current:
        record["seo_history"] = ([current] + list(record.get("seo_history") or []))[:3]
    record["seo"] = {
        "title": seo_obj["title"],
        "description": seo_obj["description"],
        "keywords": list(seo_obj.get("keywords") or []),
        "prompt_version": PROMPT_VERSION,
        "updated_at": updated_at,
        "model": model,
        "source_tags": list(record.get("tags") or []),
    }
    record["seo_refresh_pending"] = False
    record["last_refreshed"] = updated_at


# -- YOUTUBE (yt-dlp, no cookies) --------------------------------------------

def get_channel_videos() -> list:
    """Complete flat listing of the channel's uploads playlist (no item cap)."""
    options = {"quiet": True, "no_warnings": True, "extract_flat": True,
               "ignoreerrors": True}
    channel_url = f"https://www.youtube.com/{CHANNEL_HANDLE}/videos"
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(channel_url, download=False)
            if not info:
                return []
            channel_id = info.get("id") or ""
            if channel_id.startswith("UC"):
                uploads_url = f"https://www.youtube.com/playlist?list=UU{channel_id[2:]}"
                info = ydl.extract_info(uploads_url, download=False) or {}
            entries = info.get("entries") or []
            return [e for e in entries if isinstance(e, dict)]
    except Exception as exc:
        print(f"Channel listing failed: {exc}")
        return []


def _is_bot_check(exc: Exception) -> bool:
    message = str(exc).lower()
    return "sign in" in message or "not a bot" in message or "confirm you" in message


def _detail_sync(db: dict) -> set:
    """
    Refresh full metadata for a rolling slice of videos (never-synced first,
    then oldest). Stops on the time budget or a YouTube bot-check.
    Returns the ids that were updated.
    """
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 3,
    }
    budget = _env_int("SYNC_TIME_BUDGET_MIN", 20) * 60
    slice_size = _env_int("SYNC_SLICE_SIZE", 50)
    started = time.monotonic()
    updated: set = set()

    def sync_key(vid):
        rec = db[vid]
        return rec.get("last_sync_attempt") or rec.get("last_synced") or ""

    candidates = sorted(
        (v for v, r in db.items() if r.get("status") != "removed"),
        key=sync_key,
    )[:slice_size]

    for vid in candidates:
        if time.monotonic() - started > budget:
            print("yt-dlp detail sync: time budget reached.")
            break
        rec = db[vid]
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={vid}", download=False
                )
        except Exception as exc:
            if _is_bot_check(exc):
                print("yt-dlp detail sync stopped: YouTube bot-check.")
                break
            rec["last_sync_attempt"] = _now_iso()
            continue
        if not info:
            rec["last_sync_attempt"] = _now_iso()
            continue

        title = (info.get("title") or "").strip()
        if title and title != rec.get("original_title"):
            rec["original_title"] = title
            rec["seo_refresh_pending"] = True
        rec["description"] = (info.get("description") or "")[:2000]
        if "tags" in info:
            rec["tags"] = (info.get("tags") or [])[:15]
        duration = _parse_duration(info.get("duration"))
        if duration is not None:
            rec["duration_seconds"] = duration
        published = _normalize_published(info.get("upload_date"))
        if published:
            rec["published_at"] = published
        views = info.get("view_count")
        if isinstance(views, int):
            rec["view_count"] = views
        rec["last_synced"] = _now_iso()
        rec.pop("last_sync_attempt", None)
        updated.add(vid)
        time.sleep(random.uniform(1, 3))

    return updated


# -- TRENDING KEYWORDS -------------------------------------------------------

_autocomplete_failures = 0


def _autocomplete_suggestions(seeds: list) -> list:
    """Best-effort YouTube autocomplete ideas. Never raises; gives up after
    repeated failures so a blocked network can't slow a large run."""
    global _autocomplete_failures
    if _dry_run() or _autocomplete_failures >= 5:
        return []
    results: list = []
    for seed in seeds:
        try:
            url = ("https://suggestqueries.google.com/complete/search?"
                   f"client=youtube&ds=yt&q={quote_plus(str(seed))}")
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read().decode("utf-8", "replace")
            match = re.search(r"\[.*\]", raw, re.DOTALL)
            if not match:
                continue
            data = json.loads(match.group())
            if isinstance(data, list) and len(data) > 1:
                for item in data[1][:6]:
                    results.append(str(item[0] if isinstance(item, list) else item))
            _autocomplete_failures = 0
        except Exception:
            _autocomplete_failures += 1
    return results


def _column_values(obj, limit: int) -> list:
    """First column of a DataFrame / dict-of-lists / list, as strings."""
    try:
        if hasattr(obj, "iloc"):
            return [str(v) for v in obj.iloc[:, 0].tolist()[:limit]]
        if isinstance(obj, dict):
            values = obj.get("query")
            if values is None:
                values = next(iter(obj.values()), [])
            return [str(v) for v in list(values)[:limit]]
        return [str(v) for v in list(obj)[:limit]]
    except Exception:
        return []


def _query_values(sub, limit: int) -> list:
    """The "query" column of a pytrends related-queries frame (or dict)."""
    if sub is None:
        return []
    try:
        return [str(v) for v in list(sub["query"])[:limit]]
    except Exception:
        return []


def _dedupe(terms) -> list:
    seen, out = set(), []
    for term in terms:
        clean = re.sub(r"\s+", " ", str(term).replace(",", " ")).strip()
        if clean and clean.casefold() not in seen:
            seen.add(clean.casefold())
            out.append(clean)
    return out


def _good_term(term: str) -> bool:
    """Drop long, sentence-like or self-repeating trend terms."""
    words = term.split()
    half = len(words) // 2
    repeated = half > 0 and len(words) % 2 == 0 and words[:half] == words[half:]
    return len(words) <= 5 and len(term) <= 40 and not repeated


def get_trending_keywords() -> str:
    """
    Comma separated string of 20 unique, currently trending search terms for
    Pakistan and India (7-day window), padded with a static fallback list.
    Any pytrends failure silently degrades to TRENDING_KEYWORDS_FALLBACK.
    """
    try:
        from pytrends.request import TrendReq

        client = TrendReq(hl="en-US", tz=-300, timeout=(2, 2), retries=0)
        trending: list = []
        related_terms: list = []

        for country in ("pakistan", "india"):
            try:
                trending += _column_values(client.trending_searches(pn=country), 5)
            except Exception:
                continue

        for geo in ("PK", "IN"):
            try:
                client.build_payload(
                    kw_list=TRENDING_TOPICS[:5], timeframe="now 7-d", geo=geo
                )
                related = client.related_queries() or {}
            except Exception:
                continue
            for data in related.values():
                for kind in ("top", "rising"):
                    related_terms += _query_values((data or {}).get(kind), 2)

        live = [t for t in _dedupe(trending + related_terms) if _good_term(t)][:15]
        if not live:
            return TRENDING_KEYWORDS_FALLBACK
        return ", ".join(_dedupe(live + _FALLBACK_KEYWORD_LIST)[:20])
    except Exception:
        return TRENDING_KEYWORDS_FALLBACK


# -- GEMINI ------------------------------------------------------------------

class GeminiRateLimiter:
    """Spaces out requests so we stay under the free-tier per-minute limit."""

    def __init__(self, calls_per_min: int = 10):
        self._gap = 60.0 / calls_per_min
        self._last = 0.0

    def wait(self):
        pause = self._gap - (time.monotonic() - self._last)
        if pause > 0:
            time.sleep(pause)
        self._last = time.monotonic()


def _candidate_models() -> list:
    raw = os.getenv("GEMINI_MODELS") or DEFAULT_GEMINI_MODELS
    return [m.strip() for m in raw.split(",") if m.strip()]


def _resolve_models(client) -> list:
    """Candidate models that actually exist for this API key (best effort)."""
    candidates = _candidate_models()
    try:
        available = {
            str(getattr(m, "name", "")).split("/", 1)[-1] for m in client.models.list()
        }
    except Exception:
        return candidates
    usable = [m for m in candidates if m in available]
    if usable:
        return usable
    skip = ("image", "tts", "live", "audio", "embedding", "vision", "robotics")
    flash = sorted(n for n in available if "flash" in n and not any(s in n for s in skip))
    if flash:
        print(f"None of {candidates} available; using {flash[:3]}")
        return flash[:3]
    return candidates


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    return re.sub(r"\s*```$", "", text).strip()


def _clean_title(value, item_id) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip()
    if not title:
        raise ValueError(f"Empty title for {item_id}")
    if len(title) > 100:
        raise ValueError(f"Title too long for {item_id} ({len(title)} chars)")
    return title


def _load_items(text: str, expected_ids: list) -> list:
    """Parse a JSON array and return its items in the requested id order."""
    parsed = json.loads(_strip_fences(text))
    if not isinstance(parsed, list):
        raise ValueError("Response is not a JSON array")
    if len(parsed) != len(expected_ids):
        raise ValueError(
            f"Response item count mismatch: got {len(parsed)}, expected {len(expected_ids)}"
        )
    by_id = {}
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("Response items must be JSON objects")
        by_id[item.get("id")] = item
    if len(by_id) != len(parsed) or set(by_id) != set(expected_ids):
        raise ValueError("Response IDs do not match the requested IDs")
    return [by_id[i] for i in expected_ids]


def parse_polished_response(text: str, expected_ids: list) -> list:
    """Title-only batch response -> [{"id", "title"}]. Raises ValueError."""
    return [
        {"id": item["id"], "title": _clean_title(item.get("title"), item["id"])}
        for item in _load_items(text, expected_ids)
    ]


def parse_seo_response(text: str, expected_ids: list) -> list:
    """
    Full SEO batch response -> [{"id", "title", "description", "keywords"}].
    Requires exactly five keywords and a 2-4 sentence description.
    Raises ValueError on any problem.
    """
    out = []
    for item in _load_items(text, expected_ids):
        item_id = item["id"]
        title = _clean_title(item.get("title"), item_id)
        description = re.sub(r"\s+", " ", str(item.get("description") or "")).strip()
        sentences = len(re.findall(r"[.!?](?:\s|$)", description))
        if not description or len(description) > 400 or not 2 <= sentences <= 4:
            raise ValueError(f"Description for {item_id} must be 2 sentences")
        raw_keywords = item.get("keywords")
        keywords = _dedupe(raw_keywords) if isinstance(raw_keywords, list) else []
        if len(keywords) != 5:
            raise ValueError(
                f"Invalid keyword count for {item_id}: expected 5, got {len(keywords)}"
            )
        out.append({"id": item_id, "title": title,
                    "description": description, "keywords": keywords})
    return out


def _duplicate_ids(results: list, db: dict, exclude_ids) -> dict:
    """{video_id: title} for results that repeat a title already in the library."""
    taken = {}
    for vid, rec in db.items():
        if vid in exclude_ids:
            continue
        title = ((rec.get("seo") or {}).get("title") or "").strip().casefold()
        if title:
            taken[title] = vid
    duplicates, seen = {}, set()
    for item in results:
        key = item["title"].strip().casefold()
        if key in taken or key in seen:
            duplicates[item["id"]] = item["title"]
        seen.add(key)
    return duplicates


def _validate_library_uniqueness(results: list, db: dict, exclude_ids) -> None:
    duplicates = _duplicate_ids(results, db, set(exclude_ids))
    if duplicates:
        raise ValueError(f"Duplicate SEO title: {sorted(duplicates.values())}")


def _seo_model_input(kind: str, record: dict, trends: str) -> dict:
    """Per-video instructions for the model (trend terms, context, ideas)."""
    title = (record.get("original_title") or record.get("title") or "").strip()
    hint = re.sub(r"\s+", " ", str(record.get("description") or "")).strip()
    hint = hint[:200].rstrip(" .!?")
    ideas = _dedupe(_autocomplete_suggestions([title[:60]]))[:5] if title else []

    parts = [
        f"Rewrite this YouTube {kind} title so it ranks in search and earns clicks "
        "while staying truthful to the video.",
        f"Use 1-2 of the currently trending search terms in Pakistan and India: {trends}. "
        "Only use a term where it fits naturally.",
        "Max 70 characters, natural Hinglish (Urdu/Hindi + English), keep the "
        "main subject of the original title.",
    ]
    if hint:
        parts.append(f"Video context hint: {hint}.")
    if ideas:
        parts.append("Autocomplete ideas: " + "; ".join(ideas) + ".")
    parts.append(f"Return ONLY the rewritten title, nothing else: {title}")
    return {"title_instruction": " ".join(parts), "autocomplete": ideas}


def _generate_seo_batch(client, models: list, batch: list, trends: str,
                        db: dict, usage: dict, limiter) -> tuple:
    """
    One Gemini request for a batch of ``(video_id, record)``.
    Returns ``(results, model_name, ok)``. Falls through the model list when a
    model is out of quota or unavailable; retries once on invalid output.
    """
    ids = [vid for vid, _ in batch]
    items = []
    for vid, rec in batch:
        item = {
            "id": vid,
            "title_instruction": _seo_model_input("video", rec, trends)["title_instruction"],
        }
        if rec.get("tags"):
            item["tags"] = rec["tags"][:8]
        items.append(item)

    used = [(r.get("seo") or {}).get("title") for r in db.values()]
    used = [t for t in used if t][-40:]
    prompt = (
        "You are a YouTube SEO editor for a Pakistani Hinglish/Urdu entertainment "
        "channel. For each item write metadata that is truthful to the video; "
        "never invent facts.\n"
        "Return ONLY a JSON array (no markdown) with exactly one object per input "
        "item, same order, each with:\n"
        '  "id": the same id\n'
        '  "title": the rewritten title, max 70 characters, unique\n'
        '  "description": exactly 2 sentences, each ending with a period, max 300 characters\n'
        '  "keywords": exactly 5 short Hinglish/English search keywords\n'
        "Follow each item's title_instruction for its title. Every title must differ "
        "from every other title and from these already used titles: "
        + "; ".join(used) + "\n\nInput JSON:\n"
        + json.dumps(items, ensure_ascii=False)
    )

    if os.getenv("SEO_USE_SEARCH_GROUNDING") == "1":
        # Search grounding cannot be combined with a JSON response mime type.
        config = {"tools": [{"google_search": {}}], "temperature": 0.7}
    else:
        config = {"response_mime_type": "application/json", "temperature": 0.7}

    daily_limit = _env_int("GEMINI_DAILY_REQUEST_LIMIT", 18)
    last_model = None
    for model in models:
        usage.setdefault(model, 0)
        for attempt in range(3):
            if usage[model] >= daily_limit:
                break
            usage[model] += 1
            last_model = model
            try:
                limiter.wait()
                response = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                results = parse_seo_response(response.text or "", ids)
                duplicates = _duplicate_ids(results, db, set(ids))
                if duplicates and attempt == 0:
                    raise ValueError(f"Duplicate SEO title: {sorted(duplicates.values())}")
                if duplicates:   # keep the unique ones, retry the rest tomorrow
                    results = [r for r in results if r["id"] not in duplicates]
                if results:
                    return results, model, True
            except ValueError as exc:
                print(f"  {model}: rejected output ({exc})")
                prompt += (f"\n\nYour previous answer was rejected: {exc}. "
                           "Return only the corrected JSON array.")
            except Exception as exc:
                message = str(exc).lower()
                if any(s in message for s in ("429", "quota", "resource_exhausted", "rate limit")):
                    if attempt == 0:
                        print(f"  {model}: rate limited, waiting 20s")
                        time.sleep(20)
                        continue
                    usage[model] = daily_limit
                elif any(s in message for s in ("404", "not found", "not_found", "403", "permission")):
                    print(f"  {model}: unavailable ({exc})")
                    usage[model] = daily_limit
                else:
                    print(f"  {model}: error ({exc})")
                    time.sleep(5 * (attempt + 1))
                    continue
                break
    return None, last_model, False


def _refresh_seo(db: dict, force_ids=None, trends=None) -> int:
    """
    Generate SEO for the next REFRESH_PER_DAY videos in the queue.
    Returns the number refreshed. Existing SEO is only replaced by output that
    passed validation. Raises RuntimeError if more than half the attempts fail.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY not set - skipping SEO refresh.")
        return 0

    limit = _env_int("REFRESH_PER_DAY", 180)
    queue = _seo_queue(db, limit, now=_now(), force_ids=force_ids)
    if not queue:
        print("SEO queue is empty.")
        return 0

    trends = TRENDING_KEYWORDS_FALLBACK if trends is None else trends
    client = genai.Client(api_key=api_key)
    models = _resolve_models(client)
    limiter = GeminiRateLimiter()
    daily_limit = _env_int("GEMINI_DAILY_REQUEST_LIMIT", 18)
    usage = {m: 0 for m in models}
    refreshed = attempted = failed = 0

    for start in range(0, len(queue), BATCH_SIZE):
        batch = queue[start:start + BATCH_SIZE]
        attempted += len(batch)
        results, model, ok = _generate_seo_batch(
            client, models, batch, trends, db, usage, limiter
        )
        if ok and results:
            stamp = _now_iso()
            for item in results:
                _record_seo(db[item["id"]], item, model, stamp)
                refreshed += 1
            failed += len(batch) - len(results)
            save_db(db)
        else:
            failed += len(batch)
        if not any(usage.get(m, 0) < daily_limit for m in models):
            print("All Gemini models are out of quota for this run.")
            break

    print(f"SEO refresh: {refreshed} updated, {failed} failed of {attempted} attempted.")
    if attempted and failed * 2 > attempted:
        save_db(db)
        raise RuntimeError(
            f"SEO refresh: more than half failed ({failed}/{attempted}); "
            "existing SEO was kept."
        )
    return refreshed


def _polish_unpolished_videos(db: dict) -> int:
    """
    Legacy helper: rewrite the plain ``title`` of records that were never
    "polished", 40 per structured JSON request. Not part of the daily cycle
    (the SEO refresh supersedes it); kept because it is covered by the tests.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return 0
    todo = [v for v, r in db.items()
            if r.get("status") != "removed" and not r.get("polished")]
    if not todo:
        return 0

    client = genai.Client(api_key=api_key)
    limiter = GeminiRateLimiter()
    model = _candidate_models()[0]
    polished = 0

    for start in range(0, len(todo), BATCH_SIZE):
        ids = todo[start:start + BATCH_SIZE]
        items = [
            {"id": v, "title": db[v].get("original_title") or db[v].get("title", "")}
            for v in ids
        ]
        prompt = (
            "Rewrite each YouTube title for search (max 70 characters, natural "
            "Hinglish, truthful). Return ONLY a JSON array of objects with "
            '"id" and "title", same ids, same order.\n\nInput JSON:\n'
            + json.dumps(items, ensure_ascii=False)
        )
        for _attempt in range(2):
            try:
                limiter.wait()
                response = client.models.generate_content(
                    model=model, contents=prompt,
                    config={"response_mime_type": "application/json"},
                )
                results = parse_polished_response(response.text or "", ids)
            except Exception as exc:
                print(f"  polish batch failed: {exc}")
                continue
            stamp = _now_iso()
            for item in results:
                rec = db[item["id"]]
                rec["title"] = item["title"]
                rec["polished"] = True
                rec["polished_at"] = stamp
                polished += 1
            break
        save_db(db)
    return polished


# -- HTML --------------------------------------------------------------------

def _e(text) -> str:
    return _html.escape(str(text), quote=True)


def _json_ld(obj: dict) -> str:
    """JSON that is safe inside <script>: no raw <, > or & can close the tag."""
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    )


_CSS = """
:root{--bg:#f0f2f5;--card:#fff;--accent:#e60000;--text:#222;--sub:#555;
  --border:#e0e0e0;--radius:14px;--shadow:0 4px 18px rgba(0,0,0,.07)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text)}
header{background:#fff;border-bottom:1px solid var(--border);padding:14px 20px;
  display:flex;align-items:center;gap:14px;flex-wrap:wrap;position:sticky;top:0;z-index:10}
header .brand{font-size:1.1rem;font-weight:700;color:var(--text);text-decoration:none}
header a{color:var(--accent);text-decoration:none;font-size:.85rem;font-weight:600}
.topic-bar{display:flex;gap:8px;flex-wrap:wrap;padding:10px 20px;background:#fff;
  border-bottom:1px solid var(--border)}
.topic-bar a{padding:4px 12px;border-radius:20px;border:1px solid var(--border);
  font-size:.8rem;color:var(--sub);text-decoration:none}
.topic-bar a:hover,.topic-bar a.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.page-head{padding:18px 20px 0;max-width:1400px;margin:0 auto}
.page-head h1{font-size:1.25rem}
.page-head p{font-size:.85rem;color:var(--sub);margin-top:4px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
  gap:20px;padding:20px;max-width:1400px;margin:0 auto}
.card{background:var(--card);border-radius:var(--radius);box-shadow:var(--shadow);
  overflow:hidden;display:flex;flex-direction:column}
.thumb{position:relative;display:block;padding-bottom:56.25%;background:#000;cursor:pointer}
.thumb img,.thumb iframe{position:absolute;border:0;inset:0;width:100%;height:100%;object-fit:cover}
.play-btn{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.25);font-size:2.5rem;color:#fff;opacity:.9;pointer-events:none}
.info{padding:12px 14px;flex:1;display:flex;flex-direction:column;gap:6px}
.info h3{font-size:.9rem;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden}
.info h3 a{color:inherit;text-decoration:none}
.info p{font-size:.78rem;color:var(--sub);flex:1;display:-webkit-box;
  -webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.btn{display:block;text-align:center;background:var(--accent);color:#fff;
  text-decoration:none;padding:9px;margin:0 14px 14px;border-radius:8px;
  font-size:.82rem;font-weight:700}
.btn:hover{background:#b30000}
.pagination{display:flex;justify-content:center;gap:8px;padding:28px 20px;flex-wrap:wrap}
.pagination a,.pagination span{padding:8px 16px;border-radius:8px;border:1px solid var(--border);
  font-size:.85rem;text-decoration:none;color:var(--text)}
.pagination a:hover,.pagination .cur{background:var(--accent);color:#fff;border-color:var(--accent)}
footer{text-align:center;padding:20px;font-size:.78rem;color:var(--sub);border-top:1px solid var(--border)}
.single{max-width:860px;margin:30px auto;padding:0 16px}
.embed{position:relative;padding-bottom:56.25%;border-radius:var(--radius);
  overflow:hidden;background:#000;cursor:pointer}
.embed img,.embed iframe{position:absolute;inset:0;width:100%;height:100%;border:0;object-fit:cover}
.embed .play-btn{opacity:1;font-size:3.5rem}
.single h1{font-size:1.3rem;margin:16px 0 8px}
.single h2{margin:22px 0 10px;font-size:1rem}
.single .desc{font-size:.88rem;color:var(--sub);line-height:1.6;margin-bottom:12px}
.single .btn{margin:14px 0}
.single .back{margin-top:16px;font-size:.8rem}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.tags span{background:var(--border);border-radius:12px;padding:3px 10px;font-size:.75rem;color:var(--sub)}
.related-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
@media(max-width:480px){.grid{grid-template-columns:1fr;padding:12px}}
"""

_PLAYER_SCRIPT = (
    '<script>(function(){function play(t){var f=document.createElement("iframe");'
    'f.src="https://www.youtube-nocookie.com/embed/"+encodeURIComponent(t.dataset.video)'
    '+"?autoplay=1&rel=0";f.title=t.dataset.title||"Video";'
    'f.allow="autoplay; encrypted-media; picture-in-picture";f.allowFullscreen=true;'
    't.removeAttribute("data-video");t.removeAttribute("role");t.replaceChildren(f);}'
    'document.addEventListener("click",function(e){var t=e.target.closest("[data-video]");'
    'if(t){e.preventDefault();play(t);}});'
    'document.addEventListener("keydown",function(e){if(e.key!=="Enter"&&e.key!==" ")return;'
    'var t=e.target.closest&&e.target.closest("[data-video]");if(t){e.preventDefault();play(t);}});'
    '})();</script>'
)


def _header(prefix: str = "", active_topic: str = "") -> str:
    links = []
    for topic in TOPICS:
        cls = ' class="active"' if topic == active_topic else ""
        label = topic.replace("-", " ").title()
        links.append(f'<a href="{prefix}topics/{topic}/"{cls}>{_e(label)}</a>')
    return (
        f'<header><a class="brand" href="{prefix or "./"}">Hassan Paracha</a>'
        f'<a href="{prefix or "./"}">All Videos</a>'
        f'<a href="https://www.youtube.com/{CHANNEL_HANDLE}?sub_confirmation=1" '
        f'target="_blank" rel="noopener">Subscribe</a></header>'
        f'<nav class="topic-bar">{"".join(links)}</nav>'
    )


def _pagination(current: int, total_pages: int) -> str:
    """Relative links: page 1 lives at the site root, page N at page/N/."""
    if total_pages <= 1:
        return ""

    def href(page: int) -> str:
        if current == 1:
            return f"page/{page}/"
        return "../../" if page == 1 else f"../{page}/"

    parts = []
    if current > 1:
        parts.append(f'<a href="{href(current - 1)}" rel="prev">&laquo; Prev</a>')
    for page in range(1, total_pages + 1):
        if page == current:
            parts.append(f'<span class="cur">{page}</span>')
        elif abs(page - current) <= 2 or page in (1, total_pages):
            parts.append(f'<a href="{href(page)}">{page}</a>')
        elif abs(page - current) == 3:
            parts.append("<span>&hellip;</span>")
    if current < total_pages:
        parts.append(f'<a href="{href(current + 1)}" rel="next">Next &raquo;</a>')
    return f'<nav class="pagination" aria-label="Pagination">{"".join(parts)}</nav>'


def _html_shell(title: str, desc: str, url: str, body: str,
                og_image: str = "", og_type: str = "website") -> str:
    image = f'<meta property="og:image" content="{_e(og_image)}">' if og_image else ""
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{_e(title)}</title>"
        f'<meta name="description" content="{_e(desc)}">'
        '<meta name="robots" content="index,follow">'
        f'<link rel="canonical" href="{_e(url)}">'
        f'<meta property="og:title" content="{_e(title)}">'
        f'<meta property="og:description" content="{_e(desc)}">'
        f'<meta property="og:url" content="{_e(url)}">'
        f'<meta property="og:type" content="{og_type}">{image}'
        f"<style>{_CSS}</style></head><body>{body}{_PLAYER_SCRIPT}"
        f"<footer><p>&copy; {date.today().year} Hassan Paracha &middot; "
        f'<a href="{SITE_BASE_URL}sitemap.xml">Sitemap</a> &middot; '
        f'<a href="https://www.youtube.com/{CHANNEL_HANDLE}" target="_blank" '
        'rel="noopener">YouTube Channel</a></p></footer></body></html>'
    )


def _video_fields(vid: str, data: dict) -> tuple:
    """(title, description) using SEO when present, else the stored metadata."""
    seo = data.get("seo") or {}
    title = (seo.get("title") or data.get("title")
             or data.get("original_title") or "Video")
    desc = seo.get("description") or data.get("description") or ""
    return str(title), str(desc)


def _video_object(vid: str, data: dict, base_url: str) -> dict:
    title, desc = _video_fields(vid, data)
    obj = {
        "@context": "https://schema.org",
        "@type": "VideoObject",
        "name": title,
        "description": desc or title,
        "thumbnailUrl": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        "url": f"{base_url}v/{vid}/",
        "embedUrl": f"https://www.youtube.com/embed/{vid}",
        "author": {"@type": "Person", "name": "Hassan Paracha",
                   "url": f"https://www.youtube.com/{CHANNEL_HANDLE}"},
    }
    upload = _normalize_published(data.get("published_at"))
    if upload:
        obj["uploadDate"] = upload
    seconds = (_parse_duration(data.get("duration_seconds"))
               or _parse_duration(data.get("duration")))
    iso = _iso_duration(seconds)
    if iso:
        obj["duration"] = iso
    return obj


def _card_html(vid: str, data: dict, prefix: str = "", base_url: str = "",
               json_ld: bool = True) -> str:
    base_url = base_url or SITE_BASE_URL
    title, desc = _video_fields(vid, data)
    page_href = f"{prefix}v/{vid}/"
    thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    ld = (f'<script type="application/ld+json">'
          f'{_json_ld(_video_object(vid, data, base_url))}</script>') if json_ld else ""
    snippet = f"<p>{_e(desc[:120])}</p>" if desc else ""
    return (
        f'<article class="card">{ld}'
        f'<div class="thumb" data-video="{_e(vid)}" data-title="{_e(title)}" '
        f'role="button" tabindex="0" aria-label="Play {_e(title)}">'
        f'<img src="{thumb}" alt="{_e(title)}" loading="lazy" width="320" height="180">'
        f'<span class="play-btn">&#9654;</span></div>'
        f'<div class="info"><h3><a href="{page_href}">{_e(title)}</a></h3>{snippet}</div>'
        f'<a class="btn" href="{page_href}">Details</a>'
        f"</article>"
    )


def build_html(videos: list, page_number: int = 1, total_pages: int = 1,
               page_path: str = "") -> str:
    """Listing page. ``videos`` is ``[(video_id, record), ...]``."""
    if page_number > 1 and not page_path:
        page_path = f"page/{page_number}/"
    depth = len([part for part in page_path.split("/") if part])
    prefix = "../" * depth
    cards = "".join(_card_html(v, d, prefix) for v, d in videos)
    if not cards:
        cards = '<p style="padding:20px">No videos yet.</p>'
    heading = "Hassan Paracha Videos"
    body = (
        f"{_header(prefix)}"
        f'<div class="page-head"><h1>{heading}</h1>'
        f"<p>Funny shorts, desi vlogs and viral moments.</p></div>"
        f'<main class="grid">{cards}</main>{_pagination(page_number, total_pages)}'
    )
    url = SITE_BASE_URL if page_number == 1 else f"{SITE_BASE_URL}{page_path}"
    title = "Hassan Paracha Videos | Funny Shorts & Viral Vlogs"
    if page_number > 1:
        title = f"Hassan Paracha Videos - Page {page_number}"
    og_image = f"https://i.ytimg.com/vi/{videos[0][0]}/hqdefault.jpg" if videos else ""
    return _html_shell(
        title,
        "Watch Hassan Paracha's funny Pakistani YouTube videos, bike start fails, "
        "family vlogs and desi comedy shorts.",
        url, body, og_image=og_image,
    )


def _build_video_page(vid: str, data: dict, related: list, base_url: str,
                      _unused=None) -> str:
    """Individual /v/<id>/ page (embed loads only after a click)."""
    title, desc = _video_fields(vid, data)
    seo = data.get("seo") or {}
    tag_source = seo.get("keywords") or data.get("tags") or []
    yt_url = f"https://www.youtube.com/watch?v={vid}"
    thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    prefix = "../../"

    tags_html = "".join(f"<span>{_e(t)}</span>" for t in tag_source[:8])
    related_html = "".join(
        _card_html(v, d, prefix, base_url, json_ld=False) for v, d in related
    )
    body = (
        f"{_header(prefix)}"
        f'<div class="single">'
        f'<script type="application/ld+json">{_json_ld(_video_object(vid, data, base_url))}</script>'
        f'<div class="embed" data-video="{_e(vid)}" data-title="{_e(title)}" '
        f'role="button" tabindex="0" aria-label="Play video">'
        f'<img src="{thumb}" alt="{_e(title)}" width="480" height="360">'
        f'<span class="play-btn">&#9654;</span></div>'
                f"<h1>{_e(title)}</h1>"
        f'<p class="desc">{_e(desc)}</p>'
        + (f'<div class="tags">{tags_html}</div>' if tags_html else "")
        + f'<a class="btn" href="{yt_url}" target="_blank" rel="noopener">'
          f"&#9654; Watch on YouTube &amp; Subscribe to Hassan Paracha</a>"
        + (f'<h2>Related Videos</h2><div class="related-grid">{related_html}</div>'
           if related_html else "")
        + f'<p class="back"><a href="{base_url}">&larr; Back to all videos</a></p></div>'
    )
    return _html_shell(
        f"{title} | Hassan Paracha",
        (desc or f"Watch {title} by Hassan Paracha")[:155],
        f"{base_url}v/{vid}/", body, og_image=thumb, og_type="video.other",
    )


# -- SITE GENERATOR ----------------------------------------------------------

def _site_path(*parts) -> Path:
    path = Path(SITE_DIR, *parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def generate_site(db: dict) -> tuple:
    """Build the whole static site. Returns ``(video_count, page_count)``."""
    videos = [
        (vid, rec) for vid, rec in db.items()
        if rec.get("status") != "removed" and rec.get("seo")
    ]
    videos.sort(key=lambda item: item[1]["seo"].get("updated_at") or "", reverse=True)

    total_pages = max(1, math.ceil(len(videos) / VIDEOS_PER_PAGE))

    for number in range(1, total_pages + 1):
        chunk = videos[(number - 1) * VIDEOS_PER_PAGE: number * VIDEOS_PER_PAGE]
        page_path = "" if number == 1 else f"page/{number}/"
        page = build_html(chunk, number, total_pages, page_path)
        target = _site_path() if number == 1 else _site_path("page", str(number))
        (target / "index.html").write_text(page, encoding="utf-8")

    for index, (vid, rec) in enumerate(videos):
        related = [
            videos[i] for i in (index - 2, index - 1, index + 1, index + 2)
            if 0 <= i < len(videos)
        ]
        page = _build_video_page(vid, rec, related, SITE_BASE_URL)
        (_site_path("v", vid) / "index.html").write_text(page, encoding="utf-8")

    for topic, words in TOPICS.items():
        matched = [
            (v, d) for v, d in videos
            if any(w in _topic_text(d) for w in words)
        ] or videos[:12]
        label = topic.replace("-", " ").title()
        cards = "".join(
            _card_html(v, d, "../../", SITE_BASE_URL, json_ld=False)
            for v, d in matched[:60]
        )
        body = (
            f'{_header("../../", topic)}'
            f'<div class="page-head"><h1>{_e(label)} Videos</h1>'
            f"<p>{len(matched)} videos &middot; Hassan Paracha</p></div>"
            f'<main class="grid">{cards}</main>'
        )
        page = _html_shell(
            f"{label} Videos | Hassan Paracha",
            f"Watch the best {label.lower()} videos by Hassan Paracha.",
            f"{SITE_BASE_URL}topics/{topic}/", body,
        )
        (_site_path("topics", topic) / "index.html").write_text(page, encoding="utf-8")

    _write_sitemap(videos, total_pages)
    return len(videos), total_pages


def _topic_text(rec: dict) -> str:
    seo = rec.get("seo") or {}
    pieces = [seo.get("title", ""), seo.get("description", ""),
              " ".join(seo.get("keywords") or []), rec.get("original_title", ""),
              rec.get("description", ""), " ".join(rec.get("tags") or [])]
    return " ".join(str(p) for p in pieces).lower()


def _write_sitemap(videos: list, total_pages: int) -> None:
    today = date.today().isoformat()
    stamps = [_normalize_timestamp(r["seo"].get("updated_at")) for _, r in videos]
    newest = max((s for s in stamps if s), default=today)
    urls = [f"<url><loc>{_e(SITE_BASE_URL)}</loc><lastmod>{newest}</lastmod>"
            "<changefreq>daily</changefreq><priority>1.0</priority></url>"]
    for number in range(2, total_pages + 1):
        urls.append(f"<url><loc>{_e(SITE_BASE_URL)}page/{number}/</loc>"
                    "<changefreq>daily</changefreq><priority>0.8</priority></url>")
    for topic in TOPICS:
        urls.append(f"<url><loc>{_e(SITE_BASE_URL)}topics/{topic}/</loc>"
                    "<changefreq>weekly</changefreq><priority>0.7</priority></url>")
    for (vid, _rec), stamp in zip(videos, stamps):
        urls.append(f"<url><loc>{_e(SITE_BASE_URL)}v/{_e(vid)}/</loc>"
                    f"<lastmod>{stamp or today}</lastmod>"
                    "<changefreq>monthly</changefreq><priority>0.6</priority></url>")
    sitemap = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
               + "\n".join(urls) + "\n</urlset>")
    (_site_path() / "sitemap.xml").write_text(sitemap, encoding="utf-8")
    (_site_path() / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {SITE_BASE_URL}sitemap.xml\n",
        encoding="utf-8",
    )


# -- MAIN CYCLE --------------------------------------------------------------

def run_rolling_cycle() -> None:
    if _dry_run():
        print("DRY_RUN=1: skipping YouTube, autocomplete, Gemini and database writes.")
        db = migrate_database(load_db())
        videos, pages = generate_site(db)
        print(f"Built {SITE_DIR}/ with {videos} video pages across {pages} listing pages.")
        return

    trends = get_trending_keywords()          # fetched once, reused for every batch
    print(f"Trending terms: {trends}")

    db = migrate_database(load_db())

    listing = get_channel_videos()
    if not listing:
        print("Warning: empty channel listing - keeping existing database as is.")
    _sync_database(db, listing)
    _apply_listing_sync(db, listing)
    save_db(db)
    print(f"Library: {len(db)} videos.")

    updated = _detail_sync(db)
    if updated:
        print(f"yt-dlp: refreshed details for {len(updated)} videos.")
        save_db(db)

    changed_titles = {v for v, r in db.items() if r.get("seo_refresh_pending")}
    try:
        refreshed = _refresh_seo(db, changed_titles, trends)
        print(f"SEO refreshed: {refreshed}")
    except RuntimeError as exc:
        # Keep going: the database and site are still worth publishing.
        print(f"::warning::{exc}")
    save_db(db)

    videos, pages = generate_site(db)
    print(f"Built {SITE_DIR}/ with {videos} video pages across {pages} listing pages.")


if __name__ == "__main__":
    run_rolling_cycle()
