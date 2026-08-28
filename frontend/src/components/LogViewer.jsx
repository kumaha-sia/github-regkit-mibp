import React, { useEffect, useRef, useState, useCallback } from 'react'
import { ListEnd, Radio, Trash2, History, RefreshCw } from 'lucide-react'
import { api, subscribeLogs } from '../api.js'
import { Badge, Button, Card, EmptyState, Spinner } from './ui.jsx'

const HISTORY_PAGE = 500

export default function LogViewer() {
  const [tab, setTab] = useState('live')
  const [lines, setLines] = useState([])
  const [follow, setFollow] = useState(true)
  const [live, setLive] = useState(false)
  const [history, setHistory] = useState([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyJobId, setHistoryJobId] = useState(null)
  const [historyExhausted, setHistoryExhausted] = useState(false)
  const boxRef = useRef(null)

  // --- live SSE subscription ---
  useEffect(() => {
    if (tab !== 'live') return
    let closed = false; let unsubscribe = () => {}
    api.get('/api/logs/snapshot?limit=500').then((data) => {
      if (closed) return
      setLines(data.lines || []); setLive(true)
      subscribeLogs(data.seq || 0, (line) => !closed && setLines((prev) => [...prev.slice(-1499), line])).then((unsub) => { if (!closed) unsubscribe = unsub }).catch(() => {})
    }).catch(() => {})
    return () => { closed = true; unsubscribe(); setLive(false) }
  }, [tab])

  useEffect(() => {
    const el = boxRef.current
    if (!follow || !el || tab !== 'live') return
    el.scrollTop = el.scrollHeight
  }, [lines, follow, tab])

  // --- history fetch ---
  const loadHistory = useCallback(async (reset) => {
    setHistoryLoading(true)
    try {
      const after = reset ? 0 : (history.length > 0 ? history[history.length - 1].id : 0)
      const url = reset
        ? '/api/logs/history'
        : `/api/logs/history?after=${after}`
      const data = await api.get(url)
      if (reset) setHistory([])
      if (data.job_id) setHistoryJobId(data.job_id)
      if (data.events && data.events.length > 0) {
        setHistory((prev) => reset ? data.events : [...prev, ...data.events])
        setHistoryExhausted(data.events.length < HISTORY_PAGE)
      } else {
        setHistoryExhausted(true)
      }
    } catch {
      // ignore — history is best-effort
    } finally {
      setHistoryLoading(false)
    }
  }, [history])

  useEffect(() => {
    if (tab === 'history') loadHistory(true)
  }, [tab]) // eslint-disable-line react-hooks/exhaustive-deps

  const color = (line) => {
    const text = typeof line === 'string' ? line : (line.message || '')
    if (text.includes('[+]')) return 'success'
    if (text.includes('[-]') || text.includes('[!]')) return 'danger'
    if (text.includes('[*]')) return 'accent'
    return ''
  }

  const formatHistoryLine = (e) => `[${(e.ts || '').slice(11)}] ${e.message}`

  return (
    <div className="log-layout">
      <Card className="log-toolbar">
        <div className="log-toolbar-group">
          <div className="log-tabs">
            <button className={`log-tab ${tab === 'live' ? 'active' : ''}`} onClick={() => setTab('live')}>
              <Radio size={12} /> Live
            </button>
            <button className={`log-tab ${tab === 'history' ? 'active' : ''}`} onClick={() => setTab('history')}>
              <History size={12} /> History
            </button>
          </div>
          {tab === 'live'
            ? <Badge tone={live ? 'success' : 'muted'}><Radio size={12} />{live ? 'Streaming' : 'Connecting'}</Badge>
            : historyJobId
              ? <Badge tone="accent"><History size={12} /> Job #{historyJobId}</Badge>
              : <Badge tone="muted"><History size={12} /> No jobs</Badge>
          }
          <span>{tab === 'live' ? `${lines.length} lines` : `${history.length} events`}</span>
        </div>
        <div className="log-toolbar-group">
          {tab === 'live' ? (
            <>
              <label className="log-follow">
                <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> Auto-scroll
              </label>
              <Button size="sm" onClick={() => setLines([])}><Trash2 size={14} /> Clear</Button>
            </>
          ) : (
            <>
              <Button size="sm" onClick={() => loadHistory(true)} disabled={historyLoading}>
                {historyLoading ? <Spinner /> : <RefreshCw size={14} />} Refresh
              </Button>
              {!historyExhausted && history.length > 0 && (
                <Button size="sm" variant="outline" onClick={() => loadHistory(false)} disabled={historyLoading}>
                  Load more
                </Button>
              )}
            </>
          )}
        </div>
      </Card>
      <Card className="log-terminal" ref={boxRef}>
        {tab === 'live' ? (
          lines.length === 0
            ? <EmptyState icon={ListEnd} title="No logs yet" description="Start a job from the Status page to view events in real time." />
            : lines.map((line, index) => <div key={index} className={`log-line ${color(line)}`}>{line}</div>)
        ) : (
          history.length === 0
            ? <EmptyState icon={History} title="No history" description="Job events from previous runs are persisted in the database and will appear here." />
            : history.map((e) => <div key={e.id} className={`log-line ${color(e)}`}>{formatHistoryLine(e)}</div>)
        )}
      </Card>
    </div>
  )
}
