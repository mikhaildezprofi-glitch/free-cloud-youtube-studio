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


def stop(message):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_topics():
    if not TRENDS_FILE.exists():
        stop("Trend report missing. Run Discover Shorts Trends first.")

    try:
        report = json.loads(TRENDS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        stop(f"Cannot read trends: {exc}")

    entries = report.get("shortlist") or report.get("candidates") or []
    topics = []
    seen = set()

    for item in entries:
        topic = str(item.get("topic", "")).strip()
        region = str(item.get("region", "")).upper()

        if not topic or region not in {"KZ", "RU", "US", "GB"}:
            continue
        if topic.casefold() in seen:
            continue

        seen.add(topic.casefold())
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
        stop("No topics for KZ, RU, US or GB in the trend report.")

    # Prefer the highest-ranked current topics.
    topics.sort(key=lambda x: int(x.get("score") or 0), reverse=True)
    return topics[:30], report


def generate(topics, report, api_key):
    model = os.getenv("GROQ_MODEL", DEFAULT_MODEL)

    prompt = {
        "task": (
            "Select one promising trending topic and create an original "
            "YouTube Shorts narration."
        ),
        "trend_report_time": report.get("generated_at"),
        "candidate_topics": topics,
        "language_rules": {
            "KZ": "Russian",
            "RU": "Russian",
            "US": "English",
            "GB": "English",
        },
        "requirements": [
            "Select exactly one supplied topic.",
            "The narration language MUST match the selected topic's region.",
            "Write original narration, never copy a prewritten fact list.",
            "Target 45-70 words for approximately 20-30 seconds.",
            "Start with an engaging, truthful hook.",
            "Use natural speech suitable for neural voiceover.",
            "Use a clear explanation, story, or useful insight.",
            "A trend is not proof that a news claim is true.",
            "Never invent current-event details, quotes, figures, or results.",
            "If the trend data is insufficient, avoid unsupported claims.",
            "Do not claim to have verified sources when you have not.",
            "Keep the title short and suitable for Shorts.",
            "Return a single JSON object, with no Markdown.",
            "Use exactly these keys: topic, region, language, title, hook, "
            "narration, fact_check_note, source_url."
        ],
    }

    body = {
        "model": model,
        "temperature": 0.65,
        "max_tokens": 1100,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an experienced English and Russian YouTube "
                    "Shorts writer. Write original scripts from trends. "
                    "Never fabricate facts. Follow the requested JSON schema."
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
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")[:1500]
        stop(f"Groq HTTP {exc.code}: {details}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        stop(f"Groq request failed: {exc}")

    try:
        result = json.loads(
            payload["choices"][0]["message"]["content"]
        )
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        stop(f"Invalid model response: {exc}")

    required = [
        "topic", "region", "language", "title",
        "hook", "narration", "fact_check_note", "source_url",
    ]
    for field in required:
        if not isinstance(result.get(field), str) or not result[field].strip():
            stop(f"Missing required field: {field}")

    topic_lookup = {t["topic"].casefold(): t for t in topics}
    chosen = topic_lookup.get(result["topic"].casefold())

    if not chosen:
        stop("The model selected a topic outside the trend report.")

    expected_language = {
        "KZ": "Russian",
        "RU": "Russian",
        "US": "English",
        "GB": "English",
    }[chosen["region"]]

    if result["language"].casefold() != expected_language.casefold():
        stop(
            f"Wrong language for {chosen['region']}: "
            f"expected {expected_language}."
        )

    narration = re.sub(r"\s+", " ", result["narration"]).strip()
    words = re.findall(r"\b[\w'-]+\b", narration, flags=re.UNICODE)

    if not 35 <= len(words) <= 85:
        stop(f"Narration length invalid: {len(words)} words.")

    # Prevent the model from replacing the selected topic's source.
    result["topic"] = chosen["topic"]
    result["region"] = chosen["region"]
    result["language"] = expected_language
    result["source_url"] = chosen["source_url"]
    result["narration"] = narration
    result["word_count"] = len(words)
    result["provider"] = "Groq"
    result["model"] = model
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["trend_report_generated_at"] = report.get("generated_at")
    result["validation"] = {
        "topic_in_trend_report": True,
        "language_matches_region": True,
        "word_count_valid": True,
        "fact_check_required": True,
    }

    return result


def main():
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        stop("Set GROQ_API_KEY in GitHub Actions secrets.")

    topics, report = load_topics()
    result = generate(topics, report, api_key)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("=" * 50)
    print("AI SCRIPT GENERATED")
    print("Region:", result["region"])
    print("Language:", result["language"])
    print("Topic:", result["topic"])
    print("Title:", result["title"])
    print("\nHOOK:")
    print(result["hook"])
    print("\nNARRATION:")
    print(result["narration"])
    print("\nWords:", result["word_count"])
    print("Fact-check note:", result["fact_check_note"])
    print("Saved to:", OUTPUT_FILE.relative_to(ROOT))
    print("=" * 50)


if __name__ == "__main__":
    main()
