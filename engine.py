import os
import yt_dlp
from google import genai
import json
import time
from datetime import datetime

# --- CONFIGURATION ---
CHANNEL_HANDLE = "@HassanParacha-c3g"
DB_FILE = "video_db.json"
DAILY_INDEX_LIMIT = 199  # Google's daily quota
BATCH_SIZE = 10         # AI batch size before sleeping
COOLDOWN = 65           # Seconds to wait to reset AI minute-limit

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {}

def save_db(db):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=4)

def get_channel_videos():
    """Scrapes all available video metadata from the channel."""
    base_url = f"https://www.youtube.com/{CHANNEL_HANDLE}/videos"
    ydl_opts = {'quiet': True, 'extract_flat': True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        result = ydl.extract_info(base_url, download=False)
        return result.get('entries', [])

def ai_polish(title, current_trends="daily routine, family fun, viral vlog"):
    """Polishes title with current trends using Gemini 3 Flash."""
    client = genai.Client()
    prompt = f"""
    Rewrite this YouTube title for maximum CTR and Pakistani SEO.
    Original: {title}
    Today's Trends: {current_trends}
    Task: Fix Roman Urdu, add 3 English keywords, and use a trend ONLY if it fits naturally.
    Return ONLY the new title text.
    """
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

    # 1. Sync DB with YouTube (Add new videos found)
    for v in all_videos:
        v_id = v['id']
        if v_id not in db:
            db[v_id] = {"title": v['title'], "last_refreshed": "2000-01-01", "url": f"https://www.youtube.com/watch?v={v_id}"}

    # 2. Sort by 'last_refreshed' to find the 200 most "stale" videos
    # Newer uploads (never refreshed) come first
    sorted_ids = sorted(db.keys(), key=lambda x: db[x].get('last_refreshed', "2000-01-01"))
    to_process = sorted_ids[:DAILY_INDEX_LIMIT]

    print(f"Starting cycle for {len(to_process)} videos...")

    # 3. Process in batches with Sleep to respect AI limits
    for i in range(0, len(to_process), BATCH_SIZE):
        batch = to_process[i:i+BATCH_SIZE]
        for v_id in batch:
            print(f"Refreshing: {db[v_id]['title']}")
            db[v_id]['title'] = ai_polish(db[v_id]['title'])
            db[v_id]['last_refreshed'] = today_str
        
        save_db(db) # Save progress after every batch
        if i + BATCH_SIZE < len(to_process):
            print(f"Batch complete. Sleeping {COOLDOWN}s to respect API limits...")
            time.sleep(COOLDOWN)

    # 4. Generate the "Fresh" HTML Hub
    cards_html = ""
    # We show ALL videos in the HTML, but the top 200 were just 'refreshed'
    for v_id, data in db.items():
        cards_html += f"""
        <div class="card">
            <script type="application/ld+json">
            {{
                "@context": "https://schema.org",
                "@type": "VideoObject",
                "name": "{data['title']}",
                "thumbnailUrl": "https://i.ytimg.com/vi/{v_id}/hqdefault.jpg",
                "uploadDate": "{data['last_refreshed']}",
                "embedUrl": "https://www.youtube.com/embed/{v_id}"
            }}
            </script>
            <div class="badge">Fresh Update</div>
            <h3>{data['title']}</h3>
            <div class="video-container">
                <iframe src="https://www.youtube.com/embed/{v_id}" loading="lazy" frameborder="0" allowfullscreen></iframe>
            </div>
            <a class="btn" href="{data['url']}" target="_blank">Watch Now</a>
        </div>
        """

    full_html = f"<!DOCTYPE html><html><head><meta charset='UTF-8'><title>SEO Hub</title><style>body{{font-family:sans-serif;background:#f4f4f4;display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:20px;padding:20px;}}.card{{background:#fff;padding:15px;border-radius:10px;box-shadow:0 2px 5px rgba(0,0,0,0.1);}}h3{{font-size:16px;height:40px;overflow:hidden;}}.video-container{{position:relative;padding-bottom:56.25%;height:0;overflow:hidden;border-radius:5px;}}.video-container iframe{{position:absolute;top:0;left:0;width:100%;height:100%;}}.btn{{display:block;text-align:center;background:#007bff;color:#fff;text-decoration:none;padding:10px;margin-top:10px;border-radius:5px;}}.badge{{background:red;color:white;font-size:10px;padding:2px 5px;border-radius:3px;width:fit-content;}}</style></head><body>{cards_html}</body></html>"
    
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(full_html)

if __name__ == "__main__":
    run_rolling_cycle()
