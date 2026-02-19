import os
import yt_dlp
from google import genai
import json
import time
from datetime import datetime

# --- CONFIGURATION ---
CHANNEL_HANDLE = "@HassanParacha-c3g" # Ensure this is correct
DB_FILE = "video_db.json"
DAILY_INDEX_LIMIT = 180  # Google's daily quota
BATCH_SIZE = 5         # AI batch size before sleeping
COOLDOWN = 65           # Seconds to wait to reset AI limit

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except: return {}
    return {}

def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=4)

def get_channel_videos():
    """DEEP SCRAPE: Pulls up to 500 videos from the channel."""
    base_url = f"https://www.youtube.com/{CHANNEL_HANDLE}/videos"
    ydl_opts = {
        'quiet': True, 
        'extract_flat': True,
        'playlist_items': '1:500' # FORCES YouTube to show the whole library
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            result = ydl.extract_info(base_url, download=False)
            return result.get('entries', [])
        except Exception as e:
            print(f"Scrape Error: {e}")
            return []

def ai_polish(title):
    """Trend-Aware AI Polishing."""
    client = genai.Client()
    prompt = f"Optimize this YouTube title for Pakistani SEO. Fix Roman Urdu, add 2 viral keywords, and 1 emoji. Return ONLY the title: {title}"
    try:
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt
        )
        return response.text.strip().replace('"', '')
    except:
        return title

def run_rolling_cycle():
    db = load_db()
    all_videos = get_channel_videos()
    today_str = datetime.now().strftime("%Y-%m-%d")

    if not all_videos:
        print("No videos found. Check your Channel Handle.")
        return

    # 1. Sync DB: Add any newly discovered videos
    for v in all_videos:
        v_id = v['id']
        if v_id not in db:
            db[v_id] = {
                "title": v.get('title', 'Video'),
                "last_refreshed": "2000-01-01",
                "url": f"https://www.youtube.com/watch?v={v_id}"
            }

    # 2. Select the 200 most 'stale' videos (oldest refresh date first)
    sorted_ids = sorted(db.keys(), key=lambda x: db[x].get('last_refreshed', "2000-01-01"))
    to_process = sorted_ids[:DAILY_INDEX_LIMIT]

    print(f"Deep Scrape successful. Found {len(all_videos)} total. Cycling {len(to_process)} today...")

    # 3. Process in batches with Cooldown
    for i in range(0, len(to_process), BATCH_SIZE):
        batch = to_process[i:i+BATCH_SIZE]
        for v_id in batch:
            print(f"Processing: {db[v_id]['title']}")
            db[v_id]['title'] = ai_polish(db[v_id]['title'])
            db[v_id]['last_refreshed'] = today_str
        
        save_db(db) # Save after every batch to prevent data loss
        if i + BATCH_SIZE < len(to_process):
            print(f"Batch done. Sleeping {COOLDOWN}s...")
            time.sleep(COOLDOWN)

    # 4. Generate Fresh HTML
    cards_html = "".join([f"""
        <div class="card">
            <script type="application/ld+json">
            {{"@context":"https://schema.org","@type":"VideoObject","name":"{v['title']}","thumbnailUrl":"https://i.ytimg.com/vi/{vid}/hqdefault.jpg","uploadDate":"{v['last_refreshed']}","embedUrl":"https://www.youtube.com/embed/{vid}"}}
            </script>
            <div class="badge">SEO Optimized</div>
            <h3>{v['title']}</h3>
            <div class="video-container">
                <iframe src="https://www.youtube.com/embed/{vid}" loading="lazy" frameborder="0" allowfullscreen></iframe>
            </div>
            <a class="btn" href="{v['url']}" target="_blank">Watch on YouTube</a>
        </div>""" for vid, v in db.items()])

    full_html = f"<!DOCTYPE html><html><head><meta charset='UTF-8'><title>Hassan SEO Hub</title><style>body{{font-family:sans-serif;background:#f0f2f5;display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:20px;padding:20px;}}.card{{background:#fff;padding:15px;border-radius:12px;box-shadow:0 4px 6px rgba(0,0,0,0.1);}}h3{{font-size:15px;margin:10px 0;height:45px;overflow:hidden;}}.video-container{{position:relative;padding-bottom:56.25%;height:0;}}.video-container iframe{{position:absolute;top:0;left:0;width:100%;height:100%;border-radius:8px;}}.btn{{display:block;text-align:center;background:#0084ff;color:#fff;text-decoration:none;padding:10px;margin-top:10px;border-radius:6px;font-weight:bold;}}.badge{{background:#28a745;color:white;font-size:10px;padding:3px 8px;border-radius:4px;width:fit-content;}}</style></head><body>{cards_html}</body></html>"
    
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(full_html)

if __name__ == "__main__":
    run_rolling_cycle()
