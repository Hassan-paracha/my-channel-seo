import os, yt_dlp, google.generativeai as genai

# 1. Scrape latest video (No API Key needed)
def get_latest():
    # Replace with your channel handle or URL
    url = "https://www.youtube.com/@YourChannelHandle/videos"
    with yt_dlp.YoutubeDL({'quiet': True, 'extract_flat': True}) as ydl:
        return ydl.extract_info(url, download=False)['entries'][0]

# 2. AI Polisher (Fixing your Hinglish)
def polish(title):
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
    model = genai.GenerativeModel('gemini-pro')
    # Custom instruction for your niche
    prompt = f"Convert this Roman Urdu/Hinglish title into a high-authority Pakistani family vlog title. Fix spellings and add 2 SEO keywords: {title}"
    return model.generate_content(prompt).text

# 3. Create the SEO Webpage
v = get_latest()
new_title = polish(v['title'])
html = f"<html><head><title>{new_title}</title></head><body><h1>{new_title}</h1>" \
       f"<iframe src='https://www.youtube.com/embed/{v['id']}'></iframe></body></html>"

with open("index.html", "w") as f: f.write(html)
print(f"Optimized: {new_title}")
