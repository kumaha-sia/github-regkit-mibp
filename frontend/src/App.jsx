import React, { useEffect, useState } from 'react'
import { api, getToken, setToken } from './api.js'
import StatusPanel from './components/StatusPanel.jsx'
import LogViewer from './components/LogViewer.jsx'
import ConfigPanel from './components/ConfigPanel.jsx'
import AccountsPanel from './components/AccountsPanel.jsx'

const NAV = [
  { id: 'status', label: 'Status', icon: '◐' },
  { id: 'log', label: 'Live Log', icon: '≡' },
  { id: 'config', label: 'Config', icon: '⚙' },
  { id: 'accounts', label: 'Accounts', icon: '⬇' },
]

export default function App() {
  const [auth, setAuth] = useState(null)
  const [tab, setTab] = useState('status')
  const [password, setPassword] = useState('')
  const [running, setRunning] = useState(false)

  useEffect(() => {
    api.get('/api/config').then((d) => setAuth({ needs: d.needs_auth })).catch(() => setAuth({ needs: true }))
  }, [])

  // running badge in the sidebar
  useEffect(() => {
    if (auth?.needs && !getToken()) return
    const t = setInterval(() => {
      api.get('/api/status').then((d) => setRunning(!!d.running)).catch(() => {})
    }, 2500)
    return () => clearInterval(t)
  }, [auth])

  async function doLogin() {
    try {
      const d = await api.post('/api/auth', { password })
      setToken(d.token)
      setAuth({ needs: d.needs_auth })
      setPassword('')
    } catch (e) {
      alert('Login gagal: ' + e.message)
    }
  }

  if (auth === null) {
    return (
      <div style={{ ...styles.center, zIndex: 1 }}>
        <div style={{ fontSize: 15, color: 'var(--muted)' }}>Loading…</div>
      </div>
    )
  }

  if (auth.needs && !getToken()) {
    return (
      <div style={{ ...styles.center, zIndex: 1 }} className="fade-in">
        <div className="glass" style={{ padding: 40, width: 380, textAlign: 'center' }}>
          <div style={{ fontSize: 40, marginBottom: 6 }}>🐙</div>
          <h1 style={{ fontSize: 21, fontWeight: 800, marginBottom: 4 }}>GitHub Register</h1>
          <p style={{ fontSize: 13, color: 'var(--muted)', marginBottom: 24 }}>
            Camoufox · Litensi · Liquid Glass
          </p>
          <input
            type="password"
            className="glass-input"
            placeholder="Access password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && doLogin()}
            style={{ textAlign: 'center', marginBottom: 14 }}
          />
          <button className="glass-btn primary" style={{ width: '100%' }} onClick={doLogin}>
            Masuk
          </button>
        </div>
      </div>
    )
  }

  return (
    <div style={styles.shell}>
      <aside className="glass" style={styles.sidebar}>
        <div style={styles.brand}>
          <div style={styles.logo}>🐙</div>
          <div>
            <div style={{ fontWeight: 800, fontSize: 14.5 }}>GitHub Register</div>
            <div style={{ fontSize: 11, color: 'var(--muted)' }}>Camoufox Engine</div>
          </div>
        </div>

        <nav style={styles.nav}>
          {NAV.map((n) => (
            <button
              key={n.id}
              className={tab === n.id ? 'seg-active' : ''}
              style={tab === n.id ? { ...styles.navBtn, ...styles.navBtnActive } : styles.navBtn}
              onClick={() => setTab(n.id)}
            >
              <span style={{ opacity: 0.85, fontSize: 15 }}>{n.icon}</span>
              {n.label}
            </button>
          ))}
        </nav>

        <div style={styles.sidebarFoot}>
          <div className={`badge ${running ? 'ok' : 'muted'}`} style={{ width: '100%', justifyContent: 'center' }}>
            {running && <span className="pulse-dot" />}
            {running ? 'Job Running' : 'Idle'}
          </div>
          {auth.needs && (
            <button
              className="glass-btn"
              style={{ width: '100%', fontSize: 12, padding: '8px 0' }}
              onClick={() => { setToken(''); window.location.reload() }}
            >
              Logout
            </button>
          )}
        </div>
      </aside>

      <main style={styles.main} key={tab} className="fade-in">
        {tab === 'status' && <StatusPanel onGotoLogs={() => setTab('log')} onGotoAccounts={() => setTab('accounts')} />}
        {tab === 'log' && <LogViewer />}
        {tab === 'config' && <ConfigPanel />}
        {tab === 'accounts' && <AccountsPanel />}
      </main>
    </div>
  )
}

const styles = {
  center: { minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' },
  shell: { display: 'flex', height: '100vh', position: 'relative', zIndex: 1 },
  sidebar: {
    width: 232, margin: 14, marginRight: 0, padding: 18,
    display: 'flex', flexDirection: 'column', gap: 18, flexShrink: 0,
  },
  brand: { display: 'flex', alignItems: 'center', gap: 12, padding: '4px 6px' },
  logo: {
    width: 38, height: 38, borderRadius: 12, fontSize: 20,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    background: 'linear-gradient(135deg, rgba(0,173,181,0.30), rgba(0,173,181,0.10))',
    border: '1px solid rgba(0,173,181,0.35)',
    boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.15)',
  },
  nav: { display: 'flex', flexDirection: 'column', gap: 4, flex: 1 },
  navBtn: {
    display: 'flex', alignItems: 'center', gap: 11,
    background: 'transparent', border: 'none', color: 'var(--muted)',
    fontFamily: 'inherit', fontSize: 13.5, fontWeight: 600,
    padding: '11px 14px', borderRadius: 12, cursor: 'pointer',
    transition: 'all 0.18s ease', textAlign: 'left',
  },
  navBtnActive: {
    color: 'var(--text)',
    background: 'rgba(0,173,181,0.16)',
    boxShadow: 'inset 0 1px 0 var(--glass-highlight), 0 2px 12px rgba(0,173,181,0.15)',
    border: '1px solid rgba(0,173,181,0.28)',
  },
  sidebarFoot: { display: 'flex', flexDirection: 'column', gap: 10 },
  main: { flex: 1, padding: 14, overflowY: 'auto', display: 'flex', flexDirection: 'column' },
}
