import os
import yt_dlp
from google import genai
import json

# --- CONFIGURATION ---
CHANNEL_HANDLE = "@HassanParacha-c3g"  # Change to your actual handle
PUBLIC_SITE_URL = "https://Hassan-paracha.github.io/my-seo-hub/"

def get_latest_content():
    base_url = f"https://www.youtube.com/{CHANNEL_HANDLE}"
    # Scraping Videos, Shorts, and Streams tabs
    tabs = ["/videos", "/shorts", "/streams"]
    latest_items = []
    
    ydl_opts = {
        'quiet': True,
        'extract_flat': True,
        'force_generic_extractor': True,
    }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for tab in tabs:
            try:
                result = ydl.extract_info(base_url + tab, download=False)
                if 'entries' in result and len(result['entries']) > 0:
                    item = result['entries'][0]
                    item['content_type'] = tab.replace("/", "").capitalize()
                    latest_items.append(item)
            except:
                continue
    return latest_items

def polish_metadata(item):
    # Initialize the modern 2026 Client
    client = genai.Client()
    
    raw_title = item.get('title', 'YouTube Content')
    c_type = item['content_type']
    
    prompt = f"""
    You are an SEO expert for a Pakistani YouTube channel.
    Content Type: {c_type}
    Hinglish Title: {raw_title}
    
    Task:
    1. Fix Roman Urdu/Hinglish spellings.
    2. Make it catchy for a Pakistani family audience.
    3. Add 2 high-volume English SEO keywords.
    4. Return ONLY the new title text without quotes.
    """
    
    try:
        # Using the current high-performance model
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt
        )
        return response.text.strip().replace('"', '')
    except Exception as e:
        print(f"AI Error: {e}")
        return raw_title

def generate_hub(items):
    cards_html = ""
    for item in items:
        new_title = polish_metadata(item)
        v_id = item['id']
        v_url = item.get('url', f"https://www.youtube.com/watch?v={v_id}")
        
        # SEO Video Schema
        schema = {
            "@context": "https://schema.org",
            "@type": "VideoObject",
            "name": new_title,
            "description": f"Watch our latest {item['content_type']} from Pakistan.",
            "thumbnailUrl": f"https://i.ytimg.com/vi/{v_id}/maxresdefault.jpg",
            "uploadDate": "2026-02-19",
            "embedUrl": f"https://www.youtube.com/embed/{v_id}"
        }

        cards_html += f"""
        <div class="card">
            <script type="application/ld+json">{json.dumps(schema)}</script>
            <div class="badge">{item['content_type']}</div>
            <h2>{new_title}</h2>
            <div class="video-container">
                <iframe src="https://www.youtube.com/embed/{v_id}" frameborder="0" allowfullscreen></iframe>
            </div>
            <a class="btn" href="{v_url}" target="_blank">View on YouTube</a>
        </div>
        """

    full_html = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Family Vlog SEO Hub</title>
        <style>
            body {{ font-family: 'Segoe UI', sans-serif; background: #f0f2f5; margin: 0; padding: 20px; display: flex; flex-wrap: wrap; justify-content: center; }}
            .card {{ background: #fff; border-radius: 15px; width: 350px; margin: 15px; padding: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
            h2 {{ font-size: 1.2rem; color: #1c1e21; height: 2.8em; overflow: hidden; }}
            .badge {{ background: #ff0000; color: #fff; padding: 5px 12px; border-radius: 50px; font-size: 10px; font-weight: bold; text-transform: uppercase; }}
            .video-container {{ position: relative; padding-bottom: 56.25%; height: 0; border-radius: 10px; overflow: hidden; margin: 10px 0; }}
            .video-container iframe {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; }}
            .btn {{ display: block; background: #0084ff; color: #fff; text-decoration: none; padding: 12px; border-radius: 8px; text-align: center; font-weight: bold; }}
        </style>
    </head>
    <body>{cards_html}</body>
    </html>
    """
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(full_html)

if __name__ == "__main__":
    content = get_latest_content()
    if content:
        generate_hub(content)
