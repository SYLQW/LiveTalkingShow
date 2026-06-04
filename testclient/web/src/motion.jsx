import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import {
  ArrowLeft,
  Cable,
  CheckCircle2,
  Clock3,
  Trash2,
  Edit3,
  Film,
  FolderOpen,
  ListPlus,
  Play,
  RefreshCw,
  Save,
  Scissors,
  Video,
  X
} from 'lucide-react';
import './styles.css';

const DEFAULT_LIVETALKING_URL = import.meta.env.VITE_LIVETALKING_URL || 'http://127.0.0.1:8050';
const URL_PARAMS = new URLSearchParams(window.location.search);
const DEFAULTS = {
  liveTalkingUrl: URL_PARAMS.get('live') || DEFAULT_LIVETALKING_URL,
  sessionId: URL_PARAMS.get('sessionid') || '',
  avatarId: URL_PARAMS.get('avatar_id') || 'mute_teacher_motion_v1_pad01000',
  source: URL_PARAMS.get('source') || 'G:/数字人/数字人原型/视频素材/生成哑巴老师上课视频-优秀，但是动作不行.mp4',
  ffmpegPath: 'G:/ffmpeg/ffmpeg-8.1-essentials_build/bin/ffmpeg.exe'
};
const INITIAL_CLIP_KIND = URL_PARAMS.get('kind') === 'idle' ? 'idle' : 'speaking';
const PAD_FIELDS = [
  { key: 'top', label: '上' },
  { key: 'bottom', label: '下' },
  { key: 'left', label: '左' },
  { key: 'right', label: '右' }
];

function defaultDraftForKind(kind) {
  if (kind === 'idle') {
    return {
      actionId: 'idle_stand',
      displayName: '静息站立',
      tags: 'idle,teaching',
      bestFor: '等待学生回答、听讲、停顿'
    };
  }
  return {
    actionId: 'lecture_explain',
    displayName: '普通讲解',
    tags: 'speaking,teaching',
    bestFor: '讲解知识点'
  };
}

function parsePads(value) {
  const values = String(value || '')
    .replace(/，/g, ',')
    .split(',')
    .map((item) => Number.parseInt(item.trim() || '0', 10))
    .filter((item) => Number.isFinite(item));
  while (values.length < 4) values.push(0);
  return values.slice(0, 4);
}

function padsToText(values) {
  const pads = Array.isArray(values) ? values : [0, 10, 0, 0];
  return pads.slice(0, 4).map((value) => Number.parseInt(value || 0, 10)).join(',');
}

function padObjectFromText(value) {
  const [top, bottom, left, right] = parsePads(value);
  return { top, bottom, left, right };
}

function boxWithPads(baseBox, pads, width, height) {
  if (!baseBox || !width || !height) return null;
  const [top, bottom, left, right] = pads;
  return {
    x1: Math.max(0, Math.round(Number(baseBox.x1 || 0) - left)),
    y1: Math.max(0, Math.round(Number(baseBox.y1 || 0) - top)),
    x2: Math.min(width, Math.round(Number(baseBox.x2 || 0) + right)),
    y2: Math.min(height, Math.round(Number(baseBox.y2 || 0) + bottom))
  };
}

function boxStyle(box, width, height) {
  if (!box || !width || !height) return {};
  return {
    left: `${(box.x1 / width) * 100}%`,
    top: `${(box.y1 / height) * 100}%`,
    width: `${Math.max(1, ((box.x2 - box.x1) / width) * 100)}%`,
    height: `${Math.max(1, ((box.y2 - box.y1) / height) * 100)}%`
  };
}

function formatBox(box) {
  if (!box) return '-';
  return `x ${box.x1}-${box.x2}，y ${box.y1}-${box.y2}`;
}

function toNumber(value, fallback = 0) {
  const parsed = Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function formatTime(value) {
  const safeValue = Math.max(0, Number(value) || 0);
  const minutes = Math.floor(safeValue / 60);
  const seconds = safeValue - minutes * 60;
  return `${String(minutes).padStart(2, '0')}:${seconds.toFixed(2).padStart(5, '0')}`;
}

function clipDuration(start, end) {
  const value = Math.max(0, toNumber(end) - toNumber(start));
  return value.toFixed(2);
}

function tagsToText(value) {
  if (Array.isArray(value)) return value.join(', ');
  return String(value || '');
}

function buildVideoUrl(baseUrl, videoUrl) {
  if (!videoUrl) return '';
  if (/^https?:\/\//i.test(videoUrl)) return videoUrl;
  return `${baseUrl}${videoUrl.startsWith('/') ? '' : '/'}${videoUrl}`;
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 30000) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, {
      ...options,
      signal: controller.signal
    });
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function makeSegmentId() {
  return `segment_${Date.now()}_${Math.random().toString(16).slice(2, 8)}`;
}

function normalizeActionId(value) {
  return String(value || '').trim();
}

function makeUniqueActionId(baseValue, usedIds) {
  const base = normalizeActionId(baseValue) || 'lecture_action';
  if (!usedIds.has(base)) return base;
  for (let index = 2; index < 1000; index += 1) {
    const candidate = `${base}_${String(index).padStart(2, '0')}`;
    if (!usedIds.has(candidate)) return candidate;
  }
  return `${base}_${Date.now()}`;
}

function App() {
  const videoRef = useRef(null);
  const sourceFileRef = useRef(null);
  const [liveTalkingUrl, setLiveTalkingUrl] = useState(DEFAULTS.liveTalkingUrl);
  const [sessionId, setSessionId] = useState(DEFAULTS.sessionId);
  const [clips, setClips] = useState([]);
  const [busy, setBusy] = useState('');
  const [status, setStatus] = useState('等待加载视频');
  const [logs, setLogs] = useState([]);
  const [sourceInfo, setSourceInfo] = useState(null);
  const [videoSrc, setVideoSrc] = useState('');
  const [facePreview, setFacePreview] = useState(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [startMark, setStartMark] = useState(0);
  const [endMark, setEndMark] = useState(0);
  const [segments, setSegments] = useState([]);
  const [draft, setDraft] = useState(() => defaultDraftForKind(INITIAL_CLIP_KIND));
  const [editingClip, setEditingClip] = useState('');
  const [editForm, setEditForm] = useState({
    actionId: '',
    nextActionId: '',
    displayName: '',
    description: '',
    bestFor: '',
    tags: ''
  });
  const [settings, setSettings] = useState({
    clipKind: INITIAL_CLIP_KIND,
    avatarId: DEFAULTS.avatarId,
    source: DEFAULTS.source,
    fps: '30',
    maxFrames: '0',
    imgSize: '256',
    pads: '0,10,0,0',
    faceBatchSize: '8',
    chromaKey: true,
    useFfmpegCut: true,
    ffmpegPath: DEFAULTS.ffmpegPath
  });

  const normalized = useMemo(() => liveTalkingUrl.replace(/\/$/, ''), [liveTalkingUrl]);
  const duration = Number(sourceInfo?.duration || 0);
  const kindLabel = settings.clipKind === 'idle' ? '静息动作' : '说话动作';
  const existingActionIds = useMemo(
    () => new Set(clips.map((clip) => normalizeActionId(clip.action_id)).filter(Boolean)),
    [clips]
  );
  const queuedActionIds = useMemo(
    () => new Set(segments.map((segment) => normalizeActionId(segment.actionId)).filter(Boolean)),
    [segments]
  );
  const allUsedActionIds = useMemo(
    () => new Set([...existingActionIds, ...queuedActionIds]),
    [existingActionIds, queuedActionIds]
  );
  const currentPads = useMemo(() => parsePads(settings.pads), [settings.pads]);
  const currentPadObject = useMemo(() => padObjectFromText(settings.pads), [settings.pads]);
  const paddedPreviewBox = useMemo(
    () => boxWithPads(facePreview?.base_box, currentPads, facePreview?.width, facePreview?.height),
    [currentPads, facePreview]
  );
  const previewBaseStyle = useMemo(
    () => boxStyle(facePreview?.base_box, facePreview?.width, facePreview?.height),
    [facePreview]
  );
  const previewPaddedStyle = useMemo(
    () => boxStyle(paddedPreviewBox, facePreview?.width, facePreview?.height),
    [facePreview, paddedPreviewBox]
  );
  const draftActionId = normalizeActionId(draft.actionId);
  const draftConflict = draftActionId && allUsedActionIds.has(draftActionId);
  const hasSegmentConflict = useMemo(() => {
    const seen = new Set();
    for (const segment of segments) {
      const actionId = normalizeActionId(segment.actionId);
      if (!actionId) continue;
      if (existingActionIds.has(actionId) || seen.has(actionId)) return true;
      seen.add(actionId);
    }
    return false;
  }, [existingActionIds, segments]);

  const addLog = useCallback((message, data) => {
    const time = new Date().toLocaleTimeString();
    const suffix = data ? ` ${JSON.stringify(data)}` : '';
    setLogs((items) => [`[${time}] ${message}${suffix}`, ...items].slice(0, 80));
  }, []);

  const setSetting = useCallback((key, value) => {
    setSettings((current) => ({ ...current, [key]: value }));
  }, []);

  const setPadValue = useCallback((key, value) => {
    const next = { ...padObjectFromText(settings.pads), [key]: Number.parseInt(value || '0', 10) };
    setSetting('pads', padsToText([next.top, next.bottom, next.left, next.right]));
  }, [setSetting, settings.pads]);

  const setDraftField = useCallback((key, value) => {
    setDraft((current) => ({ ...current, [key]: value }));
  }, []);

  const setEditField = useCallback((key, value) => {
    setEditForm((current) => ({ ...current, [key]: value }));
  }, []);

  const beginEditClip = useCallback((clip) => {
    const actionId = String(clip.action_id || '');
    setEditingClip(actionId);
    setEditForm({
      actionId,
      nextActionId: actionId,
      displayName: clip.display_name || actionId,
      description: clip.description || '',
      bestFor: clip.best_for || '',
      tags: tagsToText(clip.tags)
    });
  }, []);

  const cancelEditClip = useCallback(() => {
    setEditingClip('');
    setEditForm({
      actionId: '',
      nextActionId: '',
      displayName: '',
      description: '',
      bestFor: '',
      tags: ''
    });
  }, []);

  const ensureSession = useCallback(async () => {
    const resp = await fetch(`${normalized}/alpha/session`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reuse: true })
    });
    const payload = await resp.json();
    if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'session failed');
    const nextSessionId = String(payload.data.sessionid || '');
    setSessionId(nextSessionId);
    return nextSessionId;
  }, [normalized]);

  const refreshClips = useCallback(async (targetSessionId = sessionId) => {
    try {
      const query = new URLSearchParams();
      if (targetSessionId) query.set('sessionid', targetSessionId);
      if (settings.avatarId.trim()) query.set('avatar_id', settings.avatarId.trim());
      query.set('kind', settings.clipKind);
      const suffix = query.toString();
      const resp = await fetch(`${normalized}/motion/clips${suffix ? `?${suffix}` : ''}`);
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'clips failed');
      setClips(Array.isArray(payload.data?.clips) ? payload.data.clips : []);
      addLog('素材库已刷新', payload.data || {});
    } catch (error) {
      addLog('素材库刷新失败', { error: String(error) });
    }
  }, [addLog, normalized, sessionId, settings.avatarId, settings.clipKind]);

  useEffect(() => {
    refreshClips();
  }, [refreshClips]);

  const saveClipMetadata = useCallback(async () => {
    if (!editForm.actionId.trim() || !editForm.nextActionId.trim()) {
      addLog('action_id 不能为空');
      return;
    }
    setBusy(`edit_${editForm.actionId}`);
    setStatus(`正在保存 ${editForm.actionId}`);
    try {
      const activeSessionId = sessionId || await ensureSession();
      const resp = await fetch(`${normalized}/motion/clips/update`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sessionid: activeSessionId,
          avatar_id: settings.avatarId.trim(),
          kind: settings.clipKind,
          action_id: editForm.actionId.trim(),
          next_action_id: editForm.nextActionId.trim(),
          display_name: editForm.displayName.trim(),
          description: editForm.description.trim(),
          best_for: editForm.bestFor.trim(),
          tags: editForm.tags
        })
      });
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'update failed');
      if (Array.isArray(payload.data?.clips)) {
        setClips(payload.data.clips);
      } else {
        await refreshClips(activeSessionId);
      }
      setStatus(`${editForm.nextActionId} 已保存`);
      addLog('素材信息已保存', payload.data?.metadata || {});
      cancelEditClip();
    } catch (error) {
      setStatus(`${editForm.actionId} 保存失败`);
      addLog('保存素材信息失败', { action_id: editForm.actionId, error: String(error) });
    } finally {
      setBusy('');
    }
  }, [addLog, cancelEditClip, editForm, ensureSession, normalized, refreshClips, sessionId, settings.avatarId, settings.clipKind]);

  const deleteClip = useCallback(async (clip) => {
    const actionId = normalizeActionId(clip?.action_id);
    if (!actionId) return;
    const displayName = clip?.display_name || actionId;
    const ok = window.confirm(`确定要删除素材“${displayName}”吗？\n\n删除后会把这个动作片段的文件夹一起删掉。`);
    if (!ok) return;

    setBusy(`delete_${actionId}`);
    setStatus(`正在删除 ${actionId}`);
    try {
      const activeSessionId = sessionId || await ensureSession();
      const resp = await fetch(`${normalized}/motion/clips/delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sessionid: activeSessionId,
          avatar_id: settings.avatarId.trim(),
          kind: settings.clipKind,
          action_id: actionId
        })
      });
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'delete failed');
      if (Array.isArray(payload.data?.clips)) {
        setClips(payload.data.clips);
      } else {
        await refreshClips(activeSessionId);
      }
      if (editingClip === actionId) cancelEditClip();
      setStatus(`${actionId} 已删除`);
      addLog('素材已删除', payload.data?.deleted || { action_id: actionId });
    } catch (error) {
      setStatus(`${actionId} 删除失败`);
      addLog('删除素材失败', { action_id: actionId, error: String(error) });
    } finally {
      setBusy('');
    }
  }, [
    addLog,
    cancelEditClip,
    editingClip,
    ensureSession,
    normalized,
    refreshClips,
    sessionId,
    settings.avatarId,
    settings.clipKind
  ]);

  const checkBackend = useCallback(async () => {
    setStatus('正在连接后端');
    try {
      const nextSessionId = await ensureSession();
      await refreshClips(nextSessionId);
      setStatus('后端已连接');
      addLog('后端连接正常', { sessionid: nextSessionId });
    } catch (error) {
      setStatus('后端连接失败');
      addLog('后端连接失败', { error: String(error) });
    }
  }, [addLog, ensureSession, refreshClips]);

  const loadSource = useCallback(async (sourceOverride = '') => {
    const source = String(sourceOverride || settings.source).trim();
    if (!source) {
      addLog('请先填写视频路径');
      return;
    }
    setBusy('probe');
    setStatus('正在加载视频');
    try {
      const resp = await fetch(`${normalized}/motion/source/probe`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source,
          ffmpeg_path: settings.ffmpegPath
        })
      });
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'probe failed');
      const data = payload.data || {};
      const src = `${buildVideoUrl(normalized, data.video_url)}&t=${Date.now()}`;
      setSourceInfo(data);
      setVideoSrc(src);
      setCurrentTime(0);
      setStartMark(0);
      setEndMark(Math.min(Number(data.duration || 0), 4));
      setStatus('视频已加载');
      addLog('视频已加载', {
        duration: data.duration,
        fps: data.fps,
        width: data.width,
        height: data.height
      });
    } catch (error) {
      setSourceInfo(null);
      setVideoSrc('');
      setStatus('视频加载失败');
      addLog('视频加载失败', { error: String(error) });
    } finally {
      setBusy('');
    }
  }, [addLog, normalized, settings.ffmpegPath, settings.source]);

  const chooseSourceFile = useCallback(() => {
    sourceFileRef.current?.click();
  }, []);

  const uploadSourceFile = useCallback(async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;

    setBusy('upload');
    setStatus('正在上传视频');
    try {
      const formData = new FormData();
      formData.append('file', file);
      const resp = await fetch(`${normalized}/motion/source/upload`, {
        method: 'POST',
        body: formData
      });
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'upload failed');
      const source = String(payload.data?.source || '');
      if (!source) throw new Error('upload source path missing');
      setSetting('source', source);
      addLog('视频已选择', {
        filename: payload.data?.filename || file.name,
        size: payload.data?.size
      });
      await loadSource(source);
    } catch (error) {
      setStatus('视频选择失败');
      addLog('视频选择失败', { error: String(error) });
    } finally {
      setBusy('');
    }
  }, [addLog, loadSource, normalized, setSetting]);

  const detectFacePreview = useCallback(async () => {
    const source = settings.source.trim();
    if (!source) {
      addLog('请先选择或填写视频路径');
      return;
    }
    setBusy('detect');
    setStatus('正在检测人脸框');
    try {
      const resp = await fetchWithTimeout(`${normalized}/motion/source/detect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source,
          pads: currentPads,
          time: startMark || 0,
          chroma_key: settings.chromaKey
        })
      }, 30000);
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'detect failed');
      setFacePreview(payload.data || null);
      setStatus('人脸框已检测');
      addLog('人脸框已检测', {
        base: payload.data?.base_box,
        padded: payload.data?.padded_box
      });
    } catch (error) {
      setFacePreview(null);
      setStatus('人脸框检测失败');
      addLog('人脸框检测失败', { error: String(error) });
    } finally {
      setBusy('');
    }
  }, [addLog, currentPads, normalized, settings.chromaKey, settings.source, startMark]);

  const seekTo = useCallback((value) => {
    const nextValue = Math.min(Math.max(0, toNumber(value)), duration || 0);
    setCurrentTime(nextValue);
    if (videoRef.current) {
      videoRef.current.currentTime = nextValue;
    }
  }, [duration]);

  const markStart = useCallback(() => {
    const nextStart = Number(currentTime.toFixed(2));
    setStartMark(nextStart);
    if (endMark <= nextStart) {
      setEndMark(Math.min(duration || nextStart + 1, nextStart + 3));
    }
  }, [currentTime, duration, endMark]);

  const markEnd = useCallback(() => {
    const nextEnd = Number(currentTime.toFixed(2));
    setEndMark(nextEnd);
    if (startMark >= nextEnd) {
      setStartMark(Math.max(0, nextEnd - 3));
    }
  }, [currentTime, startMark]);

  const addSegment = useCallback(() => {
    const actionId = normalizeActionId(draft.actionId);
    if (!sourceInfo) {
      addLog('请先加载视频');
      return;
    }
    if (!actionId) {
      addLog('请填写片段 id');
      return;
    }
    if (endMark <= startMark) {
      addLog('结束点需要大于开始点');
      return;
    }
    const usedIds = new Set([
      ...clips.map((clip) => normalizeActionId(clip.action_id)).filter(Boolean),
      ...segments.map((segment) => normalizeActionId(segment.actionId)).filter(Boolean)
    ]);
    const uniqueActionId = makeUniqueActionId(actionId, usedIds);
    if (uniqueActionId !== actionId) {
      addLog('片段 id 已存在，已自动换成新名字', {
        from: actionId,
        to: uniqueActionId
      });
    }
    const segment = {
      localId: makeSegmentId(),
      actionId: uniqueActionId,
      displayName: draft.displayName.trim() || uniqueActionId,
      start: Number(startMark.toFixed(2)),
      end: Number(endMark.toFixed(2)),
      tags: draft.tags,
      bestFor: draft.bestFor
    };
    setSegments((items) => [...items, segment]);
    usedIds.add(uniqueActionId);
    setDraft((current) => ({
      ...current,
      actionId: makeUniqueActionId(actionId, usedIds)
    }));
    addLog('片段已加入队列', segment);
  }, [addLog, clips, draft, endMark, segments, sourceInfo, startMark]);

  const updateSegment = useCallback((localId, key, value) => {
    setSegments((items) => items.map((item) => (
      item.localId === localId ? { ...item, [key]: value } : item
    )));
  }, []);

  const removeSegment = useCallback((localId) => {
    setSegments((items) => items.filter((item) => item.localId !== localId));
  }, []);

  const generateSegment = useCallback(async (segment) => {
    if (!settings.source.trim()) {
      addLog('请先填写视频路径');
      return;
    }
    if (!segment.actionId.trim()) {
      addLog('片段 id 不能为空');
      return;
    }
    if (toNumber(segment.end) <= toNumber(segment.start)) {
      addLog('片段结束点需要大于开始点', segment);
      return;
    }
    const actionId = normalizeActionId(segment.actionId);
    const sameQueuedCount = segments.filter((item) => normalizeActionId(item.actionId) === actionId).length;
    const isDraftSegment = segment.localId === 'draft';
    if (existingActionIds.has(actionId) || (!isDraftSegment && sameQueuedCount > 1)) {
      setStatus(`${actionId} 名字已存在`);
      addLog('片段 id 已存在，请先换一个名字', { action_id: actionId });
      return;
    }

    const busyKey = segment.localId || segment.actionId;
    setBusy(busyKey);
    setStatus(`正在生成 ${segment.actionId}`);
    try {
      const activeSessionId = sessionId || await ensureSession();
      const resp = await fetch(`${normalized}/motion/clips/create`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sessionid: activeSessionId,
          avatar_id: settings.avatarId.trim(),
          kind: settings.clipKind,
          source: settings.source.trim(),
          action_id: String(segment.actionId).trim(),
          display_name: String(segment.displayName || '').trim(),
          start: toNumber(segment.start),
          end: toNumber(segment.end),
          fps: toNumber(settings.fps, 30),
          max_frames: Number.parseInt(settings.maxFrames || '0', 10),
          img_size: Number.parseInt(settings.imgSize || '256', 10),
          pads: parsePads(settings.pads),
          face_det_batch_size: Number.parseInt(settings.faceBatchSize || '8', 10),
          tags: segment.tags || draft.tags,
          best_for: segment.bestFor || draft.bestFor,
          chroma_key: settings.chromaKey,
          use_ffmpeg_cut: settings.useFfmpegCut,
          ffmpeg_path: settings.ffmpegPath,
          overwrite: false
        })
      });
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'create failed');
      setStatus(`${segment.actionId} 已生成`);
      updateSegment(segment.localId, 'generated', true);
      addLog('片段已生成到素材库', payload.data?.metadata || {});
      if (Array.isArray(payload.data?.clips)) {
        setClips(payload.data.clips);
      } else {
        await refreshClips(activeSessionId);
      }
    } catch (error) {
      setStatus(`${segment.actionId} 生成失败`);
      addLog('生成失败', { action_id: segment.actionId, error: String(error) });
    } finally {
      setBusy('');
    }
  }, [addLog, draft.bestFor, draft.tags, ensureSession, existingActionIds, normalized, refreshClips, segments, sessionId, settings, updateSegment]);

  const generateDraft = useCallback(async () => {
    await generateSegment({
      localId: 'draft',
      actionId: draft.actionId,
      displayName: draft.displayName,
      start: startMark,
      end: endMark,
      tags: draft.tags,
      bestFor: draft.bestFor
    });
  }, [draft, endMark, generateSegment, startMark]);

  const generateAll = useCallback(async () => {
    for (const segment of segments) {
      await generateSegment(segment);
    }
  }, [generateSegment, segments]);

  const timelinePercent = duration ? Math.min(100, (currentTime / duration) * 100) : 0;
  const rangeLeft = duration ? Math.min(100, (startMark / duration) * 100) : 0;
  const rangeWidth = duration ? Math.max(0, ((endMark - startMark) / duration) * 100) : 0;

  return (
    <main className="app motionApp">
      <section className="motionShell">
        <div className="motionTop">
          <div className="title">
            <Scissors size={19} />
            <span>动作片段制作</span>
          </div>
          <a className="buttonLink" href="/">
            <ArrowLeft size={15} />返回测试页
          </a>
        </div>

        <div className="motionEditor">
          <section className="panel motionVideoPanel">
            <div className="sourceBar">
              <label>
                源视频路径
                <input
                  value={settings.source}
                  onChange={(event) => setSetting('source', event.target.value)}
                  placeholder="G:/数字人/数字人原型/视频素材/xxx.mp4"
                />
              </label>
              <input
                ref={sourceFileRef}
                className="hiddenFileInput"
                type="file"
                accept="video/*"
                onChange={uploadSourceFile}
              />
              <button type="button" onClick={chooseSourceFile} disabled={busy === 'upload' || busy === 'probe'}>
                <FolderOpen size={16} />{busy === 'upload' ? '上传中' : '选择视频'}
              </button>
              <button type="button" onClick={() => loadSource()} disabled={busy === 'probe'}>
                <Video size={16} />{busy === 'probe' ? '加载中' : '加载视频'}
              </button>
            </div>

            <div className="videoPreviewBox">
              {videoSrc ? (
                <video
                  ref={videoRef}
                  className="motionVideo"
                  src={videoSrc}
                  controls
                  onTimeUpdate={(event) => setCurrentTime(event.currentTarget.currentTime)}
                  onLoadedMetadata={(event) => {
                    if (!sourceInfo?.duration) {
                      setSourceInfo((current) => ({ ...(current || {}), duration: event.currentTarget.duration }));
                    }
                  }}
                />
              ) : (
                <div className="videoPlaceholder">
                  <Video size={28} />
                  <span>加载视频后在这里预览</span>
                </div>
              )}
            </div>

            <div className="timelinePanel">
              <div className="timeReadout">
                <span><Clock3 size={15} />当前 {formatTime(currentTime)}</span>
                <span>开始 {formatTime(startMark)}</span>
                <span>结束 {formatTime(endMark)}</span>
                <span>片段 {clipDuration(startMark, endMark)} 秒</span>
              </div>
              <div className="timelineTrack">
                <div className="timelineSelection" style={{ left: `${rangeLeft}%`, width: `${rangeWidth}%` }} />
                <div className="timelineCursor" style={{ left: `${timelinePercent}%` }} />
                <input
                  aria-label="视频进度"
                  type="range"
                  min="0"
                  max={duration || 0}
                  step="0.01"
                  value={Math.min(currentTime, duration || 0)}
                  onChange={(event) => seekTo(event.target.value)}
                  disabled={!duration}
                />
              </div>
              <div className="markButtons">
                <button type="button" onClick={markStart} disabled={!sourceInfo}>
                  <Scissors size={16} />设为开始点
                </button>
                <button type="button" onClick={markEnd} disabled={!sourceInfo}>
                  <Scissors size={16} />设为结束点
                </button>
                <button type="button" onClick={() => videoRef.current?.play()} disabled={!videoSrc}>
                  <Play size={16} />播放
                </button>
              </div>
            </div>

            <div className="sourceMeta">
              <span>{sourceInfo ? `${sourceInfo.width || 0} x ${sourceInfo.height || 0}` : '未加载'}</span>
              <span>{sourceInfo ? `${Number(sourceInfo.fps || 0).toFixed(2)} fps` : 'fps -'}</span>
              <span>{sourceInfo ? `${formatTime(sourceInfo.duration || 0)}` : '时长 -'}</span>
              <span>{sourceInfo ? `${sourceInfo.frame_count || 0} 帧` : '帧数 -'}</span>
            </div>
          </section>

          <aside className="panel motionWorkPanel">
            <div className="controlHeader">
              <span><ListPlus size={16} />当前片段</span>
              <button type="button" onClick={checkBackend}>
                <Cable size={15} />检查后端
              </button>
            </div>
            <label>
              片段类型
              <select
                value={settings.clipKind}
                onChange={(event) => {
                  const nextKind = event.target.value;
                  setSetting('clipKind', nextKind);
                  if (nextKind === 'idle') {
                    setDraft((current) => ({
                      ...current,
                      actionId: current.actionId.startsWith('idle_') ? current.actionId : 'idle_stand',
                      displayName: current.displayName === '普通讲解' ? '静息站立' : current.displayName,
                      tags: 'idle,teaching',
                      bestFor: current.bestFor === '讲解知识点' ? '等待学生回答、听讲、停顿' : current.bestFor
                    }));
                  } else {
                    setDraft((current) => ({
                      ...current,
                      actionId: current.actionId.startsWith('idle_') ? 'lecture_explain' : current.actionId,
                      displayName: current.displayName === '静息站立' ? '普通讲解' : current.displayName,
                      tags: 'speaking,teaching',
                      bestFor: current.bestFor === '等待学生回答、听讲、停顿' ? '讲解知识点' : current.bestFor
                    }));
                  }
                }}
              >
                <option value="speaking">说话动作</option>
                <option value="idle">静息动作</option>
              </select>
              <span className="fieldHint">
                {settings.clipKind === 'idle'
                  ? '静息动作会在数字人不说话的时候播放，建议动作幅度小一些。'
                  : '说话动作会在数字人说话的时候播放，同时会生成嘴型。'}
              </span>
            </label>
            <div className="grid2">
              <label>
                片段 id
                <input value={draft.actionId} onChange={(event) => setDraftField('actionId', event.target.value)} />
                {draftConflict && <span className="fieldHint dangerHint">这个 action_id 已经在素材库或者队列里了，加入队列时会自动改名，直接生成需要先换名。</span>}
              </label>
              <label>
                片段名称
                <input value={draft.displayName} onChange={(event) => setDraftField('displayName', event.target.value)} />
              </label>
            </div>
            <div className="grid2">
              <label>
                tags
                <input value={draft.tags} onChange={(event) => setDraftField('tags', event.target.value)} />
              </label>
              <label>
                适合场景
                <input value={draft.bestFor} onChange={(event) => setDraftField('bestFor', event.target.value)} />
              </label>
            </div>
            <div className="buttons">
              <button type="button" onClick={addSegment} disabled={!sourceInfo}>
                <ListPlus size={16} />加入队列
              </button>
              <button type="button" onClick={generateDraft} disabled={!sourceInfo || busy === 'draft' || !!draftConflict}>
                <Save size={16} />{busy === 'draft' ? '生成中' : '直接生成'}
              </button>
            </div>

            <details className="advancedBox">
              <summary>生成参数</summary>
              <div className="grid2">
                <label>
                  LiveTalking
                  <input value={liveTalkingUrl} onChange={(event) => setLiveTalkingUrl(event.target.value)} />
                </label>
                <label>
                  Session
                  <input value={sessionId} onChange={(event) => setSessionId(event.target.value)} />
                </label>
              </div>
              <label>
                Avatar
                <input value={settings.avatarId} onChange={(event) => setSetting('avatarId', event.target.value)} />
              </label>
              <div className="grid2">
                <label>
                  FPS
                  <input type="number" step="1" value={settings.fps} onChange={(event) => setSetting('fps', event.target.value)} />
                </label>
                <label>
                  img_size
                  <input type="number" step="1" value={settings.imgSize} onChange={(event) => setSetting('imgSize', event.target.value)} />
                </label>
              </div>
              <div className="grid2">
                <label>
                  最多帧数
                  <input type="number" step="1" min="0" value={settings.maxFrames} onChange={(event) => setSetting('maxFrames', event.target.value)} />
                </label>
                <label>
                  人脸批量
                  <input type="number" step="1" min="1" value={settings.faceBatchSize} onChange={(event) => setSetting('faceBatchSize', event.target.value)} />
                </label>
              </div>
              <label>
                pads
                <input value={settings.pads} onChange={(event) => setSetting('pads', event.target.value)} />
              </label>
              <div className="facePadTool">
                <div className="facePadHead">
                  <span>人脸框预览</span>
                  <button type="button" onClick={detectFacePreview} disabled={!settings.source.trim() || busy === 'detect'}>
                    <RefreshCw size={15} />{busy === 'detect' ? '检测中' : '检查人脸框'}
                  </button>
                </div>
                {facePreview?.image ? (
                  <div className="facePreviewGrid">
                    <div className="facePreviewStage">
                      <img src={facePreview.image} alt="首帧人脸框预览" />
                      <div className="faceBox faceBoxBase" style={previewBaseStyle} />
                      <div className="faceBox faceBoxPadded" style={previewPaddedStyle} />
                    </div>
                    <div className="facePadControls">
                      <div className="faceLegend">
                        <span><i className="legendBase" />检测框：{formatBox(facePreview.base_box)}</span>
                        <span><i className="legendPadded" />生成框：{formatBox(paddedPreviewBox)}</span>
                      </div>
                      <div className="padsGrid motionPadsGrid">
                        {PAD_FIELDS.map(({ key, label }) => (
                          <label key={key}>
                            <span>{label} {currentPadObject[key]}</span>
                            <input
                              type="range"
                              min="-220"
                              max="220"
                              step="1"
                              value={currentPadObject[key]}
                              onChange={(event) => setPadValue(key, event.target.value)}
                            />
                          </label>
                        ))}
                      </div>
                    </div>
                  </div>
                ) : (
                  <span className="fieldHint">点“检查人脸框”后，会用片段开始点附近的一帧显示检测框和生成框。</span>
                )}
              </div>
              <label>
                FFmpeg
                <input value={settings.ffmpegPath} onChange={(event) => setSetting('ffmpegPath', event.target.value)} />
              </label>
              <label className="checkboxLine">
                <input
                  type="checkbox"
                  checked={settings.chromaKey}
                  onChange={(event) => setSetting('chromaKey', event.target.checked)}
                />
                <span>扣绿色背景</span>
              </label>
              <label className="checkboxLine">
                <input
                  type="checkbox"
                  checked={settings.useFfmpegCut}
                  onChange={(event) => setSetting('useFfmpegCut', event.target.checked)}
                />
                <span>先截出片段再生成</span>
              </label>
            </details>
          </aside>
        </div>

        <div className="motionLists">
          <section className="panel queuePanel">
            <div className="controlHeader">
              <span><Film size={16} />待生成片段</span>
              <button type="button" onClick={generateAll} disabled={segments.length === 0 || !!busy || hasSegmentConflict}>
                <Save size={15} />全部生成
              </button>
            </div>
            <div className="segmentList">
              {segments.length === 0 && <span className="clipEmpty">还没有加入片段</span>}
              {segments.map((segment) => {
                const segmentActionId = normalizeActionId(segment.actionId);
                const repeatedInQueue = segments.filter((item) => normalizeActionId(item.actionId) === segmentActionId).length > 1;
                const segmentConflict = existingActionIds.has(segmentActionId) || repeatedInQueue;
                return (
                  <div className={`segmentCard${segmentConflict ? ' segmentCardWarning' : ''}`} key={segment.localId}>
                    <div className="segmentFields">
                      <input value={segment.actionId} onChange={(event) => updateSegment(segment.localId, 'actionId', event.target.value)} />
                      <input value={segment.displayName} onChange={(event) => updateSegment(segment.localId, 'displayName', event.target.value)} />
                      <input type="number" step="0.01" value={segment.start} onChange={(event) => updateSegment(segment.localId, 'start', event.target.value)} />
                      <input type="number" step="0.01" value={segment.end} onChange={(event) => updateSegment(segment.localId, 'end', event.target.value)} />
                    </div>
                    {segmentConflict && <span className="fieldHint dangerHint">这个 action_id 已经存在，请换一个名字再生成。</span>}
                    <div className="segmentActions">
                      <span>{formatTime(segment.start)} - {formatTime(segment.end)} / {clipDuration(segment.start, segment.end)} 秒</span>
                      {segment.generated && <span className="doneText"><CheckCircle2 size={14} />已生成</span>}
                      <button type="button" onClick={() => generateSegment(segment)} disabled={!!busy || segmentConflict}>
                        <Save size={15} />{busy === segment.localId ? '生成中' : '生成'}
                      </button>
                      <button type="button" onClick={() => removeSegment(segment.localId)} disabled={!!busy}>
                        删除
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </section>

          <section className="panel libraryPanel">
            <div className="controlHeader">
              <span><FolderOpen size={16} />{kindLabel}素材库</span>
              <button type="button" onClick={() => refreshClips()}>
                <RefreshCw size={15} />刷新
              </button>
            </div>
            <div className="clipCards">
              {clips.length === 0 && <span className="clipEmpty">暂无动作片段</span>}
              {clips.map((clip) => (
                <div className="clipCard" key={clip.action_id}>
                  <div className="clipCardHead">
                    <div>
                      <strong>{clip.display_name || clip.action_id}</strong>
                      <span>{clip.action_id}</span>
                    </div>
                    <div className="clipCardActions">
                      <button type="button" onClick={() => beginEditClip(clip)} disabled={!!busy}>
                        <Edit3 size={14} />编辑
                      </button>
                      <button
                        type="button"
                        className="dangerButton"
                        onClick={() => deleteClip(clip)}
                        disabled={!!busy}
                      >
                        <Trash2 size={14} />{busy === `delete_${clip.action_id}` ? '删除中' : '删除'}
                      </button>
                    </div>
                  </div>
                  <span>{clip.frame_count || 0} 帧 / {clip.fps || '-'} fps</span>
                  {clip.description && <span>{clip.description}</span>}
                  {clip.best_for && <span>适合：{clip.best_for}</span>}
                  {Array.isArray(clip.tags) && clip.tags.length > 0 && <span>标签：{clip.tags.join(', ')}</span>}
                  {clip.current && <em>当前使用</em>}
                  {editingClip === clip.action_id && (
                    <div className="clipEditBox">
                      <div className="grid2">
                        <label>
                          action_id
                          <input value={editForm.nextActionId} onChange={(event) => setEditField('nextActionId', event.target.value)} />
                        </label>
                        <label>
                          显示名称
                          <input value={editForm.displayName} onChange={(event) => setEditField('displayName', event.target.value)} />
                        </label>
                      </div>
                      <label>
                        说明
                        <textarea value={editForm.description} onChange={(event) => setEditField('description', event.target.value)} />
                      </label>
                      <div className="grid2">
                        <label>
                          适合场景
                          <input value={editForm.bestFor} onChange={(event) => setEditField('bestFor', event.target.value)} />
                        </label>
                        <label>
                          标签
                          <input value={editForm.tags} onChange={(event) => setEditField('tags', event.target.value)} />
                        </label>
                      </div>
                      <div className="buttons">
                        <button type="button" onClick={saveClipMetadata} disabled={busy === `edit_${editForm.actionId}`}>
                          <Save size={15} />{busy === `edit_${editForm.actionId}` ? '保存中' : '保存'}
                        </button>
                        <button type="button" onClick={cancelEditClip}>
                          <X size={15} />取消
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </section>

          <section className="panel logPanel">
            <div className="title motionStatus">
              <Video size={17} />
              <span>{status}</span>
            </div>
            <pre>{logs.join('\n')}</pre>
          </section>
        </div>
      </section>
    </main>
  );
}

function itemsSuffix(value) {
  return String(value).padStart(2, '0');
}

createRoot(document.getElementById('root')).render(<App />);
