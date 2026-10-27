#!/usr/bin/env python3
"""Find a trend, generate its script, and locate reusable video footage."""

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

USER_AGENT = "FreeCloudYouTubeStudio/3.2"
MAX_TRENDS = 10
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


def words(value):
    return set(
        re.findall(r"[^\W_]{3,}", str(value).casefold(), re.UNICODE)
    )


def clean_text(value):
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]*>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def api_query(base_url, params, headers=None):
    url = base_url + "?" + urllib.parse.urlencode(params, doseq=True)
    return request_json(url, headers=headers)


def license_is_allowed(name):
    """Allow recognized public-domain, CC0, CC BY and CC BY-SA licenses."""
    value = clean_text(name).casefold()
    compact = re.sub(r"[\s_-]+", "", value)
    compact = compact.replace("https://", "").replace("http://", "")

    if any(term in value for term in (
        "noncommercial",
        "non-commercial",
        "no derivatives",
        "noderivatives",
        "all rights reserved",
    )):
        return False

    if any(term in compact for term in (
        "ccbync",
        "ccbynd",
        "ccbyncnd",
        "ccbyncsa",
    )):
        return False

    if "creativecommons.org/licenses/by-nc" in value:
        return False
    if "creativecommons.org/licenses/by-nd" in value:
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
    if "cc by-sa" in value or "cc by " in value or value.strip() == "cc by":
        return True

    return False


def wikimedia_search(topic, title, media_kind):
    """Search Wikimedia Commons for reusable video or still-image media."""
    query = f"{topic} {title}".strip()

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

    wanted = words(topic)
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
            valid_mime = mime in ("video/webm", "video/ogg", "video/mp4")
            valid_extension = media_url.lower().endswith(
                (".webm", ".ogv", ".ogg", ".mp4")
            )
            if not valid_mime and not valid_extension:
                continue
        elif mime not in ("image/jpeg", "image/png", "image/webp"):
            continue

        metadata = info.get("extmetadata", {}) or {}

        license_name = clean_text(
            (metadata.get("LicenseShortName") or {}).get("value", "")
        )
        license_url = clean_text(
            (metadata.get("LicenseUrl") or {}).get("value", "")
        )

        if not license_is_allowed(license_name or license_url):
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
                "provider": (
                    "Wikimedia Commons"
                    if media_kind == "video"
                    else "Wikimedia Commons (animated still image)"
                ),
                "download_url": media_url,
                "page_url": source_page,
                "creator": clean_text(
                    (metadata.get("Artist") or {}).get(
                        "value", "Not specified"
                    )
                ),
                "license": license_name or license_url,
                "license_url": license_url,
                "filename": filename,
                "size": int(info.get("size") or 0),
                "media_type": media_kind,
            },
        ))

    matches.sort(key=lambda item: item[0], reverse=True)
    minimum_score = 0.15 if media_kind == "video" else 0.20

    return (
        matches[0][1]
        if matches and matches[0][0] >= minimum_score
        else None
    )


def search_wikimedia(topic, title):
    return wikimedia_search(topic, title, "video")


def search_wikimedia_image(topic, title):
    return wikimedia_search(topic, title, "image")


def search_pixabay(topic, title):
    api_key = os.getenv("PIXABAY_API_KEY", "").strip()

    if not api_key:
        log("  Pixabay: PIXABAY_API_KEY not configured; skipping.")
        return None

    response = api_query(PIXABAY_API, {
        "key": api_key,
        "q": f"{topic} {title}".strip(),
        "per_page": 20,
        "safesearch": "true",
        "video_type": "all",
    })

    wanted = words(topic)
    matches = []

    for video in response.get("hits", []):
        variants = video.get("videos", {}) or {}
        chosen = None

        for size_name in ("medium", "large", "small", "tiny"):
            candidate = variants.get(size_name, {}) or {}
            if candidate.get("url", "").startswith("https://"):
                chosen = candidate
                break

        if not chosen:
            continue

        overlap = wanted & words(clean_text(video.get("tags", "")))
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
                "media_type": "video",
            },
        ))

    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1] if matches else None


def search_internet_archive(topic, title):
    """Use Archive.org only when metadata explicitly indicates reuse."""
    query = f"{topic} {title}".strip()
    wanted = words(topic)

    response = api_query(ARCHIVE_SEARCH, {
        "q": f'({query}) AND mediatype:movies',
        "fl[]": ["identifier", "title"],
        "rows": 10,
        "output": "json",
    })

    docs = response.get("response", {}).get("docs", []) or []
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

        if not license_is_allowed(rights):
            continue

        video_files = [
            item for item in (metadata.get("files", []) or [])
            if str(item.get("name", "")).lower().endswith(
                (".mp4", ".webm", ".ogv", ".mov")
            )
            and not item.get("private")
        ]

        video_files.sort(key=lambda item: (
            0 if item.get("name", "").lower().endswith(".mp4") else 1,
            int(item.get("size") or 0),
        ))

        chosen = next(
            (
                item for item in video_files
                if int(item.get("size") or 0) <= MAX_BYTES
            ),
            None,
        )

        if not chosen:
            continue

        overlap = wanted & words(
            doc.get("title", "") + " " + identifier
        )
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
            log(f"  Searching {name} for video...")
            result = search_function(topic, title)

            if result:
                log(f"  Found a candidate on {name}.")
                return result

            log(f"  No suitable candidate on {name}.")

        except Exception as error:
            log(
                f"  {name} search failed: "
                f"{type(error).__name__}: {error}"
            )

    try:
        log("  Searching Wikimedia Commons for a licensed still image...")
        image = search_wikimedia_image(topic, title)

        if image:
            log("  Found a licensed image; FFmpeg will animate it.")
            return image

        log("  No suitable licensed still image found.")

    except Exception as error:
        log(
            "  Wikimedia image search failed: "
            f"{type(error).__name__}: {error}"
        )

    return None


def download_media(url, destination, max_bytes, expected_size=0):
    if expected_size and int(expected_size) > max_bytes:
        raise ValueError("Media exceeds the configured size limit.")

    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
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
                    raise ValueError("Download exceeded the size limit.")

                output.write(chunk)

    if total < 5_000:
        raise ValueError("Downloaded media is too small to be usable.")


def prepare_image_footage(image, duration):
    """Animate a licensed still image into a vertical silent MP4."""
    temporary = OUT / "source_image.download"
    footage = OUT / "footage.mp4"

    temporary.unlink(missing_ok=True)
    footage.unlink(missing_ok=True)

    try:
        download_media(
            image["download_url"],
            temporary,
            30 * 1024 * 1024,
            image.get("size", 0),
        )

        render = subprocess.run([
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
            render.returncode != 0
            or not footage.exists()
            or footage.stat().st_size < 100_000
        ):
            raise ValueError(
                (render.stderr or "FFmpeg failed to animate the image.")[-1500:]
            )

        return footage

    finally:
        temporary.unlink(missing_ok=True)


def generate_script(candidate, region, original_report):
    topic = str(candidate.get("topic", "")).strip()

    isolated = (
        dict(original_report)
        if isinstance(original_report, dict)
        else {}
    )
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
    """Download and convert a reusable video into a vertical silent MP4."""
    temporary = OUT / "source_clip.download"
    footage = OUT / "footage.mp4"

    temporary.unlink(missing_ok=True)
    footage.unlink(missing_ok=True)

    try:
        download_media(
            video["download_url"],
            temporary,
            MAX_BYTES,
            video.get("size", 0),
        )

        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(temporary),
        ], capture_output=True, text=True, timeout=30)

        if probe.returncode != 0 or not probe.stdout.strip():
            raise ValueError("ffprobe could not read the downloaded video.")

        if float(probe.stdout.strip()) < 2:
            raise ValueError("Source clip is shorter than two seconds.")

        render = subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-stream_loop", "-1", "-i", str(temporary),
            "-t", str(duration),
            "-vf",
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,fps=30,format=yuv420p",
            "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "25", "-movflags", "+faststart", str(footage),
        ], capture_output=True, text=True, timeout=180)

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


def write_attribution(topic, script_data, visual, attempt):
    """Write attribution and source metadata for the selected visual."""
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

    (OUT / "attribution.txt").write_text(
        attribution,
        encoding="utf-8",
    )

    metadata_path = OUT / "metadata.json"
    metadata = {}

    if metadata_path.exists():
        try:
            metadata = json.loads(
                metadata_path.read_text(encoding="utf-8")
            )
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
        if not isinstance(item, dict):
            continue

        topic = str(item.get("topic", "")).strip()
        item_region = str(item.get("region", "")).upper()
        regions = item.get("regions", [item_region])

        if not topic:
            continue

        if item_region and item_region != region and region not in regions:
            continue

        normalized_topic = re.sub(
            r"\s+", " ", topic.casefold()
        ).strip()

        if normalized_topic in AMBIGUOUS_TRENDS:
            log(f"Skipping ambiguous trend: {topic}")
            continue

        if normalized_topic in GENERIC_TRENDS:
            log(f"Skipping generic trend: {topic}")
            continue

        if normalized_topic in seen:
            continue

        seen.add(normalized_topic)
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
        raise RuntimeError(
            f"No suitable trends remain for region {region} after filtering."
        )

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

            title = str(script_data.get("title", "")).strip()
            visual = search_video(topic, title)

            if not visual:
                failures.append(
                    f"{topic}: no usable video or licensed image"
                )
                continue

            try:
                if visual.get("media_type") == "image":
                    prepare_image_footage(visual, duration)
                else:
                    prepare_footage(visual, duration)

            except Exception as error:
                log(f"  Could not prepare selected visual: {error}")
                failures.append(f"{topic}: visual processing failed")
                continue

            write_attribution(topic, script_data, visual, number)

            log(
                f"SUCCESS: prepared video for '{topic}' "
                f"using {visual['provider']}."
            )
            return

    finally:
        # Restore the complete trend report even when generation fails.
        TRENDS.write_text(original_text, encoding="utf-8")

    raise RuntimeError(
        f"No usable visual found in the first {len(filtered)} trends. "
        + "; ".join(failures)
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        log(f"FATAL: {type(error).__name__}: {error}")
        raise
