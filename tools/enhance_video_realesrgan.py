from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


DEFAULT_FFMPEG = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"
DEFAULT_FFPROBE = os.getenv("FFPROBE_PATH") or shutil.which("ffprobe") or "ffprobe"
DEFAULT_REALESRGAN = os.getenv("REALESRGAN_PATH") or shutil.which("realesrgan-ncnn-vulkan") or "realesrgan-ncnn-vulkan"


def run(command: list[str]) -> None:
    print(" ".join(f'"{item}"' if " " in item else item for item in command))
    subprocess.run(command, check=True)


def probe_video(path: Path, ffprobe: str) -> dict:
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,duration,nb_frames",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    return streams[0] if streams else {}


def parse_rate(value: str, fallback: float = 25.0) -> float:
    if not value:
        return fallback
    if "/" in value:
        left, right = value.split("/", 1)
        denominator = float(right or 1)
        return float(left) / denominator if denominator else fallback
    return float(value)


def main() -> None:
    parser = argparse.ArgumentParser(description="Enhance a video with Real-ESRGAN ncnn-vulkan.")
    parser.add_argument("--input", required=True, help="Input video path.")
    parser.add_argument("--output", required=True, help="Output video path.")
    parser.add_argument("--work-dir", default="", help="Temporary frame directory. Defaults to output sibling.")
    parser.add_argument("--realesrgan", default=DEFAULT_REALESRGAN, help="Path to realesrgan-ncnn-vulkan.exe.")
    parser.add_argument("--ffmpeg", default=DEFAULT_FFMPEG, help="Path to ffmpeg.exe.")
    parser.add_argument("--ffprobe", default=DEFAULT_FFPROBE, help="Path to ffprobe.exe.")
    parser.add_argument("--model", default="realesrgan-x4plus", help="Real-ESRGAN model name.")
    parser.add_argument("--model-dir", default="", help="Real-ESRGAN model directory. Defaults to exe sibling models.")
    parser.add_argument("--scale", default="2", help="Output scale passed to -s.")
    parser.add_argument("--tile", default="128", help="Tile size passed to -t; lower uses less memory.")
    parser.add_argument("--format", default="png", choices=["png", "jpg", "webp"], help="Intermediate frame format.")
    parser.add_argument("--max-frames", type=int, default=0, help="Only process the first N frames for quick tests.")
    parser.add_argument("--keep-size", action="store_true", help="Downscale enhanced frames back to source size.")
    parser.add_argument("--keep-work", action="store_true", help="Keep temporary frames.")
    parser.add_argument("--crf", default="18", help="x264 CRF. Lower is clearer and larger, e.g. 16 or 18.")
    parser.add_argument("--preset", default="medium", help="x264 preset, e.g. fast, medium, slow.")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"input video not found: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    work_dir = Path(args.work_dir).resolve() if args.work_dir else output_path.parent / f"{output_path.stem}_work_{stamp}"
    raw_dir = work_dir / "frames_raw"
    enhanced_dir = work_dir / "frames_enhanced"
    raw_dir.mkdir(parents=True, exist_ok=True)
    enhanced_dir.mkdir(parents=True, exist_ok=True)

    info = probe_video(input_path, args.ffprobe)
    fps = parse_rate(str(info.get("r_frame_rate", "")), 25.0)

    raw_pattern = raw_dir / f"%08d.{args.format}"
    enhanced_pattern = enhanced_dir / f"%08d.{args.format}"

    extract_command = [
        args.ffmpeg,
        "-y",
        "-i",
        str(input_path),
    ]
    if args.max_frames > 0:
        extract_command.extend(["-frames:v", str(args.max_frames)])
    extract_command.extend([
        "-vsync",
        "0",
        str(raw_pattern),
    ])
    run(extract_command)

    model_dir = Path(args.model_dir).resolve() if args.model_dir else Path(args.realesrgan).resolve().parent / "models"

    run([
        args.realesrgan,
        "-i",
        str(raw_dir),
        "-o",
        str(enhanced_dir),
        "-n",
        args.model,
        "-m",
        str(model_dir),
        "-s",
        str(args.scale),
        "-t",
        str(args.tile),
        "-f",
        args.format,
    ])

    video_filters = []
    if args.keep_size and info.get("width") and info.get("height"):
        video_filters.extend(["-vf", f"scale={int(info['width'])}:{int(info['height'])}:flags=lanczos"])

    encode_command = [
        args.ffmpeg,
        "-y",
        "-framerate",
        f"{fps:.6f}",
        "-i",
        str(enhanced_pattern),
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-c:v",
        "libx264",
    ]
    encode_command.extend(video_filters)
    encode_command.extend([
        "-pix_fmt",
        "yuv420p",
        "-crf",
        str(args.crf),
        "-preset",
        str(args.preset),
        "-shortest",
        str(output_path),
    ])
    run(encode_command)

    meta = {
        "input": str(input_path),
        "output": str(output_path),
        "work_dir": str(work_dir),
        "model": args.model,
        "scale": args.scale,
        "tile": args.tile,
        "format": args.format,
        "keep_size": bool(args.keep_size),
        "crf": str(args.crf),
        "preset": str(args.preset),
        "fps": fps,
        "source_info": info,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    (output_path.with_suffix(output_path.suffix + ".json")).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if not args.keep_work:
        shutil.rmtree(work_dir, ignore_errors=True)

    print(f"enhanced video: {output_path}")


if __name__ == "__main__":
    main()
