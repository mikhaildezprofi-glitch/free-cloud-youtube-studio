#!/usr/bin/env python3
"""Перебор трендов до нахождения подходящего видео Wikimedia Commons."""

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

API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "FreeCloudYouTubeStudio/1.0"
MAX_TRENDS = 10
MAX_BYTES = 120 * 1024 * 1024


def log(message):
    print(message, flush=True)


def api_json(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def words(text):
    # Поддерживает английские и русские названия.
    return set(re.findall(r"[^\W_]{3,}", text.casefold(), re.UNICODE))


def search_video(topic, title):
    query = f"{topic} {title}".strip()

    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": query + " filetype:video",
        "gsrnamespace": "6",
        "gsrlimit": "15",
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
    }

    url = API + "?" + urllib.parse.urlencode(params)
    response = api_json(url)
    pages = list(
        (response.get("query", {}).get("pages", {}) or {}).values()
    )

    wanted = words(topic + " " + title)
    matches = []

    for page in pages:
        images = page.get("imageinfo", [])
        if not images:
            continue

        info = images[0]
        media_url = info.get("url", "")
        mime = info.get("mime", "").lower()

        if not media_url.startswith("https://"):
            continue

        if mime not in ("video/webm", "video/ogg", "video/mp4"):
            if not media_url.lower().endswith(
                (".webm", ".ogv", ".ogg", ".mp4")
            ):
                continue

        metadata = info.get("extmetadata", {})
        license_name = (
            metadata.get("LicenseShortName", {}).get("value", "")
        )
        usage = metadata.get("UsageTerms", {}).get("value", "")
        license_text = (license_name + " " + usage).casefold()

        # Не используем файлы с неизвестной или неподходящей лицензией.
        allowed = (
            "cc0",
            "cc by",
            "cc by-sa",
            "public domain",
        )
        if not any(item in license_text for item in allowed):
            continue

        filename = page.get("title", "").replace("File:", "")
        overlap = wanted & words(filename)

        if not overlap:
            continue

        # Для многословной темы требуем совпадение минимум двух слов.
        if len(wanted) >= 3 and len(overlap) < 2:
            continue

        score = len(overlap) / max(1, len(wanted))
        matches.append((score, page, info, metadata))

    matches.sort(key=lambda item: item[0], reverse=True)

    if not matches or matches[0][0] < 0.15:
        return None

    return matches[0][1:]


def download_video(url, destination, expected_size):
    if expected_size and int(expected_size) > MAX_BYTES:
        raise ValueError("Файл превышает лимит 120 МБ")

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
                if total > MAX_BYTES:
                    raise ValueError("Загрузка превысила 120 МБ")

                output.write(chunk)

    if total < 100_000:
        raise ValueError("Скачанный файл слишком мал")


def main():
    region = os.getenv("TARGET_REGION", "KZ").upper()

    if not os.getenv("GROQ_API_KEY", "").strip():
        raise RuntimeError("Не задан секрет GROQ_API_KEY")

    report = json.loads(TRENDS.read_text(encoding="utf-8"))

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
        raise RuntimeError(
            f"В data/trends/latest.json нет трендов для региона {region}"
        )

    OUT.mkdir(parents=True, exist_ok=True)
    original_report = TRENDS.read_text(encoding="utf-8")
    failures = []

    try:
        for number, candidate in enumerate(filtered, 1):
            topic = str(candidate["topic"]).strip()
            log(f"[{number}/{len(filtered)}] Тренд: {topic}")

            # Перед каждой попыткой генератор получает только текущий тренд.
            isolated = dict(report) if isinstance(report, dict) else {}
            isolated["shortlist"] = [candidate]
            isolated["candidates"] = [candidate]

            TRENDS.write_text(
                json.dumps(isolated, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["TARGET_REGION"] = region

            generated = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/generate_ai_script.py"),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )

            if generated.returncode != 0:
                log("  Не удалось создать сценарий. Следующий тренд.")
                failures.append(f"{topic}: ошибка сценария")
                continue

            try:
                script_data = json.loads(
                    SCRIPT.read_text(encoding="utf-8")
                )
            except Exception:
                failures.append(f"{topic}: нет корректного сценария")
                continue

            actual_topic = str(script_data.get("topic", "")).strip()
            if actual_topic.casefold() != topic.casefold():
                log("  Сценарий не соответствует тренду. Пропуск.")
                failures.append(f"{topic}: несовпадение темы")
                continue

            try:
                match = search_video(
                    topic,
                    str(script_data.get("title", "")),
                )
            except Exception as error:
                log(f"  Ошибка поиска видео: {error}")
                match = None

            if not match:
                log("  Подходящий лицензированный клип не найден.")
                log("  Переходим к следующему тренду.")
                failures.append(f"{topic}: клип не найден")
                continue

            page, info, metadata = match
            temporary = OUT / "source_clip.download"
            footage = OUT / "footage.mp4"

            try:
                download_video(
                    info["url"], temporary, info.get("size", 0)
                )

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
                    raise ValueError("ffprobe не распознал видео")

                if float(probe.stdout.strip()) < 2:
                    raise ValueError("Клип короче двух секунд")

                duration = os.getenv("VIDEO_DURATION", "30")

                render = subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-loglevel", "error",
                        "-y", "-stream_loop", "-1", "-i", str(temporary),
                        "-t", duration,
                        "-vf",
                        "scale=1080:1920:force_original_aspect_ratio=increase,"
                        "crop=1080:1920,fps=30,format=yuv420p",
                        "-an", "-c:v", "libx264", "-preset", "ultrafast",
                        "-crf", "25", "-movflags", "+faststart",
                        str(footage),
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
                        (render.stderr or "Ошибка преобразования видео")[-500:]
                    )

            except Exception as error:
                log(f"  Клип не удалось подготовить: {error}")
                log("  Пробуем следующий тренд.")
                failures.append(f"{topic}: непригодный клип")
                temporary.unlink(missing_ok=True)
                footage.unlink(missing_ok=True)
                continue

            finally:
                temporary.unlink(missing_ok=True)

            license_name = (
                metadata.get("LicenseShortName", {}).get("value", "")
            )
            license_url = metadata.get("LicenseUrl", {}).get("value", "")
            creator = metadata.get("Artist", {}).get(
                "value", "Автор не указан"
            )
            source_page = (
                "https://commons.wikimedia.org/wiki/"
                + urllib.parse.quote(
                    page.get("title", "").replace(" ", "_")
                )
            )

            (OUT / "attribution.txt").write_text(
                f"Тренд: {topic}\n"
                f"Заголовок: {script_data.get('title', '')}\n"
                f"Файл: {page.get('title', '')}\n"
                f"Автор: {creator}\n"
                f"Лицензия: {license_name}\n"
                f"Лицензия URL: {license_url}\n"
                f"Источник: {source_page}\n",
                encoding="utf-8",
            )

            metadata_file = OUT / "metadata.json"
            video_metadata = {}
            if metadata_file.exists():
                try:
                    video_metadata = json.loads(
                        metadata_file.read_text(encoding="utf-8")
                    )
                except json.JSONDecodeError:
                    pass

            video_metadata.update({
                "topic": topic,
                "visual_source": source_page,
                "visual_license": license_name,
                "visual_creator": creator,
                "visual_license_url": license_url,
                "visual_search_attempt": number,
            })

            metadata_file.write_text(
                json.dumps(video_metadata, ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )

            log(f"УСПЕХ: найден видеоряд для темы «{topic}»")
            return

    finally:
        TRENDS.write_text(original_report, encoding="utf-8")

    raise RuntimeError(
        "Не найдено подходящего видео для первых "
        f"{len(filtered)} трендов. " + "; ".join(failures)
    )


if __name__ == "__main__":
    main()
