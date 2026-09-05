import React, { useEffect, useState, useRef } from 'react'
import { Bot, Play, Square, RefreshCw, CheckCircle2, XCircle, Loader, Rocket, AlertTriangle, Activity } from 'lucide-react'
import { api } from '../api.js'
import { Badge, Button } from './ui.jsx'

export default function CodeBuddyPanel() {
  const [status, setStatus] = useState(null)
  const [accounts, setAccounts] = useState([])
  const [available, setAvailable] = useState([])
  const [count, setCount] = useState(5)
  const [region, setRegion] = useState('')
  const [accountId, setAccountId] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [page, setPage] = useState(1)
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
  const activeCount = accounts.filter(a => a.status === 'active').length
  const failedCount = accounts.filter(a => a.status === 'failed').length
  const successRate = activeCount + failedCount > 0 
    ? Math.round((activeCount / (activeCount + failedCount)) * 100) 
    : 0

  const itemsPerPage = 8
  const totalPages = Math.ceil(accounts.length / itemsPerPage)
  const displayedAccounts = accounts.slice((page - 1) * itemsPerPage, page * itemsPerPage)

  return (
    <div style={styles.wrap}>
      
      {/* Header */}
      <div style={styles.headerRow}>
        <div>
          <h1 style={styles.pageTitle}>
            <Bot size={24} style={{ color: 'var(--accent)' }} />
            CodeBuddy Fleet Manager
          </h1>
          <p style={styles.pageDesc}>Automate GitHub account registration for Tencent CodeBuddy.</p>
        </div>
        {s.running ? (
          <Button variant="destructive" size="lg" onClick={stopJob} disabled={busy} style={styles.actionBtnDanger}>
            <Square size={16} /> Halt Fleet
          </Button>
        ) : (
          <Button variant="primary" size="lg" onClick={startJob} disabled={busy || !s.ok} style={styles.actionBtn}>
            <Rocket size={16} /> Launch Fleet
          </Button>
        )}
      </div>

      <div style={styles.mainGrid}>
        
        {/* Left Column: Controls */}
        <div style={styles.leftCol}>
          
          {/* Status Card */}
          <div style={{...styles.glassCard, borderTop: s.running ? '4px solid var(--accent)' : '4px solid var(--border-strong)' }}>
            <div style={styles.statusBadgeRow}>
              <div style={{...styles.statusDot, background: s.running ? 'var(--accent)' : 'var(--text-muted)' }}></div>
              <span style={styles.statusLabel}>{s.running ? 'System Active' : 'System Idle'}</span>
              <button style={styles.refreshBtn} onClick={() => { loadStatus(); loadAccounts(); loadAvailable() }}>
                <RefreshCw size={14} className={s.running ? 'spin' : ''} />
              </button>
            </div>
            {s.running ? (
              <div style={styles.statusContent}>
                <span style={styles.statusValueMain}>{s.success + s.fail} / {s.target}</span>
                <span style={styles.statusValueSub}>{s.current_email || 'Initializing...'}</span>
                
                <div style={styles.progressBarWrap}>
                  <div style={{ ...styles.progressBarFill, width: `${s.target ? Math.round(((s.success + s.fail) / s.target) * 100) : 0}%` }} />
                </div>
                
                <div style={{ display: 'flex', gap: 10, marginTop: 4, fontSize: 13 }}>
                   <span style={{ color: 'var(--success)' }}>{s.success} OK</span>
                   <span style={{ color: 'var(--danger)' }}>{s.fail} Fail</span>
                   <span style={{ color: 'var(--accent)', marginLeft: 'auto' }}>{s.step}</span>
                </div>
              </div>
            ) : (
              <div style={styles.statusContent}>
                <span style={styles.statusValueMain}>Ready</span>
                <span style={styles.statusValueSub}>Awaiting job configuration.</span>
              </div>
            )}
          </div>

          {/* Config Card */}
          <div style={styles.glassCard}>
            <h3 style={styles.cardHeader}><Activity size={16}/> Job Configuration</h3>
            
            <div style={styles.formStack}>
              <label style={styles.formGroup}>
                <span style={styles.formLabel}>Target Account</span>
                <select value={accountId} onChange={(e) => setAccountId(e.target.value)} style={styles.input}>
                  <option value="" style={{ background: '#1e293b', color: '#f8fafc' }}>Auto (Pick next available)</option>
                  {available.map((a) => {
                    const ageDays = a.created_at ? Math.floor((Date.now() - new Date(a.created_at.replace(' ', 'T') + 'Z').getTime()) / 86400000) : 0
                    return (
                      <option key={a.id} value={a.id} style={{ background: '#1e293b', color: '#f8fafc' }}>
                        {a.email} (Umur: {ageDays} hari)
                      </option>
                    )
                  })}
                </select>
              </label>

              <label style={styles.formGroup}>
                <span style={styles.formLabel}>Batch Size (Count)</span>
                <input
                  type="number" min={1} max={1000} value={count}
                  onChange={(e) => setCount(Math.max(1, Number(e.target.value) || 1))}
                  style={styles.input} disabled={!!accountId || s.running}
                />
              </label>

              <label style={styles.formGroup}>
                <span style={styles.formLabel}>Region Override (Optional)</span>
                <input
                  type="text" value={region} placeholder="e.g. Singapore"
                  onChange={(e) => setRegion(e.target.value)}
                  style={styles.input} disabled={s.running}
                />
              </label>
            </div>
            
            {msg && (
              <div style={{ fontSize: 13, color: msg.startsWith('Error') ? 'var(--danger)' : 'var(--success)', marginTop: 16 }}>
                {msg}
              </div>
            )}
          </div>

        </div>

        {/* Right Column: Metrics & Output */}
        <div style={styles.rightCol}>
          
          <div style={styles.metricsRow}>
            <div style={styles.metricCard}>
              <span style={styles.metricLabel}>Total Registered</span>
              <div style={styles.metricValWrap}>
                <span style={styles.metricVal}>{activeCount}</span>
              </div>
            </div>
            
            <div style={styles.metricCard}>
              <span style={styles.metricLabel}>Success Rate</span>
              <div style={styles.metricValWrap}>
                <span style={styles.metricVal}>{successRate}%</span>
              </div>
              <div style={styles.miniBarWrap}>
                <div style={{ ...styles.miniBarFill, width: `${successRate}%` }}></div>
              </div>
            </div>

            <div style={{...styles.metricCard, ...styles.metricCardDanger}}>
              <span style={{...styles.metricLabel, color: 'rgba(252,165,165,0.8)'}}>Failed Jobs</span>
              <div style={styles.metricValWrap}>
                <span style={styles.metricVal}>{failedCount}</span>
                {failedCount > 0 && <span style={styles.metricSubDanger}>Needs review</span>}
              </div>
            </div>
          </div>

          <div style={styles.tableCard}>
            <div style={styles.tableHeader}>
              <h3 style={styles.cardHeader}>Provisioned Accounts</h3>
              <Badge tone="muted">{accounts.length}</Badge>
            </div>
            
            <div style={styles.tableWrap}>
              {accounts.length === 0 ? (
                 <div style={styles.emptyState}>No CodeBuddy accounts found.</div>
              ) : (
                <table style={styles.table}>
                  <thead>
                    <tr>
                      <th style={styles.th}>Email / Username</th>
                      <th style={styles.th}>Region</th>
                      <th style={styles.th}>Connection ID</th>
                      <th style={styles.th}>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {displayedAccounts.map((a) => (
                      <tr key={a.id} style={a.status === 'failed' ? styles.trFailed : styles.tr}>
                        <td style={styles.td}>
                          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                            <span style={{ color: a.status === 'failed' ? '#FCA5A5' : 'var(--text-primary)', fontWeight: 600 }}>{a.email}</span>
                            <span style={{ color: 'var(--text-muted)', fontSize: 11.5, fontWeight: 500 }}>{a.username}</span>
                          </div>
                        </td>
                        <td style={styles.td}>
                          {a.region ? <span style={styles.monoBadge}>{a.region}</span> : <span style={{color:'var(--text-muted)'}}>—</span>}
                        </td>
                        <td style={styles.td}>
                          {a.connection_id ? <code style={styles.codeCell}>{a.connection_id}</code> : <span style={{color:'var(--text-muted)'}}>—</span>}
                        </td>
                        <td style={styles.td}>
                          <Badge tone={a.status === 'active' ? 'success' : 'danger'}>
                            {a.status === 'active' ? <CheckCircle2 size={12} /> : <XCircle size={12} />}
                            {' '}{a.status}
                          </Badge>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            
            {accounts.length > 0 && (
              <div style={styles.pagination}>
                <button 
                  style={{ ...styles.pageBtn, opacity: page === 1 ? 0.5 : 1 }} 
                  disabled={page === 1} 
                  onClick={() => setPage(p => p - 1)}
                >
                  &larr; Prev
                </button>
                <span style={styles.pageText}>Page {page} of {totalPages || 1}</span>
                <button 
                  style={{ ...styles.pageBtn, opacity: page >= totalPages ? 0.5 : 1 }} 
                  disabled={page >= totalPages} 
                  onClick={() => setPage(p => p + 1)}
                >
                  Next &rarr;
                </button>
              </div>
            )}
          </div>

        </div>
      </div>
    </div>
  )
}

const styles = {
  wrap: { display: 'flex', flexDirection: 'column', gap: 20, maxWidth: 1200, width: '100%', margin: '0 auto', fontFamily: 'var(--font-sans, "Inter", sans-serif)' },
  
  headerRow: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' },
  pageTitle: { fontSize: 22, fontWeight: 700, color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: 10, margin: 0 },
  pageDesc: { fontSize: 14, color: 'var(--text-muted)', margin: '4px 0 0 0' },
  actionBtn: { background: 'rgba(var(--accent-rgb), 0.1)', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.3)', padding: '10px 18px', gap: 8, fontSize: 14 },
  actionBtnDanger: { background: 'rgba(var(--danger-rgb), 0.15)', color: '#FCA5A5', border: '1px solid rgba(var(--danger-rgb), 0.4)', padding: '10px 18px', gap: 8, fontSize: 14 },

  mainGrid: { display: 'flex', gap: 24, flexWrap: 'wrap', alignItems: 'flex-start' },
  leftCol: { flex: '1 1 320px', display: 'flex', flexDirection: 'column', gap: 20 },
  rightCol: { flex: '2 1 600px', display: 'flex', flexDirection: 'column', gap: 20, minWidth: 0 },

  glassCard: { 
    background: 'rgba(var(--bg-card-rgb, 24,30,38), 0.7)', backdropFilter: 'blur(12px)',
    border: '1px solid rgba(255, 255, 255, 0.08)', borderRadius: 16, padding: 22,
    boxShadow: '0 8px 32px rgba(0,0,0,0.2)', position: 'relative'
  },
  
  statusBadgeRow: { display: 'flex', alignItems: 'center', gap: 10, marginBottom: 16 },
  statusDot: { width: 10, height: 10, borderRadius: '50%' },
  statusLabel: { color: 'var(--text-secondary)', fontWeight: 600, textTransform: 'uppercase', fontSize: 11, letterSpacing: 0.5 },
  refreshBtn: { marginLeft: 'auto', background: 'transparent', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', padding: 4 },

  statusContent: { display: 'flex', flexDirection: 'column', gap: 4 },
  statusValueMain: { fontSize: 28, fontWeight: 700, color: 'var(--text-primary)', lineHeight: 1.1 },
  statusValueSub: { fontSize: 13, color: 'var(--text-muted)' },
  
  progressBarWrap: { height: 6, background: 'var(--bg-input)', borderRadius: 4, overflow: 'hidden', marginTop: 14 },
  progressBarFill: { height: '100%', background: 'var(--accent)', borderRadius: 4, transition: 'width 0.3s ease' },

  cardHeader: { fontSize: 13, fontWeight: 600, color: 'var(--text-primary)', textTransform: 'uppercase', letterSpacing: 0.5, margin: '0 0 16px 0', display: 'flex', alignItems: 'center', gap: 8 },
  formStack: { display: 'flex', flexDirection: 'column', gap: 16 },
  formGroup: { display: 'flex', flexDirection: 'column', gap: 6 },
  formLabel: { fontSize: 11.5, fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: 0.3 },
  input: { width: '100%', padding: '10px 14px', border: '1px solid var(--border)', borderRadius: 10, background: 'rgba(0,0,0,0.15)', color: 'var(--text-primary)', fontSize: 13.5, outline: 'none', transition: 'border-color 0.2s' },

  metricsRow: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 16 },
  metricCard: { 
    background: 'rgba(var(--bg-card-rgb, 24,30,38), 0.7)', backdropFilter: 'blur(12px)',
    border: '1px solid rgba(255, 255, 255, 0.08)', borderRadius: 16, padding: 20,
    display: 'flex', flexDirection: 'column', justifyContent: 'space-between'
  },
  metricCardDanger: { border: '1px solid rgba(var(--danger-rgb), 0.2)', background: 'rgba(var(--danger-rgb), 0.05)' },
  metricLabel: { fontSize: 11.5, fontWeight: 600, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: 0.3 },
  metricValWrap: { marginTop: 10, display: 'flex', alignItems: 'baseline', gap: 8 },
  metricVal: { fontSize: 32, fontWeight: 700, color: 'var(--text-primary)', letterSpacing: '-0.5px', lineHeight: 1 },
  metricSubDanger: { fontSize: 12, fontWeight: 600, color: '#FCA5A5' },
  
  miniBarWrap: { width: '100%', height: 6, background: 'rgba(0,0,0,0.2)', borderRadius: 4, marginTop: 14, overflow: 'hidden', border: '1px solid var(--border)' },
  miniBarFill: { height: '100%', background: 'var(--accent)', borderRadius: 4 },

  tableCard: { 
    background: 'rgba(var(--bg-card-rgb, 24,30,38), 0.7)', backdropFilter: 'blur(12px)',
    border: '1px solid rgba(255, 255, 255, 0.08)', borderRadius: 16, display: 'flex', flexDirection: 'column', flex: 1, overflow: 'hidden'
  },
  tableHeader: { padding: '20px 20px', borderBottom: '1px solid rgba(255,255,255,0.05)', background: 'rgba(0,0,0,0.1)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' },
  
  tableWrap: { overflowX: 'auto' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th: { textAlign: 'left', fontWeight: 600, fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: 0.5, padding: '14px 20px', borderBottom: '1px solid rgba(255,255,255,0.05)', background: 'rgba(0,0,0,0.2)' },
  tr: { transition: 'background 0.2s' },
  trFailed: { background: 'rgba(var(--danger-rgb), 0.05)' },
  td: { padding: '14px 20px', borderBottom: '1px solid rgba(255,255,255,0.03)', verticalAlign: 'middle' },
  
  monoBadge: { color: 'var(--text-secondary)', background: 'rgba(0,0,0,0.2)', padding: '3px 6px', borderRadius: 6, fontSize: 11, border: '1px solid var(--border)' },
  codeCell: { fontSize: 11.5, color: 'var(--text-muted)', fontFamily: 'monospace', background: 'rgba(0,0,0,0.2)', padding: '4px 8px', borderRadius: 6 },
  
  pagination: { padding: '14px 20px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderTop: '1px solid rgba(255,255,255,0.05)', background: 'rgba(0,0,0,0.1)' },
  pageBtn: { background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', color: 'var(--text-primary)', padding: '6px 12px', borderRadius: 6, fontSize: 12, cursor: 'pointer', transition: 'background 0.2s' },
  pageText: { fontSize: 12, color: 'var(--text-muted)', fontWeight: 500 },

  emptyState: { padding: 40, textAlign: 'center', color: 'var(--text-muted)', fontSize: 14 }
}

