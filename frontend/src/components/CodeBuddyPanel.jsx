import React, { useEffect, useState, useRef } from 'react'
import { Bot, Play, Square, RefreshCw, CheckCircle2, XCircle, Loader } from 'lucide-react'
import { api } from '../api.js'
import { Badge, Button, Card, EmptyState } from './ui.jsx'

export default function CodeBuddyPanel() {
  const [status, setStatus] = useState(null)
  const [accounts, setAccounts] = useState([])
  const [available, setAvailable] = useState([])
  const [count, setCount] = useState(1)
  const [region, setRegion] = useState('')
  const [accountId, setAccountId] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const timer = useRef(null)

  // poll status every 3s when running
  useEffect(() => {
    loadStatus()
    loadAccounts()
    loadAvailable()
    return () => { if (timer.current) clearInterval(timer.current) }
  }, [])

  useEffect(() => {
    if (status?.running) {
      if (!timer.current) {
        timer.current = setInterval(() => {
          loadStatus()
          loadAccounts()
        }, 3000)
      }
    } else {
      if (timer.current) { clearInterval(timer.current); timer.current = null }
    }
  }, [status?.running])

  async function loadStatus() {
    try {
      const d = await api.get('/api/codebuddy/status')
      setStatus(d)
    } catch { /* ignore */ }
  }

  async function loadAccounts() {
    try {
      const d = await api.get('/api/codebuddy/accounts')
      setAccounts(d.accounts || [])
    } catch { /* ignore */ }
  }

  async function loadAvailable() {
    try {
      const d = await api.get('/api/codebuddy/available-accounts')
      setAvailable(d.accounts || [])
    } catch { /* ignore */ }
  }

  async function startJob() {
    setBusy(true); setMsg('')
    try {
      const body = { count, region: region || null }
      if (accountId) body.account_id = Number(accountId)
      await api.post('/api/codebuddy/start', body)
      setMsg('Job started')
      loadStatus()
    } catch (e) {
      setMsg('Error: ' + e.message)
    } finally { setBusy(false) }
  }

  async function stopJob() {
    setBusy(true)
    try {
      await api.post('/api/codebuddy/stop')
      setMsg('Stop requested')
      loadStatus()
    } catch (e) {
      setMsg('Error: ' + e.message)
    } finally { setBusy(false) }
  }

  const s = status || {}
  const elapsed = s.started_at ? Math.floor((Date.now() / 1000) - s.started_at) : 0

  return (
    <div style={styles.wrap}>
      {/* Job Control */}
      <Card style={styles.card}>
        <div style={styles.header}>
          <div style={styles.headerLeft}>
            <Bot size={20} style={{ color: 'var(--accent)' }} />
            <span style={styles.title}>CodeBuddy Registration</span>
            <Badge tone={s.running ? 'success' : 'muted'}>
              {s.running ? <><Loader size={12} className="spin" /> Running</> : 'Idle'}
            </Badge>
          </div>
          <Button size="sm" variant="ghost" onClick={() => { loadStatus(); loadAccounts(); loadAvailable() }}>
            <RefreshCw size={14} />
          </Button>
        </div>

        {s.running ? (
          <div style={styles.runningInfo}>
            <div style={styles.statRow}>
              <span style={styles.statLabel}>Current account:</span>
              <span style={styles.statValue}>{s.current_email || '...'}</span>
            </div>
            <div style={styles.statRow}>
              <span style={styles.statLabel}>Step:</span>
              <Badge tone="accent">{s.step || '...'}</Badge>
            </div>
            <div style={styles.statRow}>
              <span style={styles.statLabel}>Progress:</span>
              <span style={styles.statValue}>{s.success + s.fail} / {s.target}</span>
            </div>
            <div style={styles.statRow}>
              <span style={styles.statLabel}>Success / Fail:</span>
              <span style={styles.statValue}>
                <span style={{ color: 'var(--ok)' }}>{s.success}</span>
                {' / '}
                <span style={{ color: s.fail ? 'var(--danger)' : 'var(--muted)' }}>{s.fail}</span>
              </span>
            </div>
            <div style={styles.progressBar}>
              <div style={{ ...styles.progressFill, width: `${s.target ? Math.round(((s.success + s.fail) / s.target) * 100) : 0}%` }} />
            </div>
            <Button variant="destructive" size="sm" onClick={stopJob} disabled={busy} style={{ marginTop: 12 }}>
              <Square size={14} /> Stop
            </Button>
          </div>
        ) : (
          <div style={styles.startForm}>
            <div style={styles.formRow}>
              <label style={styles.formLabel}>
                <span>Account (blank = auto-pick)</span>
                <select
                  value={accountId}
                  onChange={(e) => setAccountId(e.target.value)}
                  style={styles.selectInput}
                >
                  <option value="">Auto (next available)</option>
                  {available.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.email} ({a.username})
                    </option>
                  ))}
                </select>
              </label>
              <label style={styles.formLabel}>
                <span>Count</span>
                <input
                  type="number" min={1} max={1000} value={count}
                  onChange={(e) => setCount(Math.max(1, Number(e.target.value) || 1))}
                  style={styles.numInput}
                  disabled={!!accountId}
                />
              </label>
              <label style={styles.formLabel}>
                <span>Region (blank = auto-detect)</span>
                <input
                  type="text" value={region} placeholder="e.g. Singapore"
                  onChange={(e) => setRegion(e.target.value)}
                  style={styles.textInput}
                />
              </label>
            </div>
            {accountId && (
              <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 6 }}>
                Using specific account — Count is ignored
              </div>
            )}
            <Button variant="primary" onClick={startJob} disabled={busy || !s.ok} style={{ marginTop: 10 }}>
              <Play size={14} /> Start CodeBuddy Registration
            </Button>
            {s.finished_at && (
              <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 8 }}>
                Last run: {s.success} ok / {s.fail} fail
                {s.error ? ` — ${s.error}` : ''}
              </div>
            )}
          </div>
        )}

        {msg && (
          <div style={{ fontSize: 12, color: msg.startsWith('Error') ? 'var(--danger)' : 'var(--ok)', marginTop: 8 }}>
            {msg}
          </div>
        )}
      </Card>

      {/* Registered Accounts */}
      <Card style={styles.card}>
        <div style={styles.header}>
          <span style={styles.title}>Registered Accounts</span>
          <Badge tone="muted">{accounts.length}</Badge>
        </div>
        {accounts.length === 0 ? (
          <EmptyState icon={Bot} title="No CodeBuddy accounts yet" description="Start a CodeBuddy registration job to register GitHub accounts on CodeBuddy." />
        ) : (
          <div style={styles.tableWrap}>
            <table style={styles.table}>
              <thead>
                <tr>
                  <th style={styles.th}>Email</th>
                  <th style={styles.th}>Username</th>
                  <th style={styles.th}>Region</th>
                  <th style={styles.th}>Connection ID</th>
                  <th style={styles.th}>Status</th>
                  <th style={styles.th}>Registered At</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((a) => (
                  <tr key={a.id}>
                    <td style={styles.td}>{a.email}</td>
                    <td style={styles.td}>{a.username}</td>
                    <td style={styles.td}>{a.region || '—'}</td>
                    <td style={styles.td}>{a.connection_id || '—'}</td>
                    <td style={styles.td}>
                      <Badge tone={a.status === 'active' ? 'success' : 'danger'}>
                        {a.status === 'active' ? <CheckCircle2 size={11} /> : <XCircle size={11} />}
                        {' '}{a.status}
                      </Badge>
                    </td>
                    <td style={{ ...styles.td, fontSize: 11, color: 'var(--muted)' }}>{a.created_at}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}

const styles = {
  wrap: { display: 'flex', flexDirection: 'column', gap: 14, maxWidth: 1100, width: '100%', margin: '0 auto' },
  card: { padding: 22 },
  header: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 },
  headerLeft: { display: 'flex', alignItems: 'center', gap: 10 },
  title: { fontSize: 15, fontWeight: 700, color: 'var(--text)' },
  runningInfo: { display: 'flex', flexDirection: 'column', gap: 8 },
  statRow: { display: 'flex', alignItems: 'center', gap: 10, fontSize: 13 },
  statLabel: { color: 'var(--muted)', minWidth: 140 },
  statValue: { color: 'var(--text)', fontWeight: 600 },
  progressBar: { height: 6, background: 'var(--bg-input)', borderRadius: 3, overflow: 'hidden', marginTop: 6 },
  progressFill: { height: '100%', background: 'var(--accent)', borderRadius: 3, transition: 'width 0.3s ease' },
  startForm: {},
  formRow: { display: 'flex', gap: 14, flexWrap: 'wrap' },
  formLabel: { display: 'flex', flexDirection: 'column', gap: 5, fontSize: 12, color: 'var(--muted)', fontWeight: 500 },
  numInput: { width: 80, padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 8, background: 'var(--bg-input)', color: 'var(--text)', fontSize: 13, outline: 'none' },
  selectInput: { width: 280, padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 8, background: 'var(--bg-input)', color: 'var(--text)', fontSize: 13, outline: 'none', cursor: 'pointer' },
  textInput: { width: 200, padding: '6px 10px', border: '1px solid var(--border)', borderRadius: 8, background: 'var(--bg-input)', color: 'var(--text)', fontSize: 13, outline: 'none' },
  tableWrap: { overflowX: 'auto', margin: '0 -4px' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 12.5 },
  th: { textAlign: 'left', fontWeight: 700, fontSize: 10.5, color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: 0.5, padding: '8px 10px', borderBottom: '1px solid var(--glass-border)', position: 'sticky', top: 0, background: 'rgba(23,33,43,0.97)' },
  td: { padding: '10px', borderBottom: '1px solid var(--border)', verticalAlign: 'middle' },
}
