import React, { useEffect, useRef, useState } from 'react'
import { api, subscribeLogs } from '../api.js'

export default function LogViewer() {
  const [lines, setLines] = useState([])
  const [follow, setFollow] = useState(true)
  const [live, setLive] = useState(false)
  const boxRef = useRef(null)

  useEffect(() => {
    let closed = false
    let unsubscribe = () => {}
    api.get('/api/logs/snapshot?limit=500').then((d) => {
      if (closed) return
      setLines(d.lines || [])
      setLive(true)
      const seq = d.seq || 0
      subscribeLogs(seq, (line) => {
        if (!closed) setLines((prev) => [...prev.slice(-1499), line])
      }).then((unsub) => { if (!closed) unsubscribe = unsub }).catch(() => {})
    }).catch(() => {})
    return () => { closed = true; unsubscribe(); setLive(false) }
  }, [])

  useEffect(() => {
    if (follow && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight
    }
  }, [lines, follow])

  const colorize = (line) => {
    if (line.includes('[+]')) return 'var(--ok)'
    if (line.includes('[-]') || line.includes('[!]')) return 'var(--danger)'
    if (line.includes('[*]')) return 'var(--accent)'
    return 'rgba(238,238,238,0.75)'
  }

  return (
    <div style={styles.wrap}>
      <div className="glass" style={styles.toolbar}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span className={`badge ${live ? 'ok' : 'muted'}`}>
            {live && <span className="pulse-dot" />} {live ? 'Streaming' : 'Connecting…'}
          </span>
          <span style={{ fontSize: 12, color: 'var(--muted)' }}>{lines.length} lines</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 12.5, color: 'var(--muted)', cursor: 'pointer' }}>
            <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />
            Auto-scroll
          </label>
          <button className="glass-btn" style={{ fontSize: 12, padding: '7px 14px' }} onClick={() => setLines([])}>
            Clear
          </button>
        </div>
      </div>

      <div className="glass" style={styles.terminal} ref={boxRef}>
        {lines.length === 0 && (
          <div style={{ color: 'var(--muted)', fontSize: 13, textAlign: 'center', marginTop: '30vh' }}>
            Belum ada log — jalankan job dari tab Status
          </div>
        )}
        {lines.map((l, i) => (
          <div key={i} style={{ ...styles.line, color: colorize(l) }}>{l}</div>
        ))}
      </div>
    </div>
  )
}

const styles = {
  wrap: { display: 'flex', flexDirection: 'column', gap: 12, height: '100%', maxWidth: 1100, width: '100%', margin: '0 auto' },
  toolbar: { padding: '12px 18px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexShrink: 0 },
  terminal: {
    flex: 1, overflowY: 'auto', padding: 16,
    fontFamily: "'SF Mono', 'Fira Code', Menlo, monospace",
    fontSize: 12.5, lineHeight: 1.75,
    background: 'rgba(34,40,49,0.55)',
  },
  line: { padding: '1px 0', whiteSpace: 'pre-wrap', wordBreak: 'break-all' },
}
