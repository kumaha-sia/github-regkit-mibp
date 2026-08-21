import React, { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api.js'

const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : '—')

export default function StatusPanel({ onGotoLogs, onGotoAccounts }) {
  const [state, setState] = useState(null)
  const [count, setCount] = useState(1)
  const [busy, setBusy] = useState(false)
  const timer = useRef(null)

  const refresh = useCallback(() => {
    api.get('/api/status').then(setState).catch(() => {})
  }, [])

  useEffect(() => {
    refresh()
    timer.current = setInterval(refresh, 2000)
    return () => clearInterval(timer.current)
  }, [refresh])

  const running = state?.running

  async function start() {
    setBusy(true)
    try {
      await api.post('/api/start', { count })
    } catch (e) {
      alert(e.message)
    } finally {
      setBusy(false)
    }
  }

  async function stop() {
    setBusy(true)
    try {
      await api.post('/api/stop')
    } catch (e) {
      alert(e.message)
    } finally {
      setBusy(false)
    }
  }

  const progress = state && state.target > 0
    ? Math.min(100, Math.round(((state.success + state.fail) / state.target) * 100))
    : 0

  return (
    <div style={styles.wrap}>
      {/* hero */}
      <div className="glass" style={styles.hero}>
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 12.5, color: 'var(--muted)', fontWeight: 600, letterSpacing: 0.4, marginBottom: 6 }}>
            GITHUB ACCOUNT REGISTRATION
          </div>
          <h1 style={{ fontSize: 26, fontWeight: 800, letterSpacing: -0.4 }}>
            {running ? 'Membuat akun…' : 'Siap mendaftar'}
          </h1>
          <p style={{ fontSize: 13, color: 'var(--muted)', marginTop: 8, lineHeight: 1.5 }}>
            Engine <b style={{ color: 'var(--accent)' }}>Camoufox</b> (Firefox anti-detect) ·
            Email activation via <b style={{ color: 'var(--accent)' }}>Litensi</b> ·
            Username otomatis dari nama email
          </p>
        </div>
        <div className={`badge ${running ? 'ok' : 'muted'}`} style={{ alignSelf: 'flex-start' }}>
          {running && <span className="pulse-dot" />}
          {running ? 'Running' : 'Idle'}
        </div>
      </div>

      {/* stats grid */}
      <div style={styles.grid}>
        <Stat label="Target" value={state?.target ?? 0} />
        <Stat label="Success" value={state?.success ?? 0} tone="ok" />
        <Stat label="Failed" value={state?.fail ?? 0} tone="bad" />
        <Stat label="Progress" value={`${progress}%`} />
        <Stat label="Started" value={fmtTime(state?.started_at)} small />
        <Stat label="Finished" value={fmtTime(state?.finished_at)} small />
      </div>

      {/* progress bar */}
      {running && (
        <div className="glass" style={{ padding: '14px 18px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 8 }}>
            <span style={{ color: 'var(--muted)', fontWeight: 600 }}>Progress</span>
            <span>{state?.success + state?.fail ?? 0} / {state?.target ?? 0}</span>
          </div>
          <div style={styles.progressTrack}>
            <div style={{ ...styles.progressFill, width: `${progress}%` }} />
          </div>
        </div>
      )}

      {state?.error && (
        <div className="glass" style={{ padding: '14px 18px', borderColor: 'rgba(255,107,107,0.4)' }}>
          <span style={{ color: 'var(--danger)', fontSize: 13 }}>{state.error}</span>
        </div>
      )}

      {/* controls */}
      <div className="glass" style={styles.controls}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 13, color: 'var(--muted)', fontWeight: 600 }}>Jumlah akun</span>
          <input
            type="number" min="1" max="1000" value={count}
            className="glass-input number"
            onChange={(e) => setCount(Math.max(1, Math.min(1000, Number(e.target.value) || 1)))}
            disabled={running}
          />
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          <button className="glass-btn primary" style={{ padding: '12px 34px', fontSize: 14 }} onClick={start} disabled={running || busy}>
            ▶ Start
          </button>
          <button className="glass-btn danger" style={{ padding: '12px 26px' }} onClick={stop} disabled={!running || busy}>
            ■ Stop
          </button>
          <button className="glass-btn" onClick={onGotoLogs}>Live Log →</button>
          <button className="glass-btn" onClick={onGotoAccounts}>Accounts →</button>
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value, tone, small }) {
  const color = tone === 'ok' ? 'var(--ok)' : tone === 'bad' ? 'var(--danger)' : 'var(--text)'
  return (
    <div className="glass" style={{ padding: small ? '14px 16px' : '18px 20px' }}>
      <div style={{ fontSize: small ? 14 : 24, fontWeight: 800, color, letterSpacing: -0.5 }}>{value}</div>
      <div style={{ fontSize: 11.5, color: 'var(--muted)', marginTop: 4, fontWeight: 600, letterSpacing: 0.3 }}>{label}</div>
    </div>
  )
}

const styles = {
  wrap: { display: 'flex', flexDirection: 'column', gap: 14, maxWidth: 980, width: '100%', margin: '0 auto' },
  hero: {
    padding: 26, display: 'flex', gap: 20, alignItems: 'flex-start',
    background: 'linear-gradient(135deg, rgba(0,173,181,0.14), rgba(57,62,70,0.42))',
  },
  grid: { display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: 12 },
  controls: { padding: 18, display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 14 },
  progressTrack: { height: 8, borderRadius: 99, background: 'rgba(34,40,49,0.7)', overflow: 'hidden' },
  progressFill: {
    height: '100%', borderRadius: 99,
    background: 'linear-gradient(90deg, #00adb5, #2dd4a7)',
    boxShadow: '0 0 12px rgba(0,173,181,0.5)',
    transition: 'width 0.5s cubic-bezier(0.4, 0, 0.2, 1)',
  },
}
