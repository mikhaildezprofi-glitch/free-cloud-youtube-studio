#!/usr/bin/env python3
"""Find a trend, generate its script, and locate reusable video footage."""

import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRENDS = ROOT / "data/trends/latest.json"
SCRIPT = ROOT / "data/scripts/latest.json"
OUT = ROOT / "output"

WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
PIXABAY_API = "https://pixabay.com/api/videos/"
ARCHIVE_SEARCH = "https://archive.org/advancedsearch.php"
ARCHIVE_METADATA = "https://archive.org/metadata/"

USER_AGENT = "FreeCloudYouTubeStudio/3.0"
MAX_TRENDS = 10
MAX_BYTES = 120 * 1024 * 1024
TIMEOUT = 30


def log(message):
    print(message, flush=True)


def request_json(url, headers=None, timeout=TIMEOUT):
    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)

    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def words(value):
    return set(re.findall(r"[^\W_]{3,}", str(value).casefold(), re.UNICODE))


def clean_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]*>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def api_query(base_url, params, headers=None):
    url = base_url + "?" + urllib.parse.urlencode(params)
    return request_json(url, headers=headers)


def license_is_allowed(name):
    """Conservative allowlist for explicitly reusable licenses."""
    value = clean_text(name).casefold()
    compact = re.sub(r"[\s_-]+", "", value)

    if any(term in value for term in (
        "noncommercial", "non-commercial", "no derivatives",
        "noderivatives", "all rights reserved", "copyright",
    )):
        return False

    if any(term in compact for term in (
        "ccbync", "ccbynd", "ccbyncnd", "ccbyncsa",
    )):
        return False

    return (
        "cc0" in compact
        or "public domain" in value
        or "publicdomain" in compact
        or "cc by-sa" in value
        or "cc by " in value
        or value.strip() == "cc by"
        or compact in {"creativecommonsattribution", "ccby"}
    )


def search_wikimedia(topic, title):
    query = f"{topic} {title}".strip()
    response = api_query(WIKIMEDIA_API, {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": query + " filetype:video",
        "gsrnamespace": 6,
        "gsrlimit": 30,
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
    })

    wanted = words(query)
    matches = []

    pages = response.get("query", {}).get("pages", {}) or {}
    for page in pages.values():
        images = page.get("imageinfo", [])
        if not images:
            continue

        info = images[0]
        media_url = info.get("url", "")
        mime = info.get("mime", "").lower()

        if not media_url.startswith("https://"):
            continue
        if mime not in ("video/webm", "video/ogg", "video/mp4"):
            if not media_url.lower().endswith((".webm", ".ogv", ".ogg", ".mp4")):
                continue

        metadata = info.get("extmetadata", {}) or {}
        license_name = clean_text(
            (metadata.get("LicenseShortName") or {}).get("value", "")
        )
        if not license_is_allowed(license_name):
            continue

        filename = page.get("title", "").removeprefix("File:")
        overlap = wanted & words(filename)
        if not overlap:
            continue
        if len(wanted) >= 3 and len(overlap) < 2:
            continue

        source_page = (
            "https://commons.wikimedia.org/wiki/"
            + urllib.parse.quote(page.get("title", "").replace(" ", "_"))
        )
        matches.append((
            len(overlap) / max(1, len(wanted)),
            {
                "provider": "Wikimedia Commons",
                "download_url": media_url,
                "page_url": source_page,
                "creator": clean_text(
                    (metadata.get("Artist") or {}).get("value", "Not specified")
                ),
                "license": license_name,
                "license_url": clean_text(
                    (metadata.get("LicenseUrl") or {}).get("value", "")
                ),
                "filename": filename,
                "size": int(info.get("size") or 0),
            },
        ))

    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1] if matches and matches[0][0] >= 0.15 else None


def search_pixabay(topic, title):
    api_key = os.getenv("PIXABAY_API_KEY", "").strip()
    if not api_key:
        log("  Pixabay: PIXABAY_API_KEY not configured; skipping.")
        return None

    query = f"{topic} {title}".strip()
    response = api_query(PIXABAY_API, {
        "key": api_key,
        "q": query,
        "per_page": 20,
        "safesearch": "true",
        "video_type": "all",
    })

    wanted = words(query)
    matches = []

    for video in response.get("hits", []):
        sets = video.get("videos", {}) or {}
        chosen = None

        for size_name in ("medium", "large", "small", "tiny"):
            candidate = sets.get(size_name, {}) or {}
            if candidate.get("url", "").startswith("https://"):
                chosen = candidate
                break

        if not chosen:
            continue

        tags = clean_text(video.get("tags", ""))
        searchable = words(tags)
        overlap = wanted & searchable

        # Do not score the query against itself; require tag relevance.
        if not overlap:
            continue

        matches.append((
            len(overlap) / max(1, len(wanted)),
            {
                "provider": "Pixabay",
                "download_url": chosen["url"],
                "page_url": video.get("pageURL", ""),
                "creator": video.get("user", "Not specified"),
                "license": "Pixabay Content License",
                "license_url": "https://pixabay.com/service/license-summary/",
                "filename": f"Pixabay video {video.get('id', '')}",
                "size": 0,
            },
        ))

    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1] if matches else None


def search_internet_archive(topic, title):
    """Use Archive.org only when item metadata explicitly indicates reuse."""
    query = f"{topic} {title}".strip()
    response = api_query(ARCHIVE_SEARCH, {
        "q": f'({query}) AND mediatype:movies',
        "fl[]": ["identifier", "title"],
        "rows": 10,
        "output": "json",
    })

    docs = response.get("response", {}).get("docs", []) or []
    wanted = words(query)
    matches = []

    for doc in docs:
        identifier = str(doc.get("identifier", "")).strip()
        if not identifier:
            continue

        try:
            metadata = request_json(
                ARCHIVE_METADATA + urllib.parse.quote(identifier, safe="")
            )
        except Exception as error:
            log(f"  Internet Archive metadata skipped: {error}")
            continue

        item_meta = metadata.get("metadata", {}) or {}
        rights = clean_text(
            item_meta.get("licenseurl")
            or item_meta.get("rights")
            or item_meta.get("license")
            or ""
        )

        # Archive.org is not itself a license. Require explicit reuse terms.
        if not license_is_allowed(rights):
            continue

        files = metadata.get("files", []) or []
        video_files = [
            f for f in files
            if str(f.get("name", "")).lower().endswith(
                (".mp4", ".webm", ".ogv", ".mov")
            )
            and not f.get("private")
        ]
        if not video_files:
            continue

        video_files.sort(
            key=lambda f: (
                0 if f.get("name", "").lower().endswith(".mp4") else 1,
                int(f.get("size") or 0),
            )
        )
        chosen = next(
            (f for f in video_files if int(f.get("size") or 0) <= MAX_BYTES),
            None,
        )
        if not chosen:
            continue

        identifier_words = words(doc.get("title", "") + " " + identifier)
        overlap = wanted & identifier_words
        if not overlap:
            continue

        file_url = (
            "https://archive.org/download/"
            + urllib.parse.quote(identifier, safe="")
            + "/"
            + urllib.parse.quote(chosen["name"], safe="/")
        )

        matches.append((
            len(overlap) / max(1, len(wanted)),
            {
                "provider": "Internet Archive",
                "download_url": file_url,
                "page_url": "https://archive.org/details/" + identifier,
                "creator": clean_text(item_meta.get("creator", "Not specified")),
                "license": rights,
                "license_url": rights if rights.startswith("https://") else "",
                "filename": chosen["name"],
                "size": int(chosen.get("size") or 0),
            },
        ))

    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1] if matches else None


def search_video(topic, title):
    providers = [
        ("Wikimedia Commons", search_wikimedia),
        ("Pixabay", search_pixabay),
        ("Internet Archive", search_internet_archive),
    ]

    for name, search_function in providers:
        try:
            log(f"  Searching {name}...")
            result = search_function(topic, title)
            if result:
                log(f"  Found a candidate on {name}.")
                return result
            log(f"  No suitable candidate on {name}.")
        except Exception as error:
            log(f"  {name} search failed: {type(error).__name__}: {error}")

    return None


def download_video(url, destination, expected_size=0):
    if expected_size and int(expected_size) > MAX_BYTES:
        raise ValueError("Video exceeds the 120 MB limit.")

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    total = 0

    with urllib.request.urlopen(request, timeout=60) as response:
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break

                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError("Download exceeded the 120 MB limit.")
                output.write(chunk)

    if total < 100_000:
        raise ValueError("Downloaded file is too small to be a usable clip.")


def generate_script(candidate, region, original_report):
    topic = str(candidate.get("topic", "")).strip()
    isolated = dict(original_report) if isinstance(original_report, dict) else {}
    isolated["shortlist"] = [candidate]
    isolated["candidates"] = [candidate]

    TRENDS.write_text(
        json.dumps(isolated, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["TARGET_REGION"] = region

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/generate_ai_script.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    if result.returncode != 0:
        log("  Script generation failed.")
        if result.stdout.strip():
            log("  STDOUT: " + result.stdout.strip()[-2500:])
        if result.stderr.strip():
            log("  STDERR: " + result.stderr.strip()[-2500:])
        return None

    try:
        data = json.loads(SCRIPT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        log(f"  Generated script JSON is invalid: {error}")
        return None

    if str(data.get("topic", "")).strip().casefold() != topic.casefold():
        log("  Generated script topic does not match selected trend.")
        return None
    if not str(data.get("narration", "")).strip():
        log("  Generated script has no narration.")
        return None

    return data


def prepare_footage(video, duration):
    temporary = OUT / "source_clip.download"
    footage = OUT / "footage.mp4"
    temporary.unlink(missing_ok=True)
    footage.unlink(missing_ok=True)

    try:
        download_video(video["download_url"], temporary, video.get("size", 0))

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(temporary),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if probe.returncode != 0 or not probe.stdout.strip():
            raise ValueError("ffprobe could not read the downloaded video.")
        if float(probe.stdout.strip()) < 2:
            raise ValueError("Source clip is shorter than two seconds.")

        render = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-y", "-stream_loop", "-1", "-i", str(temporary),
                "-t", str(duration),
                "-vf",
                "scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,fps=30,format=yuv420p",
                "-an", "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "25", "-movflags", "+faststart", str(footage),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )

        if (
            render.returncode != 0
            or not footage.exists()
            or footage.stat().st_size < 100_000
        ):
            raise ValueError(
                (render.stderr or "FFmpeg failed to prepare the clip.")[-1500:]
            )

        return footage
    finally:
        temporary.unlink(missing_ok=True)


def write_attribution(topic, script_data, video, attempt):
    attribution = (
        f"Trend: {topic}\n"
        f"Title: {script_data.get('title', '')}\n"
        f"Provider: {video.get('provider', '')}\n"
        f"File: {video.get('filename', '')}\n"
        f"Creator: {video.get('creator', 'Not specified')}\n"
        f"License: {video.get('license', 'Not specified')}\n"
        f"License URL: {video.get('license_url', '')}\n"
        f"Source page: {video.get('page_url', '')}\n"
    )
    (OUT / "attribution.txt").write_text(attribution, encoding="utf-8")

    metadata_path = OUT / "metadata.json"
    metadata = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    metadata.update({
        "topic": topic,
        "visual_provider": video.get("provider"),
        "visual_source": video.get("page_url"),
        "visual_license": video.get("license"),
        "visual_license_url": video.get("license_url"),
        "visual_creator": video.get("creator"),
        "visual_search_attempt": attempt,
    })
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main():
    region = os.getenv("TARGET_REGION", "KZ").strip().upper()
    duration = os.getenv("VIDEO_DURATION", "30").strip()

    if duration not in {"15", "30", "45", "60"}:
        raise RuntimeError("VIDEO_DURATION must be 15, 30, 45 or 60.")
    if not os.getenv("GROQ_API_KEY", "").strip():
        raise RuntimeError("Missing GROQ_API_KEY secret.")

    original_text = TRENDS.read_text(encoding="utf-8")
    report = json.loads(original_text)

    if isinstance(report, list):
        candidates = report
    else:
        candidates = (
            report.get("shortlist")
            or report.get("candidates")
            or report.get("trends")
            or []
        )

    filtered = []
    seen = set()

    for item in candidates:
        topic = str(item.get("topic", "")).strip()
        item_region = str(item.get("region", "")).upper()
        regions = item.get("regions", [item_region])

        if not topic:
            continue
        if item_region and item_region != region and region not in regions:
            continue
        if topic.casefold() in seen:
            continue

        seen.add(topic.casefold())
        filtered.append(item)

    filtered.sort(
        key=lambda item: float(item.get("score") or 0),
        reverse=True,
    )
    filtered = filtered[:MAX_TRENDS]

    if not filtered:
        raise RuntimeError(f"No trends available for region {region}.")

    OUT.mkdir(parents=True, exist_ok=True)
    failures = []

    try:
        for number, candidate in enumerate(filtered, 1):
            topic = str(candidate["topic"]).strip()
            log(f"[{number}/{len(filtered)}] Trend: {topic}")

            script_data = generate_script(candidate, region, report)
            if not script_data:
                failures.append(f"{topic}: script generation failed")
                continue

            video = search_video(topic, str(script_data.get("title", "")))
            if not video:
                failures.append(f"{topic}: no usable video")
                continue

            try:
                prepare_footage(video, duration)
            except Exception as error:
                log(f"  Could not prepare selected video: {error}")
                failures.append(f"{topic}: video processing failed")
                continue

            write_attribution(topic, script_data, video, number)
            log(f"SUCCESS: prepared video for '{topic}' using {video['provider']}.")
            return
    finally:
        TRENDS.write_text(original_text, encoding="utf-8")

    raise RuntimeError(
        f"No usable video found in the first {len(filtered)} trends. "
        + "; ".join(failures)
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"FATAL: {type(error).__name__}: {error}")
        raise
