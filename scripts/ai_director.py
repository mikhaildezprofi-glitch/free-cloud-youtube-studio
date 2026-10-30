#!/usr/bin/env python3
"""AI Director: create a four-scene vertical video using topical licensed media."""

import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
SCRIPT_FILE = ROOT / "data/scripts/latest.json"
PLAN_FILE = OUT / "director_plan.json"

API_URL = "https://api.groq.com/openai/v1/chat/completions"
PIXABAY_API = "https://pixabay.com/api/videos/"
OPENVERSE_API = "https://api.openverse.org/v1/images/"

USER_AGENT = "FreeCloudYouTubeStudio-AIDirector/2.0"
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
MAX_IMAGE_BYTES = 30 * 1024 * 1024
SCENE_COUNT = 4


def log(message):
    print(message, flush=True)


def run(command, timeout=180):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed: {' '.join(map(str, command))}\n"
            f"{(result.stderr or result.stdout)[-2500:]}"
        )
    return result


def request_json(url, headers=None, timeout=30):
    request_headers = {"User-Agent": USER_AGENT}
    request_headers.update(headers or {})
    request = urllib.request.Request(url, headers=request_headers)

    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url, payload, headers, timeout=90):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            **headers,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def words(text):
    return set(
        re.findall(r"[^\W_]{3,}", str(text).casefold(), re.UNICODE)
    )


def clean_text(value):
    value = re.sub(r"<[^>]*>", " ", str(value or ""))
    return re.sub(r"\s+", " ", value).strip()


def license_is_allowed(name):
    """Allow CC0, public domain, CC BY and CC BY-SA only."""
    value = str(name or "").strip().casefold()
    compact = re.sub(r"[\s_-]+", "", value)

    if any(term in value or term in compact for term in (
        "noncommercial",
        "non-commercial",
        "no derivatives",
        "noderivatives",
        "ccbync",
        "ccbynd",
        "all rights reserved",
    )):
        return False

    return value in {
        "cc0",
        "pdm",
        "by",
        "by-sa",
    }


def relevance_score(topic, query, title="", tags=""):
    wanted = words(topic) | words(query)
    available = words(f"{title} {tags}")

    if not wanted or not available:
        return 0

    return len(wanted & available) / len(wanted)


def fallback_plan(topic, narration, count):
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", narration)
        if sentence.strip()
    ]

    plan = []

    for index in range(count):
        sentence = (
            sentences[min(index, len(sentences) - 1)]
            if sentences else topic
        )

        query = f"{topic} {sentence[:65]} real world"
        plan.append({
            "visual_query": query[:120],
            "purpose": sentence[:250],
        })

    return plan


def make_scene_plan(topic, title, narration, language, count):
    api_key = os.getenv("GROQ_API_KEY", "").strip()

    if not api_key:
        log("No Groq key for director plan; using fallback.")
        return fallback_plan(topic, narration, count)

    prompt = {
        "topic": topic,
        "title": title,
        "narration": narration,
        "language": language,
        "scene_count": count,
        "task": (
            "Act as a factual short-form video director. "
            "Split the narration into sequential visual scenes. "
            "Return JSON only in this format: "
            '{"scenes":[{"visual_query":"short English search query",'
            '"purpose":"what viewers should see"}]}. '
            "Return exactly scene_count scenes. "
            "Describe visible real-world subjects, objects, actions "
            "or locations. Keep the main topic recognizable. "
            "Do not invent facts. Search queries must be in English."
        ),
    }

    payload = {
        "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        "temperature": 0.3,
        "max_tokens": 700,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a careful video director. "
                    "Return valid JSON only."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt, ensure_ascii=False),
            },
        ],
    }

    try:
        result = post_json(
            API_URL,
            payload,
            {
                "Authorization": f"Bearer {api_key}",
                "User-Agent": USER_AGENT,
            },
        )

        content = result["choices"][0]["message"]["content"]
        content = content.strip()

        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

        data = json.loads(content)
        scenes = data.get("scenes", [])
        cleaned = []

        for scene in scenes[:count]:
            query = re.sub(
                r"\s+",
                " ",
                str(scene.get("visual_query", "")),
            ).strip()

            if query:
                cleaned.append({
                    "visual_query": query[:120],
                    "purpose": str(scene.get("purpose", ""))[:250],
                })

        if cleaned:
            while len(cleaned) < count:
                cleaned.append(dict(cleaned[-1]))

            return cleaned[:count]

    except Exception as error:
        log(
            "Groq scene plan unavailable: "
            f"{type(error).__name__}: {error}"
        )

    return fallback_plan(topic, narration, count)


def search_pixabay(query, topic):
    api_key = os.getenv("PIXABAY_API_KEY", "").strip()

    if not api_key:
        return None

    url = PIXABAY_API + "?" + urllib.parse.urlencode({
        "key": api_key,
        "q": query[:100],
        "per_page": 20,
        "safesearch": "true",
        "video_type": "all",
    })

    data = request_json(url)
    matches = []

    for item in data.get("hits", []):
        variants = item.get("videos", {}) or {}

        chosen = next((
            variants.get(size, {})
            for size in ("large", "medium", "small", "tiny")
            if (
                variants.get(size, {}) or {}
            ).get("url", "").startswith("https://")
        ), None)

        if not chosen:
            continue

        tags = clean_text(item.get("tags", ""))
        score = relevance_score(topic, query, "", tags)

        if score < 0.15:
            continue

        matches.append((
            score,
            {
                "media_type": "video",
                "url": chosen["url"],
                "page_url": item.get("pageURL", ""),
                "creator": item.get("user", "Not specified"),
                "provider": "Pixabay",
                "license": "Pixabay Content License",
                "license_url": (
                    "https://pixabay.com/service/license-summary/"
                ),
                "query": query,
                "title": tags,
            },
        ))

    matches.sort(key=lambda entry: entry[0], reverse=True)
    return matches[0][1] if matches else None


def search_openverse(query, topic):
    url = OPENVERSE_API + "?" + urllib.parse.urlencode({
        "q": query[:180],
        "page_size": 20,
        "license_type": "commercial",
        "mature": "false",
        "filter_dead": "true",
        "size": "medium,large",
    })

    data = request_json(url)
    matches = []

    for item in data.get("results", []):
        license_name = str(item.get("license", "")).lower()

        if not license_is_allowed(license_name):
            continue

        image_url = item.get("url", "")
        page_url = item.get("foreign_landing_url", "")

        if not image_url.startswith("https://"):
            continue
        if not page_url.startswith("https://"):
            continue

        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)

        if width and height and min(width, height) < 400:
            continue

        title = clean_text(item.get("title", ""))
        tags = " ".join(
            str(tag.get("name", ""))
            for tag in item.get("tags", [])
            if isinstance(tag, dict)
        )

        score = relevance_score(topic, query, title, tags)

        if score < 0.15:
            continue

        matches.append((
            score,
            {
                "media_type": "image",
                "url": image_url,
                "page_url": page_url,
                "creator": clean_text(
                    item.get("creator") or "Not specified"
                ),
                "provider": "Openverse",
                "license": license_name.upper(),
                "license_url": item.get("license_url", ""),
                "query": query,
                "title": title,
                "width": width,
                "height": height,
            },
        ))

    matches.sort(key=lambda entry: entry[0], reverse=True)
    return matches[0][1] if matches else None


def search_scene_media(query, topic):
    """Prefer topical video, then use an openly licensed image."""
    try:
        log(f"  Searching Pixabay video: {query}")
        result = search_pixabay(query, topic)

        if result:
            log("  Found a topical Pixabay video.")
            return result

    except Exception as error:
        log(f"  Pixabay unavailable: {error}")

    try:
        log(f"  Searching Openverse image: {query}")
        result = search_openverse(query, topic)

        if result:
            log("  Found a licensed Openverse image.")
            return result

    except Exception as error:
        log(f"  Openverse unavailable: {error}")

    return None


def download(url, destination, limit):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT},
    )

    total = 0

    with urllib.request.urlopen(request, timeout=60) as response:
        content_type = response.headers.get("Content-Type", "").lower()

        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)

                if not chunk:
                    break

                total += len(chunk)

                if total > limit:
                    raise ValueError("Media exceeds download limit.")

                output.write(chunk)

    if total < 5_000:
        raise ValueError("Downloaded media is too small.")

    return content_type


def make_video_scene(source, destination, duration):
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-stream_loop", "-1", "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,fps=30,setsar=1,format=yuv420p",
        "-an", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "24", "-pix_fmt", "yuv420p",
        str(destination),
    ], timeout=240)


def make_image_scene(source, destination, duration):
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-loop", "1", "-framerate", "30", "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "zoompan=z='min(zoom+0.0005,1.10)':"
        "x='iw/2-(iw/zoom/2)':"
        "y='ih/2-(ih/zoom/2)':"
        "d=1:s=1080x1920:fps=30,"
        "setsar=1,format=yuv420p",
        "-an", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "24", "-pix_fmt", "yuv420p",
        str(destination),
    ], timeout=240)


def probe_duration(path):
    result = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ], timeout=30)

    return float(result.stdout.strip())


def validate_scene(path):
    if not path.is_file() or path.stat().st_size < 50_000:
        raise RuntimeError(f"Scene file is missing or too small: {path}")

    duration = probe_duration(path)

    if duration <= 0:
        raise RuntimeError(f"Invalid scene duration: {path}")

    return duration


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    audio = OUT / "narration.wav"
    fallback = OUT / "footage.mp4"
    directed = OUT / "footage_directed.mp4"

    if not audio.is_file() or audio.stat().st_size == 0:
        raise RuntimeError("Missing output/narration.wav.")

    if not SCRIPT_FILE.is_file():
        raise RuntimeError("Missing data/scripts/latest.json.")

    if not fallback.is_file() or fallback.stat().st_size == 0:
        raise RuntimeError("Missing output/footage.mp4 fallback.")

    script = json.loads(SCRIPT_FILE.read_text(encoding="utf-8"))

    topic = str(script.get("topic", "")).strip()
    title = str(script.get("title", "")).strip()
    narration = str(script.get("narration", "")).strip()
    language = str(script.get("language", "Russian")).strip()

    if not topic or not narration:
        raise RuntimeError("Script must contain topic and narration.")

    audio_duration = probe_duration(audio)

    if audio_duration <= 0:
        raise RuntimeError("Could not determine narration duration.")

    plan = make_scene_plan(
        topic,
        title,
        narration,
        language,
        SCENE_COUNT,
    )

    scene_duration = audio_duration / len(plan)
    results = []
    sources = []
    scene_files = []

    log(f"Director topic: {topic}")
    log(f"Narration duration: {audio_duration:.2f} seconds")
    log(f"Planned scenes: {len(plan)}")

    for index, scene in enumerate(plan, 1):
        query = scene["visual_query"]

        log(f"\nScene {index}/{len(plan)}: {query}")

        candidate = search_scene_media(query, topic)

        raw = OUT / f"director_source_{index}.download"
        normalized = OUT / f"director_scene_{index:02d}.mp4"

        raw.unlink(missing_ok=True)
        normalized.unlink(missing_ok=True)

        source_record = {
            "scene": index,
            "query": query,
            "purpose": scene.get("purpose", ""),
        }

        try:
            if candidate:
                try:
                    limit = (
                        MAX_IMAGE_BYTES
                        if candidate["media_type"] == "image"
                        else MAX_DOWNLOAD_BYTES
                    )

                    download(
                        candidate["url"],
                        raw,
                        limit,
                    )

                    if candidate["media_type"] == "image":
                        make_image_scene(
                            raw,
                            normalized,
                            scene_duration,
                        )
                    else:
                        make_video_scene(
                            raw,
                            normalized,
                            scene_duration,
                        )

                    source_record.update({
                        "provider": candidate["provider"],
                        "page_url": candidate["page_url"],
                        "creator": candidate["creator"],
                        "license": candidate["license"],
                        "license_url": candidate["license_url"],
                        "media_type": candidate["media_type"],
                        "title": candidate.get("title", ""),
                    })

                    log(
                        f"Scene {index}: using "
                        f"{candidate['provider']} "
                        f"{candidate['media_type']}."
                    )

                except Exception as error:
                    log(
                        f"Scene {index}: selected media failed: {error}"
                    )
                    candidate = None
                    normalized.unlink(missing_ok=True)

            if not candidate:
                make_video_scene(
                    fallback,
                    normalized,
                    scene_duration,
                )

                source_record.update({
                    "provider": "Verified main footage fallback",
                    "page_url": "",
                    "creator": "See output/attribution.txt",
                    "license": "See output/attribution.txt",
                    "license_url": "",
                    "media_type": "fallback",
                })

                log(
                    f"Scene {index}: using verified main footage "
                    "because no suitable separate visual was available."
                )

            validate_scene(normalized)

            results.append({
                "scene": index,
                "start_seconds": round(
                    (index - 1) * scene_duration, 3
                ),
                "duration_seconds": round(scene_duration, 3),
                "visual_query": query,
                "purpose": scene.get("purpose", ""),
                "file": normalized.name,
            })

            sources.append(source_record)
            scene_files.append(normalized)

        finally:
            raw.unlink(missing_ok=True)

    concat_file = OUT / "director_concat.txt"

    concat_file.write_text(
        "".join(
            f"file '{path.resolve().as_posix()}'\n"
            for path in scene_files
        ),
        encoding="utf-8",
    )

    directed.unlink(missing_ok=True)

    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-t", f"{audio_duration:.3f}",
        "-an", "-c:v", "libx264",
        "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(directed),
    ], timeout=360)

    if not directed.exists() or directed.stat().st_size < 100_000:
        raise RuntimeError("AI Director did not produce a usable video.")

    final_duration = probe_duration(directed)
    difference = abs(final_duration - audio_duration)

    if difference > 0.5:
        raise RuntimeError(
            f"Director duration mismatch: audio={audio_duration:.2f}s, "
            f"video={final_duration:.2f}s."
        )

    plan_output = {
        "version": "2.0",
        "topic": topic,
        "title": title,
        "narration_duration_seconds": round(audio_duration, 3),
        "video_duration_seconds": round(final_duration, 3),
        "scene_count": len(results),
        "resolution": "1080x1920",
        "fps": 30,
        "scenes": results,
        "sources": sources,
        "notes": [
            "Scene queries are generated by Groq or fallback logic.",
            "Openverse images are filtered to CC0, PDM, CC BY and CC BY-SA.",
            "Pixabay videos require PIXABAY_API_KEY.",
            "Missing scene visuals use the verified main footage.",
            "Center crop is not vision-verified.",
            "Subject size and position are not automatically verified.",
        ],
    }

    PLAN_FILE.write_text(
        json.dumps(plan_output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Replace the original footage with the completed directed timeline.
    fallback.unlink(missing_ok=True)
    directed.replace(fallback)

    # Append all scene attributions without discarding the original source.
    attribution_file = OUT / "attribution.txt"
    existing = ""

    if attribution_file.exists():
        existing = attribution_file.read_text(encoding="utf-8").strip()

    lines = ["", "", "AI DIRECTOR SCENE SOURCES"]

    for source in sources:
        lines.extend([
            "",
            f"Scene: {source['scene']}",
            f"Query: {source['query']}",
            f"Provider: {source['provider']}",
            f"Creator: {source.get('creator', '')}",
            f"License: {source.get('license', '')}",
            f"License URL: {source.get('license_url', '')}",
            f"Source page: {source.get('page_url', '')}",
        ])

    attribution_file.write_text(
        existing + "\n" + "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    log(f"\nAI Director complete: {len(results)} scenes.")
    log(f"Video duration: {final_duration:.2f} seconds.")
    log(f"Scene plan saved: {PLAN_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
