# My Channel SEO

This repository runs a daily GitHub Actions job for the YouTube channel
`@hassanparachaoffical`. It syncs uploads and metadata, polishes changed titles
with Gemini, generates a paginated static site, and deploys it to GitHub Pages.
The workflow also commits `video_db.json` to the repository so channel state
persists between runs.

## Pipeline

1. Fetch the channel's uploads playlist and video metadata through YouTube Data
   API v3 when `YOUTUBE_API_KEY` is configured. Otherwise, use yt-dlp.
2. Sync original titles, descriptions, upload dates, durations, and removed
   videos into `video_db.json`. A title is polished only when it is new or its
   original YouTube title changes.
3. Batch up to 40 titles per Gemini request, validate the JSON response, and
   retry transient rate-limit and server errors.
4. Generate `site/` with pages of 24 videos, topic pages, sitemap.xml, and
   robots.txt. Videos use click-to-play thumbnail facades rather than loading
   iframes on page load.
5. Commit database updates and publish the generated artifact to GitHub Pages.

The scheduled run remains at 19:00 UTC. It can also be started manually with
the `workflow_dispatch` action.

## GitHub configuration

Required repository secret:

- `GEMINI_API_KEY` — API key for the Gemini title-polishing model.

Optional repository secret:

- `YOUTUBE_API_KEY` — enables complete, metadata-rich YouTube Data API sync.
  Without it, channel enumeration uses yt-dlp.

Enable GitHub Pages for this repository with the GitHub Actions source. The
workflow deploy job uses the `github-pages` environment and GitHub's Pages
deployment permissions; no personal access token or second repository is used.

## Environment variables

- `GEMINI_MODEL` — model name; defaults to `gemini-3.5-flash`.
- `SITE_BASE_URL` — absolute site URL used by canonical tags and sitemap;
  defaults to `https://hassan-paracha.github.io/my-channel-seo/`.
- `DRY_RUN=1` — skip YouTube and Gemini requests and database writes, but build
  `site/` from the current local `video_db.json`.

## Run locally

Use Python 3.12 or newer:

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest
DRY_RUN=1 python engine.py
```

For a live sync and title-polishing run, export `GEMINI_API_KEY` and optionally
`YOUTUBE_API_KEY`, then run `python engine.py`. The engine writes a rebuilt
`site/` directory and atomically updates `video_db.json`.
