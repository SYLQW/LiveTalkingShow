import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { ArrowLeft, RefreshCw, Video } from 'lucide-react';
import './styles.css';

const DEFAULT_LIVETALKING_URL = import.meta.env.VITE_LIVETALKING_URL || 'http://127.0.0.1:8050';
const URL_PARAMS = new URLSearchParams(window.location.search);
const LIVE_URL = URL_PARAMS.get('live') || DEFAULT_LIVETALKING_URL;

function formatSize(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function shortId(value) {
  const text = String(value || '');
  return text.length > 14 ? `${text.slice(0, 8)}...${text.slice(-6)}` : text;
}

function RenderLibraryApp() {
  const [items, setItems] = useState([]);
  const [selectedId, setSelectedId] = useState(URL_PARAMS.get('task_id') || '');
  const [status, setStatus] = useState('加载中');
  const [error, setError] = useState('');

  const selectedItem = useMemo(() => (
    items.find((item) => item.task_id === selectedId) || items[0] || null
  ), [items, selectedId]);

  const loadLibrary = useCallback(async () => {
    setStatus('加载中');
    setError('');
    try {
      const resp = await fetch(`${LIVE_URL}/render/library?limit=200`);
      const payload = await resp.json();
      if (!resp.ok || payload.code !== 0) throw new Error(payload.msg || 'render library failed');
      const nextItems = payload.data?.items || [];
      setItems(nextItems);
      if (!selectedId && nextItems[0]?.task_id) setSelectedId(nextItems[0].task_id);
      setStatus(nextItems.length ? `共 ${nextItems.length} 个视频` : '暂无视频');
    } catch (err) {
      setError(String(err));
      setStatus('加载失败');
    }
  }, [selectedId]);

  useEffect(() => {
    loadLibrary();
  }, [loadLibrary]);

  return (
    <div className="app renderLibraryApp">
      <header className="libraryHeader">
        <div>
          <a className="buttonLink" href="/">
            <ArrowLeft size={16} />返回主页
          </a>
          <h1>生成视频库</h1>
          <span>{status}</span>
        </div>
        <button type="button" onClick={loadLibrary}>
          <RefreshCw size={16} />刷新
        </button>
      </header>

      {error && <div className="libraryError">{error}</div>}

      <main className="renderLibraryGrid">
        <section className="panel libraryPlayer">
          {selectedItem ? (
            <>
              <video
                controls
                src={`${LIVE_URL}${selectedItem.output_url}?t=${selectedItem.updated_at || Date.now()}`}
              />
              <div className="libraryMeta">
                <strong>{selectedItem.name || selectedItem.task_id}</strong>
                <span>{selectedItem.updated_at}</span>
                <code>{selectedItem.output}</code>
              </div>
            </>
          ) : (
            <div className="libraryEmpty">
              <Video size={24} />
              <span>还没有生成过视频</span>
            </div>
          )}
        </section>

        <section className="libraryList">
          {items.map((item) => (
            <button
              type="button"
              className={`libraryCard ${item.task_id === selectedItem?.task_id ? 'libraryCardActive' : ''}`}
              key={item.task_id}
              onClick={() => setSelectedId(item.task_id)}
            >
              <video
                muted
                preload="metadata"
                src={`${LIVE_URL}${item.output_url}?t=${item.updated_at}`}
              />
              <span>{shortId(item.task_id)}</span>
              <small>{item.updated_at}</small>
              <small>{formatSize(item.size_bytes)}</small>
              {(item.lip_backend || item.enhance_mode) && (
                <em>{[item.lip_backend, item.enhance_mode].filter(Boolean).join(' / ')}</em>
              )}
            </button>
          ))}
        </section>
      </main>
    </div>
  );
}

createRoot(document.getElementById('root')).render(<RenderLibraryApp />);
