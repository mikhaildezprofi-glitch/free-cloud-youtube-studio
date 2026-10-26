#!/usr/bin/env python3
"""Collect Google Trends RSS and rank candidate Shorts topics."""

import argparse
import json
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

FEED = "https://trends.google.com/trending/rss?geo={}"
NAMESPACE = {"ht": "https://trends.google.com/trending/rss"}


def now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def parse_date(value):
    if not value:
        return None
    try:
        result = parsedate_to_datetime(value)
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def traffic_number(value):
    if not value:
        return None
    match = re.search(r"([\d,.]+)\s*([KMB]?)\+?", value, re.I)
    if not match:
        return None
    number = float(match.group(1).replace(",", ""))
    multiplier = {
        "": 1,
        "K": 1_000,
        "M": 1_000_000,
        "B": 1_000_000_000,
    }
    return int(number * multiplier[match.group(2).upper()])


def fetch_region(region, timeout):
    url = FEED.format(region)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "free-cloud-youtube-studio/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            root = ET.fromstring(response.read())
    except Exception as error:
        return [], f"{region}: {type(error).__name__}: {error}"

    items = []

    for node in root.findall(".//item"):
        title = (node.findtext("title") or "").strip()
        if not title:
            continue

        description = (node.findtext("description") or "").strip()
        traffic = node.findtext(
            "ht:approx_traffic", namespaces=NAMESPACE
        )

        if not traffic:
            match = re.search(
                r"([\d,.]+\s*[KMB]?\+?)\s+searches",
                description,
                re.I,
            )
            traffic = match.group(1) if match else None

        growth_match = re.search(
            r"(-?\d+(?:\.\d+)?)\s*%", description
        )
        published = parse_date(node.findtext("pubDate"))

        items.append({
            "topic": title,
            "region": region,
            "regions": [region],
            "source": "Google Trends Trending Now RSS",
            "source_url": (node.findtext("link") or url).strip(),
            "feed_url": url,
            "published_at": iso(published) if published else None,
            "approx_traffic_label": traffic,
            "approx_traffic_numeric": traffic_number(traffic),
            "growth_percent": (
                float(growth_match.group(1))
                if growth_match else None
            ),
            "description": description[:1200],
            "discovered_at": iso(now()),
        })

    return items, None


def calculate_score(item):
    traffic = item.get("approx_traffic_numeric")
    growth = item.get("growth_percent")

    volume_score = (
        0 if traffic is None
        else min(40, round(40 * traffic / (traffic + 100_000)))
    )

    growth_score = (
        0 if growth is None
        else max(0, min(40, round(growth / 10)))
    )

    published = parse_date(item.get("published_at"))
    age_hours = (
        max(0, (now() - published).total_seconds() / 3600)
        if published else 168
    )
    freshness_score = max(
        0, min(20, round(20 * (1 - age_hours / 168)))
    )

    parts = {
        "volume": volume_score,
        "growth": growth_score,
        "freshness": freshness_score,
    }
    return sum(parts.values()), parts


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regions", default="KZ,RU")
    parser.add_argument("--output", default="data/trends/latest.json")
    parser.add_argument("--history", default="data/trends/history.json")
    parser.add_argument("--timeout", type=int, default=15)
    args = parser.parse_args()

    regions = list(dict.fromkeys(
        region.strip().upper()
        for region in args.regions.split(",")
        if region.strip()
    ))

    if not regions:
        print("No regions configured.", file=sys.stderr)
        return 2

    output_path = Path(args.output)
    history_path = Path(args.history)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)

    previous = load_json(history_path, {"topics": {}})
    old_topics = previous.get("topics", {})
    cutoff = now() - timedelta(days=30)
    recent_cutoff = now() - timedelta(days=7)

    collected = []
    errors = []

    for region in regions:
        items, error = fetch_region(region, args.timeout)
        collected.extend(items)
        if error:
            errors.append(error)
        time.sleep(0.2)

    merged = {}

    for item in collected:
        key = re.sub(r"\s+", " ", item["topic"]).casefold()

        if key not in merged:
            merged[key] = item
            continue

        existing = merged[key]
        if item["region"] not in existing["regions"]:
            existing["regions"].append(item["region"])

        new_traffic = item.get("approx_traffic_numeric")
        old_traffic = existing.get("approx_traffic_numeric")

        if new_traffic is not None and (
            old_traffic is None or new_traffic > old_traffic
        ):
            existing["approx_traffic_numeric"] = new_traffic
            existing["approx_traffic_label"] = item.get(
                "approx_traffic_label"
            )

        growth = item.get("growth_percent")
        if growth is not None:
            existing["growth_percent"] = max(
                existing.get("growth_percent") or 0, growth
            )

    history = {}

    for key, value in old_topics.items():
        seen = (
            parse_date(value.get("last_seen"))
            if isinstance(value, dict) else None
        )
        if seen and seen >= cutoff:
            history[key] = value

    ranked = []

    for key, item in merged.items():
        prior = history.get(key, {})
        previous_seen = (
            parse_date(prior.get("last_seen"))
            if isinstance(prior, dict) else None
        )

        item["previously_seen_at"] = (
            iso(previous_seen) if previous_seen else None
        )
        item["recently_seen"] = bool(
            previous_seen and previous_seen >= recent_cutoff
        )
        item["score"], item["score_components"] = calculate_score(item)
        item["regions"] = sorted(set(item["regions"]))
        ranked.append(item)

        history[key] = {
            "topic": item["topic"],
            "last_seen": iso(now()),
            "first_seen": (
                prior.get("first_seen", iso(now()))
                if isinstance(prior, dict) else iso(now())
            ),
            "regions": item["regions"],
        }

    ranked.sort(key=lambda item: (
        item["recently_seen"],
        -item["score"],
        item["topic"].casefold(),
    ))

    shortlist = [
        item for item in ranked if not item["recently_seen"]
    ]

    report = {
        "generated_at": iso(now()),
        "regions_requested": regions,
        "source": "Google Trends Trending Now RSS",
        "candidate_count": len(ranked),
        "shortlist_count": len(shortlist),
        "scoring": {
            "max_score": 100,
            "weights": {
                "approximate_search_volume": 40,
                "growth_percent": 40,
                "freshness": 20,
            },
            "note": (
                "Missing metrics score zero. The score prioritizes "
                "research and does not predict video views."
            ),
        },
        "source_errors": errors,
        "candidates": ranked,
        "shortlist": shortlist[:20],
    }

    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    history_path.write_text(
        json.dumps({
            "updated_at": iso(now()),
            "topics": dict(list(history.items())[-500:]),
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        f"Regions: {','.join(regions)} | "
        f"candidates: {len(ranked)} | "
        f"fresh shortlist: {len(shortlist)}"
    )

    for item in shortlist[:10]:
        print(
            f"{item['score']:3d} | {item['topic']} | "
            f"{','.join(item['regions'])} | {item['source_url']}"
        )

    for error in errors:
        print("WARNING:", error, file=sys.stderr)

    if not ranked:
        print("No trend items returned.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
