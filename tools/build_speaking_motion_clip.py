from __future__ import annotations

import argparse
import json
import pickle
import shutil
import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import torch
from tqdm import tqdm

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from avatars.wav2lip import face_detection


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
DEFAULT_FFMPEG = Path("G:/ffmpeg/ffmpeg-8.1-essentials_build/bin/ffmpeg.exe")


def write_png(path: Path, image: np.ndarray) -> None:
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError(f"Failed to encode PNG: {path}")
    encoded.tofile(str(path))


def read_image(path: Path) -> np.ndarray:
    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise RuntimeError(f"Cannot read image: {path}")
    return image


def list_images(directory: Path) -> list[Path]:
    def sort_key(path: Path):
        return int(path.stem) if path.stem.isdigit() else path.name

    return sorted(
        [path for path in directory.iterdir() if path.suffix.lower() in IMAGE_EXTS],
        key=sort_key,
    )


def chroma_key_green(bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    green = (
        (hsv[:, :, 0] >= 35)
        & (hsv[:, :, 0] <= 95)
        & (hsv[:, :, 1] >= 35)
        & (hsv[:, :, 2] >= 45)
    ).astype(np.uint8)

    kernel = np.ones((5, 5), np.uint8)
    green = cv2.morphologyEx(green, cv2.MORPH_CLOSE, kernel, iterations=2)
    green = cv2.dilate(green, kernel, iterations=1)

    _, labels = cv2.connectedComponents(green, connectivity=8)
    border_labels = np.unique(
        np.concatenate([labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1]])
    )
    bg = (np.isin(labels, border_labels) & (green > 0)).astype(np.uint8) * 255
    bg = cv2.morphologyEx(bg, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=1)
    bg = cv2.GaussianBlur(bg, (9, 9), 0)

    alpha = 255 - bg
    alpha[alpha < 18] = 0
    alpha[alpha > 245] = 255

    keyed_bgr = bgr.copy().astype(np.float32)
    blue = keyed_bgr[:, :, 0]
    green_channel = keyed_bgr[:, :, 1]
    red = keyed_bgr[:, :, 2]
    max_red_blue = np.maximum(red, blue)
    spill = np.clip(green_channel - max_red_blue * 1.05, 0, 80)
    edge = ((alpha > 0) & (alpha < 255)).astype(np.float32)
    keyed_bgr[:, :, 1] = green_channel - spill * (0.55 + 0.35 * edge)
    keyed_bgr = np.clip(keyed_bgr, 0, 255).astype(np.uint8)

    bgra = cv2.cvtColor(keyed_bgr, cv2.COLOR_BGR2BGRA)
    bgra[:, :, 3] = alpha
    return bgra


def frame_for_detection(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 3 and frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
    return frame


def load_source_frames(
    source: Path,
    start: float,
    end: float | None,
    fps: float,
    chroma_key: bool,
    max_frames: int,
) -> tuple[list[np.ndarray], dict]:
    if source.is_dir():
        images = list_images(source)
        if not images:
            raise RuntimeError(f"No images found in: {source}")
        frames = [read_image(path) for path in images]
        if max_frames > 0:
            frames = frames[:max_frames]
        return frames, {
            "source_type": "image_dir",
            "source_fps": None,
            "requested_fps": fps,
            "start": start,
            "end": end,
        }

    if source.suffix.lower() not in VIDEO_EXTS:
        raise RuntimeError(f"Unsupported source file: {source}")

    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {source}")

    source_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    target_fps = fps if fps > 0 else source_fps
    target_interval = 1.0 / target_fps
    next_sample_at = max(0.0, start)
    frame_index = 0
    frames: list[np.ndarray] = []

    while True:
        ok, frame = cap.read()
        if not ok:
            break

        current_time = frame_index / source_fps
        frame_index += 1
        if current_time + 1e-6 < start:
            continue
        if end is not None and current_time > end:
            break
        if current_time + 1e-6 < next_sample_at:
            continue

        if chroma_key:
            frame = chroma_key_green(frame)
        frames.append(frame)
        next_sample_at += target_interval

        if max_frames > 0 and len(frames) >= max_frames:
            break

    cap.release()
    if not frames:
        raise RuntimeError("No frames selected from source")

    return frames, {
        "source_type": "video",
        "source_fps": source_fps,
        "requested_fps": target_fps,
        "start": start,
        "end": end,
    }


def cut_source_video(source: Path, target: Path, start: float, end: float | None, ffmpeg_path: str) -> Path:
    import subprocess

    ffmpeg = Path(ffmpeg_path) if ffmpeg_path else DEFAULT_FFMPEG
    if not ffmpeg.exists():
        raise RuntimeError(f"ffmpeg not found: {ffmpeg}")

    target.parent.mkdir(parents=True, exist_ok=True)
    command = [str(ffmpeg), "-y"]
    if start > 0:
        command.extend(["-ss", str(start)])
    command.extend(["-i", str(source)])
    if end is not None and end > start:
        command.extend(["-t", str(end - start)])
    command.extend(["-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)])
    subprocess.run(command, check=True)
    return target


def smooth_boxes(boxes: np.ndarray, window_size: int) -> np.ndarray:
    if window_size <= 1 or len(boxes) < window_size:
        return boxes
    for index in range(len(boxes)):
        if index + window_size > len(boxes):
            window = boxes[len(boxes) - window_size :]
        else:
            window = boxes[index : index + window_size]
        boxes[index] = np.mean(window, axis=0)
    return boxes


def detect_boxes(
    frames: list[np.ndarray],
    pads: list[int],
    batch_size: int,
    nosmooth: bool,
) -> list[tuple[int, int, int, int]]:
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Using {device} for face detection.")
    detector = face_detection.FaceAlignment(
        face_detection.LandmarksType._2D,
        flip_input=False,
        device=device,
    )

    detect_frames = [frame_for_detection(frame) for frame in frames]
    predictions = []
    current_batch_size = max(1, batch_size)
    while True:
        predictions = []
        try:
            for index in tqdm(range(0, len(detect_frames), current_batch_size), desc="detect faces"):
                batch = np.asarray(detect_frames[index : index + current_batch_size])
                predictions.extend(detector.get_detections_for_batch(batch))
        except RuntimeError:
            if current_batch_size == 1:
                raise
            current_batch_size //= 2
            print(f"Recovering from OOM; new batch size: {current_batch_size}")
            continue
        break

    top, bottom, left, right = pads
    boxes = []
    for rect, frame in zip(predictions, detect_frames):
        height, width = frame.shape[:2]
        if rect is None:
            rect = [0, 0, width, height]
        y1 = max(0, int(rect[1]) - top)
        y2 = min(height, int(rect[3]) + bottom)
        x1 = max(0, int(rect[0]) - left)
        x2 = min(width, int(rect[2]) + right)
        boxes.append([x1, y1, x2, y2])

    boxes_array = np.asarray(boxes, dtype=np.float32)
    if not nosmooth:
        boxes_array = smooth_boxes(boxes_array, window_size=5)
    return [(int(x1), int(y1), int(x2), int(y2)) for x1, y1, x2, y2 in boxes_array]


def fixed_boxes(
    frames: list[np.ndarray],
    box: list[int] | tuple[int, int, int, int],
) -> list[tuple[int, int, int, int]]:
    if len(box) != 4:
        raise RuntimeError(f"fixed face box must have 4 values: {box}")
    x1, y1, x2, y2 = [int(value) for value in box]
    if x2 <= x1 or y2 <= y1:
        raise RuntimeError(f"invalid fixed face box: {box}")
    return [(x1, y1, x2, y2) for _ in frames]


def make_preview(frame: np.ndarray, box: tuple[int, int, int, int], label: str) -> np.ndarray:
    if frame.ndim == 3 and frame.shape[2] == 4:
        bgr = frame[:, :, :3]
        alpha = frame[:, :, 3].astype(np.float32) / 255.0
        checker = np.indices(frame.shape[:2]).sum(axis=0) // 28 % 2
        checker = np.where(checker[..., None] == 0, 210, 165).astype(np.uint8)
        checker = np.repeat(checker, 3, axis=2)
        preview = (bgr.astype(np.float32) * alpha[..., None] + checker * (1 - alpha[..., None])).astype(np.uint8)
    else:
        preview = frame.copy()

    x1, y1, x2, y2 = box
    cv2.rectangle(preview, (x1, y1), (x2, y2), (30, 60, 240), 2)
    cv2.putText(preview, label, (18, 36), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (30, 30, 30), 3, cv2.LINE_AA)
    cv2.putText(preview, label, (18, 36), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (245, 245, 245), 1, cv2.LINE_AA)
    max_width = 720
    if preview.shape[1] > max_width:
        scale = max_width / preview.shape[1]
        preview = cv2.resize(preview, (max_width, int(preview.shape[0] * scale)), interpolation=cv2.INTER_AREA)
    return preview


def build_clip(args: argparse.Namespace) -> None:
    source = Path(args.source)
    target = Path(args.out_root) / args.avatar_id / args.action_id
    full_dir = target / "full_imgs"
    face_dir = target / "face_imgs"
    coords_path = target / "coords.pkl"
    metadata_path = target / "metadata.json"
    preview_path = target / "preview.png"

    if target.exists():
        if not args.overwrite:
            raise RuntimeError(f"Target already exists: {target}")
        shutil.rmtree(target)
    full_dir.mkdir(parents=True)
    face_dir.mkdir(parents=True)

    clip_source = source
    source_start = args.start
    source_end = args.end
    cut_video_path = None
    if args.use_ffmpeg_cut and source.is_file() and source.suffix.lower() in VIDEO_EXTS:
        cut_video_path = target / "source_clip.mp4"
        clip_source = cut_source_video(source, cut_video_path, args.start, args.end, args.ffmpeg_path)
        source_start = 0.0
        source_end = None

    frames, source_info = load_source_frames(
        source=clip_source,
        start=source_start,
        end=source_end,
        fps=args.fps,
        chroma_key=args.chroma_key,
        max_frames=args.max_frames,
    )
    if getattr(args, "fixed_face_box", None):
        boxes = fixed_boxes(frames, args.fixed_face_box)
    else:
        boxes = detect_boxes(
            frames=frames,
            pads=args.pads,
            batch_size=args.face_det_batch_size,
            nosmooth=args.nosmooth,
        )

    resampling = cv2.INTER_AREA
    coords = []
    for index, (frame, box) in enumerate(tqdm(list(zip(frames, boxes)), desc="write clip")):
        x1, y1, x2, y2 = box
        face = frame[y1:y2, x1:x2]
        if face.size == 0:
            raise RuntimeError(f"Empty face crop at frame {index}: {box}")
        face = cv2.resize(face, (args.img_size, args.img_size), interpolation=resampling)
        write_png(full_dir / f"{index:08d}.png", frame)
        write_png(face_dir / f"{index:08d}.png", face)
        coords.append((y1, y2, x1, x2))

    with coords_path.open("wb") as file:
        pickle.dump(coords, file)

    preview_frame_index = min(len(frames) - 1, max(0, len(frames) // 2))
    preview = make_preview(frames[preview_frame_index], boxes[preview_frame_index], args.action_id)
    write_png(preview_path, preview)

    metadata = {
        "action_id": args.action_id,
        "display_name": args.display_name or args.action_id,
        "avatar_id": args.avatar_id,
        "source": str(source),
        "cut_video": str(cut_video_path) if cut_video_path else "",
        "source_info": source_info,
        "fps": source_info["requested_fps"],
        "frame_count": len(frames),
        "img_size": args.img_size,
        "pads": args.pads,
        "fixed_face_box": list(args.fixed_face_box) if getattr(args, "fixed_face_box", None) else [],
        "loop": not args.no_loop,
        "tags": [tag.strip() for tag in args.tags.split(",") if tag.strip()],
        "best_for": args.best_for,
        "chroma_key": bool(args.chroma_key),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"motion clip: {target}")
    print(f"frames: {len(frames)}")
    print(f"coords: {coords_path}")
    print(f"preview: {preview_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a speaking motion clip for LiveTalking Wav2Lip.")
    parser.add_argument("--source", required=True, help="Source video file or image directory.")
    parser.add_argument("--avatar-id", required=True)
    parser.add_argument("--action-id", required=True)
    parser.add_argument("--display-name", default="")
    parser.add_argument("--out-root", default="data/speaking_actions")
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--end", type=float)
    parser.add_argument("--fps", type=float, default=25.0)
    parser.add_argument("--img-size", type=int, default=256)
    parser.add_argument("--pads", nargs=4, type=int, default=[0, 10, 0, 0], metavar=("TOP", "BOTTOM", "LEFT", "RIGHT"))
    parser.add_argument("--face-det-batch-size", type=int, default=8)
    parser.add_argument("--fixed-face-box", nargs=4, type=int, metavar=("X1", "Y1", "X2", "Y2"))
    parser.add_argument("--max-frames", type=int, default=0, help="Limit frames for quick tests; 0 means no limit.")
    parser.add_argument("--tags", default="speaking,teaching")
    parser.add_argument("--best-for", default="")
    parser.add_argument("--chroma-key", action="store_true")
    parser.add_argument("--use-ffmpeg-cut", action="store_true")
    parser.add_argument("--ffmpeg-path", default=str(DEFAULT_FFMPEG))
    parser.add_argument("--nosmooth", action="store_true")
    parser.add_argument("--no-loop", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> None:
    build_clip(parse_args())


if __name__ == "__main__":
    main()
