###############################################################################
#  服务器路由 — 统一异常处理的 API 路由
###############################################################################

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from shutil import which
from types import SimpleNamespace
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
from aiohttp import web

from utils.logger import logger
from server.alpha_stream import alpha_audio_ws, alpha_ws
from utils.audio import pcm_to_float32, resample_audio


# ─── 路由工具函数 ──────────────────────────────────────────────────────────

def json_ok(data=None):
    """返回成功 JSON 响应"""
    body = {"code": 0, "msg": "ok"}
    if data is not None:
        body["data"] = data
    return web.Response(
        content_type="application/json",
        text=json.dumps(body),
    )


def json_error(msg: str, code: int = -1):
    """返回错误 JSON 响应"""
    return web.Response(
        content_type="application/json",
        text=json.dumps({"code": code, "msg": str(msg)}),
    )


from server.session_manager import session_manager
from server.avatar_routes import setup_avatar_routes


_motion_face_detector = None
_motion_face_detector_device = ""
_motion_face_detector_lock = threading.Lock()
MOTION_UPLOAD_MAX_BYTES = int(os.getenv("MOTION_UPLOAD_MAX_BYTES", str(512 * 1024 * 1024)))
MOTION_ALLOWED_SOURCE_DIRS = ("tmp/uploaded_sources", "data")
MOTION_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".avi"}
MOTION_PLAY_MODES = {"forward", "pingpong", "reverse", "random_direction"}
AVATAR_MOTION_CONFIG = "motion.json"
MOTION_TASKS: dict[str, dict] = {}
MOTION_TASK_LOCK = threading.Lock()
MOTION_TASK_MAX_ITEMS = 100
RENDER_LIP_BACKENDS = {"wav2lip256", "onnx_base"}
RENDER_ENHANCE_MODES = {"none", "realesr-animevideov3", "realesrgan-x4plus-anime", "clear_reality_x4"}
RENDER_REALTIME_CONFIG = {
    "enabled": False,
    "lip_backend": "wav2lip256",
    "enhance_mode": "none",
    "updated_at": "",
}


@dataclass
class HardwareAudioSession:
    """State for robot-tts task target websocket input."""
    sessionid: str
    task_id: str = ""
    sample_rate: int = 16000
    channels: int = 1
    sample_width: int = 2
    text: str = ""
    pending: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    started: bool = False
    input_chunks: int = 0
    input_bytes: int = 0
    fed_chunks: int = 0
    finalized: bool = False


async def read_json_params(request) -> dict:
    """Read a JSON body, allowing empty alpha helper requests."""
    if not request.can_read_body:
        return {}
    try:
        params = await request.json()
    except json.JSONDecodeError:
        return {}
    return params if isinstance(params, dict) else {}


def get_session(request, sessionid: str):
    """从 app 中获取 session 实例"""
    return session_manager.get_session(sessionid)


def _clean_motion_id(value: str, field_name: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required")
    if ".." in cleaned or "/" in cleaned or "\\" in cleaned:
        raise ValueError(f"{field_name} must be a simple id")
    if not re.fullmatch(r"[0-9A-Za-z_-]+", cleaned):
        raise ValueError(f"{field_name} may only contain letters, numbers, underscores, and hyphens")
    return cleaned


def _motion_pads(params: dict) -> list[int]:
    pads = params.get("pads")
    if pads is None:
        pads = [params.get("top", 0), params.get("bottom", 10), params.get("left", 0), params.get("right", 0)]
    elif isinstance(pads, str):
        pads = pads.replace("，", ",").replace(" ", ",").split(",")
    elif isinstance(pads, (int, float)):
        pads = [pads]
    values = []
    for value in list(pads):
        if str(value).strip() == "":
            continue
        values.append(int(value))
        if len(values) == 4:
            break
    while len(values) < 4:
        values.append(0)
    return values


def _motion_box(value) -> list[int] | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        items = value.replace("，", ",").replace(" ", ",").split(",")
    else:
        items = list(value)
    values = [int(item) for item in items if str(item).strip()][:4]
    if len(values) != 4:
        raise ValueError("fixed_face_box must contain 4 numbers")
    return values


def _bool_param(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int_param(value, default: int = 1, min_value: int = 1, max_value: int = 100) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _float_param(value, default: float = 1.0, min_value: float = 0.0, max_value: float = 1000.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def _motion_play_mode(value, default: str = "forward") -> str:
    play_mode = str(value or default).strip().lower()
    return play_mode if play_mode in MOTION_PLAY_MODES else default


def _motion_tags(value) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        items = str(value or "").replace("，", ",").split(",")
    return [str(item).strip() for item in items if str(item).strip()]


def _motion_kind(params: dict) -> str:
    kind = str(params.get("kind", params.get("motion_kind", params.get("clip_kind", "speaking")))).strip().lower()
    if kind in {"idle", "silent", "silence", "rest"}:
        return "idle"
    return "speaking"


def _motion_root_for_kind(kind: str) -> str:
    return "data/idle_actions" if kind == "idle" else "data/speaking_actions"


def _avatar_motion_config_path(avatar_id: str) -> Path:
    return Path.cwd() / "data" / "avatars" / avatar_id / AVATAR_MOTION_CONFIG


def _avatar_uses_local_motion_format(avatar_id: str) -> bool:
    return _avatar_motion_config_path(avatar_id).exists()


def _avatar_motion_root(avatar_id: str, kind: str) -> Path:
    return Path.cwd() / "data" / "avatars" / avatar_id / "motions" / kind


def _legacy_motion_root(avatar_id: str, kind: str) -> Path:
    return Path.cwd() / _motion_root_for_kind(kind) / avatar_id


def _motion_clip_root(avatar_id: str, kind: str, out_root: str = "") -> Path:
    raw_root = str(out_root or "").strip()
    if raw_root:
        return _motion_output_root(kind, raw_root) / avatar_id
    if _avatar_uses_local_motion_format(avatar_id):
        return _avatar_motion_root(avatar_id, kind)
    return _legacy_motion_root(avatar_id, kind)


def _motion_create_clip_root(avatar_id: str, kind: str, out_root: str = "") -> Path:
    raw_root = str(out_root or "").strip()
    if raw_root:
        return _motion_output_root(kind, raw_root) / avatar_id
    return _avatar_motion_root(avatar_id, kind)


def _motion_output_root(kind: str, out_root: str = "") -> Path:
    default_root = _motion_root_for_kind(kind)
    raw_root = str(out_root or default_root).strip() or default_root
    allowed = {default_root, "data/idle_actions", "data/speaking_actions"}
    normalized = raw_root.replace("\\", "/").rstrip("/")
    if normalized not in allowed:
        raise ValueError("out_root is not allowed")
    root = Path(raw_root)
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve()


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _motion_allowed_source_roots() -> list[Path]:
    raw_dirs = os.getenv("MOTION_ALLOWED_SOURCE_DIRS", "")
    entries = [item.strip() for item in raw_dirs.split(os.pathsep) if item.strip()]
    entries.extend(MOTION_ALLOWED_SOURCE_DIRS)
    roots = []
    for entry in entries:
        root = Path(entry)
        if not root.is_absolute():
            root = Path.cwd() / root
        roots.append(root.resolve())
    return roots


def _ensure_motion_source_allowed(source_path: Path) -> None:
    allowed_roots = _motion_allowed_source_roots()
    if not any(_path_is_relative_to(source_path, root) for root in allowed_roots):
        allowed = ", ".join(str(root) for root in allowed_roots)
        raise PermissionError(f"source path is not allowed. allowed roots: {allowed}")


def _motion_source_path(source: str) -> Path:
    raw_source = str(source or "").strip()
    if not raw_source:
        raise ValueError("source is required")
    source_path = Path(raw_source)
    if not source_path.is_absolute():
        source_path = Path.cwd() / source_path
    source_path = source_path.resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"source not found: {source_path}")
    _ensure_motion_source_allowed(source_path)
    return source_path


def _ensure_motion_video_file(source_path: Path) -> None:
    if source_path.is_dir():
        raise ValueError("source must be a video file")
    if source_path.suffix.lower() not in MOTION_VIDEO_SUFFIXES:
        raise ValueError("only video files are supported")


def _ensure_motion_build_source(source_path: Path) -> None:
    if source_path.is_dir():
        return
    _ensure_motion_video_file(source_path)


def _safe_upload_filename(filename: str) -> str:
    raw_name = Path(str(filename or "source.mp4")).name
    stem = Path(raw_name).stem or "source"
    suffix = Path(raw_name).suffix.lower() or ".mp4"
    safe_stem = re.sub(r"[^0-9A-Za-z._-]+", "_", stem).strip("._-") or "source"
    return f"{safe_stem}{suffix}"


def _find_executable(name: str, configured: str = "") -> str:
    configured = str(configured or "").strip()
    if configured:
        path = Path(configured)
        if path.exists():
            return str(path)
    found = which(name)
    return found or name


def _default_ffmpeg_path(configured: str = "") -> str:
    return _find_executable("ffmpeg", configured or os.getenv("FFMPEG_PATH", ""))


def _default_ffprobe_path(ffmpeg_path: str = "") -> str:
    configured = os.getenv("FFPROBE_PATH", "")
    if not configured and ffmpeg_path:
        path = Path(ffmpeg_path)
        probe_name = "ffprobe.exe" if path.suffix.lower() == ".exe" else "ffprobe"
        probe = path.with_name(probe_name)
        if probe.exists():
            configured = str(probe)
    return _find_executable("ffprobe", configured)


def _parse_fps(value: str) -> float:
    if not value or value == "0/0":
        return 0.0
    if "/" in value:
        top, bottom = value.split("/", 1)
        bottom_value = float(bottom)
        return float(top) / bottom_value if bottom_value else 0.0
    return float(value)


def _probe_video_source(source_path: Path, ffprobe_path: str) -> dict:
    if source_path.is_dir():
        raise ValueError("source must be a video file")

    probe = Path(ffprobe_path)
    if probe.exists():
        command = [
            str(probe),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(source_path),
        ]
        result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
        payload = json.loads(result.stdout or "{}")
        video_stream = next((stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"), {})
        duration = float(payload.get("format", {}).get("duration") or video_stream.get("duration") or 0)
        fps = _parse_fps(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or "0/0")
        raw_frames = video_stream.get("nb_frames")
        try:
            frame_count = int(raw_frames) if raw_frames and raw_frames != "N/A" else 0
        except ValueError:
            frame_count = 0
        if not frame_count and duration and fps:
            frame_count = int(round(duration * fps))
        return {
            "source": str(source_path),
            "duration": duration,
            "fps": fps,
            "width": int(video_stream.get("width") or 0),
            "height": int(video_stream.get("height") or 0),
            "frame_count": frame_count,
            "format": payload.get("format", {}).get("format_name", ""),
        }

    import cv2

    cap = cv2.VideoCapture(str(source_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {source_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    cap.release()
    return {
        "source": str(source_path),
        "duration": frame_count / fps if fps else 0,
        "fps": fps,
        "width": width,
        "height": height,
        "frame_count": frame_count,
        "format": "opencv",
    }


def _motion_frame_at(source_path: Path, time_sec: float = 0.0):
    import cv2

    if source_path.is_dir():
        from tools.build_speaking_motion_clip import list_images, read_image

        images = list_images(source_path)
        if not images:
            raise RuntimeError(f"No images found in: {source_path}")
        return read_image(images[0])

    cap = cv2.VideoCapture(str(source_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {source_path}")
    if time_sec > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, time_sec * 1000)
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        raise RuntimeError(f"Cannot read frame from: {source_path}")
    return frame


def _box_xyxy(rect, width: int, height: int, pads: list[int] | None = None) -> dict:
    top, bottom, left, right = pads or [0, 0, 0, 0]
    x1 = max(0, int(rect[0]) - left)
    y1 = max(0, int(rect[1]) - top)
    x2 = min(width, int(rect[2]) + right)
    y2 = min(height, int(rect[3]) + bottom)
    return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}


def _preview_image_data_url(frame) -> str:
    import cv2

    if frame.ndim == 3 and frame.shape[2] == 4:
        bgr = frame[:, :, :3]
        alpha = frame[:, :, 3].astype(np.float32) / 255.0
        checker = np.indices(frame.shape[:2]).sum(axis=0) // 28 % 2
        checker = np.where(checker[..., None] == 0, 226, 196).astype(np.uint8)
        checker = np.repeat(checker, 3, axis=2)
        preview = (bgr.astype(np.float32) * alpha[..., None] + checker * (1 - alpha[..., None])).astype(np.uint8)
    else:
        preview = frame
    ok, encoded = cv2.imencode(".jpg", preview, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    if not ok:
        raise RuntimeError("failed to encode preview frame")
    return "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode("ascii")


def _get_motion_face_detector():
    global _motion_face_detector, _motion_face_detector_device

    import torch
    from avatars.wav2lip import face_detection

    device = "cuda" if torch.cuda.is_available() else "cpu"
    with _motion_face_detector_lock:
        if _motion_face_detector is None or _motion_face_detector_device != device:
            logger.info("init motion face detector device=%s", device)
            _motion_face_detector = face_detection.FaceAlignment(
                face_detection.LandmarksType._2D,
                flip_input=False,
                device=device,
            )
            _motion_face_detector_device = device
        return _motion_face_detector


def _detect_motion_preview(source: str, pads: list[int], time_sec: float, chroma_key: bool) -> dict:
    source_path = _motion_source_path(source)
    _ensure_motion_build_source(source_path)

    from tools.build_speaking_motion_clip import chroma_key_green, frame_for_detection

    frame = _motion_frame_at(source_path, time_sec)
    if chroma_key and frame.ndim == 3 and frame.shape[2] == 3:
        frame = chroma_key_green(frame)
    detect_frame = frame_for_detection(frame)
    height, width = detect_frame.shape[:2]

    detector = _get_motion_face_detector()
    rect = detector.get_detections_for_batch(np.asarray([detect_frame]))[0]
    if rect is None:
        rect = [0, 0, width, height]

    return {
        "source": str(source_path),
        "time": time_sec,
        "width": width,
        "height": height,
        "pads": pads,
        "base_box": _box_xyxy(rect, width, height),
        "padded_box": _box_xyxy(rect, width, height, pads),
        "image": _preview_image_data_url(frame),
    }


def _motion_avatar_id(params: dict, avatar_session) -> str:
    avatar_id = str(params.get("avatar_id", "")).strip()
    if not avatar_id and avatar_session is not None:
        avatar_id = str(getattr(avatar_session.opt, "avatar_id", "")).strip()
    return _clean_motion_id(avatar_id, "avatar_id")


def _list_motion_clip_metadata(avatar_id: str, out_root: str = "", kind: str = "speaking") -> list[dict]:
    if out_root:
        kind = "idle" if str(out_root).replace("\\", "/").rstrip("/").endswith("idle_actions") else "speaking"
    root = _motion_clip_root(avatar_id, kind, out_root)
    if not root.is_dir():
        return []

    motion_config = _read_avatar_motion_config(avatar_id) if not out_root else {}
    clip_overrides = _avatar_motion_clip_overrides(avatar_id, kind, motion_config) if not out_root else {}
    default_play_mode = _avatar_motion_default_play_mode(motion_config, kind)
    clips = []
    for action_dir in sorted([path for path in root.iterdir() if path.is_dir()], key=lambda path: path.name):
        metadata_path = action_dir / "metadata.json"
        metadata = {}
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig") or "{}")
            except json.JSONDecodeError:
                metadata = {}
        metadata["action_id"] = action_dir.name
        metadata.setdefault("display_name", action_dir.name)
        metadata.setdefault("avatar_id", avatar_id)
        metadata.setdefault("kind", kind)
        _apply_avatar_motion_clip_override(metadata, clip_overrides.get(action_dir.name))
        metadata["play_mode"] = _motion_play_mode(metadata.get("play_mode"), default_play_mode)
        metadata["can_reverse"] = _bool_param(metadata.get("can_reverse"), False)
        metadata["weight"] = _float_param(metadata.get("weight"), 1.0, 0.0, 1000.0)
        metadata["min_cycles"] = _int_param(metadata.get("min_cycles"), 1, 1, 100)
        metadata["max_cycles"] = _int_param(metadata.get("max_cycles"), metadata["min_cycles"], metadata["min_cycles"], 100)
        metadata["switch_at_boundary"] = _bool_param(metadata.get("switch_at_boundary"), True)
        metadata["enabled"] = _bool_param(metadata.get("enabled"), True)
        if "frame_count" not in metadata:
            full_imgs = action_dir / "full_imgs"
            metadata["frame_count"] = len(list(full_imgs.glob("*.png"))) if full_imgs.is_dir() else 0
        metadata["current"] = False
        clips.append(metadata)
    return clips


def _read_avatar_motion_config(avatar_id: str) -> dict:
    config_path = _avatar_motion_config_path(avatar_id)
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8-sig") or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _avatar_motion_default_play_mode(config: dict, kind: str) -> str:
    if not isinstance(config, dict) or not config:
        return "pingpong" if kind == "idle" else "forward"
    states = config.get("states") if isinstance(config.get("states"), dict) else {}
    state = states.get(kind) if isinstance(states.get(kind), dict) else {}
    return _motion_play_mode(state.get("default_play_mode"), "forward")


def _request_motion_default_play_mode(avatar_id: str, kind: str, out_root: str = "", create: bool = False) -> str:
    if str(out_root or "").strip():
        return "pingpong" if kind == "idle" else "forward"
    config = _read_avatar_motion_config(avatar_id)
    if config:
        return _avatar_motion_default_play_mode(config, kind)
    if create:
        return "forward"
    return "pingpong" if kind == "idle" else "forward"


def _avatar_motion_clip_overrides(avatar_id: str, kind: str, config: dict | None = None) -> dict:
    config = config if isinstance(config, dict) else _read_avatar_motion_config(avatar_id)
    states = config.get("states") if isinstance(config.get("states"), dict) else {}
    state = states.get(kind) if isinstance(states.get(kind), dict) else {}
    clips = state.get("clips") if isinstance(state.get("clips"), list) else []
    overrides = {}
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        action_id = str(clip.get("action_id", "")).strip()
        if action_id:
            overrides[action_id] = clip
    return overrides


def _apply_avatar_motion_clip_override(metadata: dict, override: dict | None) -> dict:
    if not isinstance(override, dict):
        return metadata
    for key in (
        "display_name",
        "description",
        "best_for",
        "tags",
        "play_mode",
        "can_reverse",
        "weight",
        "min_cycles",
        "max_cycles",
        "switch_at_boundary",
        "enabled",
    ):
        if key in override:
            metadata[key] = override[key]
    return metadata


def _write_avatar_motion_config(avatar_id: str, config: dict) -> None:
    config_path = _avatar_motion_config_path(avatar_id)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _ensure_avatar_motion_config(avatar_id: str) -> dict:
    config = _read_avatar_motion_config(avatar_id)
    if not config:
        config = {
            "version": 1,
            "layout": "avatar-local-motion",
            "strategy": os.getenv("LIVETALKING_MOTION_STRATEGY", "weighted_no_repeat"),
            "states": {
                "idle": {
                    "path": "motions/idle",
                    "selection": "auto",
                    "strategy": os.getenv("LIVETALKING_IDLE_MOTION_STRATEGY", "weighted_no_repeat"),
                    "default_play_mode": "forward",
                },
                "speaking": {
                    "path": "motions/speaking",
                    "selection": "auto",
                    "strategy": os.getenv("LIVETALKING_SPEAKING_MOTION_STRATEGY", "weighted_no_repeat"),
                    "default_play_mode": "forward",
                },
            },
        }
    states = config.setdefault("states", {})
    for kind in ("idle", "speaking"):
        state = states.setdefault(kind, {})
        state.setdefault("path", f"motions/{kind}")
        state.setdefault("selection", "auto")
        state.setdefault("strategy", os.getenv(
            "LIVETALKING_IDLE_MOTION_STRATEGY" if kind == "idle" else "LIVETALKING_SPEAKING_MOTION_STRATEGY",
            os.getenv("LIVETALKING_MOTION_STRATEGY", "weighted_no_repeat"),
        ))
        state.setdefault("default_play_mode", "forward")
    config.setdefault("version", 1)
    config.setdefault("layout", "avatar-local-motion")
    config.setdefault("strategy", os.getenv("LIVETALKING_MOTION_STRATEGY", "weighted_no_repeat"))
    _write_avatar_motion_config(avatar_id, config)
    return config


def _sync_avatar_motion_clip_config(avatar_id: str, kind: str, action_id: str, metadata: dict | None, remove: bool = False) -> None:
    config = _ensure_avatar_motion_config(avatar_id)
    state = config.setdefault("states", {}).setdefault(kind, {})
    clips = state.setdefault("clips", [])
    clips = [clip for clip in clips if isinstance(clip, dict) and clip.get("action_id") != action_id]
    if not remove:
        metadata = metadata or {}
        clips.append({
            "action_id": action_id,
            "display_name": metadata.get("display_name", action_id),
            "description": metadata.get("description", ""),
            "best_for": metadata.get("best_for", ""),
            "enabled": _bool_param(metadata.get("enabled"), True),
            "weight": _float_param(metadata.get("weight"), 1.0, 0.0, 1000.0),
            "play_mode": _motion_play_mode(metadata.get("play_mode"), "forward"),
            "can_reverse": _bool_param(metadata.get("can_reverse"), False),
            "min_cycles": _int_param(metadata.get("min_cycles"), 1, 1, 100),
            "max_cycles": _int_param(metadata.get("max_cycles"), metadata.get("min_cycles", 1), 1, 100),
            "switch_at_boundary": _bool_param(metadata.get("switch_at_boundary"), True),
            "tags": _motion_tags(metadata.get("tags", [])),
        })
    state["clips"] = sorted(clips, key=lambda clip: str(clip.get("action_id", "")))
    _write_avatar_motion_config(avatar_id, config)


def _clip_text(clip: dict) -> str:
    tags = clip.get("tags", [])
    if isinstance(tags, list):
        tags_text = " ".join(str(tag) for tag in tags)
    else:
        tags_text = str(tags or "")
    return " ".join([
        str(clip.get("action_id", "")),
        str(clip.get("display_name", "")),
        str(clip.get("description", "")),
        str(clip.get("best_for", "")),
        tags_text,
    ]).lower()


def _compact_motion_clip(clip: dict) -> dict:
    tags = clip.get("tags", [])
    if not isinstance(tags, list):
        tags = _motion_tags(tags)
    return {
        "action_id": str(clip.get("action_id", "")).strip(),
        "display_name": str(clip.get("display_name", "")).strip(),
        "description": str(clip.get("description", "")).strip(),
        "best_for": str(clip.get("best_for", "")).strip(),
        "tags": tags,
        "frame_count": int(clip.get("frame_count") or 0),
        "fps": float(clip.get("fps") or 0),
        "current": bool(clip.get("current")),
    }


def _fallback_motion_action(clips: list[dict]) -> str:
    if not clips:
        return ""
    for clip in clips:
        if clip.get("current") and clip.get("action_id"):
            return str(clip["action_id"])
    for keyword in ("explain", "讲解", "介绍", "普通"):
        matched = next((clip for clip in clips if keyword in _clip_text(clip)), None)
        if matched and matched.get("action_id"):
            return str(matched["action_id"])
    return str(clips[0].get("action_id", ""))


def _set_motion_task(task_id: str, **updates) -> None:
    with MOTION_TASK_LOCK:
        current = MOTION_TASKS.setdefault(task_id, {})
        current.update(updates)
        current["updated_at"] = datetime.now().isoformat(timespec="seconds")
        while len(MOTION_TASKS) > MOTION_TASK_MAX_ITEMS:
            oldest_id = next(iter(MOTION_TASKS))
            if oldest_id == task_id and len(MOTION_TASKS) > 1:
                oldest_id = next(item for item in MOTION_TASKS if item != task_id)
            MOTION_TASKS.pop(oldest_id, None)


def _get_motion_task(task_id: str) -> dict | None:
    with MOTION_TASK_LOCK:
        task = MOTION_TASKS.get(task_id)
        return dict(task) if task else None


def _motion_task_step(task_id: str, step: str, message: str) -> None:
    _set_motion_task(task_id, status="running", step=step, message=message)


def _render_lip_backend(value: str) -> str:
    backend = str(value or "wav2lip256").strip().lower()
    if backend not in RENDER_LIP_BACKENDS:
        raise ValueError(f"lip_backend must be one of: {', '.join(sorted(RENDER_LIP_BACKENDS))}")
    return backend


def _render_enhance_mode(value: str) -> str:
    mode = str(value or "none").strip().lower()
    if mode in {"animevideov3", "anime-v3", "realesr_animevideov3"}:
        mode = "realesr-animevideov3"
    if mode in {"x4plus-anime", "realesrgan_x4plus_anime"}:
        mode = "realesrgan-x4plus-anime"
    if mode in {"clear-reality-x4", "clear_reality"}:
        mode = "clear_reality_x4"
    if mode not in RENDER_ENHANCE_MODES:
        raise ValueError(f"enhance_mode must be one of: {', '.join(sorted(RENDER_ENHANCE_MODES))}")
    return mode


def _ensure_render_combo(lip_backend: str, enhance_mode: str) -> None:
    if lip_backend == "wav2lip256" and enhance_mode == "clear_reality_x4":
        raise ValueError("clear_reality_x4 只用于 ONNX base 实验后端")
    if lip_backend != "wav2lip256" and enhance_mode in {"realesr-animevideov3", "realesrgan-x4plus-anime"}:
        raise ValueError("ONNX base 暂时只支持 none 或 clear_reality_x4 增强")


def _default_realesrgan_path(configured: str = "") -> str:
    return _find_executable("realesrgan-ncnn-vulkan", configured or os.getenv("REALESRGAN_PATH", ""))


def _render_output_root() -> Path:
    root = Path(os.getenv("RENDER_OUTPUT_DIR") or "tmp/render_outputs")
    if not root.is_absolute():
        root = Path.cwd() / root
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _render_task_output(task_id: str, suffix: str = ".mp4") -> Path:
    task_dir = _render_output_root() / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir / f"render{suffix}"


def _render_clip_dir(avatar_id: str, action_id: str, kind: str = "speaking") -> Path:
    clip_root = _motion_clip_root(avatar_id, kind)
    clip_dir = (clip_root / action_id).resolve()
    root = clip_root.resolve()
    if root not in clip_dir.parents or not clip_dir.is_dir():
        raise FileNotFoundError(f"motion clip not found: {action_id}")
    return clip_dir


def _render_clip_video_source(clip_dir: Path) -> Path:
    metadata_path = clip_dir / "metadata.json"
    metadata = {}
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig") or "{}")
        except json.JSONDecodeError:
            metadata = {}
    for key in ("cut_video", "source_clip"):
        value = str(metadata.get(key, "")).strip()
        if value:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            if candidate.is_file():
                return candidate.resolve()
    candidate = clip_dir / "source_clip.mp4"
    if candidate.is_file():
        return candidate.resolve()
    raise FileNotFoundError(f"source_clip.mp4 not found in motion clip: {clip_dir}")


def _render_task_output_url(task_id: str) -> str:
    return f"/render/output/{task_id}"


def _render_library_items(limit: int = 100) -> list[dict]:
    root = _render_output_root()
    items = []
    for output_path in root.glob("*/render.mp4"):
        if not output_path.is_file():
            continue
        task_id = output_path.parent.name
        try:
            stat = output_path.stat()
        except OSError:
            continue
        task = _get_motion_task(task_id) or {}
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        items.append(
            {
                "task_id": task_id,
                "name": task_id,
                "output": str(output_path.resolve()),
                "output_url": _render_task_output_url(task_id),
                "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(timespec="seconds"),
                "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "size_bytes": stat.st_size,
                "lip_backend": result.get("lip_backend") or task.get("lip_backend") or "",
                "enhance_mode": result.get("enhance_mode") or task.get("enhance_mode") or "",
                "avatar_id": result.get("avatar_id") or "",
                "action_id": result.get("action_id") or "",
                "elapsed_seconds": result.get("elapsed_seconds") or None,
            }
        )
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    return items[:max(1, limit)]


def _render_task_output_path(task_id: str) -> Path:
    root = _render_output_root()
    task = _get_motion_task(task_id)
    result = task.get("result") if isinstance(task, dict) and isinstance(task.get("result"), dict) else {}
    output = str(result.get("output", "")).strip()
    output_path = Path(output).resolve() if output else (root / task_id / "render.mp4").resolve()
    if not _path_is_relative_to(output_path, root):
        raise PermissionError("render output path is not allowed")
    if not output_path.is_file():
        raise FileNotFoundError(f"render output not found: {output_path}")
    return output_path


def _run_subprocess_logged(command: list[str], cwd: Path | None = None, env: dict | None = None) -> None:
    logger.info("run render command: %s", " ".join(str(item) for item in command))
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        tail = "\n".join((result.stderr or result.stdout or "").splitlines()[-30:])
        raise RuntimeError(tail or f"command failed with code {result.returncode}")


def _tts_server_url(params: dict) -> str:
    value = str(params.get("tts_server_url") or os.getenv("ROBOT_TTS_URL") or "http://127.0.0.1:8036").strip()
    return value.rstrip("/")


def _synthesize_render_audio(params: dict, output_path: Path) -> Path:
    audio_path = str(params.get("audio_path", "")).strip()
    if audio_path:
        path = Path(audio_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"audio_path not found: {path}")
        return path

    text = str(params.get("text", "")).strip()
    if not text:
        raise ValueError("text or audio_path is required")
    request_body = {
        "text": text,
        "voice_id": int(params.get("voice_id", 0) or 0),
        "prompts": str(params.get("prompts", "请自然清晰地朗读。")),
        "mode": str(params.get("mode", "instruct2")),
        "output_path": str(output_path),
    }
    raw = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{_tts_server_url(params)}/tts",
        data=raw,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=float(os.getenv("RENDER_TTS_TIMEOUT", "120") or 120)) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("success") is not True:
        raise RuntimeError(payload.get("message") or payload.get("error") or "TTS failed")
    path = Path(payload.get("audio_path") or output_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"TTS audio not found: {path}")
    return path


def _render_wav2lip256_command(params: dict, clip_dir: Path, audio_path: Path, output_path: Path, enhance_mode: str) -> list[str]:
    python_path = str(params.get("python_path") or sys.executable)
    checkpoint = str(params.get("checkpoint") or os.getenv("WAV2LIP_CHECKPOINT") or Path.cwd() / "models" / "wav2lip.pth")
    ffmpeg = _default_ffmpeg_path(params.get("ffmpeg_path", ""))
    fps = float(params.get("fps", 30) or 30)
    batch_size = int(params.get("batch_size") or os.getenv("LIVETALKING_BATCH_SIZE") or 1)
    max_frames = int(params.get("max_frames", 0) or 0)
    command = [
        python_path,
        str(Path.cwd() / "tools" / "render_wav2lip_motion_sample.py"),
        "--clip-dir",
        str(clip_dir),
        "--audio",
        str(audio_path),
        "--checkpoint",
        checkpoint,
        "--output",
        str(output_path),
        "--fps",
        str(fps),
        "--batch-size",
        str(batch_size),
        "--max-frames",
        str(max_frames),
        "--ffmpeg",
        ffmpeg,
        "--crf",
        str(params.get("crf", "16")),
        "--preset",
        str(params.get("preset", "medium")),
    ]
    if enhance_mode != "none":
        model = "realesr-animevideov3"
        scale = "2"
        if enhance_mode == "realesrgan-x4plus-anime":
            model = "realesrgan-x4plus-anime"
            scale = "4"
        command.extend([
            "--enhance-roi",
            "lower-face",
            "--realesrgan",
            _default_realesrgan_path(params.get("realesrgan_path", "")),
            "--realesrgan-model",
            model,
            "--realesrgan-scale",
            str(params.get("realesrgan_scale") or scale),
            "--realesrgan-tile",
            str(params.get("realesrgan_tile", "0")),
            "--realesrgan-jobs",
            str(params.get("realesrgan_jobs", "2:2:2")),
            "--enhance-format",
            str(params.get("enhance_format", "png")),
            "--enhance-roi-y-start-ratio",
            str(params.get("enhance_roi_y_start_ratio", "0.58")),
            "--enhance-roi-expand",
            str(params.get("enhance_roi_expand", "0 4 19 23")),
            "--enhance-roi-feather",
            str(params.get("enhance_roi_feather", "18")),
            "--enhance-io-workers",
            str(params.get("enhance_io_workers", "4")),
        ])
        model_dir = str(params.get("realesrgan_model_dir", "")).strip()
        if model_dir:
            command.extend(["--realesrgan-model-dir", model_dir])
    else:
        command.extend(["--enhance-roi", "none"])
    return command


def _render_onnx_base_command(params: dict, clip_dir: Path, audio_path: Path, output_path: Path, enhance_mode: str) -> tuple[list[str], Path]:
    raw_onnx_root = str(params.get("onnx_root") or os.getenv("WAV2LIP_ONNX_HQ_ROOT") or "").strip()
    if not raw_onnx_root:
        raise ValueError("WAV2LIP_ONNX_HQ_ROOT is required when lip_backend=onnx_base")
    onnx_root = Path(raw_onnx_root).resolve()
    script = onnx_root / "inference_onnxModel.py"
    if not script.is_file():
        raise FileNotFoundError(f"ONNX Wav2Lip script not found: {script}")
    checkpoint = Path(str(params.get("onnx_checkpoint") or onnx_root / "checkpoints" / "wav2lip.onnx")).resolve()
    if not checkpoint.is_file():
        raise FileNotFoundError(f"ONNX checkpoint not found: {checkpoint}")
    default_onnx_python = onnx_root.parent / ".venv" / "Scripts" / "python.exe"
    python_path = str(
        params.get("onnx_python_path")
        or os.getenv("WAV2LIP_ONNX_PYTHON")
        or (default_onnx_python if default_onnx_python.exists() else sys.executable)
    )
    command = [
        python_path,
        str(script),
        "--checkpoint_path",
        str(checkpoint),
        "--face",
        str(_render_clip_video_source(clip_dir)),
        "--audio",
        str(audio_path),
        "--outfile",
        str(output_path),
        "--auto_roi",
        "--fps",
        str(float(params.get("fps", 30) or 30)),
        "--face_mode",
        str(int(params.get("onnx_face_mode", 0) or 0)),
        "--pads",
        str(int(params.get("onnx_pads", 4) or 4)),
        "--pingpong",
    ]
    max_frames = int(params.get("max_frames", 0) or 0)
    if max_frames > 0:
        command.extend(["--cut_out", str(max_frames)])
    if _bool_param(params.get("onnx_face_mask"), enhance_mode != "none"):
        command.append("--face_mask")
    if _bool_param(params.get("onnx_ellipse_mask"), False):
        command.append("--ellipse_mask")
    if enhance_mode == "clear_reality_x4":
        command.append("--frame_enhancer")
    return command, onnx_root


def _build_render_video_data(task_id: str, params: dict) -> dict:
    started = time.perf_counter()
    sessionid = str(params.get("sessionid", "")).strip() or session_manager.default_alpha_sessionid
    avatar_session = params.get("_avatar_session")
    avatar_id = str(params.get("avatar_id", "")).strip()
    if not avatar_id and avatar_session is not None:
        avatar_id = str(getattr(avatar_session.opt, "avatar_id", "")).strip()
    if not avatar_id:
        avatar_id = str(os.getenv("AVATAR_ID", "")).strip()
    avatar_id = _clean_motion_id(avatar_id, "avatar_id")

    action_id = str(params.get("action_id", "")).strip()
    if action_id in {"", "auto"}:
        clips = _list_motion_clip_metadata(avatar_id, _motion_root_for_kind("speaking"), "speaking")
        action_id = _fallback_motion_action(clips)
    action_id = _clean_motion_id(action_id, "action_id")
    clip_dir = _render_clip_dir(avatar_id, action_id, "speaking")
    lip_backend = _render_lip_backend(params.get("lip_backend", "wav2lip256"))
    enhance_mode = _render_enhance_mode(params.get("enhance_mode", "none"))
    _ensure_render_combo(lip_backend, enhance_mode)

    task_dir = _render_output_root() / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    audio_path = task_dir / "tts.wav"
    _motion_task_step(task_id, "tts", "正在生成音频")
    audio_path = _synthesize_render_audio(params, audio_path)

    output_path = _render_task_output(task_id)
    _motion_task_step(task_id, "render", "正在生成口型视频")
    if lip_backend == "onnx_base":
        command, cwd = _render_onnx_base_command(params, clip_dir, audio_path, output_path, enhance_mode)
    else:
        command = _render_wav2lip256_command(params, clip_dir, audio_path, output_path, enhance_mode)
        cwd = Path.cwd()
    _run_subprocess_logged(command, cwd=cwd)

    elapsed = round(time.perf_counter() - started, 2)
    return {
        "task_id": task_id,
        "sessionid": sessionid,
        "avatar_id": avatar_id,
        "action_id": action_id,
        "clip_dir": str(clip_dir),
        "audio": str(audio_path),
        "output": str(output_path),
        "output_url": _render_task_output_url(task_id),
        "lip_backend": lip_backend,
        "enhance_mode": enhance_mode,
        "elapsed_seconds": elapsed,
    }


def _split_motion_text(text: str, max_segments: int = 8) -> list[str]:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return []
    parts = [item.strip() for item in re.findall(r"[^。！？!?；;\n]+[。！？!?；;]?", cleaned) if item.strip()]
    if not parts:
        parts = [cleaned]

    segments = []
    for part in parts:
        if len(part) <= 70:
            segments.append(part)
            continue
        for index in range(0, len(part), 70):
            segments.append(part[index:index + 70])

    if len(segments) <= max_segments:
        return segments
    merged = []
    chunk_size = max(1, int(np.ceil(len(segments) / max_segments)))
    for index in range(0, len(segments), chunk_size):
        merged.append("".join(segments[index:index + chunk_size]))
    return merged[:max_segments]


def _match_motion_action(segment_text: str, clips: list[dict]) -> str:
    if not clips:
        return ""
    rules = [
        (("重点", "注意", "关键", "常考", "强调", "一定要", "易错"), ("emphasize", "key", "warning", "重点", "强调", "注意")),
        (("问题", "想一想", "思考", "回答", "为什么", "请大家", "提问"), ("question", "think", "wait", "提问", "思考", "等待")),
        (("对比", "比较", "区别", "不同", "相同", "左边", "右边"), ("compare", "对比", "比较")),
        (("第一", "第二", "第三", "步骤", "要点", "列举", "分别", "一是", "二是"), ("list", "count", "列举", "要点")),
        (("这里", "看这里", "图中", "左", "右", "上", "下", "指向"), ("point", "指向", "手指")),
        (("总结", "回顾", "最后", "归纳", "小结"), ("summary", "总结", "收束")),
        (("很好", "不错", "答对", "表扬", "鼓励"), ("praise", "鼓励", "表扬", "肯定")),
    ]
    for text_keywords, clip_keywords in rules:
        if any(keyword in segment_text for keyword in text_keywords):
            for clip in clips:
                clip_text = _clip_text(clip)
                if any(keyword in clip_text for keyword in clip_keywords):
                    return str(clip.get("action_id", ""))
    return _fallback_motion_action(clips)


def _heuristic_motion_plan(text: str, clips: list[dict], max_segments: int = 8) -> list[dict]:
    return [
        {
            "text": segment,
            "action_id": _match_motion_action(segment, clips),
            "reason": "按关键词规则选择",
        }
        for segment in _split_motion_text(text, max_segments)
    ]


def _extract_json_payload(content: str):
    raw = str(content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.S | re.I)
    if fenced:
        raw = fenced.group(1).strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start >= 0 and end > start:
        return json.loads(raw[start:end + 1])
    payload = json.loads(raw)
    if isinstance(payload, dict):
        return payload.get("plan", [])
    return payload


def _normalize_motion_plan(plan_items, text: str, clips: list[dict], max_segments: int = 8) -> list[dict]:
    valid_actions = {str(clip.get("action_id", "")): clip for clip in clips if clip.get("action_id")}
    fallback_action = _fallback_motion_action(clips)
    if not isinstance(plan_items, list):
        plan_items = []
    normalized = []
    for item in plan_items[:max_segments]:
        if not isinstance(item, dict):
            continue
        segment_text = str(item.get("text", "")).strip()
        if not segment_text:
            continue
        action_id = str(item.get("action_id", "")).strip()
        if action_id not in valid_actions:
            action_id = _match_motion_action(segment_text, clips) or fallback_action
        clip = valid_actions.get(action_id, {})
        normalized.append({
            "text": segment_text,
            "action_id": action_id,
            "display_name": clip.get("display_name", action_id),
            "reason": str(item.get("reason", "")).strip(),
        })
    if normalized:
        return normalized

    return [
        {
            **item,
            "display_name": valid_actions.get(item.get("action_id"), {}).get("display_name", item.get("action_id", "")),
        }
        for item in _heuristic_motion_plan(text, clips, max_segments)
    ]


def _call_motion_plan_llm(text: str, clips: list[dict], max_segments: int = 8) -> list[dict]:
    api_key = os.getenv("MOTION_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("MOTION_LLM_API_KEY is not configured")

    base_url = (os.getenv("MOTION_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "").rstrip("/")
    model = os.getenv("MOTION_LLM_MODEL") or os.getenv("OPENAI_MODEL") or ""
    if not base_url:
        raise RuntimeError("MOTION_LLM_BASE_URL is not configured")
    if not model:
        raise RuntimeError("MOTION_LLM_MODEL is not configured")
    timeout = float(os.getenv("MOTION_LLM_TIMEOUT", "30") or 30)
    clip_payload = [_compact_motion_clip(clip) for clip in clips if clip.get("action_id")]
    system_prompt = (
        "你是课堂数字人的动作编排助手。你只根据讲课文本选择动作，不处理 PPT。"
        "你必须把讲课文本切成适合朗读的小段，每段选择一个已有 action_id。"
        "只能使用动作列表中出现过的 action_id，不能创造新动作。"
        "输出必须是 JSON 数组，不要输出解释文字。"
        "数组元素格式为 {\"text\":\"这一小段讲课文本\",\"action_id\":\"已有动作 id\",\"reason\":\"简短原因\"}。"
    )
    user_prompt = json.dumps(
        {
            "max_segments": max_segments,
            "motion_clips": clip_payload,
            "lecture_text": text,
        },
        ensure_ascii=False,
    )
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
    return _extract_json_payload(content)


def _collect_session_params(params: dict) -> dict:
    """Keep only avatar-construction fields when alpha/speak creates a session."""
    nested = params.get("session")
    if isinstance(nested, dict):
        return nested

    session_params = {}
    for key in ("avatar", "refaudio", "reftext", "custom_config"):
        if key in params:
            session_params[key] = params[key]
    return session_params


def _collect_tts_params(params: dict) -> dict:
    tts_params = params.get("tts")
    if not isinstance(tts_params, dict):
        tts_params = {}
    else:
        tts_params = dict(tts_params)

    for key in ("voice_id", "prompts", "mode", "ref_file", "ref_text"):
        if key in params:
            tts_params[key] = params[key]
    return tts_params


# ─── 路由处理函数 ──────────────────────────────────────────────────────────

async def human(request):
    """文本输入（echo/chat 模式），支持 voice/emotion 参数"""
    try:
        params: dict = await request.json()

        sessionid: str = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")

        if params.get('interrupt'):
            avatar_session.flush_talk()

        datainfo = {}
        if params.get('tts'):  # tts 参数透传（voice, emotion 等）
            datainfo['tts'] = params.get('tts')

        if params['type'] == 'echo':
            avatar_session.put_msg_txt(params['text'], datainfo)
        elif params['type'] == 'chat':
            llm_response = request.app.get("llm_response")
            if llm_response:
                asyncio.get_event_loop().run_in_executor(
                    None, llm_response, params['text'], avatar_session, datainfo
                )

        return json_ok()
    except Exception as e:
        logger.exception('human route exception:')
        return json_error(str(e))


async def interrupt_talk(request):
    """打断当前说话"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        avatar_session.flush_talk()
        return json_ok()
    except Exception as e:
        logger.exception('interrupt_talk exception:')
        return json_error(str(e))


async def humanaudio(request):
    """上传音频文件"""
    try:
        form = await request.post()
        sessionid = str(form.get('sessionid', ''))
        fileobj = form["file"]
        filebytes = fileobj.file.read()

        datainfo = {}

        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        avatar_session.put_audio_file(filebytes, datainfo)
        return json_ok()
    except Exception as e:
        logger.exception('humanaudio exception:')
        return json_error(str(e))


async def set_audiotype(request):
    """设置自定义状态（动作编排）"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        avatar_session.set_custom_state(params['audiotype'])
        return json_ok()
    except Exception as e:
        logger.exception('set_audiotype exception:')
        return json_error(str(e))


async def record(request):
    """录制控制"""
    try:
        params = await request.json()
        sessionid = params.get('sessionid', '')
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        if params['type'] == 'start_record':
            avatar_session.start_recording()
        elif params['type'] == 'end_record':
            avatar_session.stop_recording()
        return json_ok()
    except Exception as e:
        logger.exception('record exception:')
        return json_error(str(e))


async def is_speaking(request):
    """查询是否正在说话"""
    params = await request.json()
    sessionid = params.get('sessionid', '')
    avatar_session = get_session(request, sessionid)
    if avatar_session is None:
        return json_error("session not found")
    return json_ok(data=avatar_session.is_speaking())

async def admin_config(request):
    """Admin: 获取全局配置参数"""
    try:
        opt = request.app.get("opt")
        if opt:
            return json_ok(data={"config": vars(opt)})
        return json_error("Config not found")
    except Exception as e:
        logger.exception('admin_config exception:')
        return json_error(str(e))


async def admin_sessions(request):
    """Admin: 获取活跃的会话及其配置"""
    try:
        sessions_info = []
        for sid, avatar_session in session_manager.sessions.items():
            if avatar_session:
                s_opt = getattr(avatar_session, 'opt', None)
                s_data = {
                    "sessionid": sid,
                    "speaking": avatar_session.is_speaking() if hasattr(avatar_session, 'is_speaking') else False,
                    "recording": getattr(avatar_session, 'recording', False),
                }
                if s_opt:
                    s_data.update({
                        "model": getattr(s_opt, "model", ""),
                        "avatar_id": getattr(s_opt, "avatar_id", ""),
                        "REF_FILE": getattr(s_opt, "REF_FILE", ""),
                        "transport": getattr(s_opt, "transport", ""),
                        "batch_size": getattr(s_opt, "batch_size", 0),
                        "customopt": getattr(s_opt, "customopt", []),
                    })
                sessions_info.append(s_data)
        return json_ok(data={"sessions": sessions_info})
    except Exception as e:
        logger.exception('admin_sessions exception:')
        return json_error(str(e))

async def close_session(request):
    """显式关闭 session，供 alpha overlay 重连/退出时清理后台 render 线程。"""
    try:
        params = await request.json()
        sessionid = str(params.get('sessionid', ''))
        if not sessionid:
            return json_error("sessionid is required")
        session_manager.remove_session(sessionid)
        return json_ok()
    except Exception as e:
        logger.exception('close_session exception:')
        return json_error(str(e))


async def alpha_session(request):
    """Create a low-latency alpha-overlay session without WebRTC tracks."""
    try:
        params = await read_json_params(request)
        requested_sessionid = str(params.get("sessionid", "")).strip() or None
        session_params = _collect_session_params(params)
        reuse = params.get("reuse", True)
        logger.info(
            "alpha/session request reuse=%s requested_sessionid=%s session_param_keys=%s",
            reuse,
            requested_sessionid or "",
            sorted(session_params.keys()),
        )
        if reuse:
            sessionid = await session_manager.get_or_create_alpha_session(session_params, requested_sessionid)
        else:
            sessionid = await session_manager.create_alpha_session(session_params, requested_sessionid)
            session_manager.default_alpha_sessionid = sessionid
        logger.info("alpha/session ready sessionid=%s", sessionid)
        return json_ok(data={"sessionid": sessionid})
    except Exception as e:
        logger.exception('alpha_session exception:')
        return json_error(str(e))


async def alpha_speak(request):
    """Create/reuse the alpha desktop session and send text to its TTS pipeline."""
    try:
        params = await read_json_params(request)
        text = str(params.get("text", ""))
        input_type = params.get("type", "echo")
        if not text:
            return json_error("text is required")

        requested_sessionid = str(params.get("sessionid", "")).strip() or None
        session_params = _collect_session_params(params)
        tts_params = _collect_tts_params(params)
        logger.info(
            "alpha/speak request type=%s interrupt=%s requested_sessionid=%s text_len=%d session_param_keys=%s tts_keys=%s",
            input_type,
            params.get("interrupt", True),
            requested_sessionid or "",
            len(text),
            sorted(session_params.keys()),
            sorted(tts_params.keys()),
        )
        sessionid = await session_manager.get_or_create_alpha_session(session_params, requested_sessionid)
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")

        if params.get("interrupt", True):
            avatar_session.flush_talk()

        datainfo = {}
        if tts_params:
            datainfo["tts"] = tts_params

        if input_type == "echo":
            avatar_session.put_msg_txt(text, datainfo)
        elif input_type == "chat":
            llm_response = request.app.get("llm_response")
            if llm_response:
                asyncio.get_event_loop().run_in_executor(
                    None, llm_response, text, avatar_session, datainfo
                )
        else:
            return json_error("type must be echo or chat")

        logger.info("alpha/speak accepted sessionid=%s type=%s text_len=%d", sessionid, input_type, len(text))
        return json_ok(data={"sessionid": sessionid})
    except Exception as e:
        logger.exception('alpha_speak exception:')
        return json_error(str(e))


async def alpha_close(request):
    """Close the default or provided alpha desktop session."""
    try:
        params = await read_json_params(request)
        sessionid = str(params.get("sessionid", "")).strip() or session_manager.default_alpha_sessionid
        if not sessionid:
            return json_error("sessionid is required")
        logger.info("alpha/close request sessionid=%s", sessionid)
        session_manager.remove_session(sessionid)
        return json_ok(data={"sessionid": sessionid})
    except Exception as e:
        logger.exception('alpha_close exception:')
        return json_error(str(e))


async def alpha_tuning(request):
    """Read or update runtime visual tuning for the alpha avatar session."""
    try:
        if request.method == "POST":
            params = await read_json_params(request)
        else:
            params = dict(request.rel_url.query)

        requested_sessionid = str(params.get("sessionid", "")).strip() or None
        sessionid = requested_sessionid or session_manager.default_alpha_sessionid
        if not sessionid:
            sessionid = await session_manager.get_or_create_alpha_session({}, None)
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")

        pads = params.get("pads")
        if pads is None:
            pads = [params.get("top", 0), params.get("bottom", 0), params.get("left", 0), params.get("right", 0)]
        if request.method == "POST" and hasattr(avatar_session, "set_runtime_pads"):
            avatar_session.set_runtime_pads(pads)

        if not hasattr(avatar_session, "get_runtime_config"):
            return json_error("current avatar does not support tuning")
        data = avatar_session.get_runtime_config()
        data["sessionid"] = sessionid
        return json_ok(data=data)
    except Exception as e:
        logger.exception('alpha_tuning exception:')
        return json_error(str(e))


async def motion_clips(request):
    """List speaking or idle motion clips loaded by the current avatar session."""
    try:
        params = dict(request.rel_url.query)
        kind = _motion_kind(params)
        sessionid = str(params.get("sessionid", "")).strip() or session_manager.default_alpha_sessionid
        avatar_session = get_session(request, sessionid) if sessionid else None
        if avatar_session is None:
            avatar_id = _clean_motion_id(params.get("avatar_id", os.getenv("AVATAR_ID", "")), "avatar_id")
            out_root = str(_motion_output_root(kind, params["out_root"])) if params.get("out_root") else ""
            return json_ok(data={
                "sessionid": sessionid,
                "kind": kind,
                "clips": _list_motion_clip_metadata(avatar_id, out_root, kind),
            })
        list_method = "list_idle_motions" if kind == "idle" else "list_speaking_motions"
        if not hasattr(avatar_session, list_method):
            return json_error(f"current avatar does not support {kind} motion clips")
        reload_method = "reload_idle_motions" if kind == "idle" else "reload_speaking_motions"
        reload_requested = str(params.get("reload", "")).strip().lower() in {"1", "true", "yes", "on"}
        if reload_requested and hasattr(avatar_session, reload_method):
            clips = getattr(avatar_session, reload_method)()
        else:
            clips = getattr(avatar_session, list_method)()
        return json_ok(data={
            "sessionid": sessionid,
            "kind": kind,
            "clips": clips,
        })
    except Exception as e:
        logger.exception('motion_clips exception:')
        return json_error(str(e))


async def motion_plan(request):
    """Plan speaking motion clips from lecture text."""
    try:
        params = await read_json_params(request)
        text = str(params.get("text", "")).strip()
        if not text:
            return json_error("text is required")
        max_segments = max(1, min(20, int(params.get("max_segments", 8) or 8)))
        sessionid = str(params.get("sessionid", "")).strip() or session_manager.default_alpha_sessionid
        avatar_session = get_session(request, sessionid) if sessionid else None
        avatar_id = _motion_avatar_id(params, avatar_session)

        if avatar_session is not None and hasattr(avatar_session, "list_speaking_motions"):
            clips = avatar_session.list_speaking_motions()
        else:
            clips = _list_motion_clip_metadata(avatar_id, _motion_root_for_kind("speaking"), "speaking")
        clips = [_compact_motion_clip(clip) for clip in clips if clip.get("action_id")]
        if not clips:
            return json_error("no speaking motion clips found")

        use_llm = str(params.get("use_llm", "1")).strip().lower() not in {"0", "false", "no"}
        provider = "heuristic"
        llm_error = ""
        raw_plan = None
        if use_llm:
            try:
                raw_plan = await asyncio.to_thread(_call_motion_plan_llm, text, clips, max_segments)
                provider = "llm"
            except Exception as exc:
                llm_error = str(exc)
                logger.warning("motion plan llm fallback: %s", exc)

        if raw_plan is None:
            raw_plan = _heuristic_motion_plan(text, clips, max_segments)

        plan = _normalize_motion_plan(raw_plan, text, clips, max_segments)
        return json_ok(data={
            "sessionid": sessionid,
            "avatar_id": avatar_id,
            "provider": provider,
            "llm_error": llm_error,
            "plan": plan,
            "clips": clips,
        })
    except Exception as e:
        logger.exception('motion_plan exception:')
        return json_error(str(e))


async def motion_source_probe(request):
    """Probe a local source video so the web page can show duration and timeline data."""
    try:
        params = await read_json_params(request)
        source_path = _motion_source_path(params.get("source", ""))
        _ensure_motion_video_file(source_path)
        ffprobe_path = str(params.get("ffprobe_path", "")).strip() or _default_ffprobe_path(params.get("ffmpeg_path", ""))
        data = await asyncio.to_thread(_probe_video_source, source_path, ffprobe_path)
        data["video_url"] = f"/motion/source/video?{urlencode({'source': str(source_path)})}"
        return json_ok(data=data)
    except Exception as e:
        logger.exception('motion_source_probe exception:')
        return json_error(str(e))


async def motion_source_video(request):
    """Serve a local source video for the motion clip maker page."""
    try:
        source_path = _motion_source_path(request.rel_url.query.get("source", ""))
        _ensure_motion_video_file(source_path)
        headers = {"Cache-Control": "no-store"}
        return web.FileResponse(path=source_path, headers=headers)
    except Exception as e:
        logger.exception('motion_source_video exception:')
        return json_error(str(e))


async def motion_source_upload(request):
    """Upload a source video from the browser and return a server-side path."""
    try:
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != "file":
            return json_error("file is required")

        filename = _safe_upload_filename(field.filename or "source.mp4")
        suffix = Path(filename).suffix.lower()
        if suffix not in MOTION_VIDEO_SUFFIXES:
            return json_error("only video files are supported")

        upload_dir = Path("tmp") / "uploaded_sources"
        upload_dir.mkdir(parents=True, exist_ok=True)
        target_path = upload_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{filename}"

        size = 0
        with target_path.open("wb") as file:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                if size > MOTION_UPLOAD_MAX_BYTES:
                    target_path.unlink(missing_ok=True)
                    return json_error(f"uploaded file is too large. max bytes: {MOTION_UPLOAD_MAX_BYTES}")
                file.write(chunk)

        if size <= 0:
            target_path.unlink(missing_ok=True)
            return json_error("uploaded file is empty")

        return json_ok(data={
            "source": str(target_path.resolve()),
            "filename": filename,
            "size": size,
        })
    except Exception as e:
        logger.exception('motion_source_upload exception:')
        return json_error(str(e))


async def motion_source_detect(request):
    """Detect the first-frame face box and show how generation pads change it."""
    try:
        params = await read_json_params(request)
        source = str(params.get("source", "")).strip()
        if not source:
            return json_error("source is required")
        pads = _motion_pads(params)
        time_sec = float(params.get("time", 0) or 0)
        data = await asyncio.to_thread(
            _detect_motion_preview,
            source,
            pads,
            time_sec,
            _bool_param(params.get("chroma_key"), False),
        )
        return json_ok(data=data)
    except Exception as e:
        logger.exception('motion_source_detect exception:')
        return json_error(str(e))


async def motion_select(request):
    """Select the motion clip used while speaking or idle."""
    try:
        params = await read_json_params(request)
        kind = _motion_kind(params)
        sessionid = str(params.get("sessionid", "")).strip() or session_manager.default_alpha_sessionid
        if not sessionid:
            return json_error("sessionid is required")
        avatar_session = get_session(request, sessionid)
        if avatar_session is None:
            return json_error("session not found")
        select_method = "set_idle_motion" if kind == "idle" else "set_speaking_motion"
        if not hasattr(avatar_session, select_method):
            return json_error(f"current avatar does not support {kind} motion clips")
        action_id = params.get("action_id", params.get("motion_id", ""))
        selected = getattr(avatar_session, select_method)(action_id)
        return json_ok(data={
            "sessionid": sessionid,
            "kind": kind,
            "selected": selected,
        })
    except Exception as e:
        logger.exception('motion_select exception:')
        return json_error(str(e))


async def motion_update_clip(request):
    """Update editable metadata for a speaking or idle motion clip."""
    try:
        params = await read_json_params(request)
        kind = _motion_kind(params)
        sessionid = str(params.get("sessionid", "")).strip() or session_manager.default_alpha_sessionid
        avatar_session = get_session(request, sessionid) if sessionid else None
        avatar_id = _motion_avatar_id(params, avatar_session)
        action_id = _clean_motion_id(params.get("action_id", ""), "action_id")
        next_action_id = _clean_motion_id(params.get("next_action_id", action_id), "next_action_id")
        explicit_out_root = str(params.get("out_root", "")).strip()
        root = _motion_clip_root(avatar_id, kind, explicit_out_root)
        source_dir = root / action_id
        if not source_dir.is_dir():
            return json_error(f"motion clip not found: {action_id}")
        default_play_mode = _request_motion_default_play_mode(avatar_id, kind, explicit_out_root)

        target_dir = root / next_action_id
        if next_action_id != action_id:
            if target_dir.exists():
                return json_error(f"target action_id already exists: {next_action_id}")
            source_dir.rename(target_dir)
        else:
            target_dir = source_dir

        metadata_path = target_dir / "metadata.json"
        metadata = {}
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig") or "{}")

        metadata["avatar_id"] = avatar_id
        metadata["action_id"] = next_action_id
        metadata["kind"] = kind
        if "display_name" in params:
            metadata["display_name"] = str(params.get("display_name", "")).strip() or next_action_id
        else:
            metadata.setdefault("display_name", next_action_id)
        if "description" in params:
            metadata["description"] = str(params.get("description", "")).strip()
        if "best_for" in params:
            metadata["best_for"] = str(params.get("best_for", "")).strip()
        if "tags" in params:
            metadata["tags"] = _motion_tags(params.get("tags"))
        if "play_mode" in params:
            metadata["play_mode"] = _motion_play_mode(params.get("play_mode"), default_play_mode)
        else:
            metadata.setdefault("play_mode", default_play_mode)
        if "can_reverse" in params:
            metadata["can_reverse"] = _bool_param(params.get("can_reverse"), False)
        else:
            metadata.setdefault("can_reverse", False)
        if "weight" in params:
            metadata["weight"] = _float_param(params.get("weight"), 1.0, 0.0, 1000.0)
        else:
            metadata.setdefault("weight", 1.0)
        if "min_cycles" in params:
            metadata["min_cycles"] = _int_param(params.get("min_cycles"), 1, 1, 100)
        else:
            metadata.setdefault("min_cycles", 1)
        if "max_cycles" in params:
            metadata["max_cycles"] = _int_param(params.get("max_cycles"), metadata.get("min_cycles", 1), int(metadata.get("min_cycles", 1) or 1), 100)
        else:
            metadata.setdefault("max_cycles", metadata.get("min_cycles", 1))
        if "switch_at_boundary" in params:
            metadata["switch_at_boundary"] = _bool_param(params.get("switch_at_boundary"), True)
        else:
            metadata.setdefault("switch_at_boundary", True)
        if "enabled" in params:
            metadata["enabled"] = _bool_param(params.get("enabled"), True)
        else:
            metadata.setdefault("enabled", True)
        metadata["updated_at"] = datetime.now().isoformat(timespec="seconds")

        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if not explicit_out_root or _avatar_uses_local_motion_format(avatar_id):
            _sync_avatar_motion_clip_config(avatar_id, kind, next_action_id, metadata)

        clips = None
        selected = None
        current_attr = "current_idle_clip_id" if kind == "idle" else "current_motion_clip_id"
        reload_method = "reload_idle_motions" if kind == "idle" else "reload_speaking_motions"
        select_method = "set_idle_motion" if kind == "idle" else "set_speaking_motion"
        was_current = bool(
            avatar_session is not None
            and getattr(avatar_session, current_attr, None) == action_id
        )
        if avatar_session is not None and hasattr(avatar_session, reload_method):
            clips = getattr(avatar_session, reload_method)()
            if was_current and hasattr(avatar_session, select_method):
                selected = getattr(avatar_session, select_method)(next_action_id)

        return json_ok(data={
            "sessionid": sessionid,
            "kind": kind,
            "metadata": metadata,
            "clips": clips,
            "selected": selected,
        })
    except Exception as e:
        logger.exception('motion_update_clip exception:')
        return json_error(str(e))


async def motion_delete_clip(request):
    """Delete a speaking or idle motion clip directory."""
    try:
        params = await read_json_params(request)
        kind = _motion_kind(params)
        sessionid = str(params.get("sessionid", "")).strip() or session_manager.default_alpha_sessionid
        avatar_session = get_session(request, sessionid) if sessionid else None
        avatar_id = _motion_avatar_id(params, avatar_session)
        action_id = _clean_motion_id(params.get("action_id", ""), "action_id")
        explicit_out_root = str(params.get("out_root", "")).strip()
        root = _motion_clip_root(avatar_id, kind, explicit_out_root).resolve()
        target_dir = (root / action_id).resolve()
        if root not in target_dir.parents:
            return json_error("invalid motion clip path")
        if not target_dir.is_dir():
            return json_error(f"motion clip not found: {action_id}")

        current_attr = "current_idle_clip_id" if kind == "idle" else "current_motion_clip_id"
        reload_method = "reload_idle_motions" if kind == "idle" else "reload_speaking_motions"
        select_method = "set_idle_motion" if kind == "idle" else "set_speaking_motion"
        was_current = bool(
            avatar_session is not None
            and getattr(avatar_session, current_attr, None) == action_id
        )

        shutil.rmtree(target_dir)
        if not explicit_out_root or _avatar_uses_local_motion_format(avatar_id):
            _sync_avatar_motion_clip_config(avatar_id, kind, action_id, None, remove=True)

        clips = None
        selected = None
        if avatar_session is not None and hasattr(avatar_session, reload_method):
            clips = getattr(avatar_session, reload_method)()
            if was_current and hasattr(avatar_session, select_method):
                selected = getattr(avatar_session, select_method)("")

        if clips is None:
            clips = _list_motion_clip_metadata(avatar_id, explicit_out_root, kind)

        return json_ok(data={
            "sessionid": sessionid,
            "kind": kind,
            "deleted": {
                "avatar_id": avatar_id,
                "action_id": action_id,
            },
            "clips": clips,
            "selected": selected,
        })
    except Exception as e:
        logger.exception('motion_delete_clip exception:')
        return json_error(str(e))


async def _create_motion_clip_data(params: dict, task_id: str = "") -> dict:
    kind = _motion_kind(params)
    sessionid = str(params.get("sessionid", "")).strip() or session_manager.default_alpha_sessionid
    avatar_session = params.get("_avatar_session")
    if avatar_session is None and params.get("_request") is not None and sessionid:
        avatar_session = get_session(params.get("_request"), sessionid)
    avatar_id = str(params.get("avatar_id", "")).strip()
    if not avatar_id and avatar_session is not None:
        avatar_id = str(getattr(avatar_session.opt, "avatar_id", "")).strip()
    avatar_id = _clean_motion_id(avatar_id, "avatar_id")
    action_id = _clean_motion_id(params.get("action_id", ""), "action_id")

    if task_id:
        _motion_task_step(task_id, "prepare", "正在检查参数和源视频")
    source_path = _motion_source_path(params.get("source", ""))
    _ensure_motion_build_source(source_path)
    source = str(source_path)
    if not source:
        raise ValueError("source is required")

    explicit_out_root = str(params.get("out_root", "")).strip()
    target_root = _motion_create_clip_root(avatar_id, kind, explicit_out_root)
    out_root = str(target_root.parent)
    default_play_mode = _request_motion_default_play_mode(avatar_id, kind, explicit_out_root, create=True)
    target_dir = target_root / action_id
    if target_dir.exists() and not (target_dir / "metadata.json").exists():
        logger.info("remove incomplete motion clip before rebuild: %s", target_dir)
        shutil.rmtree(target_dir)

    fixed_face_box = _motion_box(params.get("fixed_face_box"))
    use_fixed_face_box = _bool_param(params.get("use_fixed_face_box"), False)
    if not fixed_face_box and use_fixed_face_box:
        if task_id:
            _motion_task_step(task_id, "detect", "正在根据首帧和 pads 生成固定人脸框")
        preview = await asyncio.to_thread(
            _detect_motion_preview,
            source,
            _motion_pads(params),
            float(params.get("start", 0) or 0),
            _bool_param(params.get("chroma_key"), False),
        )
        padded_box = preview.get("padded_box") or {}
        fixed_face_box = [
            int(padded_box.get("x1", 0)),
            int(padded_box.get("y1", 0)),
            int(padded_box.get("x2", 0)),
            int(padded_box.get("y2", 0)),
        ]

    args = SimpleNamespace(
        source=source,
        avatar_id=avatar_id,
        action_id=action_id,
        display_name=str(params.get("display_name", "")).strip(),
        out_root=out_root,
        start=float(params.get("start", 0) or 0),
        end=float(params["end"]) if params.get("end") not in (None, "") else None,
        fps=float(params.get("fps", 25) or 25),
        img_size=int(params.get("img_size", 256) or 256),
        pads=_motion_pads(params),
        face_det_batch_size=int(params.get("face_det_batch_size", 8) or 8),
        fixed_face_box=fixed_face_box,
        max_frames=int(params.get("max_frames", 0) or 0),
        tags=str(params.get("tags", "idle,teaching" if kind == "idle" else "speaking,teaching")),
        best_for=str(params.get("best_for", "")),
        play_mode=_motion_play_mode(params.get("play_mode"), default_play_mode),
        can_reverse=_bool_param(params.get("can_reverse"), False),
        weight=_float_param(params.get("weight"), 1.0, 0.0, 1000.0),
        min_cycles=_int_param(params.get("min_cycles"), 1, 1, 100),
        max_cycles=_int_param(params.get("max_cycles"), _int_param(params.get("min_cycles"), 1, 1, 100), 1, 100),
        switch_at_boundary=_bool_param(params.get("switch_at_boundary"), True),
        enabled=_bool_param(params.get("enabled"), True),
        chroma_key=_bool_param(params.get("chroma_key"), False),
        use_ffmpeg_cut=_bool_param(params.get("use_ffmpeg_cut"), False),
        ffmpeg_path=_default_ffmpeg_path(params.get("ffmpeg_path", "")),
        nosmooth=_bool_param(params.get("nosmooth"), False),
        no_loop=_bool_param(params.get("no_loop"), False),
        overwrite=_bool_param(params.get("overwrite"), False),
    )

    from tools.build_speaking_motion_clip import build_clip

    try:
        if task_id:
            _motion_task_step(task_id, "build", "正在截取视频、检测人脸并写入动作素材帧")
        await asyncio.to_thread(build_clip, args)
    except Exception:
        if target_dir.exists() and not (target_dir / "metadata.json").exists():
            logger.info("remove incomplete motion clip after failed build: %s", target_dir)
            shutil.rmtree(target_dir)
        raise
    if task_id:
        _motion_task_step(task_id, "metadata", "正在写入素材信息并刷新素材库")
    metadata_path = target_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["kind"] = kind
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if not explicit_out_root or _avatar_uses_local_motion_format(avatar_id):
        _sync_avatar_motion_clip_config(avatar_id, kind, action_id, metadata)
    clips = None
    reload_method = "reload_idle_motions" if kind == "idle" else "reload_speaking_motions"
    if avatar_session is not None and hasattr(avatar_session, reload_method):
        clips = getattr(avatar_session, reload_method)()

    return {
        "sessionid": sessionid,
        "kind": kind,
        "metadata": metadata,
        "clips": clips,
    }


async def motion_create_clip(request):
    """Create a speaking or idle motion clip from a local source video or image directory."""
    try:
        params = await read_json_params(request)
        params["_request"] = request
        return json_ok(data=await _create_motion_clip_data(params))
    except Exception as e:
        logger.exception('motion_create_clip exception:')
        return json_error(str(e))


async def _run_motion_clip_task(task_id: str, params: dict) -> None:
    try:
        _motion_task_step(task_id, "queued", "任务已开始")
        result = await _create_motion_clip_data(params, task_id=task_id)
        _set_motion_task(
            task_id,
            status="succeeded",
            step="done",
            message="动作素材已生成",
            result=result,
        )
    except Exception as exc:
        logger.exception("motion clip task failed: %s", task_id)
        _set_motion_task(
            task_id,
            status="failed",
            step="failed",
            message="动作素材生成失败",
            error=str(exc),
        )


async def motion_create_clip_task(request):
    """Start a motion clip build task and let the web page poll status."""
    try:
        params = await read_json_params(request)
        task_id = uuid.uuid4().hex
        sessionid = str(params.get("sessionid", "")).strip() or session_manager.default_alpha_sessionid
        params["_avatar_session"] = get_session(request, sessionid) if sessionid else None
        _set_motion_task(
            task_id,
            status="queued",
            step="queued",
            message="任务已创建，等待开始",
            created_at=datetime.now().isoformat(timespec="seconds"),
            result=None,
            error="",
        )
        asyncio.create_task(_run_motion_clip_task(task_id, params))
        return json_ok(data={"task_id": task_id, "task": _get_motion_task(task_id)})
    except Exception as e:
        logger.exception('motion_create_clip_task exception:')
        return json_error(str(e))


async def motion_task_status(request):
    """Return the current status for a motion clip build task."""
    task_id = str(request.match_info.get("task_id", "")).strip()
    task = _get_motion_task(task_id)
    if not task:
        return json_error("task not found", code=404)
    return json_ok(data=task)


async def _run_render_video_task(task_id: str, params: dict) -> None:
    try:
        _motion_task_step(task_id, "queued", "视频生成任务已开始")
        result = await asyncio.to_thread(_build_render_video_data, task_id, params)
        _set_motion_task(
            task_id,
            status="succeeded",
            step="done",
            message="视频已生成",
            result=result,
        )
    except Exception as exc:
        logger.exception("render video task failed: %s", task_id)
        _set_motion_task(
            task_id,
            status="failed",
            step="failed",
            message="视频生成失败",
            error=str(exc),
        )


async def render_video_task(request):
    """Start an offline render task for comparing lip-sync and enhancement settings."""
    try:
        params = await read_json_params(request)
        task_id = uuid.uuid4().hex
        sessionid = str(params.get("sessionid", "")).strip() or session_manager.default_alpha_sessionid
        params["_avatar_session"] = get_session(request, sessionid) if sessionid else None
        _set_motion_task(
            task_id,
            task_type="render_video",
            status="queued",
            step="queued",
            message="任务已创建，等待开始",
            created_at=datetime.now().isoformat(timespec="seconds"),
            result=None,
            error="",
        )
        asyncio.create_task(_run_render_video_task(task_id, params))
        return json_ok(data={"task_id": task_id, "task": _get_motion_task(task_id)})
    except Exception as e:
        logger.exception('render_video_task exception:')
        return json_error(str(e))


async def render_task_status(request):
    """Return the current status for an offline render task."""
    return await motion_task_status(request)


async def render_output(request):
    """Serve an output video produced by a render task."""
    try:
        task_id = str(request.match_info.get("task_id", "")).strip()
        output_path = _render_task_output_path(task_id)
        headers = {"Cache-Control": "no-store"}
        return web.FileResponse(path=output_path, headers=headers)
    except Exception as e:
        logger.exception('render_output exception:')
        return json_error(str(e))


async def render_library(request):
    """List generated render videos from the local render output directory."""
    try:
        limit = int(request.query.get("limit", "100") or 100)
        return json_ok(data={"items": _render_library_items(limit=limit)})
    except Exception as e:
        logger.exception('render_library exception:')
        return json_error(str(e))


async def render_realtime_config(request):
    """Read or update the experimental realtime enhancement config."""
    try:
        if request.method == "POST":
            params = await read_json_params(request)
            if "enabled" in params:
                RENDER_REALTIME_CONFIG["enabled"] = _bool_param(params.get("enabled"), False)
            if "lip_backend" in params:
                RENDER_REALTIME_CONFIG["lip_backend"] = _render_lip_backend(params.get("lip_backend"))
            if "enhance_mode" in params:
                RENDER_REALTIME_CONFIG["enhance_mode"] = _render_enhance_mode(params.get("enhance_mode"))
            _ensure_render_combo(RENDER_REALTIME_CONFIG["lip_backend"], RENDER_REALTIME_CONFIG["enhance_mode"])
            RENDER_REALTIME_CONFIG["updated_at"] = datetime.now().isoformat(timespec="seconds")
        data = dict(RENDER_REALTIME_CONFIG)
        data["note"] = "当前只是实验配置，默认不会把 RealESRGAN 接进实时帧处理。"
        return json_ok(data=data)
    except Exception as e:
        logger.exception('render_realtime_config exception:')
        return json_error(str(e))


async def alpha_audio_input_ws(request):
    """Receive robot-tts task audio and feed the default alpha avatar session."""
    ws = web.WebSocketResponse(max_msg_size=0, compress=False)
    await ws.prepare(request)

    params = request.rel_url.query
    requested_sessionid = str(params.get("sessionid", "")).strip() or None
    avatar_session = None
    state: Optional[HardwareAudioSession] = None
    logger.info(
        "alpha audio input websocket connected peer=%s requested_sessionid=%s query=%s",
        request.remote,
        requested_sessionid or "",
        dict(params),
    )

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "code": "InvalidRequest", "message": "message must be JSON"})
                    continue

                msg_type = payload.get("type")
                if msg_type == "start":
                    sessionid = await session_manager.get_or_create_alpha_session({}, requested_sessionid)
                    avatar_session = get_session(request, sessionid)
                    if avatar_session is None:
                        await ws.send_json({"type": "error", "code": "NotFound", "message": "session not found"})
                        continue

                    if params.get("interrupt", "1") != "0":
                        avatar_session.flush_talk()

                    state = HardwareAudioSession(
                        sessionid=sessionid,
                        task_id=str(payload.get("task_id", "")),
                        sample_rate=int(payload.get("sample_rate", 16000)),
                        channels=int(payload.get("channels", 1)),
                        sample_width=int(payload.get("sample_width", 2)),
                        text=str(payload.get("text", "")),
                    )
                    logger.info(
                        "alpha audio input start sessionid=%s task_id=%s sample_rate=%d channels=%d sample_width=%d text_len=%d",
                        sessionid,
                        state.task_id,
                        state.sample_rate,
                        state.channels,
                        state.sample_width,
                        len(state.text),
                    )
                    await ws.send_json({"type": "started", "task_id": state.task_id, "sessionid": sessionid})
                    continue

                if msg_type == "end":
                    if state is not None and avatar_session is not None:
                        _flush_hardware_audio_state(avatar_session, state, final=True)
                        logger.info(
                            "alpha audio input end sessionid=%s task_id=%s input_chunks=%d input_bytes=%d fed_chunks=%d pending=%d",
                            state.sessionid,
                            state.task_id,
                            state.input_chunks,
                            state.input_bytes,
                            state.fed_chunks,
                            state.pending.size,
                        )
                    await ws.close()
                    break

                if msg_type == "error":
                    logger.warning("hardware audio input upstream error: %s", payload)
                    continue

            elif msg.type == web.WSMsgType.BINARY:
                if state is None or avatar_session is None:
                    await ws.send_json({"type": "error", "code": "InvalidState", "message": "binary audio received before start"})
                    continue
                state.input_chunks += 1
                state.input_bytes += len(msg.data)
                _feed_hardware_audio_chunk(avatar_session, state, msg.data)

            elif msg.type == web.WSMsgType.ERROR:
                break
    except Exception:
        logger.exception("alpha audio input websocket exception")
    finally:
        if state is not None and avatar_session is not None and not state.finalized:
            _flush_hardware_audio_state(avatar_session, state, final=True)
            logger.info(
                "alpha audio input final sessionid=%s task_id=%s input_chunks=%d input_bytes=%d fed_chunks=%d",
                state.sessionid,
                state.task_id,
                state.input_chunks,
                state.input_bytes,
                state.fed_chunks,
            )
        logger.info("alpha audio input websocket disconnected peer=%s", request.remote)

    return ws


def _feed_hardware_audio_chunk(avatar_session, state: HardwareAudioSession, data: bytes):
    audio = pcm_to_float32(data, sample_width=state.sample_width)
    if state.channels > 1 and audio.size:
        usable = (audio.size // state.channels) * state.channels
        audio = audio[:usable].reshape(-1, state.channels)[:, 0]
    if state.sample_rate != avatar_session.sample_rate and audio.size:
        audio = resample_audio(audio, state.sample_rate, avatar_session.sample_rate).astype(np.float32)

    if state.pending.size:
        audio = np.concatenate([state.pending, audio.astype(np.float32, copy=False)])
    else:
        audio = audio.astype(np.float32, copy=False)

    chunk = avatar_session.chunk
    offset = 0
    while audio.size - offset >= chunk:
        eventpoint = {}
        if not state.started:
            eventpoint = {"status": "start", "text": state.text, "task_id": state.task_id}
            state.started = True
        avatar_session.put_audio_frame(audio[offset:offset + chunk], eventpoint)
        state.fed_chunks += 1
        offset += chunk

    state.pending = audio[offset:].copy() if offset < audio.size else np.zeros(0, dtype=np.float32)


def _flush_hardware_audio_state(avatar_session, state: HardwareAudioSession, final: bool):
    if final and state.finalized:
        return
    chunk = avatar_session.chunk
    if state.pending.size:
        if state.pending.size < chunk:
            state.pending = np.pad(state.pending, (0, chunk - state.pending.size))
        eventpoint = {"status": "end", "text": state.text, "task_id": state.task_id} if final else {}
        if not state.started:
            eventpoint.update({"status": "start", "text": state.text, "task_id": state.task_id})
            state.started = True
        avatar_session.put_audio_frame(state.pending[:chunk].astype(np.float32, copy=False), eventpoint)
        state.fed_chunks += 1
        state.pending = np.zeros(0, dtype=np.float32)
    elif final and state.started:
        avatar_session.put_audio_frame(np.zeros(chunk, dtype=np.float32), {"status": "end", "text": state.text, "task_id": state.task_id})
        state.fed_chunks += 1
    if final:
        state.finalized = True

# ─── 路由注册 ──────────────────────────────────────────────────────────────

def setup_routes(app):
    """注册所有路由到 aiohttp app"""
    app.router.add_post("/human", human)
    app.router.add_post("/humanaudio", humanaudio)
    app.router.add_post("/set_audiotype", set_audiotype)
    app.router.add_post("/record", record)
    app.router.add_post("/interrupt_talk", interrupt_talk)
    app.router.add_post("/is_speaking", is_speaking)
    app.router.add_get("/api/admin/config", admin_config)
    app.router.add_get("/api/admin/sessions", admin_sessions)

    # 注册 avatar 生成相关的路由
    setup_avatar_routes(app)

    app.router.add_post("/close_session", close_session)
    app.router.add_post("/alpha/session", alpha_session)
    app.router.add_post("/alpha/speak", alpha_speak)
    app.router.add_post("/alpha/close", alpha_close)
    app.router.add_get("/alpha/tuning", alpha_tuning)
    app.router.add_post("/alpha/tuning", alpha_tuning)
    app.router.add_get("/motion/clips", motion_clips)
    app.router.add_post("/motion/plan", motion_plan)
    app.router.add_post("/motion/source/upload", motion_source_upload)
    app.router.add_post("/motion/source/probe", motion_source_probe)
    app.router.add_post("/motion/source/detect", motion_source_detect)
    app.router.add_get("/motion/source/video", motion_source_video)
    app.router.add_post("/motion/select", motion_select)
    app.router.add_post("/motion/clips/update", motion_update_clip)
    app.router.add_post("/motion/clips/delete", motion_delete_clip)
    app.router.add_post("/motion/clips/create", motion_create_clip)
    app.router.add_post("/motion/clips/create-task", motion_create_clip_task)
    app.router.add_get("/motion/tasks/{task_id}", motion_task_status)
    app.router.add_post("/render/video-task", render_video_task)
    app.router.add_get("/render/tasks/{task_id}", render_task_status)
    app.router.add_get("/render/library", render_library)
    app.router.add_get("/render/output/{task_id}", render_output)
    app.router.add_get("/render/realtime", render_realtime_config)
    app.router.add_post("/render/realtime", render_realtime_config)
    app.router.add_get("/alpha/ws", alpha_ws)
    app.router.add_get("/alpha/audio", alpha_audio_ws)
    app.router.add_get("/alpha/input/audio", alpha_audio_input_ws)
    app.router.add_static('/', path='web')
