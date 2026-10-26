#!/usr/bin/env python3
"""AI Director: plan scenes, find topical clips, and assemble a vertical timeline."""

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
USER_AGENT = "FreeCloudYouTubeStudio-AIDirector/1.0"
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
SCENE_COUNT = 4


def log(message):
    print(message, flush=True)


def run(command, timeout=180):
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed: {' '.join(map(str, command))}\n"
            f"{(result.stderr or result.stdout)[-2500:]}"
        )
    return result


def words(text):
    return set(re.findall(r"[^\W_]{3,}", str(text).casefold(), re.UNICODE))


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
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    return request_json(url, headers=None, timeout=timeout) if False else _post(request, timeout)


def _post(request, timeout):
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def make_scene_plan(topic, title, narration, language, count):
    """Ask Groq for visual search queries; use a safe fallback if unavailable."""
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        log("No Groq key for director plan; using fallback scene queries.")
        return fallback_plan(topic, narration, count)

    prompt = {
        "topic": topic,
        "title": title,
        "narration": narration,
        "language": language,
        "scene_count": count,
        "task": (
            "Act as a factual short-form video director. Split the narration "
            "into sequential visual scenes. Return JSON only: "
            '{"scenes":[{"visual_query":"short English search query",'
            '"purpose":"what viewers should see"}]}. '
            "Return exactly scene_count scenes. Queries must describe visible "
            "real-world subjects, objects, actions or locations, not abstract "
            "ideas. Keep the main topic visually recognizable. Do not invent "
            "facts. Search queries must be in English."
        ),
    }
    payload = {
        "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        "temperature": 0.3,
        "max_tokens": 700,
        "messages": [
            {
                "role": "system",
                "content": "You are a careful video director. Return valid JSON only.",
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
    }

    try:
        result = post_json(
            API_URL,
            payload,
            {"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT},
        )
        content = result["choices"][0]["message"]["content"]
        data = json.loads(content)
        scenes = data.get("scenes", [])
        cleaned = []
        for scene in scenes[:count]:
            query = re.sub(r"\s+", " ", str(scene.get("visual_query", ""))).strip()
            if query:
                cleaned.append({
                    "visual_query": query[:120],
                    "purpose": str(scene.get("purpose", ""))[:250],
                })
        if len(cleaned) >= 2:
            while len(cleaned) < count:
                cleaned.append(dict(cleaned[-1]))
            return cleaned[:count]
    except Exception as exc:
        log(f"Groq scene plan unavailable: {type(exc).__name__}: {exc}")

    return fallback_plan(topic, narration, count)


def fallback_plan(topic, narration, count):
    sentences = [
        s.strip() for s in re.split(r"(?<=[.!?])\s+", narration) if s.strip()
    ]
    plan = []
    for index in range(count):
        sentence = sentences[min(index, len(sentences) - 1)] if sentences else topic
        # Use the topic in every query so the subject remains relevant.
        query = f"{topic} {sentence[:65]} real footage"
        plan.append({
            "visual_query": query[:120],
            "purpose": sentence[:250],
        })
    return plan


def search_pixabay(query, topic):
    api_key = os.getenv("PIXABAY_API_KEY", "").strip()
    if not api_key:
        return None

    url = "https://pixabay.com/api/videos/?" + urllib.parse.urlencode({
        "key": api_key,
        "q": query[:100],
        "per_page": 20,
        "safesearch": "true",
        "video_type": "all",
    })
    data = request_json(url)
    wanted = words(topic) | words(query)
    matches = []

    for item in data.get("hits", []):
        variants = item.get("videos", {}) or {}
        chosen = next(
            (
                variants.get(size, {})
                for size in ("large", "medium", "small", "tiny")
                if (variants.get(size, {}) or {}).get("url", "").startswith("https://")
            ),
            None,
        )
        if not chosen:
            continue

        tags = words(item.get("tags", ""))
        overlap = len(wanted & tags)
        # Require some topic relevance; don't accept completely unrelated footage.
        if overlap < 1:
            continue

        matches.append((
            overlap,
            {
                "url": chosen["url"],
                "page_url": item.get("pageURL", ""),
                "creator": item.get("user", "Not specified"),
                "provider": "Pixabay",
                "license": "Pixabay Content License",
                "license_url": "https://pixabay.com/service/license-summary/",
                "query": query,
            },
        ))

    matches.sort(key=lambda entry: entry[0], reverse=True)
    return matches[0][1] if matches else None


def download(url, destination):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    total = 0
    with urllib.request.urlopen(request, timeout=60) as response:
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError("Scene clip exceeds 100 MB download limit.")
                output.write(chunk)
    if total < 50_000:
        raise ValueError("Downloaded clip is too small to be usable.")


def make_scene(source, destination, duration):
    """Loop a source if needed and normalize it to vertical 1080x1920."""
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-stream_loop", "-1", "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,fps=30,setsar=1,format=yuv420p",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-pix_fmt", "yuv420p", str(destination),
    ], timeout=240)


def probe_duration(path):
    result = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], timeout=30)
    return float(result.stdout.strip())


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
        raise RuntimeError("Missing output/footage.mp4 fallback clip.")

    script = json.loads(SCRIPT_FILE.read_text(encoding="utf-8"))
    topic = str(script.get("topic", "")).strip()
    title = str(script.get("title", "")).strip()
    narration = str(script.get("narration", "")).strip()
    language = str(script.get("language", "Russian")).strip()

    if not topic or not narration:
        raise RuntimeError("Script JSON must contain topic and narration.")

    # The audio, not the requested menu duration, is the timeline source of truth.
    audio_duration = probe_duration(audio)
    if audio_duration <= 0:
        raise RuntimeError("Could not determine narration duration.")

    plan = make_scene_plan(topic, title, narration, language, SCENE_COUNT)
    scene_duration = audio_duration / len(plan)
    results = []
    sources = []
    scene_files = []

    log(f"Director topic: {topic}")
    log(f"Narration duration: {audio_duration:.2f} seconds")
    log(f"Planned scenes: {len(plan)}")

    for index, scene in enumerate(plan, 1):
        query = scene["visual_query"]
        log(f"Scene {index}: {query}")
        candidate = None

        try:
            candidate = search_pixabay(query, topic)
        except Exception as exc:
            log(f"Pixabay search failed for scene {index}: {exc}")

        raw = OUT / f"director_source_{index}.download"
        normalized = OUT / f"director_scene_{index:02d}.mp4"
        raw.unlink(missing_ok=True)
        normalized.unlink(missing_ok=True)

        try:
            if candidate:
                download(candidate["url"], raw)
                source_path = raw
                sources.append({
                    "scene": index,
                    "query": query,
                    **candidate,
                })
                log(f"Scene {index}: using Pixabay clip.")
            else:
                source_path = fallback
                sources.append({
                    "scene": index,
                    "query": query,
                    "provider": "Existing selected footage fallback",
                    "page_url": "",
                    "creator": "See output/attribution.txt",
                    "license": "See output/attribution.txt",
                    "license_url": "",
                })
                log(f"Scene {index}: no matching Pixabay clip; using fallback.")

            make_scene(source_path, normalized, scene_duration)
            results.append({
                "scene": index,
                "start_seconds": round((index - 1) * scene_duration, 3),
                "duration_seconds": round(scene_duration, 3),
                "visual_query": query,
                "purpose": scene.get("purpose", ""),
                "file": normalized.name,
            })
            scene_files.append(normalized)
        finally:
            raw.unlink(missing_ok=True)

    # Concat demuxer preserves scene order. All clips share the same codec and size.
    concat_file = OUT / "director_concat.txt"
    concat_file.write_text(
        "".join(f"file '{path.resolve().as_posix()}'\n" for path in scene_files),
        encoding="utf-8",
    )
    directed.unlink(missing_ok=True)
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-t", f"{audio_duration:.3f}",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(directed),
    ], timeout=360)

    if not directed.exists() or directed.stat().st_size < 100_000:
        raise RuntimeError("AI Director did not produce a usable video.")

    plan_output = {
        "topic": topic,
        "title": title,
        "narration_duration_seconds": round(audio_duration, 3),
        "scene_count": len(results),
        "resolution": "1080x1920",
        "fps": 30,
        "scenes": results,
        "sources": sources,
        "notes": [
            "Center crop is heuristic; subject positioning is not vision-verified.",
            "Pixabay footage is used only when a relevant search result is available.",
            "Fallback scenes reuse the already selected, license-checked source clip.",
        ],
    }
    PLAN_FILE.write_text(
        json.dumps(plan_output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Keep the original filename expected by the existing workflow.
    fallback.unlink(missing_ok=True)
    directed.replace(fallback)

    log(f"AI Director complete: {len(results)} scenes.")
    log(f"Video duration: {probe_duration(fallback):.2f} seconds")
    log(f"Scene plan saved: {PLAN_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
