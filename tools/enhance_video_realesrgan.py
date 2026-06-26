from __future__ import annotations

import argparse
import json
import os
import pickle
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np


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


def sorted_frames(directory: Path, suffix: str) -> list[Path]:
    return sorted(directory.glob(f"*.{suffix}"), key=lambda item: int(item.stem) if item.stem.isdigit() else item.stem)


def read_image(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise RuntimeError(f"failed to read frame: {path}")
    return image


def write_image(path: Path, image: np.ndarray) -> None:
    ext = path.suffix.lower() or ".png"
    ok, buffer = cv2.imencode(ext, image)
    if not ok:
        raise RuntimeError(f"failed to encode frame: {path}")
    buffer.tofile(str(path))


def mirror_index(size: int, index: int) -> int:
    if size <= 1:
        return 0
    turn = index // size
    offset = index % size
    if turn % 2 == 0:
        return offset
    return size - offset - 1


def coord_index(size: int, index: int, mode: str) -> int:
    if size <= 0:
        raise RuntimeError("coords is empty")
    if mode == "repeat":
        return index % size
    if mode == "clamp":
        return min(index, size - 1)
    return mirror_index(size, index)


def load_coords(args: argparse.Namespace) -> list[tuple[int, int, int, int]]:
    coords_path = Path(args.coords).resolve() if args.coords else None
    if not coords_path and args.clip_dir:
        coords_path = Path(args.clip_dir).resolve() / "coords.pkl"
    if not coords_path:
        return []
    if not coords_path.is_file():
        raise FileNotFoundError(f"coords file not found: {coords_path}")
    with coords_path.open("rb") as file:
        coords = pickle.load(file)
    normalized = []
    for item in coords:
        if len(item) != 4:
            raise RuntimeError(f"invalid coord item: {item}")
        y1, y2, x1, x2 = [int(value) for value in item]
        normalized.append((y1, y2, x1, x2))
    return normalized


def parse_expand(value: str) -> tuple[int, int, int, int]:
    parts = [part for part in value.replace("，", ",").replace(" ", ",").split(",") if part != ""]
    if len(parts) != 4:
        raise ValueError("--roi-expand must contain 4 values: TOP BOTTOM LEFT RIGHT")
    return tuple(int(part) for part in parts)


def lower_face_box(
    coord: tuple[int, int, int, int],
    width: int,
    height: int,
    y_start_ratio: float,
    expand: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    y1, y2, x1, x2 = coord
    face_h = max(1, y2 - y1)
    roi_y1 = y1 + int(face_h * y_start_ratio)
    top, bottom, left, right = expand
    roi_y1 = max(0, roi_y1 - top)
    roi_y2 = min(height, y2 + bottom)
    roi_x1 = max(0, x1 - left)
    roi_x2 = min(width, x2 + right)
    if roi_y2 <= roi_y1 or roi_x2 <= roi_x1:
        raise RuntimeError(f"invalid roi box from coord={coord}: {(roi_y1, roi_y2, roi_x1, roi_x2)}")
    return roi_y1, roi_y2, roi_x1, roi_x2


def make_blend_mask(height: int, width: int, feather: int) -> np.ndarray:
    if feather <= 0:
        return np.ones((height, width), dtype=np.float32)
    y = np.minimum(np.arange(height), np.arange(height)[::-1]).astype(np.float32)
    x = np.minimum(np.arange(width), np.arange(width)[::-1]).astype(np.float32)
    yy = np.minimum(y / max(1, feather), 1.0)[:, None]
    xx = np.minimum(x / max(1, feather), 1.0)[None, :]
    mask = np.minimum(yy, xx)
    return cv2.GaussianBlur(mask, (0, 0), max(1, feather / 3.0))


def prepare_roi_frames(
    raw_frames: list[Path],
    roi_raw_dir: Path,
    coords: list[tuple[int, int, int, int]],
    args: argparse.Namespace,
) -> list[dict]:
    expand = parse_expand(args.roi_expand)
    workers = max(1, int(getattr(args, "io_workers", 1) or 1))

    def prepare_one(index: int, frame_path: Path) -> tuple[int, dict]:
        frame = read_image(frame_path)
        height, width = frame.shape[:2]
        coord = coords[coord_index(len(coords), index, args.coord_mode)]
        y1, y2, x1, x2 = lower_face_box(coord, width, height, args.roi_y_start_ratio, expand)
        crop = frame[y1:y2, x1:x2]
        write_image(roi_raw_dir / frame_path.name, crop)
        return index, {
            "frame": frame_path.name,
            "coord": [int(v) for v in coord],
            "roi": [int(y1), int(y2), int(x1), int(x2)],
        }

    if workers == 1:
        results = [prepare_one(index, frame_path) for index, frame_path in enumerate(raw_frames)]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda item: prepare_one(*item), enumerate(raw_frames)))
    return [meta for _index, meta in sorted(results, key=lambda item: item[0])]


def paste_roi_frames(
    raw_frames: list[Path],
    roi_enhanced_dir: Path,
    output_dir: Path,
    roi_meta: list[dict],
    feather: int,
    workers: int = 1,
) -> None:
    workers = max(1, int(workers or 1))

    def paste_one(frame_path: Path, meta: dict) -> None:
        frame = read_image(frame_path)
        roi_path = roi_enhanced_dir / frame_path.name
        enhanced = read_image(roi_path)
        y1, y2, x1, x2 = meta["roi"]
        target_h = y2 - y1
        target_w = x2 - x1
        if enhanced.shape[:2] != (target_h, target_w):
            enhanced = cv2.resize(enhanced, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)

        base_crop = frame[y1:y2, x1:x2].copy()
        enhanced_rgb = enhanced[:, :, :3] if enhanced.ndim == 3 and enhanced.shape[2] >= 3 else enhanced
        base_rgb = base_crop[:, :, :3] if base_crop.ndim == 3 and base_crop.shape[2] >= 3 else base_crop
        if enhanced_rgb.shape[:2] != base_rgb.shape[:2]:
            raise RuntimeError(f"enhanced roi size mismatch: {roi_path}")

        mask = make_blend_mask(target_h, target_w, feather)
        if base_rgb.ndim == 3:
            mask = mask[:, :, None]
        blended = (enhanced_rgb.astype(np.float32) * mask + base_rgb.astype(np.float32) * (1.0 - mask)).clip(0, 255).astype(np.uint8)
        if frame.ndim == 3 and frame.shape[2] == 4:
            frame[y1:y2, x1:x2, :3] = blended[:, :, :3]
        else:
            frame[y1:y2, x1:x2] = blended
        write_image(output_dir / frame_path.name, frame)

    if workers == 1:
        for frame_path, meta in zip(raw_frames, roi_meta):
            paste_one(frame_path, meta)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda item: paste_one(*item), zip(raw_frames, roi_meta)))


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
    parser.add_argument("--clip-dir", default="", help="Motion clip directory that contains coords.pkl.")
    parser.add_argument("--coords", default="", help="coords.pkl path. Overrides --clip-dir when both are set.")
    parser.add_argument("--roi", choices=["full-frame", "lower-face"], default="full-frame", help="Enhance the full frame or only the lower face area from coords.")
    parser.add_argument("--coord-mode", choices=["mirror", "repeat", "clamp"], default="mirror", help="How to map video frame index to coords index.")
    parser.add_argument("--roi-y-start-ratio", type=float, default=0.58, help="Lower-face ROI starts at this ratio inside the face box.")
    parser.add_argument("--roi-expand", default="0 4 19 23", help="Expand lower-face ROI as TOP BOTTOM LEFT RIGHT pixels.")
    parser.add_argument("--roi-feather", type=int, default=18, help="Blend feather size when pasting enhanced ROI.")
    parser.add_argument("--io-workers", type=int, default=4, help="Workers used for ROI crop and paste steps.")
    parser.add_argument("--realesrgan-jobs", default="", help="Pass Real-ESRGAN ncnn-vulkan -j value, e.g. 2:2:2.")
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
    roi_raw_dir = work_dir / "roi_raw"
    roi_enhanced_dir = work_dir / "roi_enhanced"
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

    roi_meta = []
    if args.roi == "lower-face":
        coords = load_coords(args)
        if not coords:
            raise RuntimeError("--roi lower-face requires --clip-dir or --coords")
        roi_raw_dir.mkdir(parents=True, exist_ok=True)
        roi_enhanced_dir.mkdir(parents=True, exist_ok=True)
        raw_frames = sorted_frames(raw_dir, args.format)
        roi_meta = prepare_roi_frames(raw_frames, roi_raw_dir, coords, args)
        enhance_command = [
            args.realesrgan,
            "-i",
            str(roi_raw_dir),
            "-o",
            str(roi_enhanced_dir),
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
        ]
        if args.realesrgan_jobs:
            enhance_command.extend(["-j", args.realesrgan_jobs])
        run(enhance_command)
        paste_roi_frames(raw_frames, roi_enhanced_dir, enhanced_dir, roi_meta, args.roi_feather, args.io_workers)
    else:
        enhance_command = [
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
        ]
        if args.realesrgan_jobs:
            enhance_command.extend(["-j", args.realesrgan_jobs])
        run(enhance_command)

    video_filters = []
    if args.roi == "full-frame" and args.keep_size and info.get("width") and info.get("height"):
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
        "roi": args.roi,
        "clip_dir": str(Path(args.clip_dir).resolve()) if args.clip_dir else "",
        "coords": str(Path(args.coords).resolve()) if args.coords else "",
        "coord_mode": args.coord_mode,
        "roi_y_start_ratio": args.roi_y_start_ratio,
        "roi_expand": args.roi_expand,
        "roi_feather": args.roi_feather,
        "io_workers": args.io_workers,
        "realesrgan_jobs": args.realesrgan_jobs,
        "roi_frames": len(roi_meta),
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
