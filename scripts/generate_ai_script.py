#!/usr/bin/env python3

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRENDS_FILE = ROOT / "data/trends/latest.json"
OUTPUT_FILE = ROOT / "data/scripts/latest.json"

API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-20b"

USER_AGENT = (
    "FreeCloudYouTubeStudio/3.3 "
    "(+https://github.com/mikhaildezprofi-glitch/"
    "free-cloud-youtube-studio)"
)

DURATION_WORD_RANGES = {
    "15": (30, 42),
    "30": (60, 82),
    "45": (90, 120),
    "60": (120, 160),
}

MAX_ATTEMPTS = 3


def stop(message):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_topics():
    if not TRENDS_FILE.exists():
        stop("Trend report missing.")

    try:
        report = json.loads(TRENDS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        stop(f"Cannot read trends: {exc}")

    entries = report.get("shortlist") or report.get("candidates") or []
    topics = []
    seen = set()

    for item in entries:
        if not isinstance(item, dict):
            continue

        topic = str(item.get("topic", "")).strip()
        region = str(item.get("region", "")).upper()
        regions = item.get("regions") or [region]

        if not topic:
            continue

        for item_region in [region] + [
            str(value).upper() for value in regions
        ]:
            if item_region not in {"KZ", "RU", "US", "GB"}:
                continue

            key = (topic.casefold(), item_region)
            if key in seen:
                continue

            seen.add(key)

            topics.append({
                "topic": topic,
                "region": item_region,
                "regions": [item_region],
                "score": item.get("score", 0),
                "source_url": (
                    item.get("source_url")
                    or item.get("feed_url")
                    or f"https://trends.google.com/trending?geo={item_region}"
                ),
                "description": item.get("description", ""),
                "published_at": item.get("published_at"),
            })

    if not topics:
        stop("No usable topics for KZ, RU, US or GB.")

    topics.sort(
        key=lambda item: int(item.get("score") or 0),
        reverse=True,
    )

    return topics[:30], report


def parse_model_json(content):
    """Parse JSON even if the model surrounds it with Markdown."""
    if not isinstance(content, str):
        raise ValueError("Model content is not text.")

    text = content.strip()

    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start < 0 or end <= start:
        raise ValueError("No complete JSON object in model response.")

    return json.loads(text[start:end + 1])


def call_groq(api_key, model, prompt):
    body = {
        "model": model,
        "temperature": 0.35,
        "max_tokens": 2400,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a professional YouTube Shorts scriptwriter. "
                    "Return exactly one valid JSON object. "
                    "Never invent facts, statistics, quotations or events. "
                    "Follow the requested language and word count exactly."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt, ensure_ascii=False),
            },
        ],
    }

    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(
                response.read().decode("utf-8")
            )

    except urllib.error.HTTPError as exc:
        details = exc.read().decode(
            "utf-8", errors="replace"
        )[:1500]
        raise RuntimeError(
            f"Groq HTTP {exc.code}: {details}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Groq connection failed: {exc.reason}"
        ) from exc

    except TimeoutError as exc:
        raise RuntimeError("Groq request timed out.") from exc

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            "Groq response has no message content."
        ) from exc

    return parse_model_json(content)


def count_words(text):
    return len(
        re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE)
    )


def validate_result(result, topics, region, duration):
    if not isinstance(result, dict):
        raise ValueError("Model response is not a JSON object.")

    required = [
        "topic",
        "region",
        "language",
        "title",
        "hook",
        "narration",
        "fact_check_note",
        "source_url",
    ]

    for field in required:
        value = result.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing required field: {field}")

    topic_lookup = {
        (item["topic"].casefold(), item["region"]): item
        for item in topics
    }

    selected_topic = result["topic"].strip().casefold()

    # The requested region is authoritative.
    # Do not let the model silently switch regions.
    chosen = topic_lookup.get((selected_topic, region))

    if not chosen:
        raise ValueError(
            "The model selected a topic outside the requested region."
        )

    expected_language = (
        "Russian" if region in {"KZ", "RU"} else "English"
    )

    if result["language"].strip().casefold() != expected_language.casefold():
        raise ValueError(
            f"Wrong language: expected {expected_language}."
        )

    narration = re.sub(
        r"\s+", " ", result["narration"]
    ).strip()

    word_count = count_words(narration)
    minimum, maximum = DURATION_WORD_RANGES[duration]

    if not minimum <= word_count <= maximum:
        raise ValueError(
            f"Word count is {word_count}; "
            f"required range is {minimum}-{maximum}."
        )

    result["topic"] = chosen["topic"]
    result["region"] = region
    result["language"] = expected_language
    result["source_url"] = chosen["source_url"]
    result["narration"] = narration
    result["word_count"] = word_count
    result["target_duration_seconds"] = int(duration)
    result["target_word_range"] = [minimum, maximum]

    return result


def generate(topics, report, api_key):
    model = os.getenv("GROQ_MODEL", DEFAULT_MODEL).strip()
    region = os.getenv("TARGET_REGION", "").strip().upper()
    duration = os.getenv("VIDEO_DURATION", "30").strip()

    if not model:
        stop("GROQ_MODEL is empty.")

    if region not in {"KZ", "RU", "US", "GB"}:
        stop(f"Unsupported TARGET_REGION: {region}")

    if duration not in DURATION_WORD_RANGES:
        stop("VIDEO_DURATION must be 15, 30, 45 or 60.")

    minimum, maximum = DURATION_WORD_RANGES[duration]

    regional_topics = [
        item for item in topics
        if item["region"] == region
    ]

    if not regional_topics:
        stop(f"No topics available for region {region}.")

    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(
            f"Generating script: attempt {attempt}/{MAX_ATTEMPTS}",
            flush=True,
        )

        prompt = {
            "task": "Create one original YouTube Shorts narration.",
            "requested_region": region,
            "required_language": (
                "Russian" if region in {"KZ", "RU"} else "English"
            ),
            "duration_seconds": int(duration),
            "word_count": {
                "minimum": minimum,
                "maximum": maximum,
                "target": (minimum + maximum) // 2,
            },
            "candidate_topics": regional_topics,
            "previous_error": str(last_error) if last_error else None,
            "requirements": [
                "Select exactly one topic from the supplied candidates.",
                "Use exactly the requested region.",
                "Write the entire narration in the required language.",
                f"Write between {minimum} and {maximum} words.",
                "Aim for the middle of the allowed word range.",
                "Count the narration words before returning the result.",
                "Do not count the title or hook as narration words.",
                "Use natural spoken language.",
                "Start with an engaging but truthful hook.",
                "Do not invent facts, statistics, quotes or dates.",
                "A search trend alone does not prove a factual claim.",
                "If information is uncertain, say so clearly.",
                "Return valid JSON only, without Markdown.",
                (
                    "Required keys: topic, region, language, title, "
                    "hook, narration, fact_check_note, source_url."
                ),
            ],
        }

        try:
            result = call_groq(api_key, model, prompt)
            result = validate_result(
                result, regional_topics, region, duration
            )

            result["provider"] = "Groq"
            result["model"] = model
            result["generated_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            result["trend_report_generated_at"] = report.get(
                "generated_at"
            )

            result["validation"] = {
                "topic_in_trend_report": True,
                "language_matches_region": True,
                "word_count_valid": True,
                "duration_target_checked": True,
                "fact_check_required": True,
                "generation_attempt": attempt,
            }

            return result

        except (
            ValueError,
            RuntimeError,
            KeyError,
            TypeError,
        ) as exc:
            last_error = exc
            print(f"Attempt failed: {exc}", file=sys.stderr)

            if attempt < MAX_ATTEMPTS:
                time.sleep(2)

    stop(
        f"Could not generate a valid script after "
        f"{MAX_ATTEMPTS} attempts. Last error: {last_error}"
    )


def main():
    api_key = os.getenv("GROQ_API_KEY", "").strip()

    if not api_key:
        stop("GROQ_API_KEY is missing.")

    topics, report = load_topics()
    result = generate(topics, report, api_key)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("=" * 60)
    print("AI SCRIPT GENERATED SUCCESSFULLY")
    print("Region:", result["region"])
    print("Language:", result["language"])
    print("Topic:", result["topic"])
    print("Title:", result["title"])
    print("Duration:", result["target_duration_seconds"])
    print("Words:", result["word_count"])
    print("Word range:", result["target_word_range"])
    print()
    print("NARRATION:")
    print(result["narration"])
    print()
    print("Source:", result["source_url"])
    print("Model:", result["model"])
    print("Attempts:", result["validation"]["generation_attempt"])
    print("Saved:", OUTPUT_FILE.relative_to(ROOT))
    print("=" * 60)


if __name__ == "__main__":
    main()
