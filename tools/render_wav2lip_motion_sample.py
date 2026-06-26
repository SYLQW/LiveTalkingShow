from __future__ import annotations

import argparse
import json
import os
import pickle
import shutil
import subprocess
from pathlib import Path
import sys
from datetime import datetime

import cv2
import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from avatars.wav2lip import audio
from avatars.wav2lip.models import Wav2Lip
from utils.device import initialize_device
from utils.image import mirror_index
from enhance_video_realesrgan import paste_roi_frames, prepare_roi_frames, run, sorted_frames, write_image


DEFAULT_FFMPEG = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg") or "ffmpeg"
DEFAULT_REALESRGAN = os.getenv("REALESRGAN_PATH") or shutil.which("realesrgan-ncnn-vulkan") or "realesrgan-ncnn-vulkan"


def sorted_images(directory: Path) -> list[Path]:
    items = list(directory.glob("*.png")) + list(directory.glob("*.jpg")) + list(directory.glob("*.jpeg"))
    return sorted(items, key=lambda item: int(item.stem) if item.stem.isdigit() else item.stem)


def read_images(paths: list[Path]) -> list[np.ndarray]:
    frames = []
    for item in paths:
        data = np.fromfile(str(item), dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
        if image is None:
            raise RuntimeError(f"failed to read image: {item}")
        frames.append(image)
    return frames


def load_clip(clip_dir: Path) -> tuple[list[np.ndarray], list[np.ndarray], list[tuple[int, int, int, int]]]:
    full_dir = clip_dir / "full_imgs"
    face_dir = clip_dir / "face_imgs"
    coords_path = clip_dir / "coords.pkl"
    if not full_dir.is_dir() or not face_dir.is_dir() or not coords_path.is_file():
        raise FileNotFoundError(f"clip must contain full_imgs, face_imgs and coords.pkl: {clip_dir}")

    frames = read_images(sorted_images(full_dir))
    faces = read_images(sorted_images(face_dir))
    with coords_path.open("rb") as file:
        coords = pickle.load(file)
    if not frames or not faces or not coords:
        raise RuntimeError(f"clip data is empty: {clip_dir}")
    return frames, faces, coords


def make_mel_chunks(audio_path: Path, fps: float) -> list[np.ndarray]:
    wav = audio.load_wav(str(audio_path), 16000)
    mel = audio.melspectrogram(wav)
    if np.isnan(mel.reshape(-1)).sum() > 0:
        raise RuntimeError("mel contains nan; please check the input audio")

    mel_step_size = 16
    mel_idx_multiplier = 80.0 / fps
    chunks = []
    index = 0
    while True:
        start_idx = int(index * mel_idx_multiplier)
        if start_idx + mel_step_size > len(mel[0]):
            chunks.append(mel[:, len(mel[0]) - mel_step_size :])
            break
        chunks.append(mel[:, start_idx : start_idx + mel_step_size])
        index += 1
    return chunks


def load_model(checkpoint_path: Path, device: str) -> Wav2Lip:
    model = Wav2Lip()
    checkpoint = torch.load(str(checkpoint_path), map_location=None if device == "cuda" else "cpu")
    state = {key.replace("module.", ""): value for key, value in checkpoint["state_dict"].items()}
    model.load_state_dict(state)
    model = model.to(device)
    return model.eval()


def to_bgr(image: np.ndarray) -> np.ndarray:
    if image.ndim == 3 and image.shape[2] == 4:
        return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    return image


def paste_back(frame: np.ndarray, pred: np.ndarray, bbox: tuple[int, int, int, int]) -> np.ndarray:
    y1, y2, x1, x2 = [int(value) for value in bbox]
    output = frame.copy()
    mouth = cv2.resize(pred.astype(np.uint8), (x2 - x1, y2 - y1))
    if output.ndim == 3 and output.shape[2] == 4:
        output[y1:y2, x1:x2, :3] = mouth
    else:
        output[y1:y2, x1:x2] = mouth
    return output


def mux_audio(ffmpeg: str, silent_video: Path, audio_path: Path, output_path: Path) -> None:
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(silent_video),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(output_path),
    ]
    subprocess.run(command, check=True)


def encode_frames_with_audio(
    ffmpeg: str,
    frame_pattern: Path,
    audio_path: Path,
    output_path: Path,
    fps: float,
    crf: str,
    preset: str,
    start_number: int = 0,
) -> None:
    command = [
        ffmpeg,
        "-y",
        "-framerate",
        f"{fps:.6f}",
        "-start_number",
        str(start_number),
        "-i",
        str(frame_pattern),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        str(crf),
        "-preset",
        str(preset),
        "-c:a",
        "aac",
        "-shortest",
        str(output_path),
    ]
    subprocess.run(command, check=True)


def enhance_rendered_frames(
    args: argparse.Namespace,
    temp_dir: Path,
    raw_frame_dir: Path,
    enhanced_frame_dir: Path,
    coords: list[tuple[int, int, int, int]],
) -> list[dict]:
    roi_raw_dir = temp_dir / "roi_raw"
    roi_enhanced_dir = temp_dir / "roi_enhanced"
    roi_raw_dir.mkdir(parents=True, exist_ok=True)
    roi_enhanced_dir.mkdir(parents=True, exist_ok=True)
    enhanced_frame_dir.mkdir(parents=True, exist_ok=True)

    raw_frames = sorted_frames(raw_frame_dir, args.enhance_format)
    roi_args = argparse.Namespace(
        coord_mode=args.enhance_coord_mode,
        roi_y_start_ratio=args.enhance_roi_y_start_ratio,
        roi_expand=args.enhance_roi_expand,
        io_workers=args.enhance_io_workers,
    )
    roi_meta = prepare_roi_frames(raw_frames, roi_raw_dir, coords, roi_args)

    model_dir = Path(args.realesrgan_model_dir).resolve() if args.realesrgan_model_dir else Path(args.realesrgan).resolve().parent / "models"
    command = [
        args.realesrgan,
        "-i",
        str(roi_raw_dir),
        "-o",
        str(roi_enhanced_dir),
        "-n",
        args.realesrgan_model,
        "-m",
        str(model_dir),
        "-s",
        str(args.realesrgan_scale),
        "-t",
        str(args.realesrgan_tile),
        "-f",
        args.enhance_format,
    ]
    if args.realesrgan_jobs:
        command.extend(["-j", args.realesrgan_jobs])
    run(command)
    paste_roi_frames(raw_frames, roi_enhanced_dir, enhanced_frame_dir, roi_meta, args.enhance_roi_feather, args.enhance_io_workers)
    return roi_meta


@torch.no_grad()
def main() -> None:
    parser = argparse.ArgumentParser(description="Render a short Wav2Lip mouth sample from a prepared motion clip.")
    parser.add_argument("--clip-dir", required=True, help="Motion clip directory with full_imgs, face_imgs and coords.pkl.")
    parser.add_argument("--audio", required=True, help="WAV audio path.")
    parser.add_argument("--checkpoint", default="models/wav2lip.pth", help="Wav2Lip checkpoint path.")
    parser.add_argument("--output", required=True, help="Output video path.")
    parser.add_argument("--fps", type=float, default=24.0, help="Output FPS.")
    parser.add_argument("--batch-size", type=int, default=4, help="Wav2Lip inference batch size.")
    parser.add_argument("--max-frames", type=int, default=0, help="Limit rendered frames for quick experiments.")
    parser.add_argument("--ffmpeg", default=DEFAULT_FFMPEG, help="Path to ffmpeg executable.")
    parser.add_argument("--work-dir", default="", help="Temporary work directory. Defaults to output sibling.")
    parser.add_argument("--keep-work", action="store_true", help="Keep temporary files for debugging.")
    parser.add_argument("--crf", default="16", help="x264 CRF used by the enhanced frame encoder.")
    parser.add_argument("--preset", default="medium", help="x264 preset used by the enhanced frame encoder.")
    parser.add_argument("--enhance-roi", choices=["none", "lower-face"], default="none", help="Enhance rendered frames before final encoding.")
    parser.add_argument("--realesrgan", default=DEFAULT_REALESRGAN, help="Path to realesrgan-ncnn-vulkan.exe.")
    parser.add_argument("--realesrgan-model-dir", default="", help="Real-ESRGAN model directory. Defaults to exe sibling models.")
    parser.add_argument("--realesrgan-model", default="realesr-animevideov3", help="Real-ESRGAN model name.")
    parser.add_argument("--realesrgan-scale", default="2", help="Output scale passed to Real-ESRGAN -s.")
    parser.add_argument("--realesrgan-tile", default="0", help="Tile size passed to Real-ESRGAN -t.")
    parser.add_argument("--realesrgan-jobs", default="", help="Pass Real-ESRGAN ncnn-vulkan -j value, e.g. 2:2:2.")
    parser.add_argument("--enhance-format", default="png", choices=["png", "jpg", "webp"], help="Intermediate frame format for enhanced rendering.")
    parser.add_argument("--enhance-coord-mode", choices=["mirror", "repeat", "clamp"], default="mirror", help="How to map output frame index to coords index.")
    parser.add_argument("--enhance-roi-y-start-ratio", type=float, default=0.58, help="Lower-face ROI starts at this ratio inside the face box.")
    parser.add_argument("--enhance-roi-expand", default="0 4 19 23", help="Expand lower-face ROI as TOP BOTTOM LEFT RIGHT pixels.")
    parser.add_argument("--enhance-roi-feather", type=int, default=18, help="Blend feather size when pasting enhanced ROI.")
    parser.add_argument("--enhance-io-workers", type=int, default=4, help="Workers used for ROI crop and paste steps.")
    args = parser.parse_args()

    clip_dir = Path(args.clip_dir).resolve()
    audio_path = Path(args.audio).resolve()
    checkpoint_path = Path(args.checkpoint).resolve()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    frames, faces, coords = load_clip(clip_dir)
    mel_chunks = make_mel_chunks(audio_path, args.fps)
    render_count = len(mel_chunks)
    if args.max_frames > 0:
        render_count = min(render_count, args.max_frames)
    if render_count <= 0:
        raise RuntimeError("nothing to render")

    device = initialize_device()
    model = load_model(checkpoint_path, device)
    face_size = faces[0].shape[0]
    height, width = to_bgr(frames[0]).shape[:2]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_dir = Path(args.work_dir).resolve() if args.work_dir else output_path.parent / f"{output_path.stem}_work_{stamp}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        enhance_enabled = args.enhance_roi != "none"
        raw_frame_dir = temp_dir / "rendered_frames"
        enhanced_frame_dir = temp_dir / "enhanced_frames"
        writer = None
        if enhance_enabled:
            raw_frame_dir.mkdir(parents=True, exist_ok=True)
        else:
            silent_video = temp_dir / "silent.mp4"
            writer = cv2.VideoWriter(
                str(silent_video),
                cv2.VideoWriter_fourcc(*"mp4v"),
                args.fps,
                (width, height),
            )
            if not writer.isOpened():
                raise RuntimeError("failed to open temporary video writer")

        rendered_index = 0
        for start in range(0, render_count, args.batch_size):
            batch_mels = mel_chunks[start : start + args.batch_size]
            batch_faces = []
            batch_frames = []
            batch_coords = []
            for offset, _mel in enumerate(batch_mels):
                frame_idx = mirror_index(len(frames), start + offset)
                face = to_bgr(faces[frame_idx])
                if face.shape[:2] != (face_size, face_size):
                    face = cv2.resize(face, (face_size, face_size))
                batch_faces.append(face)
                batch_frames.append(frames[frame_idx])
                batch_coords.append(coords[frame_idx])

            img_batch = np.asarray(batch_faces)
            img_masked = img_batch.copy()
            img_masked[:, face_size // 2 :] = 0
            img_batch = np.concatenate((img_masked, img_batch), axis=3) / 255.0
            mel_batch = np.asarray(batch_mels)
            mel_batch = np.reshape(mel_batch, [len(mel_batch), mel_batch.shape[1], mel_batch.shape[2], 1])

            img_tensor = torch.FloatTensor(np.transpose(img_batch, (0, 3, 1, 2))).to(device)
            mel_tensor = torch.FloatTensor(np.transpose(mel_batch, (0, 3, 1, 2))).to(device)
            pred = model(mel_tensor, img_tensor).cpu().numpy().transpose(0, 2, 3, 1) * 255.0

            for pred_frame, full_frame, bbox in zip(pred, batch_frames, batch_coords):
                rendered = to_bgr(paste_back(full_frame, pred_frame, bbox))
                if enhance_enabled:
                    write_image(raw_frame_dir / f"{rendered_index:08d}.{args.enhance_format}", rendered)
                    rendered_index += 1
                else:
                    writer.write(rendered)

        roi_meta = []
        if enhance_enabled:
            roi_meta = enhance_rendered_frames(args, temp_dir, raw_frame_dir, enhanced_frame_dir, coords)
            encode_frames_with_audio(
                args.ffmpeg,
                enhanced_frame_dir / f"%08d.{args.enhance_format}",
                audio_path,
                output_path,
                args.fps,
                args.crf,
                args.preset,
                start_number=0,
            )
        else:
            writer.release()
            mux_audio(args.ffmpeg, silent_video, audio_path, output_path)
    finally:
        if "writer" in locals() and writer is not None:
            writer.release()
        if not args.keep_work:
            shutil.rmtree(temp_dir, ignore_errors=True)

    meta = {
        "clip_dir": str(clip_dir),
        "audio": str(audio_path),
        "checkpoint": str(checkpoint_path),
        "output": str(output_path),
        "fps": args.fps,
        "batch_size": args.batch_size,
        "render_count": render_count,
        "face_size": face_size,
        "work_dir": str(temp_dir),
        "enhance_roi": args.enhance_roi,
        "realesrgan_model": args.realesrgan_model if args.enhance_roi != "none" else "",
        "realesrgan_scale": args.realesrgan_scale if args.enhance_roi != "none" else "",
        "realesrgan_tile": args.realesrgan_tile if args.enhance_roi != "none" else "",
        "realesrgan_jobs": args.realesrgan_jobs if args.enhance_roi != "none" else "",
        "enhance_roi_y_start_ratio": args.enhance_roi_y_start_ratio if args.enhance_roi != "none" else "",
        "enhance_roi_expand": args.enhance_roi_expand if args.enhance_roi != "none" else "",
        "enhance_roi_feather": args.enhance_roi_feather if args.enhance_roi != "none" else "",
        "enhance_io_workers": args.enhance_io_workers if args.enhance_roi != "none" else "",
        "enhanced_roi_frames": len(roi_meta) if args.enhance_roi != "none" else 0,
    }
    output_path.with_suffix(output_path.suffix + ".json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"rendered wav2lip sample: {output_path}")


if __name__ == "__main__":
    main()
