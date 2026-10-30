#!/usr/bin/env python3
"""Find a trend, locate reusable visuals, then generate its script."""

import html
import json
import os
import re
import subprocess
import sys
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
OPENVERSE_API = "https://api.openverse.org/v1/images/"

USER_AGENT = "FreeCloudYouTubeStudio/4.0 (open-source project)"
MAX_TRENDS = 20
MAX_BYTES = 120 * 1024 * 1024
TIMEOUT = 30

AMBIGUOUS_TRENDS = {
    "collapse", "jaguar", "apple", "mercury", "phoenix",
    "delta", "python", "threads", "meta", "oracle", "pioneer",
}
GENERIC_TRENDS = {"news", "today", "latest", "weather", "live"}


def log(message):
    print(message, flush=True)


def request_json(url, headers=None, timeout=TIMEOUT):
    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def api_query(base_url, params, headers=None):
    url = base_url + "?" + urllib.parse.urlencode(params, doseq=True)
    return request_json(url, headers=headers)


def clean_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]*>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def words(value):
    return set(re.findall(r"[^\W_]{3,}", str(value).casefold(), re.UNICODE))


def license_is_allowed(name, url=""):
    """Only accept public domain, CC0, CC BY and CC BY-SA."""
    value = (clean_text(name) + " " + clean_text(url)).casefold()
    compact = re.sub(r"[\s_-]+", "", value)

    forbidden = (
        "noncommercial", "non-commercial", "no derivatives",
        "noderivatives", "all rights reserved", "ccbync",
        "ccbynd", "sampling+",
    )
    if any(term in value or term in compact for term in forbidden):
        return False

    if "public domain" in value or "publicdomain" in compact:
        return True
    if "cc0" in compact or "creativecommons.org/publicdomain/zero" in value:
        return True
    if "creativecommons.org/licenses/by-sa/" in value:
        return True
    if "creativecommons.org/licenses/by/" in value:
        return True
    if compact in {"ccby", "creativecommonsattribution"}:
        return True
    if "cc by-sa" in value or value.strip() == "cc by":
        return True
    return False


def query_variants(topic, title=""):
    """Search original wording and a simplified English-oriented query."""
    base = clean_text(f"{topic} {title}")
    variants = [base]

    # Keep names and useful nouns; remove common Russian question words.
    simplified = re.sub(
        r"\b(что|как|почему|когда|где|зачем|какой|какая|какие|"
        r"сегодня|это|за|ли|the|what|why|how|when|where)\b",
        " ", base, flags=re.IGNORECASE,
    )
    simplified = re.sub(r"\s+", " ", simplified).strip()
    if simplified and simplified.casefold() != base.casefold():
        variants.append(simplified)

    # Search exact topic separately to avoid over-specific title queries.
    if clean_text(topic) and clean_text(topic) not in variants:
        variants.append(clean_text(topic))

    return variants[:3]


def relevance_score(topic, title, tags=""):
    wanted = words(topic)
    available = words(f"{title} {tags}")
    if not wanted:
        return 0.0

    overlap = wanted & available
    if not overlap:
        return 0.0

    return len(overlap) / max(1, len(wanted))


def wikimedia_search(query, media_kind):
    response = api_query(WIKIMEDIA_API, {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": 6,
        "gsrlimit": 30,
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
    })

    matches = []
    pages = response.get("query", {}).get("pages", {}) or {}

    for page in pages.values():
        images = page.get("imageinfo", [])
        if not images:
            continue

        info = images[0]
        media_url = info.get("url", "")
        mime = str(info.get("mime", "")).lower()

        if not media_url.startswith("https://"):
            continue

        if media_kind == "video":
            valid = (
                mime in ("video/webm", "video/ogg", "video/mp4")
                or media_url.lower().endswith(
                    (".webm", ".ogv", ".ogg", ".mp4")
                )
            )
        else:
            valid = mime in ("image/jpeg", "image/png", "image/webp")

        if not valid:
            continue

        metadata = info.get("extmetadata", {}) or {}
        license_name = clean_text(
            (metadata.get("LicenseShortName") or {}).get("value", "")
        )
        license_url = clean_text(
            (metadata.get("LicenseUrl") or {}).get("value", "")
        )

        if not license_is_allowed(license_name, license_url):
            continue

        filename = page.get("title", "").removeprefix("File:")
        source_page = (
            "https://commons.wikimedia.org/wiki/"
            + urllib.parse.quote(page.get("title", "").replace(" ", "_"))
        )

        matches.append({
            "provider": "Wikimedia Commons",
            "download_url": media_url,
            "page_url": source_page,
            "creator": clean_text(
                (metadata.get("Artist") or {}).get(
                    "value", "Not specified"
                )
            ),
            "license": license_name,
            "license_url": license_url,
            "filename": filename,
            "size": int(info.get("size") or 0),
            "media_type": media_kind,
            "match_text": filename,
        })

    return matches


def search_openverse(topic, title):
    """Search openly licensed images without requiring an API key."""
    matches = []

    for query in query_variants(topic, title):
        try:
            data = api_query(OPENVERSE_API, {
                "q": query[:180],
                "page_size": 20,
                "license_type": "commercial",
                "mature": "false",
                "filter_dead": "true",
                "size": "medium,large",
            })
        except Exception as error:
            log(f"  Openverse query failed: {error}")
            continue

        for item in data.get("results", []):
            image_url = item.get("url", "")
            page_url = item.get("foreign_landing_url", "")
            license_name = str(item.get("license", "")).lower()
            license_url = item.get("license_url", "")

            # Do not rely on the broad commercial filter alone.
            if license_name not in {"cc0", "pdm", "by", "by-sa"}:
                continue
            if not image_url.startswith("https://"):
                continue
            if not page_url.startswith("https://"):
                continue

            width = int(item.get("width") or 0)
            height = int(item.get("height") or 0)
            if width and height and min(width, height) < 400:
                continue

            title_text = clean_text(item.get("title", ""))
            tags = " ".join(
                str(tag.get("name", ""))
                for tag in item.get("tags", [])
                if isinstance(tag, dict)
            )
            score = relevance_score(topic, title_text, tags)

            # Search engine relevance is useful, but not a guarantee.
            if score < 0.15:
                continue

            matches.append((
                score,
                {
                    "provider": "Openverse",
                    "download_url": image_url,
                    "page_url": page_url,
                    "creator": clean_text(
                        item.get("creator") or "Not specified"
                    ),
                    "license": license_name.upper(),
                    "license_url": license_url,
                    "filename": title_text or "Openverse image",
                    "size": int(item.get("filesize") or 0),
                    "media_type": "image",
                    "match_text": f"{title_text} {tags}",
                },
            ))

    matches.sort(key=lambda pair: pair[0], reverse=True)
    return matches[0][1] if matches else None


def search_wikimedia_media(topic, title, media_kind):
    matches = []

    for query in query_variants(topic, title):
        try:
            results = wikimedia_search(query, media_kind)
        except Exception as error:
            log(f"  Wikimedia query failed: {error}")
            continue

        for item in results:
            score = relevance_score(
                topic, item.get("filename", ""), query
            )
            if score >= 0.15:
                matches.append((score, item))

    matches.sort(key=lambda pair: pair[0], reverse=True)
    return matches[0][1] if matches else None


def search_pixabay(topic, title):
    api_key = os.getenv("PIXABAY_API_KEY", "").strip()
    if not api_key:
        log("  Pixabay: API key absent; skipping.")
        return None

    matches = []
    for query in query_variants(topic, title):
        data = api_query(PIXABAY_API, {
            "key": api_key,
            "q": query[:100],
            "per_page": 20,
            "safesearch": "true",
            "video_type": "all",
        })

        for video in data.get("hits", []):
            variants = video.get("videos", {}) or {}
            chosen = next((
                variants.get(size, {})
                for size in ("large", "medium", "small", "tiny")
                if (variants.get(size, {}) or {}).get("url", "").startswith(
                    "https://"
                )
            ), None)

            if not chosen:
                continue

            tags = clean_text(video.get("tags", ""))
            score = relevance_score(topic, "", tags)
            if score < 0.15:
                continue

            matches.append((
                score,
                {
                    "provider": "Pixabay",
                    "download_url": chosen["url"],
                    "page_url": video.get("pageURL", ""),
                    "creator": video.get("user", "Not specified"),
                    "license": "Pixabay Content License",
                    "license_url": "https://pixabay.com/service/license-summary/",
                    "filename": f"Pixabay video {video.get('id', '')}",
                    "size": 0,
                    "media_type": "video",
                    "match_text": tags,
                },
            ))

    matches.sort(key=lambda pair: pair[0], reverse=True)
    return matches[0][1] if matches else None


def search_internet_archive(topic, title):
    matches = []

    for query in query_variants(topic, title):
        try:
            response = api_query(ARCHIVE_SEARCH, {
                "q": f'({query}) AND mediatype:movies',
                "fl[]": ["identifier", "title"],
                "rows": 10,
                "output": "json",
            })
        except Exception as error:
            log(f"  Internet Archive search failed: {error}")
            continue

        for doc in response.get("response", {}).get("docs", []):
            identifier = str(doc.get("identifier", "")).strip()
            if not identifier:
                continue

            try:
                metadata = request_json(
                    ARCHIVE_METADATA
                    + urllib.parse.quote(identifier, safe="")
                )
            except Exception as error:
                log(f"  Archive metadata skipped: {error}")
                continue

            item_meta = metadata.get("metadata", {}) or {}
            rights = clean_text(
                item_meta.get("licenseurl")
                or item_meta.get("rights")
                or item_meta.get("license")
                or ""
            )
            if not license_is_allowed(rights):
                continue

            files = [
                item for item in metadata.get("files", [])
                if str(item.get("name", "")).lower().endswith(
                    (".mp4", ".webm", ".ogv")
                )
                and not item.get("private")
            ]
            files.sort(key=lambda item: (
                0 if item.get("name", "").lower().endswith(".mp4") else 1,
                int(item.get("size") or 0),
            ))

            chosen = next((
                item for item in files
                if int(item.get("size") or 0) <= MAX_BYTES
            ), None)
            if not chosen:
                continue

            title_text = clean_text(doc.get("title", ""))
            score = relevance_score(topic, title_text, identifier)
            if score < 0.15:
                continue

            file_url = (
                "https://archive.org/download/"
                + urllib.parse.quote(identifier, safe="")
                + "/"
                + urllib.parse.quote(chosen["name"], safe="/")
            )
            matches.append((
                score,
                {
                    "provider": "Internet Archive",
                    "download_url": file_url,
                    "page_url": "https://archive.org/details/" + identifier,
                    "creator": clean_text(
                        item_meta.get("creator", "Not specified")
                    ),
                    "license": rights,
                    "license_url": (
                        rights if rights.startswith("https://") else ""
                    ),
                    "filename": chosen["name"],
                    "size": int(chosen.get("size") or 0),
                    "media_type": "video",
                    "match_text": title_text,
                },
            ))

    matches.sort(key=lambda pair: pair[0], reverse=True)
    return matches[0][1] if matches else None


def search_video(topic, title):
    providers = [
        ("Wikimedia Commons video", lambda: search_wikimedia_media(
            topic, title, "video"
        )),
        ("Pixabay video", lambda: search_pixabay(topic, title)),
        ("Internet Archive video", lambda: search_internet_archive(
            topic, title
        )),
    ]

    for name, search_function in providers:
        try:
            log(f"  Searching {name}...")
            result = search_function()
            if result:
                log(f"  Found visual on {result['provider']}.")
                return result
            log(f"  No suitable result from {name}.")
        except Exception as error:
            log(f"  {name} unavailable: {type(error).__name__}: {error}")

    image_providers = [
        ("Openverse", lambda: search_openverse(topic, title)),
        ("Wikimedia Commons image", lambda: search_wikimedia_media(
            topic, title, "image"
        )),
    ]

    for name, search_function in image_providers:
        try:
            log(f"  Searching {name} for a licensed image...")
            result = search_function()
            if result:
                log(f"  Found image on {result['provider']}.")
                return result
            log(f"  No suitable image from {name}.")
        except Exception as error:
            log(f"  {name} unavailable: {type(error).__name__}: {error}")

    return None


def download_media(url, destination, max_bytes, expected_size=0):
    if expected_size and int(expected_size) > max_bytes:
        raise ValueError("Media exceeds configured size limit.")

    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT}
    )
    total = 0

    with urllib.request.urlopen(request, timeout=60) as response:
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("Download exceeded size limit.")
                output.write(chunk)

    if total < 5_000:
        raise ValueError("Downloaded media is too small.")


def prepare_image_footage(image, duration):
    temporary = OUT / "source_image.download"
    footage = OUT / "footage.mp4"
    temporary.unlink(missing_ok=True)
    footage.unlink(missing_ok=True)

    try:
        download_media(
            image["download_url"], temporary,
            30 * 1024 * 1024, image.get("size", 0),
        )
        result = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-framerate", "30", "-i", str(temporary),
            "-t", str(duration),
            "-vf",
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,"
            "zoompan=z='min(zoom+0.0005,1.08)':"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            "d=1:s=1080x1920:fps=30,format=yuv420p",
            "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "25", "-movflags", "+faststart", str(footage),
        ], capture_output=True, text=True, timeout=240)

        if (
            result.returncode != 0
            or not footage.exists()
            or footage.stat().st_size < 100_000
        ):
            raise ValueError(
                (result.stderr or "FFmpeg image animation failed.")[-1500:]
            )
        return footage
    finally:
        temporary.unlink(missing_ok=True)


def prepare_footage(video, duration):
    temporary = OUT / "source_clip.download"
    footage = OUT / "footage.mp4"
    temporary.unlink(missing_ok=True)
    footage.unlink(missing_ok=True)

    try:
        download_media(
            video["download_url"], temporary,
            MAX_BYTES, video.get("size", 0),
        )
        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(temporary),
        ], capture_output=True, text=True, timeout=30)

        if probe.returncode != 0 or not probe.stdout.strip():
            raise ValueError("ffprobe could not read source video.")
        if float(probe.stdout.strip()) < 2:
            raise ValueError("Source clip is shorter than two seconds.")

        result = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-stream_loop", "-1", "-i", str(temporary),
            "-t", str(duration),
            "-vf",
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,fps=30,format=yuv420p",
            "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "25", "-movflags", "+faststart", str(footage),
        ], capture_output=True, text=True, timeout=240)

        if (
            result.returncode != 0
            or not footage.exists()
            or footage.stat().st_size < 100_000
        ):
            raise ValueError(
                (result.stderr or "FFmpeg video preparation failed.")[-1500:]
            )
        return footage
    finally:
        temporary.unlink(missing_ok=True)


def generate_script(candidate, region, original_report):
    topic = str(candidate.get("topic", "")).strip()
    isolated = dict(original_report)
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
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=180,
    )

    if result.returncode != 0:
        log("  Script generation failed.")
        if result.stdout.strip():
            log("  STDOUT: " + result.stdout.strip()[-2000:])
        if result.stderr.strip():
            log("  STDERR: " + result.stderr.strip()[-2000:])
        return None

    try:
        data = json.loads(SCRIPT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        log(f"  Invalid generated script JSON: {error}")
        return None

    if str(data.get("topic", "")).strip().casefold() != topic.casefold():
        log("  Generated script topic does not match selected trend.")
        return None
    if not str(data.get("narration", "")).strip():
        log("  Generated script has no narration.")
        return None
    return data


def write_attribution(topic, script_data, visual, attempt):
    attribution = (
        f"Trend: {topic}\n"
        f"Title: {script_data.get('title', '')}\n"
        f"Provider: {visual.get('provider', '')}\n"
        f"File: {visual.get('filename', '')}\n"
        f"Creator: {visual.get('creator', 'Not specified')}\n"
        f"License: {visual.get('license', 'Not specified')}\n"
        f"License URL: {visual.get('license_url', '')}\n"
        f"Source page: {visual.get('page_url', '')}\n"
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
        "visual_provider": visual.get("provider"),
        "visual_source": visual.get("page_url"),
        "visual_license": visual.get("license"),
        "visual_license_url": visual.get("license_url"),
        "visual_creator": visual.get("creator"),
        "visual_search_attempt": attempt,
    })
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main():
    region = os.getenv("TARGET_REGION", "KZ").strip().upper()
    duration = os.getenv("VIDEO_DURATION", "30").strip()

    if region not in {"KZ", "RU", "US", "GB"}:
        raise RuntimeError(f"Unsupported region: {region}")
    if duration not in {"15", "30", "45", "60"}:
        raise RuntimeError("VIDEO_DURATION must be 15, 30, 45 or 60.")
    if not os.getenv("GROQ_API_KEY", "").strip():
        raise RuntimeError("Missing GROQ_API_KEY secret.")
    if not TRENDS.exists():
        raise RuntimeError(f"Trend report not found: {TRENDS}")

    original_text = TRENDS.read_text(encoding="utf-8")
    report = json.loads(original_text)
    if isinstance(report, list):
        candidates = report
        report_object = {"candidates": report}
    else:
        report_object = report
        candidates = (
            report.get("shortlist")
            or report.get("candidates")
            or report.get("trends")
            or []
        )

    filtered = []
    seen = set()

    for item in candidates:
        if not isinstance(item, dict):
            continue
        topic = str(item.get("topic", "")).strip()
        item_region = str(item.get("region", "")).upper()
        regions = item.get("regions") or [item_region]
        if not topic:
            continue
        if item_region and item_region != region and region not in regions:
            continue

        normalized = re.sub(r"\s+", " ", topic.casefold()).strip()
        if normalized in AMBIGUOUS_TRENDS or normalized in GENERIC_TRENDS:
            log(f"Skipping unsuitable trend: {topic}")
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        filtered.append(item)

    filtered.sort(
        key=lambda item: (
            float(item.get("score") or 0),
            str(item.get("published_at") or ""),
        ),
        reverse=True,
    )
    filtered = filtered[:MAX_TRENDS]

    if not filtered:
        raise RuntimeError(f"No suitable trends for region {region}.")

    OUT.mkdir(parents=True, exist_ok=True)
    failures = []

    try:
        for number, candidate in enumerate(filtered, 1):
            topic = str(candidate["topic"]).strip()
            log(f"\n[{number}/{len(filtered)}] Checking trend: {topic}")

            # Search for a visual BEFORE spending a Groq script request.
            visual = search_video(topic, "")
            if not visual:
                failures.append(f"{topic}: no reusable visual found")
                log("  Skipping topic; trying the next trend.")
                continue

            try:
                if visual.get("media_type") == "image":
                    prepare_image_footage(visual, duration)
                else:
                    prepare_footage(visual, duration)
            except Exception as error:
                failures.append(f"{topic}: visual preparation failed: {error}")
                log("  Visual could not be prepared; trying next trend.")
                continue

            log("  Visual prepared. Generating the matching script...")
            script_data = generate_script(candidate, region, report_object)
            if not script_data:
                failures.append(f"{topic}: script generation failed")
                (OUT / "footage.mp4").unlink(missing_ok=True)
                continue

            write_attribution(topic, script_data, visual, number)
            log(
                f"SUCCESS: '{topic}' selected with "
                f"{visual['provider']} visual."
            )
            return

    finally:
        TRENDS.write_text(original_text, encoding="utf-8")

    raise RuntimeError(
        f"No usable visual/script found after checking "
        f"{len(filtered)} trends. " + "; ".join(failures)
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"FATAL: {type(error).__name__}: {error}")
        raise
