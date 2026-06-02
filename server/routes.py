###############################################################################
#  服务器路由 — 统一异常处理的 API 路由
###############################################################################

import asyncio
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from urllib.parse import urlencode

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
    return cleaned


def _motion_pads(params: dict) -> list[int]:
    pads = params.get("pads")
    if pads is None:
        pads = [params.get("top", 0), params.get("bottom", 10), params.get("left", 0), params.get("right", 0)]
    values = [int(value) for value in list(pads)[:4]]
    while len(values) < 4:
        values.append(0)
    return values


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
    return source_path


def _default_ffprobe_path(ffmpeg_path: str = "") -> str:
    if ffmpeg_path:
        path = Path(ffmpeg_path)
        probe = path.with_name("ffprobe.exe")
        if probe.exists():
            return str(probe)
    return "G:/ffmpeg/ffmpeg-8.1-essentials_build/bin/ffprobe.exe"


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


def _motion_avatar_id(params: dict, avatar_session) -> str:
    avatar_id = str(params.get("avatar_id", "")).strip()
    if not avatar_id and avatar_session is not None:
        avatar_id = str(getattr(avatar_session.opt, "avatar_id", "")).strip()
    return _clean_motion_id(avatar_id, "avatar_id")


def _list_motion_clip_metadata(avatar_id: str, out_root: str = "data/speaking_actions") -> list[dict]:
    root = Path(out_root) / avatar_id
    if not root.is_dir():
        return []

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
        if "frame_count" not in metadata:
            full_imgs = action_dir / "full_imgs"
            metadata["frame_count"] = len(list(full_imgs.glob("*.png"))) if full_imgs.is_dir() else 0
        metadata["current"] = False
        clips.append(metadata)
    return clips


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
            avatar_id = _clean_motion_id(params.get("avatar_id", ""), "avatar_id")
            out_root = str(params.get("out_root", _motion_root_for_kind(kind))).strip() or _motion_root_for_kind(kind)
            return json_ok(data={
                "sessionid": sessionid,
                "kind": kind,
                "clips": _list_motion_clip_metadata(avatar_id, out_root),
            })
        list_method = "list_idle_motions" if kind == "idle" else "list_speaking_motions"
        if not hasattr(avatar_session, list_method):
            return json_error(f"current avatar does not support {kind} motion clips")
        return json_ok(data={
            "sessionid": sessionid,
            "kind": kind,
            "clips": getattr(avatar_session, list_method)(),
        })
    except Exception as e:
        logger.exception('motion_clips exception:')
        return json_error(str(e))


async def motion_source_probe(request):
    """Probe a local source video so the web page can show duration and timeline data."""
    try:
        params = await read_json_params(request)
        source_path = _motion_source_path(params.get("source", ""))
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
        if source_path.is_dir():
            return json_error("source must be a video file")
        headers = {"Cache-Control": "no-store"}
        return web.FileResponse(path=source_path, headers=headers)
    except Exception as e:
        logger.exception('motion_source_video exception:')
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
        out_root = str(params.get("out_root", _motion_root_for_kind(kind))).strip() or _motion_root_for_kind(kind)

        root = Path(out_root) / avatar_id
        source_dir = root / action_id
        if not source_dir.is_dir():
            return json_error(f"motion clip not found: {action_id}")

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
        metadata["updated_at"] = datetime.now().isoformat(timespec="seconds")

        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

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


async def motion_create_clip(request):
    """Create a speaking or idle motion clip from a local source video or image directory."""
    try:
        params = await read_json_params(request)
        kind = _motion_kind(params)
        sessionid = str(params.get("sessionid", "")).strip() or session_manager.default_alpha_sessionid
        avatar_session = get_session(request, sessionid) if sessionid else None
        avatar_id = str(params.get("avatar_id", "")).strip()
        if not avatar_id and avatar_session is not None:
            avatar_id = str(getattr(avatar_session.opt, "avatar_id", "")).strip()
        avatar_id = _clean_motion_id(avatar_id, "avatar_id")
        action_id = _clean_motion_id(params.get("action_id", ""), "action_id")

        source = str(params.get("source", "")).strip()
        if not source:
            return json_error("source is required")

        out_root = str(params.get("out_root", _motion_root_for_kind(kind))).strip() or _motion_root_for_kind(kind)
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
            max_frames=int(params.get("max_frames", 0) or 0),
            tags=str(params.get("tags", "idle,teaching" if kind == "idle" else "speaking,teaching")),
            best_for=str(params.get("best_for", "")),
            chroma_key=bool(params.get("chroma_key", False)),
            use_ffmpeg_cut=bool(params.get("use_ffmpeg_cut", False)),
            ffmpeg_path=str(params.get("ffmpeg_path", "G:/ffmpeg/ffmpeg-8.1-essentials_build/bin/ffmpeg.exe")),
            nosmooth=bool(params.get("nosmooth", False)),
            no_loop=bool(params.get("no_loop", False)),
            overwrite=bool(params.get("overwrite", False)),
        )

        from tools.build_speaking_motion_clip import build_clip

        await asyncio.to_thread(build_clip, args)
        metadata_path = Path(out_root) / avatar_id / action_id / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["kind"] = kind
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        clips = None
        reload_method = "reload_idle_motions" if kind == "idle" else "reload_speaking_motions"
        if avatar_session is not None and hasattr(avatar_session, reload_method):
            clips = getattr(avatar_session, reload_method)()

        return json_ok(data={
            "sessionid": sessionid,
            "kind": kind,
            "metadata": metadata,
            "clips": clips,
        })
    except Exception as e:
        logger.exception('motion_create_clip exception:')
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
    app.router.add_post("/motion/source/probe", motion_source_probe)
    app.router.add_get("/motion/source/video", motion_source_video)
    app.router.add_post("/motion/select", motion_select)
    app.router.add_post("/motion/clips/update", motion_update_clip)
    app.router.add_post("/motion/clips/create", motion_create_clip)
    app.router.add_get("/alpha/ws", alpha_ws)
    app.router.add_get("/alpha/audio", alpha_audio_ws)
    app.router.add_get("/alpha/input/audio", alpha_audio_input_ws)
    app.router.add_static('/', path='web')
