import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity,
  Cable,
  ChevronLeft,
  ChevronRight,
  Eye,
  EyeOff,
  ImageUp,
  Mic,
  Play,
  Presentation,
  RotateCcw,
  Send,
  SlidersHorizontal,
  Square,
  Video,
  Volume2,
  VolumeX
} from 'lucide-react';
import './styles.css';

function wsUrl(base, path) {
  const url = new URL(base);
  const target = new URL(path, url.origin);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = target.pathname;
  url.search = target.search;
  return url.toString();
}

const DEFAULT_LIVETALKING_URL = import.meta.env.VITE_LIVETALKING_URL || 'http://127.0.0.1:8050';
const URL_PARAMS = new URLSearchParams(window.location.search);

function paramOrEnv(paramName, envName, fallback) {
  return URL_PARAMS.get(paramName) ?? import.meta.env[envName] ?? fallback;
}

const DEFAULTS = {
  liveTalkingUrl: DEFAULT_LIVETALKING_URL,
  avatarId: import.meta.env.VITE_AVATAR_ID || 'mute_teacher_motion_v1_pad01000',
  ttsServerUrl: import.meta.env.VITE_TTS_SERVER_URL || 'http://127.0.0.1:8036',
  alphaInputWs: import.meta.env.VITE_ALPHA_INPUT_WS || wsUrl(DEFAULT_LIVETALKING_URL, '/alpha/input/audio'),
  text: import.meta.env.VITE_DEFAULT_TEXT || '这是一段 robottts 兼容接口流式测试。',
  prompts: import.meta.env.VITE_DEFAULT_PROMPTS || '请自然清晰地朗读。',
  voiceId: Number.parseInt(import.meta.env.VITE_DEFAULT_VOICE_ID || '0', 10),
  mode: import.meta.env.VITE_DEFAULT_MODE || 'instruct2',
  audioSampleRate: Number.parseInt(import.meta.env.VITE_ALPHA_AUDIO_SAMPLE_RATE || '16000', 10),
  videoMaxHeight: Number.parseInt(paramOrEnv('max_height', 'VITE_ALPHA_VIDEO_MAX_HEIGHT', '0'), 10),
  videoPreviewFps: Number.parseFloat(paramOrEnv('fps', 'VITE_ALPHA_VIDEO_FPS', '25')),
  videoFormat: paramOrEnv('format', 'VITE_ALPHA_VIDEO_FORMAT', 'raw'),
  videoQuality: Number.parseInt(paramOrEnv('quality', 'VITE_ALPHA_VIDEO_QUALITY', '80'), 10),
  videoRenderIntervalMs: Math.max(16, Number.parseInt(paramOrEnv('render_ms', 'VITE_VIDEO_RENDER_INTERVAL_MS', '40'), 10)),
  sharpness: Math.max(0, Math.min(100, Number.parseInt(paramOrEnv('sharpness', 'VITE_ALPHA_SHARPNESS', '0'), 10))),
  videoFit: paramOrEnv('fit', 'VITE_ALPHA_VIDEO_FIT', 'contain'),
  alphaAutoConnect: (import.meta.env.VITE_ALPHA_AUTO_CONNECT || '1') !== '0'
};

const PAD_LABELS = {
  top: '上',
  bottom: '下',
  left: '左',
  right: '右'
};
const PAD_KEYS = ['top', 'bottom', 'left', 'right'];

function waitMs(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function estimateSpeakMs(value) {
  const textLength = String(value || '').replace(/\s+/g, '').length;
  return Math.max(1800, Math.min(12000, textLength * 180));
}

function padsFromArray(values) {
  const source = Array.isArray(values) ? values : [0, 0, 0, 0];
  return {
    top: Number(source[0]) || 0,
    bottom: Number(source[1]) || 0,
    left: Number(source[2]) || 0,
    right: Number(source[3]) || 0
  };
}

function formatPads(padsValue) {
  return PAD_KEYS.map((key) => `${PAD_LABELS[key]} ${padsValue[key] ?? 0}`).join('，');
}

function parseFrame(packet) {
  if (!packet || packet.byteLength < 24) return null;
  const view = new DataView(packet);
  const magic = String.fromCharCode(...new Uint8Array(packet.slice(0, 4)));
  if (magic !== 'LTAF') return null;
  const version = view.getUint8(4);
  const format = view.getUint8(5);
  const width = view.getUint32(8, true);
  const height = view.getUint32(12, true);
  const seq = Number(view.getBigUint64(16, true));
  if (version !== 1 || width <= 0 || height <= 0) return null;
  const payload = packet.slice(24);
  if (format === 1) {
    const pixels = new Uint8ClampedArray(packet, 24);
    if (pixels.byteLength !== width * height * 4) return null;
    return { width, height, seq, format, pixels };
  }
  if (format === 2 || format === 3 || format === 4) {
    return { width, height, seq, format, payload };
  }
  return null;
}

function App() {
  const [liveTalkingUrl, setLiveTalkingUrl] = useState(DEFAULTS.liveTalkingUrl);
  const [ttsServerUrl, setTtsServerUrl] = useState(DEFAULTS.ttsServerUrl);
  const [alphaInputWs, setAlphaInputWs] = useState(DEFAULTS.alphaInputWs);
  const [text, setText] = useState(DEFAULTS.text);
  const [prompts, setPrompts] = useState(DEFAULTS.prompts);
  const [voiceId, setVoiceId] = useState(DEFAULTS.voiceId);
  const [mode, setMode] = useState(DEFAULTS.mode);
  const [videoMaxHeight, setVideoMaxHeight] = useState(DEFAULTS.videoMaxHeight);
  const [videoPreviewFps, setVideoPreviewFps] = useState(DEFAULTS.videoPreviewFps);
  const [videoRenderIntervalMs, setVideoRenderIntervalMs] = useState(DEFAULTS.videoRenderIntervalMs);
  const [sharpness, setSharpness] = useState(DEFAULTS.sharpness);
  const [showOverlay, setShowOverlay] = useState(true);
  const [pads, setPads] = useState({ top: 0, bottom: 0, left: 0, right: 0 });
  const [generationPads, setGenerationPads] = useState({ top: 0, bottom: 0, left: 0, right: 0 });
  const [pasteDeltaPads, setPasteDeltaPads] = useState({ top: 0, bottom: 0, left: 0, right: 0 });
  const [motionClips, setMotionClips] = useState([]);
  const [selectedMotion, setSelectedMotion] = useState('');
  const [idleClips, setIdleClips] = useState([]);
  const [selectedIdleMotion, setSelectedIdleMotion] = useState('');
  const [tuningInfo, setTuningInfo] = useState(null);
  const [canvasBox, setCanvasBox] = useState({ left: 0, top: 0, width: 0, height: 0 });
  const [voices, setVoices] = useState([]);
  const [sessionId, setSessionId] = useState('');
  const [status, setStatus] = useState('idle');
  const [videoState, setVideoState] = useState('disconnected');
  const [audioState, setAudioState] = useState('disconnected');
  const [frameInfo, setFrameInfo] = useState({ width: 0, height: 0, seq: 0, fps: 0 });
  const [audioInfo, setAudioInfo] = useState({ chunks: 0, bytes: 0 });
  const [logs, setLogs] = useState([]);
  const [motionPlan, setMotionPlan] = useState([]);
  const [motionPlanProvider, setMotionPlanProvider] = useState('');
  const [motionPlanRunning, setMotionPlanRunning] = useState(false);
  const [classroomMode, setClassroomMode] = useState(false);
  const [slideItems, setSlideItems] = useState([]);
  const [slideIndex, setSlideIndex] = useState(0);
  const [avatarSize, setAvatarSize] = useState(24);
  const canvasRef = useRef(null);
  const stageRef = useRef(null);
  const slideInputRef = useRef(null);
  const socketRef = useRef(null);
  const audioSocketRef = useRef(null);
  const audioContextRef = useRef(null);
  const audioScheduleRef = useRef(0);
  const latestFramePacketRef = useRef(null);
  const renderTimerRef = useRef(0);
  const drawingFrameRef = useRef(false);
  const lastVideoDrawAtRef = useRef(0);
  const alphaAutoStartedRef = useRef(false);
  const motionPlanStopRef = useRef(false);
  const frameStatsRef = useRef({ lastAt: performance.now(), lastSeq: 0, fps: 0 });
  const audioStatsRef = useRef({ chunks: 0, bytes: 0, lastUpdateAt: 0 });
  const currentSlide = slideItems[slideIndex] || null;

  const addLog = useCallback((message, data) => {
    const time = new Date().toLocaleTimeString();
    const suffix = data ? ` ${JSON.stringify(data)}` : '';
    setLogs((items) => [`[${time}] ${message}${suffix}`, ...items].slice(0, 80));
  }, []);

  const normalized = useMemo(() => {
    const live = liveTalkingUrl.replace(/\/$/, '');
    const tts = ttsServerUrl.replace(/\/$/, '');
    return {
      live,
      tts,
      alphaWs: wsUrl(
        live,
        `/alpha/ws?max_height=${videoMaxHeight}&fps=${videoPreviewFps}&format=${DEFAULTS.videoFormat}&quality=${DEFAULTS.videoQuality}`
      ),
      audioWs: wsUrl(live, '/alpha/audio')
    };
  }, [liveTalkingUrl, ttsServerUrl, videoMaxHeight, videoPreviewFps]);

  const updateCanvasBox = useCallback(() => {
    const canvas = canvasRef.current;
    const stage = stageRef.current;
    if (!canvas || !stage) return;
    const canvasRect = canvas.getBoundingClientRect();
    const stageRect = stage.getBoundingClientRect();
    setCanvasBox({
      left: canvasRect.left - stageRect.left,
      top: canvasRect.top - stageRect.top,
      width: canvasRect.width,
      height: canvasRect.height
    });
  }, []);

  useEffect(() => {
    updateCanvasBox();
    window.addEventListener('resize', updateCanvasBox);
    return () => window.removeEventListener('resize', updateCanvasBox);
  }, [updateCanvasBox]);

  const buildOverlayStyle = useCallback((bbox) => {
    if (!showOverlay || !bbox || !canvasBox.width || !canvasBox.height) return null;
    const sourceWidth = tuningInfo?.source_width || frameInfo.width || 1;
    const sourceHeight = tuningInfo?.source_height || frameInfo.height || 1;
    const scaleX = canvasBox.width / sourceWidth;
    const scaleY = canvasBox.height / sourceHeight;
    return {
      left: `${canvasBox.left + bbox.x1 * scaleX}px`,
      top: `${canvasBox.top + bbox.y1 * scaleY}px`,
      width: `${Math.max(1, (bbox.x2 - bbox.x1) * scaleX)}px`,
      height: `${Math.max(1, (bbox.y2 - bbox.y1) * scaleY)}px`
    };
  }, [canvasBox, frameInfo.height, frameInfo.width, showOverlay, tuningInfo]);

  const baseOverlayStyle = useMemo(
    () => buildOverlayStyle(tuningInfo?.base_bbox),
    [buildOverlayStyle, tuningInfo]
  );
  const paddedOverlayStyle = useMemo(
    () => buildOverlayStyle(tuningInfo?.padded_bbox),
    [buildOverlayStyle, tuningInfo]
  );
  const canvasStyle = useMemo(() => ({
    filter: sharpness > 0
      ? `contrast(${1 + sharpness * 0.006}) saturate(${1 + sharpness * 0.002})`
      : 'none'
  }), [sharpness]);

  const drawFrame = useCallback(async (packet) => {
    const frame = parseFrame(packet);
    if (!frame) {
      addLog('丢弃无效视频帧');
      return;
    }
    const canvas = canvasRef.current;
    if (!canvas) return;
    if (canvas.width !== frame.width || canvas.height !== frame.height) {
      canvas.width = frame.width;
      canvas.height = frame.height;
    }
    const ctx = canvas.getContext('2d');
    if (frame.format === 1) {
      ctx.putImageData(new ImageData(frame.pixels, frame.width, frame.height), 0, 0);
    } else {
      const mime = frame.format === 2 ? 'image/jpeg' : frame.format === 3 ? 'image/png' : 'image/webp';
      const bitmap = await createImageBitmap(new Blob([frame.payload], { type: mime }));
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(bitmap, 0, 0, frame.width, frame.height);
      bitmap.close();
    }
    window.requestAnimationFrame(updateCanvasBox);

    const stats = frameStatsRef.current;
    const now = performance.now();
    const elapsed = now - stats.lastAt;
    let fps = stats.fps;
    if (elapsed >= 1000) {
      fps = ((frame.seq - stats.lastSeq) * 1000) / elapsed;
      stats.lastAt = now;
      stats.lastSeq = frame.seq;
      stats.fps = fps;
    }
    setFrameInfo({ width: frame.width, height: frame.height, seq: frame.seq, fps });
  }, [addLog, updateCanvasBox]);

  const renderLatestFrame = useCallback(() => {
    renderTimerRef.current = 0;
    if (drawingFrameRef.current) return;
    const packet = latestFramePacketRef.current;
    latestFramePacketRef.current = null;
    if (packet) {
      drawingFrameRef.current = true;
      drawFrame(packet)
        .catch((error) => addLog('绘制视频帧失败', { error: String(error) }))
        .finally(() => {
          drawingFrameRef.current = false;
          lastVideoDrawAtRef.current = performance.now();
          if (latestFramePacketRef.current) {
            renderLatestFrame();
          }
        });
    }
  }, [addLog, drawFrame]);

  const enqueueVideoFrame = useCallback((packet) => {
    latestFramePacketRef.current = packet;
    if (renderTimerRef.current) return;
    const elapsed = performance.now() - lastVideoDrawAtRef.current;
    const wait = Math.max(0, videoRenderIntervalMs - elapsed);
    renderTimerRef.current = window.setTimeout(renderLatestFrame, wait);
  }, [renderLatestFrame, videoRenderIntervalMs]);

  const connectVideo = useCallback(() => {
    if (socketRef.current) socketRef.current.close();
    const socket = new WebSocket(normalized.alphaWs);
    socket.binaryType = 'arraybuffer';
    socketRef.current = socket;
    setVideoState('connecting');
    addLog('连接 alpha 视频流', { url: normalized.alphaWs });
    socket.onopen = () => {
      setVideoState('connected');
      addLog('alpha 视频流已连接');
    };
    socket.onmessage = (event) => enqueueVideoFrame(event.data);
    socket.onerror = () => {
      setVideoState('error');
      addLog('alpha 视频流连接错误');
    };
    socket.onclose = (event) => {
      setVideoState('disconnected');
      addLog('alpha 视频流已断开', { code: event.code });
    };
  }, [addLog, enqueueVideoFrame, normalized.alphaWs]);

  const disconnectVideo = useCallback(() => {
    socketRef.current?.close();
    socketRef.current = null;
    latestFramePacketRef.current = null;
    if (renderTimerRef.current) {
      clearTimeout(renderTimerRef.current);
      renderTimerRef.current = 0;
    }
    setVideoState('disconnected');
  }, []);

  const playAudioChunk = useCallback((packet) => {
    const audioContext = audioContextRef.current;
    if (!audioContext || !(packet instanceof ArrayBuffer) || packet.byteLength < 2) return;

    const samples = new Int16Array(packet);
    const buffer = audioContext.createBuffer(1, samples.length, DEFAULTS.audioSampleRate);
    const channel = buffer.getChannelData(0);
    for (let index = 0; index < samples.length; index += 1) {
      channel[index] = Math.max(-1, Math.min(1, samples[index] / 32768));
    }

    const now = audioContext.currentTime;
    if (
      !audioScheduleRef.current ||
      audioScheduleRef.current < now + 0.03 ||
      audioScheduleRef.current > now + 0.8
    ) {
      audioScheduleRef.current = now + 0.05;
    }

    const source = audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(audioContext.destination);
    source.start(audioScheduleRef.current);
    audioScheduleRef.current += buffer.duration;

    const stats = audioStatsRef.current;
    stats.chunks += 1;
    stats.bytes += packet.byteLength;
    const updateAt = performance.now();
    if (updateAt - stats.lastUpdateAt >= 500) {
      stats.lastUpdateAt = updateAt;
      setAudioInfo({ chunks: stats.chunks, bytes: stats.bytes });
    }
  }, []);

  const disconnectAudio = useCallback(() => {
    audioSocketRef.current?.close();
    audioSocketRef.current = null;
    audioScheduleRef.current = 0;
    audioStatsRef.current = { chunks: 0, bytes: 0, lastUpdateAt: 0 };
    setAudioState('disconnected');
    setAudioInfo({ chunks: 0, bytes: 0 });
    if (audioContextRef.current) {
      audioContextRef.current.close().catch(() => {});
      audioContextRef.current = null;
    }
  }, []);

  const connectAudio = useCallback(async () => {
    disconnectAudio();
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) {
      setAudioState('error');
      addLog('浏览器不支持 Web Audio');
      return;
    }

    const audioContext = new AudioContextClass();
    audioContextRef.current = audioContext;
    await audioContext.resume();

    const socket = new WebSocket(normalized.audioWs);
    socket.binaryType = 'arraybuffer';
    audioSocketRef.current = socket;
    setAudioState('connecting');
    addLog('连接 alpha 音频流', { url: normalized.audioWs, sampleRate: DEFAULTS.audioSampleRate });
    socket.onopen = () => {
      setAudioState('connected');
      addLog('alpha 音频流已连接');
    };
    socket.onmessage = (event) => playAudioChunk(event.data);
    socket.onerror = () => {
      setAudioState('error');
      addLog('alpha 音频流连接错误');
    };
    socket.onclose = (event) => {
      setAudioState('disconnected');
      addLog('alpha 音频流已断开', { code: event.code });
    };
  }, [addLog, disconnectAudio, normalized.audioWs, playAudioChunk]);

  useEffect(() => () => {
    disconnectVideo();
    disconnectAudio();
  }, [disconnectAudio, disconnectVideo]);

  const checkHealth = async () => {
    setStatus('checking');
    try {
      const [liveResp, ttsResp, voiceResp] = await Promise.all([
        fetch(`${normalized.live}/alpha/session`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reuse: true })
        }),
        fetch(`${normalized.tts}/health`),
        fetch(`${normalized.tts}/tts/voices`)
      ]);
      const liveJson = await liveResp.json();
      const ttsJson = await ttsResp.json();
      const voiceJson = await voiceResp.json();
      if (liveJson?.data?.sessionid) setSessionId(String(liveJson.data.sessionid));
      setVoices(Array.isArray(voiceJson.voices) ? voiceJson.voices : []);
      setStatus('ready');
      addLog('健康检查完成', { live: liveJson.code, tts: ttsJson.status || ttsJson.provider });
      if (liveJson?.data?.sessionid) syncTuning(String(liveJson.data.sessionid));
      if (liveJson?.data?.sessionid) refreshMotionClips(String(liveJson.data.sessionid));
      if (liveJson?.data?.sessionid) refreshIdleClips(String(liveJson.data.sessionid));
    } catch (error) {
      setStatus('error');
      addLog('健康检查失败', { error: String(error) });
    }
  };

  const applyTuningPayload = useCallback((payload) => {
    if (!payload?.data) return;
    const data = payload.data;
    setTuningInfo(data);
    if (data.sessionid) setSessionId(String(data.sessionid));
    if (Array.isArray(data.pads) && data.pads.length >= 4) {
      setPads(padsFromArray(data.pads));
    }
    setGenerationPads(padsFromArray(data.generation_pads || data.baked_pads));
    setPasteDeltaPads(padsFromArray(data.paste_delta_pads));
    window.requestAnimationFrame(updateCanvasBox);
  }, [updateCanvasBox]);

  const syncTuning = useCallback(async (targetSessionId = sessionId) => {
    try {
      const query = targetSessionId ? `?sessionid=${encodeURIComponent(targetSessionId)}` : '';
      const resp = await fetch(`${normalized.live}/alpha/tuning${query}`);
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'tuning failed');
      applyTuningPayload(payload);
      addLog('调试参数已同步', payload.data);
    } catch (error) {
      addLog('同步调试参数失败', { error: String(error) });
    }
  }, [addLog, applyTuningPayload, normalized.live, sessionId]);

  const refreshMotionClips = useCallback(async (targetSessionId = sessionId, options = {}) => {
    try {
      const query = new URLSearchParams({
        kind: 'speaking',
        avatar_id: DEFAULTS.avatarId
      });
      if (targetSessionId) query.set('sessionid', targetSessionId);
      if (options.reload) query.set('reload', '1');
      const resp = await fetch(`${normalized.live}/motion/clips?${query.toString()}`);
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'motion clips failed');
      const clips = Array.isArray(payload.data?.clips) ? payload.data.clips : [];
      setMotionClips(clips);
      const current = clips.find((clip) => clip.current);
      if (current?.action_id) {
        setSelectedMotion(current.action_id);
      }
      addLog('说话动作已刷新', { clips: clips.map((clip) => clip.action_id) });
    } catch (error) {
      addLog('刷新说话动作失败', { error: String(error) });
    }
  }, [addLog, normalized.live, sessionId]);

  const refreshIdleClips = useCallback(async (targetSessionId = sessionId, options = {}) => {
    try {
      const query = new URLSearchParams({
        kind: 'idle',
        avatar_id: DEFAULTS.avatarId
      });
      if (targetSessionId) query.set('sessionid', targetSessionId);
      if (options.reload) query.set('reload', '1');
      const resp = await fetch(`${normalized.live}/motion/clips?${query.toString()}`);
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'idle clips failed');
      const clips = Array.isArray(payload.data?.clips) ? payload.data.clips : [];
      setIdleClips(clips);
      const current = clips.find((clip) => clip.current);
      if (current?.action_id) {
        setSelectedIdleMotion(current.action_id);
      } else {
        setSelectedIdleMotion('');
      }
      addLog('静息动作已刷新', { clips: clips.map((clip) => clip.action_id) });
    } catch (error) {
      addLog('刷新静息动作失败', { error: String(error) });
    }
  }, [addLog, normalized.live, sessionId]);

  const selectMotionClip = useCallback(async (actionId) => {
    setSelectedMotion(actionId);
    try {
      const resp = await fetch(`${normalized.live}/motion/select`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sessionid: sessionId || undefined,
          kind: 'speaking',
          action_id: actionId
        })
      });
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'motion select failed');
      addLog('说话动作已选择', payload.data?.selected || {});
      refreshMotionClips(payload.data?.sessionid || sessionId);
    } catch (error) {
      addLog('选择说话动作失败', { error: String(error) });
    }
  }, [addLog, normalized.live, refreshMotionClips, sessionId]);

  const selectIdleClip = useCallback(async (actionId) => {
    setSelectedIdleMotion(actionId);
    try {
      const resp = await fetch(`${normalized.live}/motion/select`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sessionid: sessionId || undefined,
          kind: 'idle',
          action_id: actionId
        })
      });
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'idle motion select failed');
      addLog('静息动作已选择', payload.data?.selected || {});
      refreshIdleClips(payload.data?.sessionid || sessionId);
    } catch (error) {
      addLog('选择静息动作失败', { error: String(error) });
    }
  }, [addLog, normalized.live, refreshIdleClips, sessionId]);

  const ensureAlphaSession = useCallback(async () => {
    if (sessionId) return sessionId;
    const resp = await fetch(`${normalized.live}/alpha/session`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reuse: true })
    });
    const payload = await resp.json();
    if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'session failed');
    const nextSessionId = String(payload.data.sessionid || '');
    setSessionId(nextSessionId);
    syncTuning(nextSessionId);
    refreshMotionClips(nextSessionId);
    refreshIdleClips(nextSessionId);
    return nextSessionId;
  }, [normalized.live, refreshIdleClips, refreshMotionClips, sessionId, syncTuning]);

  const planMotions = useCallback(async () => {
    const content = text.trim();
    if (!content) {
      addLog('请先输入讲课文本');
      return;
    }
    setStatus('planning');
    setMotionPlanProvider('');
    try {
      const activeSessionId = await ensureAlphaSession();
      const resp = await fetch(`${normalized.live}/motion/plan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sessionid: activeSessionId,
          text: content,
          max_segments: 8,
          use_llm: true
        })
      });
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'motion plan failed');
      const nextPlan = Array.isArray(payload.data?.plan) ? payload.data.plan : [];
      setMotionPlan(nextPlan);
      setMotionPlanProvider(payload.data?.provider || '');
      setStatus(`已生成 ${nextPlan.length} 段动作计划`);
      addLog('动作编排已生成', {
        provider: payload.data?.provider,
        count: nextPlan.length,
        llm_error: payload.data?.llm_error || undefined
      });
    } catch (error) {
      setStatus('动作编排失败');
      addLog('动作编排失败', { error: String(error) });
    }
  }, [addLog, ensureAlphaSession, normalized.live, text]);

  const speakPlannedMotions = useCallback(async () => {
    if (motionPlan.length === 0) {
      addLog('还没有动作计划，请先点击编排动作');
      return;
    }
    motionPlanStopRef.current = false;
    setMotionPlanRunning(true);
    setStatus('按计划讲课中');
    let activeSessionId = sessionId;
    try {
      activeSessionId = await ensureAlphaSession();
      for (let index = 0; index < motionPlan.length; index += 1) {
        if (motionPlanStopRef.current) break;
        const item = motionPlan[index];
        const actionId = String(item.action_id || '').trim();
        const segmentText = String(item.text || '').trim();
        if (!segmentText) continue;

        if (actionId) {
          setSelectedMotion(actionId);
          const selectResp = await fetch(`${normalized.live}/motion/select`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              sessionid: activeSessionId,
              kind: 'speaking',
              action_id: actionId
            })
          });
          const selectPayload = await selectResp.json();
          if (!selectResp.ok || selectPayload.code !== 0) {
            throw new Error(selectPayload.msg || `select ${actionId} failed`);
          }
        }

        const speakResp = await fetch(`${normalized.live}/alpha/speak`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            text: segmentText,
            type: 'echo',
            interrupt: index === 0,
            sessionid: activeSessionId,
            tts: {
              voice_id: voiceId,
              prompts,
              mode
            }
          })
        });
        const speakPayload = await speakResp.json();
        if (!speakResp.ok || speakPayload.code !== 0) throw new Error(speakPayload.msg || 'speak failed');
        addLog('计划片段已提交', {
          index: index + 1,
          action_id: actionId,
          text: segmentText
        });
        await waitMs(estimateSpeakMs(segmentText));
      }
      setStatus(motionPlanStopRef.current ? '动作计划已停止' : '动作计划讲课完成');
    } catch (error) {
      setStatus('动作计划执行失败');
      addLog('动作计划执行失败', { error: String(error) });
    } finally {
      setMotionPlanRunning(false);
      motionPlanStopRef.current = false;
      refreshMotionClips(activeSessionId);
    }
  }, [
    addLog,
    ensureAlphaSession,
    mode,
    motionPlan,
    normalized.live,
    prompts,
    refreshMotionClips,
    sessionId,
    voiceId
  ]);

  const updatePads = useCallback(async (nextPads) => {
    setPads(nextPads);
    try {
      const resp = await fetch(`${normalized.live}/alpha/tuning`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sessionid: sessionId || undefined,
          pads: [nextPads.top, nextPads.bottom, nextPads.left, nextPads.right]
        })
      });
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'tuning failed');
      applyTuningPayload(payload);
    } catch (error) {
      addLog('更新 pads 失败', { error: String(error) });
    }
  }, [addLog, applyTuningPayload, normalized.live, sessionId]);

  const setPadValue = useCallback((key, value) => {
    const nextPads = { ...pads, [key]: Number.parseInt(value || '0', 10) };
    updatePads(nextPads);
  }, [pads, updatePads]);

  const createSession = async () => {
    try {
      const resp = await fetch(`${normalized.live}/alpha/session`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reuse: true })
      });
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'session failed');
      setSessionId(String(payload.data.sessionid));
      addLog('alpha session ready', { sessionid: payload.data.sessionid });
      syncTuning(String(payload.data.sessionid));
      refreshMotionClips(String(payload.data.sessionid));
      refreshIdleClips(String(payload.data.sessionid));
    } catch (error) {
      addLog('创建 alpha session 失败', { error: String(error) });
    }
  };

  useEffect(() => {
    if (!DEFAULTS.alphaAutoConnect || alphaAutoStartedRef.current) return;
    alphaAutoStartedRef.current = true;
    createSession();
    connectVideo();
  }, [connectVideo]);

  const speakViaLiveTalking = async () => {
    setStatus('speaking');
    try {
      const resp = await fetch(`${normalized.live}/alpha/speak`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text,
          type: 'echo',
          interrupt: true,
          sessionid: sessionId || undefined,
          tts: {
            voice_id: voiceId,
            prompts,
            mode
          }
        })
      });
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'speak failed');
      if (payload?.data?.sessionid) setSessionId(String(payload.data.sessionid));
      setStatus('speaking');
      addLog('/alpha/speak 已提交', payload.data || {});
    } catch (error) {
      setStatus('error');
      addLog('/alpha/speak 失败', { error: String(error) });
    }
  };

  const pushViaTask = async () => {
    setStatus('task');
    try {
      const taskId = `test-${Date.now()}`;
      const resp = await fetch(`${normalized.tts}/tts/task/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          task_id: taskId,
          text,
          voice_id: voiceId,
          prompts,
          mode,
          target_hardware: alphaInputWs
        })
      });
      const payload = await resp.json();
      if (!resp.ok || payload.error) throw new Error(payload.message || JSON.stringify(payload));
      addLog('TTS task 已启动', { taskId });
    } catch (error) {
      setStatus('error');
      addLog('TTS task 失败', { error: String(error) });
    }
  };

  const interrupt = async () => {
    motionPlanStopRef.current = true;
    setMotionPlanRunning(false);
    try {
      await fetch(`${normalized.live}/interrupt_talk`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionid: sessionId })
      });
      setStatus('interrupted');
      addLog('已发送打断');
    } catch (error) {
      addLog('打断失败', { error: String(error) });
    }
  };

  const chooseSlides = useCallback(() => {
    slideInputRef.current?.click();
  }, []);

  const loadSlideImages = useCallback((event) => {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    if (files.length === 0) return;
    setSlideItems((current) => {
      current.forEach((item) => URL.revokeObjectURL(item.url));
      return files
        .filter((file) => file.type.startsWith('image/'))
        .sort((left, right) => left.name.localeCompare(right.name, 'zh-CN', { numeric: true }))
        .map((file, index) => ({
          id: `${file.name}_${file.lastModified}_${index}`,
          name: file.name,
          url: URL.createObjectURL(file)
        }));
    });
    setSlideIndex(0);
    setClassroomMode(true);
    addLog('幻灯片图片已加载', { count: files.length });
  }, [addLog]);

  useEffect(() => () => {
    slideItems.forEach((item) => URL.revokeObjectURL(item.url));
  }, [slideItems]);

  const prevSlide = useCallback(() => {
    setSlideIndex((index) => Math.max(0, index - 1));
  }, []);

  const nextSlide = useCallback(() => {
    setSlideIndex((index) => Math.min(slideItems.length - 1, index + 1));
  }, [slideItems.length]);

  useEffect(() => {
    const handleKeyDown = (event) => {
      if (!classroomMode) return;
      if (event.key === 'ArrowLeft') prevSlide();
      if (event.key === 'ArrowRight') nextSlide();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [classroomMode, nextSlide, prevSlide]);

  return (
    <main className="app">
      <section className={`workspace ${classroomMode ? 'workspaceClassroom' : ''}`}>
        <div className="panel controls">
          <div className="title">
            <Activity size={18} />
            <span>RobotTTS 流式测试</span>
          </div>

          <label>
            LiveTalking
            <input value={liveTalkingUrl} onChange={(event) => setLiveTalkingUrl(event.target.value)} />
          </label>
          <label>
            TTS Server
            <input value={ttsServerUrl} onChange={(event) => setTtsServerUrl(event.target.value)} />
          </label>
          <label>
            Alpha Audio Input
            <input value={alphaInputWs} onChange={(event) => setAlphaInputWs(event.target.value)} />
          </label>

          <div className="grid2">
            <label>
              Voice
              <select value={voiceId} onChange={(event) => setVoiceId(Number.parseInt(event.target.value, 10))}>
                {voices.length === 0 && <option value={voiceId}>voice {voiceId}</option>}
                {voices.map((voice) => (
                  <option value={voice.id} key={voice.id}>{voice.id} {voice.name}</option>
                ))}
              </select>
            </label>
            <label>
              Mode
              <select value={mode} onChange={(event) => setMode(event.target.value)}>
                <option value="instruct2">instruct2</option>
                <option value="zero-shot">zero-shot</option>
              </select>
            </label>
          </div>

          <div className="grid2">
            <label>
              Max Height
              <input
                type="number"
                min="0"
                step="1"
                value={videoMaxHeight}
                onChange={(event) => setVideoMaxHeight(Number.parseInt(event.target.value || '0', 10))}
              />
            </label>
            <label>
              Video FPS
              <input
                type="number"
                min="0"
                step="1"
                value={videoPreviewFps}
                onChange={(event) => setVideoPreviewFps(Number.parseFloat(event.target.value || '0'))}
              />
            </label>
          </div>
          <label>
            Render Interval
            <input
              type="number"
              min="16"
              step="1"
              value={videoRenderIntervalMs}
              onChange={(event) => setVideoRenderIntervalMs(Math.max(16, Number.parseInt(event.target.value || '40', 10)))}
            />
          </label>

          <div className="tuningPanel">
            <div className="controlHeader">
              <span><SlidersHorizontal size={16} />贴回区域</span>
              <button type="button" className="iconButton" onClick={() => setShowOverlay((value) => !value)}>
                {showOverlay ? <Eye size={15} /> : <EyeOff size={15} />}
              </button>
            </div>
            <div className="padSummary">
              <span>当前使用：{formatPads(pads)}</span>
              <span>生成 avatar 时：{formatPads(generationPads)}</span>
              <strong>贴回差值：{formatPads(pasteDeltaPads)}</strong>
            </div>
            <div className="padsGrid">
              {Object.entries(PAD_LABELS).map(([key, label]) => (
                <label className="padControl" key={key}>
                  <span>{label} {pads[key]}</span>
                  <input
                    type="range"
                    min="-300"
                    max="300"
                    step="1"
                    value={pads[key]}
                    onChange={(event) => setPadValue(key, event.target.value)}
                  />
                </label>
              ))}
            </div>
            <div className="controlRow">
              <button type="button" onClick={() => updatePads({ top: 0, bottom: 0, left: 0, right: 0 })}>
                <RotateCcw size={15} />重置
              </button>
              <button type="button" onClick={() => syncTuning()}>
                <Cable size={15} />同步
              </button>
            </div>
          </div>

          <div className="tuningPanel">
            <div className="controlHeader">
              <span><SlidersHorizontal size={16} />说话动作</span>
              <div className="controlActions">
                <button type="button" onClick={() => refreshMotionClips(sessionId, { reload: true })}>
                  <Cable size={15} />刷新
                </button>
                <a className="buttonLink" href="/motion.html" target="_blank" rel="noreferrer">
                  <Play size={15} />制作
                </a>
              </div>
            </div>
            <label>
              当前动作片段
              <select value={selectedMotion} onChange={(event) => selectMotionClip(event.target.value)}>
                <option value="">默认动作</option>
                {motionClips.map((clip) => (
                  <option value={clip.action_id} key={clip.action_id}>
                    {clip.display_name || clip.action_id} ({clip.frame_count || 0})
                  </option>
                ))}
              </select>
            </label>
            <div className="clipList">
              {motionClips.length === 0 && <span className="clipEmpty">暂无动作片段</span>}
              {motionClips.map((clip) => (
                <button
                  type="button"
                  className={`clipPill ${clip.current ? 'clipPillActive' : ''}`}
                  key={clip.action_id}
                  onClick={() => selectMotionClip(clip.action_id)}
                >
                  {clip.display_name || clip.action_id}
                </button>
              ))}
            </div>
          </div>

          <div className="tuningPanel">
            <div className="controlHeader">
              <span><SlidersHorizontal size={16} />静息动作</span>
              <div className="controlActions">
                <button type="button" onClick={() => refreshIdleClips(sessionId, { reload: true })}>
                  <Cable size={15} />刷新
                </button>
                <a className="buttonLink" href="/motion.html?kind=idle" target="_blank" rel="noreferrer">
                  <Play size={15} />制作
                </a>
              </div>
            </div>
            <label>
              当前静息片段
              <select value={selectedIdleMotion} onChange={(event) => selectIdleClip(event.target.value)}>
                <option value="">固定第一帧</option>
                {idleClips.map((clip) => (
                  <option value={clip.action_id} key={clip.action_id}>
                    {clip.display_name || clip.action_id} ({clip.frame_count || 0})
                  </option>
                ))}
              </select>
            </label>
            <div className="clipList">
              {idleClips.length === 0 && <span className="clipEmpty">暂无静息动作片段</span>}
              {idleClips.map((clip) => (
                <button
                  type="button"
                  className={`clipPill ${clip.current ? 'clipPillActive' : ''}`}
                  key={clip.action_id}
                  onClick={() => selectIdleClip(clip.action_id)}
                >
                  {clip.display_name || clip.action_id}
                </button>
              ))}
            </div>
          </div>

          <label>
            清晰度 {sharpness}
            <input
              type="range"
              min="0"
              max="100"
              step="1"
              value={sharpness}
              onChange={(event) => setSharpness(Number.parseInt(event.target.value || '0', 10))}
            />
          </label>

          <div className="tuningPanel">
            <div className="controlHeader">
              <span><Presentation size={16} />课堂模式</span>
              <button type="button" onClick={() => setClassroomMode((value) => !value)}>
                {classroomMode ? '退出' : '开启'}
              </button>
            </div>
            <input
              ref={slideInputRef}
              className="hiddenFileInput"
              type="file"
              accept="image/*"
              multiple
              onChange={loadSlideImages}
            />
            <button type="button" onClick={chooseSlides}>
              <ImageUp size={16} />选择幻灯片图片
            </button>
            <div className="slideControls">
              <button type="button" onClick={prevSlide} disabled={slideItems.length === 0 || slideIndex <= 0}>
                <ChevronLeft size={16} />上一页
              </button>
              <span>{slideItems.length ? `${slideIndex + 1} / ${slideItems.length}` : '未加载'}</span>
              <button type="button" onClick={nextSlide} disabled={slideItems.length === 0 || slideIndex >= slideItems.length - 1}>
                下一页<ChevronRight size={16} />
              </button>
            </div>
            <label>
              数字人大小 {avatarSize}%
              <input
                type="range"
                min="14"
                max="42"
                step="1"
                value={avatarSize}
                onChange={(event) => setAvatarSize(Number.parseInt(event.target.value || '24', 10))}
              />
            </label>
          </div>

          <label>
            Prompts
            <input value={prompts} onChange={(event) => setPrompts(event.target.value)} />
          </label>
          <label>
            Text
            <textarea value={text} onChange={(event) => setText(event.target.value)} />
          </label>

          <div className="tuningPanel motionPlanPanel">
            <div className="controlHeader">
              <span><SlidersHorizontal size={16} />动作编排</span>
              {motionPlanProvider && <em>{motionPlanProvider === 'llm' ? '大模型' : '规则兜底'}</em>}
            </div>
            <div className="buttons">
              <button type="button" onClick={planMotions}>
                <Send size={16} />编排动作
              </button>
              <button type="button" onClick={speakPlannedMotions} disabled={motionPlanRunning || motionPlan.length === 0}>
                <Play size={16} />{motionPlanRunning ? '执行中' : '按计划讲课'}
              </button>
            </div>
            <div className="motionPlanList">
              {motionPlan.length === 0 && <span className="clipEmpty">暂无动作计划</span>}
              {motionPlan.map((item, index) => (
                <div className="motionPlanItem" key={`${item.action_id || 'action'}_${index}`}>
                  <strong>{index + 1}. {item.display_name || item.action_id || '默认动作'}</strong>
                  <span>{item.action_id || 'default'}</span>
                  <p>{item.text}</p>
                  {item.reason && <small>{item.reason}</small>}
                </div>
              ))}
            </div>
          </div>

          <div className="buttons">
            <button onClick={checkHealth}><Cable size={16} />检查</button>
            <button onClick={createSession}><RotateCcw size={16} />Session</button>
            <button onClick={connectVideo}><Video size={16} />视频</button>
            <button onClick={disconnectVideo}><Square size={16} />断开</button>
            <button onClick={connectAudio}><Volume2 size={16} />音频</button>
            <button onClick={disconnectAudio}><VolumeX size={16} />静音</button>
            <button onClick={speakViaLiveTalking}><Send size={16} />alpha/speak</button>
            <button onClick={pushViaTask}><Mic size={16} />TTS task</button>
            <button onClick={interrupt}><Square size={16} />打断</button>
          </div>
        </div>

        <div className={`panel videoPanel ${classroomMode ? 'classroomPanel' : ''}`}>
          <div className="videoHead">
            <span>{classroomMode ? '课堂展示' : 'Alpha Video'}</span>
            <span>{videoState} | {frameInfo.width}x{frameInfo.height} | #{frameInfo.seq} | {frameInfo.fps.toFixed(1)} fps</span>
          </div>
          <div
            className={`canvasWrap ${DEFAULTS.videoFit === 'native' ? 'canvasWrapNative' : ''} ${classroomMode ? 'classroomStage' : ''}`}
            ref={stageRef}
            style={classroomMode ? { '--avatar-size': `${avatarSize}%` } : undefined}
          >
            {classroomMode && (
              <div className="slideStage">
                {currentSlide ? (
                  <img src={currentSlide.url} alt={currentSlide.name} />
                ) : (
                  <div className="slidePlaceholder">
                    <Presentation size={44} />
                    <span>选择 PPT 导出的图片页后开始展示</span>
                  </div>
                )}
              </div>
            )}
            <canvas ref={canvasRef} style={canvasStyle} />
            {baseOverlayStyle && <div className="cropOverlay cropOverlayBase" style={baseOverlayStyle} />}
            {paddedOverlayStyle && <div className="cropOverlay cropOverlayActive" style={paddedOverlayStyle} />}
          </div>
        </div>

        <div className="panel logs">
          <div className="title">
            <Play size={18} />
            <span>
              Status: {status} {sessionId ? `| session ${sessionId}` : ''}
              {' '}| audio {audioState} {audioInfo.chunks ? `| ${audioInfo.chunks} chunks` : ''}
            </span>
          </div>
          <pre>{logs.join('\n')}</pre>
        </div>
      </section>
    </main>
  );
}

createRoot(document.getElementById('root')).render(<App />);
