import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  Activity,
  Cable,
  ChevronLeft,
  ChevronRight,
  ImageUp,
  Maximize2,
  Minimize2,
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

function httpUrl(base, path) {
  const url = new URL(base);
  const target = new URL(path, url.origin);
  url.pathname = target.pathname;
  url.search = target.search;
  return url.toString();
}

const DEFAULT_LIVETALKING_URL = import.meta.env.VITE_LIVETALKING_URL || 'http://127.0.0.1:8050';
const URL_PARAMS = new URLSearchParams(window.location.search);

function paramOrEnv(paramName, envName, fallback) {
  return URL_PARAMS.get(paramName) ?? import.meta.env[envName] ?? fallback;
}

function normalizeAlphaOutput(value) {
  const mode = String(value || '').trim().toLowerCase();
  if (['webrtc-packed', 'packed-webrtc', 'packed', 'webrtc'].includes(mode)) return 'webrtc-packed';
  if (['ws', 'websocket', 'raw'].includes(mode)) return 'ws';
  return 'webrtc-packed';
}

function boolParamOrEnv(paramName, envName, fallback = false) {
  const value = paramOrEnv(paramName, envName, fallback ? '1' : '0');
  return !['0', 'false', 'no', 'off'].includes(String(value).trim().toLowerCase());
}

const DEFAULTS = {
  liveTalkingUrl: DEFAULT_LIVETALKING_URL,
  avatarId: paramOrEnv('avatar_id', 'VITE_AVATAR_ID', ''),
  ttsServerUrl: import.meta.env.VITE_TTS_SERVER_URL || 'http://127.0.0.1:8036',
  alphaInputWs: import.meta.env.VITE_ALPHA_INPUT_WS || wsUrl(DEFAULT_LIVETALKING_URL, '/alpha/input/audio'),
  text: import.meta.env.VITE_DEFAULT_TEXT || '这是一段 robottts 兼容接口流式测试。',
  prompts: import.meta.env.VITE_DEFAULT_PROMPTS || '请自然清晰地朗读。',
  voiceId: Number.parseInt(import.meta.env.VITE_DEFAULT_VOICE_ID || '0', 10),
  mode: import.meta.env.VITE_DEFAULT_MODE || 'instruct2',
  audioSampleRate: Number.parseInt(import.meta.env.VITE_ALPHA_AUDIO_SAMPLE_RATE || '16000', 10),
  alphaOutput: normalizeAlphaOutput(paramOrEnv('output', 'VITE_ALPHA_OUTPUT', 'webrtc-packed')),
  alphaPlayAudio: boolParamOrEnv('play_audio', 'VITE_ALPHA_PLAY_AUDIO', false),
  alphaRenderer: paramOrEnv('renderer', 'VITE_ALPHA_RENDERER', 'webgl').trim().toLowerCase(),
  alphaForceOpaque: boolParamOrEnv('force_opaque', 'VITE_ALPHA_FORCE_OPAQUE', false),
  videoMaxWidth: Number.parseInt(paramOrEnv('max_width', 'VITE_ALPHA_VIDEO_MAX_WIDTH', '0'), 10),
  videoMaxHeight: Number.parseInt(paramOrEnv('max_height', 'VITE_ALPHA_VIDEO_MAX_HEIGHT', '0'), 10),
  videoPreviewFps: Number.parseFloat(paramOrEnv('fps', 'VITE_ALPHA_VIDEO_FPS', '0')),
  videoFormat: paramOrEnv('format', 'VITE_ALPHA_VIDEO_FORMAT', 'bgra'),
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
  if (format === 1 || format === 5) {
    const pixels = new Uint8ClampedArray(packet, 24);
    if (pixels.byteLength !== width * height * 4) return null;
    return { width, height, seq, format, pixels, bgra: format === 5 };
  }
  if (format === 2 || format === 3 || format === 4) {
    return { width, height, seq, format, payload };
  }
  return null;
}

function bgraToRgba(bgra) {
  const rgba = new Uint8ClampedArray(bgra.byteLength);
  for (let i = 0; i < bgra.byteLength; i += 4) {
    rgba[i] = bgra[i + 2];
    rgba[i + 1] = bgra[i + 1];
    rgba[i + 2] = bgra[i];
    rgba[i + 3] = bgra[i + 3];
  }
  return rgba;
}

function waitIceComplete(peer) {
  if (peer.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve) => {
    const check = () => {
      if (peer.iceGatheringState === 'complete') {
        peer.removeEventListener('icegatheringstatechange', check);
        resolve();
      }
    };
    peer.addEventListener('icegatheringstatechange', check);
  });
}

function createShader(gl, type, source) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    throw new Error(gl.getShaderInfoLog(shader) || 'WebGL shader compile failed');
  }
  return shader;
}

function createProgram(gl, vertexSource, fragmentSource) {
  const program = gl.createProgram();
  gl.attachShader(program, createShader(gl, gl.VERTEX_SHADER, vertexSource));
  gl.attachShader(program, createShader(gl, gl.FRAGMENT_SHADER, fragmentSource));
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    throw new Error(gl.getProgramInfoLog(program) || 'WebGL program link failed');
  }
  gl.useProgram(program);
  return program;
}

function createPackedAlphaRenderer(canvas, options = {}) {
  if (options.renderer === '2d') {
    return createPackedCanvas2dRenderer(canvas, options);
  }
  try {
    return createPackedWebGLRenderer(canvas, options);
  } catch (error) {
    if (options.onFallback) options.onFallback(error);
    return createPackedCanvas2dRenderer(canvas, options);
  }
}

function createPackedWebGLRenderer(canvas, options = {}) {
  const gl = canvas.getContext('webgl', {
    alpha: true,
    antialias: false,
    depth: false,
    desynchronized: true,
    premultipliedAlpha: false,
    preserveDrawingBuffer: false,
    stencil: false
  });
  if (!gl) throw new Error('浏览器不支持 WebGL packed alpha 渲染');

  const program = createProgram(
    gl,
    `
      attribute vec2 aPosition;
      attribute vec2 aTexCoord;
      varying vec2 vTexCoord;
      void main() {
        gl_Position = vec4(aPosition, 0.0, 1.0);
        vTexCoord = aTexCoord;
      }
    `,
    `
      precision mediump float;
      uniform sampler2D uPacked;
      uniform float uForceOpaque;
      varying vec2 vTexCoord;
      void main() {
        vec2 colorCoord = vec2(vTexCoord.x * 0.5, vTexCoord.y);
        vec2 alphaCoord = vec2(0.5 + vTexCoord.x * 0.5, vTexCoord.y);
        vec4 color = texture2D(uPacked, colorCoord);
        float alpha = mix(texture2D(uPacked, alphaCoord).r, 1.0, uForceOpaque);
        gl_FragColor = vec4(color.rgb, alpha);
      }
    `
  );

  const vertices = new Float32Array([
    -1, -1, 0, 1,
     1, -1, 1, 1,
    -1,  1, 0, 0,
     1,  1, 1, 0
  ]);
  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW);

  const stride = 4 * Float32Array.BYTES_PER_ELEMENT;
  const position = gl.getAttribLocation(program, 'aPosition');
  gl.enableVertexAttribArray(position);
  gl.vertexAttribPointer(position, 2, gl.FLOAT, false, stride, 0);

  const texCoord = gl.getAttribLocation(program, 'aTexCoord');
  gl.enableVertexAttribArray(texCoord);
  gl.vertexAttribPointer(texCoord, 2, gl.FLOAT, false, stride, 2 * Float32Array.BYTES_PER_ELEMENT);

  const texture = gl.createTexture();
  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.useProgram(program);
  gl.uniform1i(gl.getUniformLocation(program, 'uPacked'), 0);
  gl.uniform1f(gl.getUniformLocation(program, 'uForceOpaque'), options.forceOpaque ? 1.0 : 0.0);
  gl.disable(gl.BLEND);

  return {
    draw(video, logicalWidth, logicalHeight) {
      if (canvas.width !== logicalWidth || canvas.height !== logicalHeight) {
        canvas.width = logicalWidth;
        canvas.height = logicalHeight;
        gl.viewport(0, 0, logicalWidth, logicalHeight);
      }
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }
  };
}

function createPackedCanvas2dRenderer(canvas, options = {}) {
  const outputCtx = canvas.getContext('2d', { alpha: true, desynchronized: true });
  if (!outputCtx) throw new Error('浏览器不支持 Canvas 2D alpha 渲染');
  const packedCanvas = document.createElement('canvas');
  const packedCtx = packedCanvas.getContext('2d', { alpha: false, desynchronized: true });
  if (!packedCtx) throw new Error('浏览器不支持 Canvas 2D packed 渲染');

  return {
    draw(video, logicalWidth, logicalHeight, packedWidth) {
      const sourcePackedWidth = packedWidth || logicalWidth * 2;
      if (canvas.width !== logicalWidth || canvas.height !== logicalHeight) {
        canvas.width = logicalWidth;
        canvas.height = logicalHeight;
      }
      if (packedCanvas.width !== sourcePackedWidth || packedCanvas.height !== logicalHeight) {
        packedCanvas.width = sourcePackedWidth;
        packedCanvas.height = logicalHeight;
      }

      packedCtx.drawImage(video, 0, 0, sourcePackedWidth, logicalHeight);
      const color = packedCtx.getImageData(0, 0, logicalWidth, logicalHeight).data;
      const alpha = options.forceOpaque
        ? null
        : packedCtx.getImageData(logicalWidth, 0, logicalWidth, logicalHeight).data;
      const output = outputCtx.createImageData(logicalWidth, logicalHeight);
      const target = output.data;
      for (let index = 0; index < target.length; index += 4) {
        target[index] = color[index];
        target[index + 1] = color[index + 1];
        target[index + 2] = color[index + 2];
        target[index + 3] = alpha ? alpha[index] : 255;
      }
      outputCtx.putImageData(output, 0, 0);
    }
  };
}

function App() {
  const [liveTalkingUrl, setLiveTalkingUrl] = useState(DEFAULTS.liveTalkingUrl);
  const [ttsServerUrl, setTtsServerUrl] = useState(DEFAULTS.ttsServerUrl);
  const [alphaInputWs, setAlphaInputWs] = useState(DEFAULTS.alphaInputWs);
  const [text, setText] = useState(DEFAULTS.text);
  const [prompts, setPrompts] = useState(DEFAULTS.prompts);
  const [voiceId, setVoiceId] = useState(DEFAULTS.voiceId);
  const [mode, setMode] = useState(DEFAULTS.mode);
  const [alphaOutput, setAlphaOutput] = useState(DEFAULTS.alphaOutput);
  const [videoMaxWidth, setVideoMaxWidth] = useState(DEFAULTS.videoMaxWidth);
  const [videoMaxHeight, setVideoMaxHeight] = useState(DEFAULTS.videoMaxHeight);
  const [videoPreviewFps, setVideoPreviewFps] = useState(DEFAULTS.videoPreviewFps);
  const [videoRenderIntervalMs, setVideoRenderIntervalMs] = useState(DEFAULTS.videoRenderIntervalMs);
  const [sharpness, setSharpness] = useState(DEFAULTS.sharpness);
  const [pads, setPads] = useState({ top: 0, bottom: 0, left: 0, right: 0 });
  const [generationPads, setGenerationPads] = useState({ top: 0, bottom: 0, left: 0, right: 0 });
  const [pasteDeltaPads, setPasteDeltaPads] = useState({ top: 0, bottom: 0, left: 0, right: 0 });
  const [motionClips, setMotionClips] = useState([]);
  const [selectedMotion, setSelectedMotion] = useState('');
  const [idleClips, setIdleClips] = useState([]);
  const [selectedIdleMotion, setSelectedIdleMotion] = useState('');
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
  const [avatarPosition, setAvatarPosition] = useState(22);
  const [avatarScale, setAvatarScale] = useState(100);
  const [classroomFullscreen, setClassroomFullscreen] = useState(false);
  const canvasRef = useRef(null);
  const stageRef = useRef(null);
  const slideInputRef = useRef(null);
  const socketRef = useRef(null);
  const audioSocketRef = useRef(null);
  const rtcPeerRef = useRef(null);
  const rtcAudioTrackRef = useRef(null);
  const rtcAudioElementRef = useRef(null);
  const packedVideoRef = useRef(null);
  const packedRendererRef = useRef(null);
  const packedRenderHandleRef = useRef(null);
  const packedRenderHandleTypeRef = useRef('');
  const packedConnectTokenRef = useRef(0);
  const playPackedAudioRef = useRef(DEFAULTS.alphaPlayAudio);
  const packedFirstFrameLoggedRef = useRef(false);
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
        `/alpha/ws?max_width=${videoMaxWidth}&max_height=${videoMaxHeight}&fps=${videoPreviewFps}&format=${DEFAULTS.videoFormat}&quality=${DEFAULTS.videoQuality}`
      ),
      packedOffer: httpUrl(live, '/alpha/webrtc/packed_offer'),
      audioWs: wsUrl(live, '/alpha/audio')
    };
  }, [liveTalkingUrl, ttsServerUrl, videoMaxHeight, videoMaxWidth, videoPreviewFps]);

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
    if (frame.format === 1 || frame.format === 5) {
      ctx.putImageData(
        new ImageData(frame.bgra ? bgraToRgba(frame.pixels) : frame.pixels, frame.width, frame.height),
        0,
        0
      );
    } else {
      const mime = frame.format === 2 ? 'image/jpeg' : frame.format === 3 ? 'image/png' : 'image/webp';
      const bitmap = await createImageBitmap(new Blob([frame.payload], { type: mime }));
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(bitmap, 0, 0, frame.width, frame.height);
      bitmap.close();
    }
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
  }, [addLog]);

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

  const stopPackedRenderLoop = useCallback(() => {
    const handle = packedRenderHandleRef.current;
    if (handle === null) return;
    const video = packedVideoRef.current;
    if (
      packedRenderHandleTypeRef.current === 'videoFrame' &&
      video &&
      typeof video.cancelVideoFrameCallback === 'function'
    ) {
      video.cancelVideoFrameCallback(handle);
    } else {
      cancelAnimationFrame(handle);
    }
    packedRenderHandleRef.current = null;
    packedRenderHandleTypeRef.current = '';
  }, []);

  const stopPackedAudioPlayback = useCallback(() => {
    const audio = rtcAudioElementRef.current;
    if (!audio) return;
    audio.pause();
    audio.srcObject = null;
    audio.remove();
    rtcAudioElementRef.current = null;
  }, []);

  const applyPackedAudioPlayback = useCallback(() => {
    const track = rtcAudioTrackRef.current;
    if (!track || alphaOutput !== 'webrtc-packed') return;
    if (!playPackedAudioRef.current) {
      stopPackedAudioPlayback();
      return;
    }
    let audio = rtcAudioElementRef.current;
    if (!audio) {
      audio = document.createElement('audio');
      audio.autoplay = true;
      audio.controls = false;
      audio.style.display = 'none';
      document.body.appendChild(audio);
      rtcAudioElementRef.current = audio;
    }
    audio.srcObject = new MediaStream([track]);
    audio.play()
      .then(() => {
        setAudioState('connected');
        addLog('packed WebRTC 音频已启用');
      })
      .catch((error) => {
        setAudioState('error');
        addLog('packed WebRTC 音频播放失败', { error: String(error) });
      });
  }, [addLog, alphaOutput, stopPackedAudioPlayback]);

  const stopPackedWebRTC = useCallback((invalidatePending = true) => {
    if (invalidatePending) packedConnectTokenRef.current += 1;
    stopPackedRenderLoop();
    const peer = rtcPeerRef.current;
    rtcPeerRef.current = null;
    if (peer) {
      peer.ontrack = null;
      peer.onconnectionstatechange = null;
      peer.oniceconnectionstatechange = null;
      for (const sender of peer.getSenders()) {
        sender.track?.stop();
      }
      for (const receiver of peer.getReceivers()) {
        receiver.track?.stop();
      }
      peer.close();
    }
    const video = packedVideoRef.current;
    if (video) {
      video.pause();
      video.srcObject = null;
      video.remove();
      packedVideoRef.current = null;
    }
    rtcAudioTrackRef.current = null;
    stopPackedAudioPlayback();
  }, [stopPackedAudioPlayback, stopPackedRenderLoop]);

  const drawPackedVideoFrame = useCallback(() => {
    const video = packedVideoRef.current;
    const canvas = canvasRef.current;
    if (!video || !canvas || video.readyState < 2) return;

    const packedWidth = video.videoWidth || 0;
    const height = video.videoHeight || 0;
    const width = Math.max(1, Math.floor(packedWidth / 2));
    if (!packedWidth || !height) return;

    const renderer = packedRendererRef.current || createPackedAlphaRenderer(canvas, {
      renderer: DEFAULTS.alphaRenderer,
      forceOpaque: DEFAULTS.alphaForceOpaque,
      onFallback: (error) => addLog('WebGL packed 渲染不可用，回退 Canvas 2D', { error: String(error) })
    });
    packedRendererRef.current = renderer;
    renderer.draw(video, width, height, packedWidth);
    window.requestAnimationFrame(updateCanvasBox);

    const stats = frameStatsRef.current;
    const now = performance.now();
    const elapsed = now - stats.lastAt;
    let fps = stats.fps;
    if (elapsed >= 1000) {
      fps = ((stats.lastSeq + 1) * 1000) / elapsed;
      stats.lastAt = now;
      stats.lastSeq = 0;
      stats.fps = fps;
    } else {
      stats.lastSeq += 1;
    }
    setFrameInfo((current) => ({
      width,
      height,
      seq: current.seq + 1,
      fps
    }));
    if (!packedFirstFrameLoggedRef.current) {
      packedFirstFrameLoggedRef.current = true;
      addLog('packed WebRTC 首帧已绘制', {
        renderer: DEFAULTS.alphaRenderer,
        force_opaque: DEFAULTS.alphaForceOpaque,
        packedWidth,
        width,
        height,
        videoReadyState: video.readyState
      });
    }
  }, [addLog, updateCanvasBox]);

  const schedulePackedRenderFrame = useCallback(() => {
    const video = packedVideoRef.current;
    if (!video || alphaOutput !== 'webrtc-packed') return;
    if (packedRenderHandleRef.current !== null) return;
    if (
      video.readyState >= 2 &&
      typeof video.requestVideoFrameCallback === 'function'
    ) {
      packedRenderHandleTypeRef.current = 'videoFrame';
      packedRenderHandleRef.current = video.requestVideoFrameCallback(() => {
        packedRenderHandleRef.current = null;
        drawPackedVideoFrame();
        schedulePackedRenderFrame();
      });
      return;
    }

    packedRenderHandleTypeRef.current = 'animationFrame';
    packedRenderHandleRef.current = requestAnimationFrame(() => {
      packedRenderHandleRef.current = null;
      drawPackedVideoFrame();
      schedulePackedRenderFrame();
    });
  }, [alphaOutput, drawPackedVideoFrame]);

  const attachPackedVideoTrack = useCallback((track) => {
    stopPackedRenderLoop();
    if (packedVideoRef.current) {
      packedVideoRef.current.pause();
      packedVideoRef.current.srcObject = null;
      packedVideoRef.current.remove();
    }
    const video = document.createElement('video');
    video.autoplay = true;
    video.playsInline = true;
    video.muted = true;
    video.style.display = 'none';
    packedFirstFrameLoggedRef.current = false;
    video.srcObject = new MediaStream([track]);
    document.body.appendChild(video);
    packedVideoRef.current = video;
    video.addEventListener('loadedmetadata', () => {
      const packedWidth = video.videoWidth || 0;
      const height = video.videoHeight || 0;
      const width = Math.max(1, Math.floor(packedWidth / 2));
      addLog('packed WebRTC 视频元数据', { packedWidth, width, height });
      setFrameInfo({ width, height, seq: 0, fps: 0 });
      schedulePackedRenderFrame();
    });
    video.play()
      .then(schedulePackedRenderFrame)
      .catch((error) => addLog('packed WebRTC 视频播放失败', { error: String(error) }));
  }, [addLog, schedulePackedRenderFrame, stopPackedRenderLoop]);

  const connectPackedWebRTC = useCallback(async () => {
    stopPackedWebRTC(false);
    const connectToken = packedConnectTokenRef.current + 1;
    packedConnectTokenRef.current = connectToken;
    socketRef.current?.close();
    socketRef.current = null;
    setVideoState('connecting');
    addLog('连接 packed WebRTC alpha 视频', {
      url: normalized.packedOffer,
      max_width: videoMaxWidth,
      max_height: videoMaxHeight,
      fps: videoPreviewFps
    });

    const peer = new RTCPeerConnection({
      sdpSemantics: 'unified-plan',
      bundlePolicy: 'max-bundle'
    });
    rtcPeerRef.current = peer;

    const isCurrentPeer = () => packedConnectTokenRef.current === connectToken && rtcPeerRef.current === peer;
    const closeStalePeer = () => {
      try {
        peer.close();
      } catch (_) {
        // Ignore stale peer cleanup errors.
      }
    };

    peer.addTransceiver('audio', { direction: 'recvonly' });
    peer.addTransceiver('video', { direction: 'recvonly' });

    peer.addEventListener('connectionstatechange', () => {
      if (!isCurrentPeer()) return;
      addLog('packed WebRTC 连接状态', { state: peer.connectionState });
      if (peer.connectionState === 'connected') setVideoState('connected');
      if (['failed', 'disconnected', 'closed'].includes(peer.connectionState)) setVideoState('disconnected');
    });
    peer.addEventListener('iceconnectionstatechange', () => {
      if (!isCurrentPeer()) return;
      addLog('packed WebRTC ICE 状态', { state: peer.iceConnectionState });
    });
    peer.addEventListener('track', (event) => {
      if (!isCurrentPeer()) return;
      addLog('packed WebRTC 收到 track', { kind: event.track.kind, id: event.track.id });
      if (event.track.kind === 'audio') {
        rtcAudioTrackRef.current = event.track;
        applyPackedAudioPlayback();
      } else {
        attachPackedVideoTrack(event.track);
      }
    });

    const offer = await peer.createOffer();
    if (!isCurrentPeer()) {
      closeStalePeer();
      return;
    }
    await peer.setLocalDescription(offer);
    if (!isCurrentPeer()) {
      closeStalePeer();
      return;
    }
    await waitIceComplete(peer);
    if (!isCurrentPeer()) {
      closeStalePeer();
      return;
    }

    const body = {
      sdp: peer.localDescription.sdp,
      type: peer.localDescription.type
    };
    if (videoMaxWidth > 0) body.max_width = videoMaxWidth;
    if (videoMaxHeight > 0) body.max_height = videoMaxHeight;
    if (videoPreviewFps > 0) body.fps = videoPreviewFps;

    const response = await fetch(normalized.packedOffer, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    const answer = await response.json();
    if (!isCurrentPeer()) {
      closeStalePeer();
      addLog('忽略过期 packed WebRTC 应答');
      return;
    }
    if (!response.ok || !answer.sdp) {
      throw new Error(answer.msg || `packed offer failed: ${response.status}`);
    }
    if (answer.sessionid) setSessionId(String(answer.sessionid));
    await peer.setRemoteDescription({ sdp: answer.sdp, type: answer.type });
    if (!isCurrentPeer()) {
      closeStalePeer();
      return;
    }
    addLog('packed WebRTC 已连接', {
      sessionid: answer.sessionid,
      tracks: answer.tracks
    });
  }, [
    addLog,
    applyPackedAudioPlayback,
    attachPackedVideoTrack,
    normalized.packedOffer,
    stopPackedWebRTC,
    videoMaxHeight,
    videoMaxWidth,
    videoPreviewFps
  ]);

  const connectVideo = useCallback(() => {
    if (alphaOutput === 'webrtc-packed') {
      connectPackedWebRTC().catch((error) => {
        setVideoState('error');
        addLog('packed WebRTC 连接失败', { error: String(error) });
      });
      return;
    }
    stopPackedWebRTC();
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
  }, [addLog, alphaOutput, connectPackedWebRTC, enqueueVideoFrame, normalized.alphaWs, stopPackedWebRTC]);

  const disconnectVideo = useCallback(() => {
    socketRef.current?.close();
    socketRef.current = null;
    stopPackedWebRTC();
    latestFramePacketRef.current = null;
    if (renderTimerRef.current) {
      clearTimeout(renderTimerRef.current);
      renderTimerRef.current = 0;
    }
    setVideoState('disconnected');
  }, [stopPackedWebRTC]);

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
    if (alphaOutput === 'webrtc-packed') {
      playPackedAudioRef.current = true;
      setAudioState(rtcAudioTrackRef.current ? 'connecting' : 'waiting');
      addLog('启用 packed WebRTC 音频播放');
      applyPackedAudioPlayback();
      return;
    }
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
  }, [addLog, alphaOutput, applyPackedAudioPlayback, disconnectAudio, normalized.audioWs, playAudioChunk]);

  const disconnectAnyAudio = useCallback(() => {
    playPackedAudioRef.current = false;
    stopPackedAudioPlayback();
    disconnectAudio();
  }, [disconnectAudio, stopPackedAudioPlayback]);

  useEffect(() => () => {
    disconnectVideo();
    disconnectAnyAudio();
  }, [disconnectAnyAudio, disconnectVideo]);

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

  const refreshVoices = useCallback(async () => {
    try {
      const resp = await fetch(`${normalized.tts}/tts/voices`);
      const payload = await resp.json();
      const nextVoices = Array.isArray(payload.voices) ? payload.voices : [];
      setVoices(nextVoices);
      if (nextVoices.length > 0 && !nextVoices.some((voice) => Number(voice.id) === Number(voiceId))) {
        setVoiceId(Number(nextVoices[0].id));
      }
    } catch (error) {
      addLog('加载音色失败', { error: String(error) });
    }
  }, [addLog, normalized.tts, voiceId]);

  useEffect(() => {
    refreshVoices();
  }, [refreshVoices]);

  const applyTuningPayload = useCallback((payload) => {
    if (!payload?.data) return;
    const data = payload.data;
    if (data.sessionid) setSessionId(String(data.sessionid));
    if (Array.isArray(data.pads) && data.pads.length >= 4) {
      setPads(padsFromArray(data.pads));
    }
    setGenerationPads(padsFromArray(data.generation_pads || data.baked_pads));
    setPasteDeltaPads(padsFromArray(data.paste_delta_pads));
  }, []);

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
      const query = new URLSearchParams({ kind: 'speaking' });
      if (DEFAULTS.avatarId) query.set('avatar_id', DEFAULTS.avatarId);
      if (targetSessionId) query.set('sessionid', targetSessionId);
      if (options.reload) query.set('reload', '1');
      const resp = await fetch(`${normalized.live}/motion/clips?${query.toString()}`);
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'motion clips failed');
      const clips = Array.isArray(payload.data?.clips) ? payload.data.clips : [];
      setMotionClips(clips);
      const poolSelected = clips.some((clip) => clip.pool_selected);
      const current = clips.find((clip) => clip.selected || clip.current);
      if (poolSelected) {
        setSelectedMotion('auto');
      } else if (current?.action_id) {
        setSelectedMotion(current.action_id);
      } else {
        setSelectedMotion('');
      }
      addLog('说话动作已刷新', { clips: clips.map((clip) => clip.action_id) });
    } catch (error) {
      addLog('刷新说话动作失败', { error: String(error) });
    }
  }, [addLog, normalized.live, sessionId]);

  const refreshIdleClips = useCallback(async (targetSessionId = sessionId, options = {}) => {
    try {
      const query = new URLSearchParams({ kind: 'idle' });
      if (DEFAULTS.avatarId) query.set('avatar_id', DEFAULTS.avatarId);
      if (targetSessionId) query.set('sessionid', targetSessionId);
      if (options.reload) query.set('reload', '1');
      const resp = await fetch(`${normalized.live}/motion/clips?${query.toString()}`);
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'idle clips failed');
      const clips = Array.isArray(payload.data?.clips) ? payload.data.clips : [];
      setIdleClips(clips);
      const poolSelected = clips.some((clip) => clip.pool_selected);
      const current = clips.find((clip) => clip.selected || clip.current);
      if (poolSelected) {
        setSelectedIdleMotion('auto');
      } else if (current?.action_id) {
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

  const createSession = async () => {
    if (alphaOutput === 'webrtc-packed') {
      if (sessionId && rtcPeerRef.current) {
        addLog('packed WebRTC session 已存在', { sessionid: sessionId });
        return;
      }
      addLog('packed WebRTC 模式由视频连接创建 session，请点击视频');
      return;
    }
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
    if (alphaOutput !== 'webrtc-packed') {
      createSession();
    }
    connectVideo();
  }, [alphaOutput, connectVideo]);

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

  const toggleClassroomFullscreen = useCallback(async () => {
    const target = stageRef.current;
    if (!target) return;
    try {
      if (document.fullscreenElement === target) {
        await document.exitFullscreen();
      } else {
        setClassroomMode(true);
        await target.requestFullscreen();
      }
    } catch (error) {
      addLog('切换课堂全屏失败', { error: String(error) });
    }
  }, [addLog]);

  useEffect(() => {
    const handleKeyDown = (event) => {
      if (!classroomMode) return;
      if (event.key === 'ArrowLeft') prevSlide();
      if (event.key === 'ArrowRight') nextSlide();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [classroomMode, nextSlide, prevSlide]);

  useEffect(() => {
    const handleFullscreenChange = () => {
      setClassroomFullscreen(document.fullscreenElement === stageRef.current);
    };
    document.addEventListener('fullscreenchange', handleFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', handleFullscreenChange);
  }, []);

  return (
    <main className="app">
      <section className={`workspace ${classroomMode ? 'workspaceClassroom' : ''}`}>
        <div className="panel controls">
          <div className="title">
            <Activity size={18} />
            <span>RobotTTS 流式测试</span>
          </div>

          <details className="tuningPanel collapsePanel">
            <summary>
              <span><SlidersHorizontal size={16} />高级设置</span>
            </summary>
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
                Alpha Output
                <select value={alphaOutput} onChange={(event) => setAlphaOutput(normalizeAlphaOutput(event.target.value))}>
                  <option value="webrtc-packed">webrtc-packed</option>
                  <option value="ws">ws</option>
                </select>
              </label>
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
                Max Width
                <input
                  type="number"
                  min="0"
                  step="1"
                  value={videoMaxWidth}
                  onChange={(event) => setVideoMaxWidth(Number.parseInt(event.target.value || '0', 10))}
                />
              </label>
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
          </details>

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
                <option value="auto">自动素材池</option>
                {motionClips.map((clip) => (
                  <option value={clip.action_id} key={clip.action_id}>
                    {clip.display_name || clip.action_id} ({clip.frame_count || 0})
                  </option>
                ))}
              </select>
            </label>
            <div className="clipList">
              {motionClips.length === 0 && <span className="clipEmpty">暂无动作片段</span>}
              {motionClips.length > 0 && (
                <button
                  type="button"
                  className={`clipPill ${selectedMotion === 'auto' ? 'clipPillActive' : ''}`}
                  onClick={() => selectMotionClip('auto')}
                >
                  自动素材池
                </button>
              )}
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
                <option value="">默认动作</option>
                <option value="auto">自动素材池</option>
                {idleClips.map((clip) => (
                  <option value={clip.action_id} key={clip.action_id}>
                    {clip.display_name || clip.action_id} ({clip.frame_count || 0})
                  </option>
                ))}
              </select>
            </label>
            <div className="clipList">
              {idleClips.length === 0 && <span className="clipEmpty">暂无静息动作片段</span>}
              {idleClips.length > 0 && (
                <button
                  type="button"
                  className={`clipPill ${selectedIdleMotion === 'auto' ? 'clipPillActive' : ''}`}
                  onClick={() => selectIdleClip('auto')}
                >
                  自动素材池
                </button>
              )}
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
              数字人位置 {avatarPosition}%
              <input
                type="range"
                min="-18"
                max="44"
                step="1"
                value={avatarPosition}
                onChange={(event) => setAvatarPosition(Number.parseInt(event.target.value || '22', 10))}
              />
            </label>
            <label>
              数字人缩放 {avatarScale}%
              <input
                type="range"
                min="60"
                max="180"
                step="5"
                value={avatarScale}
                onChange={(event) => setAvatarScale(Number.parseInt(event.target.value || '100', 10))}
              />
            </label>
            <span className="fieldHint">位置会以右下角为基准调整，数值越小越靠右，可以拖到负数让数字人伸出展示区；缩放只改变数字人大小。</span>
            <button type="button" onClick={toggleClassroomFullscreen}>
              {classroomFullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
              {classroomFullscreen ? '退出全屏' : '全屏播放'}
            </button>
          </div>

          <label>
            Prompts
            <input value={prompts} onChange={(event) => setPrompts(event.target.value)} />
          </label>
          <label>
            Text
            <textarea value={text} onChange={(event) => setText(event.target.value)} />
          </label>

          <details className="tuningPanel motionPlanPanel collapsePanel">
            <summary>
              <span><SlidersHorizontal size={16} />动作编排</span>
              {motionPlanProvider && <em>{motionPlanProvider === 'llm' ? '大模型' : '规则兜底'}</em>}
            </summary>
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
          </details>

          <div className="buttons">
            <button onClick={checkHealth}><Cable size={16} />检查</button>
            <button onClick={createSession}><RotateCcw size={16} />Session</button>
            <button onClick={connectVideo}><Video size={16} />视频</button>
            <button onClick={disconnectVideo}><Square size={16} />断开</button>
            <button onClick={connectAudio}><Volume2 size={16} />音频</button>
            <button onClick={disconnectAnyAudio}><VolumeX size={16} />静音</button>
            <button onClick={speakViaLiveTalking}><Send size={16} />alpha/speak</button>
            <button onClick={pushViaTask}><Mic size={16} />TTS task</button>
            <button onClick={interrupt}><Square size={16} />打断</button>
          </div>
        </div>

        <div className={`panel videoPanel ${classroomMode ? 'classroomPanel' : ''}`}>
          <div className="videoHead">
            <span>{classroomMode ? '课堂展示' : 'Alpha Video'}</span>
            <span>{alphaOutput} | {videoState} | {frameInfo.width}x{frameInfo.height} | #{frameInfo.seq} | {frameInfo.fps.toFixed(1)} fps</span>
          </div>
          <div
            className={`canvasWrap ${DEFAULTS.videoFit === 'native' ? 'canvasWrapNative' : ''} ${classroomMode ? 'classroomStage' : ''}`}
            ref={stageRef}
            style={classroomMode ? {
              '--avatar-position': `${avatarPosition}%`,
              '--avatar-scale': avatarScale / 100
            } : undefined}
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
