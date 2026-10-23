
#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


def run(command):
    return subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def fail(message):
    print(f"QUALITY GATE FAILED: {message}", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Validate a rendered YouTube Shorts MP4."
    )
    parser.add_argument("--file", required=True)
    parser.add_argument("--expected-duration", type=float, required=True)
    args = parser.parse_args()

    path = Path(args.file)

    if not path.is_file() or path.stat().st_size == 0:
        fail(f"Video is missing or empty: {path}")

    probe = run([
        "ffprobe", "-v", "error",
        "-show_streams", "-show_format",
        "-of", "json", str(path),
    ])

    if probe.returncode != 0:
        fail("ffprobe could not read the video.")

    try:
        data = json.loads(probe.stdout)
        streams = data["streams"]
        duration = float(data["format"]["duration"])
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        fail("Invalid or incomplete media metadata.")

    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]

    if len(videos) != 1:
        fail(f"Expected 1 video stream, found {len(videos)}.")

    if len(audios) != 1:
        fail(f"Expected 1 audio stream, found {len(audios)}.")

    video = videos[0]
    audio = audios[0]

    if video.get("codec_name") != "h264":
        fail(f"Expected H.264 video, got {video.get('codec_name')}.")

    if video.get("width") != 1080 or video.get("height") != 1920:
        fail(
            f"Expected 1080x1920, got "
            f"{video.get('width')}x{video.get('height')}."
        )

    try:
        fps = float(Fraction(video.get("avg_frame_rate", "0/1")))
    except (ValueError, ZeroDivisionError):
        fail("Could not determine frame rate.")

    if abs(fps - 30.0) > 0.15:
        fail(f"Expected approximately 30 fps, got {fps:.3f}.")

    if audio.get("codec_name") != "aac":
        fail(f"Expected AAC audio, got {audio.get('codec_name')}.")

    if abs(duration - args.expected_duration) > 1.5:
        fail(
            f"Duration {duration:.2f}s differs from requested "
            f"{args.expected_duration:.2f}s by more than 1.5s."
        )

    decode = run([
        "ffmpeg", "-hide_banner", "-v", "error",
        "-i", str(path),
        "-f", "null", "-",
    ])

    if decode.returncode != 0 or decode.stderr.strip():
        fail("FFmpeg reported decoding errors.")

    print("QUALITY GATE PASSED")
    print(f"File: {path}")
    print(f"Size: {path.stat().st_size} bytes")
    print(f"Resolution: 1080x1920")
    print(f"Frame rate: {fps:.3f} fps")
    print(f"Duration: {duration:.2f}s")
    print("Video: H.264; Audio: AAC")
    print("Full decode: successful")


if __name__ == "__main__":
    main()
