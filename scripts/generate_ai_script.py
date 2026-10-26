#!/usr/bin/env python3

import json
import os
import re
import sys
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
    "free-cloud-youtube-studio/1.0 "
    "(+https://github.com/mikhaildezprofi-glitch/"
    "free-cloud-youtube-studio)"
)


def stop(message):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_topics():
    if not TRENDS_FILE.exists():
        stop(
            "Trend report missing. "
            "Run Discover Shorts Trends first."
        )

    try:
        report = json.loads(
            TRENDS_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        stop(f"Cannot read trends: {exc}")

    entries = report.get("shortlist") or report.get("candidates") or []

    topics = []
    seen = set()

    allowed_regions = {"KZ", "RU", "US", "GB"}

    for item in entries:
        topic = str(item.get("topic", "")).strip()
        region = str(item.get("region", "")).upper()

        if not topic or region not in allowed_regions:
            continue

        key = (topic.casefold(), region)

        if key in seen:
            continue

        seen.add(key)

        topics.append({
            "topic": topic,
            "region": region,
            "regions": item.get("regions", [region]),
            "score": item.get("score", 0),
            "source_url": (
                item.get("source_url")
                or item.get("feed_url")
                or f"https://trends.google.com/trending?geo={region}"
            ),
            "description": item.get("description", ""),
            "published_at": item.get("published_at"),
        })

    if not topics:
        stop(
            "No topics for KZ, RU, US or GB in the trend report."
        )

    topics.sort(
        key=lambda item: int(item.get("score") or 0),
        reverse=True,
    )

    return topics[:30], report


def call_groq(api_key, model, prompt):
    body = {
        "model": model,
        "temperature": 0.65,
        "max_tokens": 1100,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an experienced YouTube Shorts scriptwriter "
                    "writing in Russian and English. Create original, "
                    "engaging scripts based on supplied trending topics. "
                    "Never invent facts, statistics, quotes, events, "
                    "or claims. Return only valid JSON."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    prompt,
                    ensure_ascii=False,
                ),
            },
        ],
    }

    request = urllib.request.Request(
        API_URL,
        data=json.dumps(
            body,
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=90,
        ) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw)

    except urllib.error.HTTPError as exc:
        details = exc.read().decode(
            "utf-8",
            errors="replace",
        )[:2000]

        stop(
            f"Groq HTTP {exc.code}: {details}"
        )

    except urllib.error.URLError as exc:
        stop(
            f"Cannot connect to Groq API: {exc.reason}"
        )

    except TimeoutError:
        stop("Groq API request timed out after 90 seconds.")

    except json.JSONDecodeError as exc:
        stop(
            f"Groq returned invalid JSON: {exc}"
        )

    try:
        content = payload["choices"][0]["message"]["content"]
        result = json.loads(content)

    except (
        KeyError,
        IndexError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        stop(
            f"Invalid model response: {exc}"
        )

    return result


def generate(topics, report, api_key):
    model = os.getenv(
        "GROQ_MODEL",
        DEFAULT_MODEL,
    ).strip()

    if not model:
        stop("GROQ_MODEL is empty.")

    target_region = os.getenv(
        "TARGET_REGION",
        "",
    ).strip().upper()

    allowed_regions = {"KZ", "RU", "US", "GB"}

    if target_region and target_region not in allowed_regions:
        stop(
            f"Unsupported TARGET_REGION: {target_region}"
        )

    if target_region:
        topics = [
            item for item in topics
            if (
                item["region"] == target_region
                or target_region in item.get("regions", [])
            )
        ]

        if not topics:
            stop(
                f"No current trend topics for region {target_region}."
            )

    prompt = {
        "task": (
            "Choose one supplied trending topic and create an original "
            "YouTube Shorts narration."
        ),
        "trend_report_time": report.get("generated_at"),
        "requested_region": target_region or "choose_best",
        "candidate_topics": topics,
        "language_rules": {
            "KZ": "Russian",
            "RU": "Russian",
            "US": "English",
            "GB": "English",
        },
        "requirements": [
            "Select exactly one topic from candidate_topics.",
            "Use the selected topic's region.",
            "KZ and RU scripts must be in Russian.",
            "US and GB scripts must be in English.",
            "Write an original narration, not a list of canned facts.",
            "Aim for 45-70 words in the narration.",
            "Start with an engaging but truthful hook.",
            "Use natural language suitable for voiceover.",
            "Explain the topic clearly and make the script interesting.",
            "A trending search term is not proof of a news claim.",
            "Do not invent facts, dates, figures, quotes or results.",
            "The supplied data may contain only a search term.",
            "If information is insufficient, keep the script cautious "
            "and explain what is not confirmed.",
            "Do not claim that a source was verified unless it was.",
            "Return one JSON object without Markdown.",
            "Use exactly these keys: topic, region, language, title, "
            "hook, narration, fact_check_note, source_url.",
        ],
    }

    result = call_groq(
        api_key,
        model,
        prompt,
    )

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
            stop(
                f"Model response is missing required field: {field}"
            )

    topic_lookup = {
        (
            item["topic"].casefold(),
            item["region"],
        ): item
        for item in topics
    }

    selected_topic = result["topic"].strip().casefold()
    selected_region = result["region"].strip().upper()

    chosen = topic_lookup.get(
        (selected_topic, selected_region)
    )

    if not chosen:
        stop(
            "The model selected a topic or region outside "
            "the supplied trend report."
        )

    expected_language = {
        "KZ": "Russian",
        "RU": "Russian",
        "US": "English",
        "GB": "English",
    }[chosen["region"]]

    if result["language"].strip().casefold() != expected_language.casefold():
        stop(
            f"Wrong language for {chosen['region']}: "
            f"expected {expected_language}."
        )

    narration = re.sub(
        r"\s+",
        " ",
        result["narration"],
    ).strip()

    words = re.findall(
        r"\b[\w'-]+\b",
        narration,
        flags=re.UNICODE,
    )

    if not 35 <= len(words) <= 85:
        stop(
            f"Narration length invalid: {len(words)} words; "
            "expected 35-85."
        )

    # Keep the topic, region and source tied to the trend report.
    result["topic"] = chosen["topic"]
    result["region"] = chosen["region"]
    result["language"] = expected_language
    result["source_url"] = chosen["source_url"]
    result["narration"] = narration
    result["word_count"] = len(words)
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
        "fact_check_required": True,
    }

    return result


def main():
    api_key = os.getenv(
        "GROQ_API_KEY",
        "",
    ).strip()

    if not api_key:
        stop(
            "GROQ_API_KEY is missing. Add it in "
            "GitHub Settings > Secrets and variables > Actions."
        )

    topics, report = load_topics()

    result = generate(
        topics,
        report,
        api_key,
    )

    OUTPUT_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    OUTPUT_FILE.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    print("=" * 60)
    print("AI SCRIPT GENERATED SUCCESSFULLY")
    print("Region:", result["region"])
    print("Language:", result["language"])
    print("Topic:", result["topic"])
    print("Title:", result["title"])
    print()
    print("HOOK:")
    print(result["hook"])
    print()
    print("NARRATION:")
    print(result["narration"])
    print()
    print("Words:", result["word_count"])
    print("Source:", result["source_url"])
    print("Fact-check note:", result["fact_check_note"])
    print("Model:", result["model"])
    print("Saved to:", OUTPUT_FILE.relative_to(ROOT))
    print("=" * 60)


if __name__ == "__main__":
    main()
