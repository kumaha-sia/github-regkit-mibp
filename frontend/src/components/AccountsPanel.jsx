import React, { useEffect, useState } from 'react'
import { Copy, Download, FileJson, FileText, KeyRound, ShieldCheck, Trash2 } from 'lucide-react'
import { api, getToken } from '../api.js'
import { Button, Card, Dialog, EmptyState, Spinner } from './ui.jsx'

const fmtSize = (n) => (n > 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`)

export default function AccountsPanel() {
  const [files, setFiles] = useState([])
  const [selected, setSelected] = useState(null)
  const [rows, setRows] = useState([])
  const [toast, setToast] = useState('')
  const [confirm, setConfirm] = useState(null) // {type:'row'|'file', ...}
  const [recovery, setRecovery] = useState(null) // {email, codes} | null
  const [recoveryLoading, setRecoveryLoading] = useState(false)
  // loading flags — only true for user-visible loads, NOT background polling
  const [loadingFiles, setLoadingFiles] = useState(true)
  const [loadingRows, setLoadingRows] = useState(false)

  function notify(msg) {
    setToast(msg)
    setTimeout(() => setToast(''), 2200)
  }

  // silent = don't show the loading indicator (used by background polling)
  const loadFiles = (silent = false) => {
    if (!silent) setLoadingFiles(true)
    return api
      .get('/api/accounts')
      .then((d) => setFiles(d.files || []))
      .catch(() => {})
      .finally(() => setLoadingFiles(false))
  }

  const currentName = selected || files[0]?.name || ''

  const loadRows = (name, silent = false) => {
    if (!name) {
      setRows([])
      setLoadingRows(false)
      return
    }
    if (!silent) setLoadingRows(true)
    api
      .get(`/api/accounts/preview?name=${encodeURIComponent(name)}`)
      .then((d) => setRows(d.rows || []))
      .catch(() => setRows([]))
      .finally(() => setLoadingRows(false))
  }

  // initial file list + background poll every 3s (silent — no spinner flash)
  useEffect(() => {
    loadFiles(false)
    const t = setInterval(() => loadFiles(true), 3000)
    return () => clearInterval(t)
  }, [])

  // load preview rows whenever selection changes (or a new file appears).
  // Show the spinner ONLY for the first load or when the user picks a file;
  // subsequent silent refreshes triggered by files.length polling are silent
  // to avoid flicker.
  useEffect(() => {
    loadRows(currentName, false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, currentName])

  async function copyAll() {
    const text = rows.map((r) => `${r.email}----${r.password}----${r.username}----${r.totp || ''}`).join('\n')
    try {
      await navigator.clipboard.writeText(text)
      notify(`✓ ${rows.length} accounts copied to clipboard`)
    } catch {
      notify('✗ Clipboard failed')
    }
  }

  function download(content, filename, mime) {
    const blob = new Blob([content], { type: mime })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
    notify(`✓ Exported ${filename}`)
  }

  function exportTxt() {
    const text = rows.map((r) => `${r.email}----${r.password}----${r.username}----${r.totp || ''}`).join('\n')
    download(text, 'github_accounts.txt', 'text/plain')
  }

  function exportCsv() {
    const csv = [
      'email,password,username,totp_secret',
      ...rows.map((r) => `${r.email},${r.password},${r.username},${r.totp || ''}`),
    ].join('\n')
    download(csv, 'github_accounts.csv', 'text/csv')
  }

  function exportJson() {
    download(JSON.stringify(rows, null, 2), 'github_accounts.json', 'application/json')
  }

  function downloadRaw() {
    const name = currentName
    if (!name) return
    const token = getToken()
    fetch(`/api/accounts/download?name=${encodeURIComponent(name)}`, {
      headers: token ? { 'X-Access-Key': token } : {},
    })
      .then((r) => r.blob())
      .then((b) => {
        const u = URL.createObjectURL(b)
        const link = document.createElement('a')
        link.href = u
        link.download = name
        link.click()
        URL.revokeObjectURL(u)
        notify(`✓ Downloaded ${name}`)
      })
      .catch(() => notify('✗ Download failed'))
  }

  async function doDeleteRow() {
    const { email } = confirm
    try {
      await api.del(`/api/accounts/row`, { email, name: currentName })
      notify(`✓ Account ${email} deleted`)
      setConfirm(null)
      loadRows(currentName)
      loadFiles()
    } catch (e) {
      notify('✗ ' + e.message)
    }
  }

  async function showTotpCode(secret, email) {
    try {
      const d = await api.get(`/api/totp?secret=${encodeURIComponent(secret)}`)
      const code = String(d.code || '')
      if (!code) throw new Error('kode kosong')
      try {
        await navigator.clipboard.writeText(code)
        notify(`🔑 ${code} copied (expires in ${d.expires_in}s)`)
      } catch {
        // clipboard denied — masih tampilkan kodenya sebagai fallback
        notify(`🔑 ${email}: ${code} (expires in ${d.expires_in}s)`)
      }
    } catch (e) {
      notify('✗ ' + e.message)
    }
  }

  async function copyValue(value, label) {
    if (!value) return
    try {
      await navigator.clipboard.writeText(String(value))
      notify(`✓ ${label} copied`)
    } catch {
      notify('✗ Clipboard failed')
    }
  }

  async function viewRecoveryCodes(email) {
    setRecoveryLoading(true)
    try {
      const d = await api.get(`/api/accounts/recovery?email=${encodeURIComponent(email)}`)
      setRecovery({ email: d.email, codes: d.codes || [] })
    } catch (e) {
      notify('✗ ' + e.message)
    } finally {
      setRecoveryLoading(false)
    }
  }

  async function copyRecoveryCodes() {
    if (!recovery?.codes?.length) return
    try {
      await navigator.clipboard.writeText(recovery.codes.join('\n'))
      notify(`✓ ${recovery.codes.length} recovery codes copied`)
    } catch {
      notify('✗ Clipboard failed')
    }
  }

  async function doDeleteFile() {
    const { name } = confirm
    try {
      await api.del(`/api/accounts/file?name=${encodeURIComponent(name)}`)
      notify(`✓ File ${name} deleted`)
      setConfirm(null)
      setSelected(null)
      loadFiles()
    } catch (e) {
      notify('✗ ' + e.message)
    }
  }

  return (
    <div style={styles.wrap}>
      {/* header + file selector */}
      <Card style={{ padding: 20 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14, flexWrap: 'wrap', gap: 12 }}>
          <div>
            <div style={{ fontSize: 19, fontWeight: 800, display: 'flex', alignItems: 'center', gap: 10 }}>
              Registered Accounts
              {(loadingFiles || loadingRows) && (
                <Spinner />
              )}
            </div>
            <div style={{ fontSize: 12.5, color: 'var(--muted)', marginTop: 3 }}>
              {loadingRows && rows.length === 0
                ? 'Loading accounts…'
                : <>{rows.length} accounts {currentName && `· ${currentName}`}</>}
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <Button onClick={copyAll} disabled={!rows.length}><Copy size={15} /> Copy Semua</Button>
            <Button onClick={exportTxt} disabled={!rows.length}><FileText size={15} /> TXT</Button>
            <Button onClick={exportCsv} disabled={!rows.length}>CSV</Button>
            <Button onClick={exportJson} disabled={!rows.length}><FileJson size={15} /> JSON</Button>
            <Button variant="primary" onClick={downloadRaw} disabled={!files.length}><Download size={15} /> Download</Button>
            {files.length > 1 && (
              <Button variant="destructive"
                onClick={() => setConfirm({ type: 'file', name: currentName })}
                disabled={!currentName}
              >
                <Trash2 size={15} /> Delete File
              </Button>
            )}
          </div>
        </div>

        {files.length > 1 && (
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            {files.slice(0, 8).map((f) => {
              const active = currentName === f.name
              return (
                <Button
                  key={f.name}
                  size="sm"
                  variant={active ? 'primary' : 'outline'}
                  onClick={() => setSelected(f.name)}
                >
                  {f.name.replace('github_accounts_', '').replace('.txt', '')}
                  <span style={{ color: 'var(--muted)', marginLeft: 4 }}>{fmtSize(f.size)}</span>
                </Button>
              )
            })}
          </div>
        )}
      </Card>

      {/* table */}
      <Card style={{ flex: 1, padding: 0, overflow: 'hidden', minHeight: 200, position: 'relative' }}>
        {rows.length === 0 && (loadingFiles || loadingRows) ? (
          <TableSkeleton />
        ) : rows.length === 0 ? (
          <EmptyState icon={FileText} title={files.length === 0 ? 'No account files yet' : 'This file is empty'} description={files.length === 0 ? 'Run a job from the Status page to create account output.' : undefined} />
        ) : (
          <div style={{ overflowY: 'auto', maxHeight: 'calc(100vh - 320px)' }}>
            <table style={styles.table}>
              <thead>
                <tr>
                  <th style={styles.th}>#</th>
                  <th style={styles.th}>Email</th>
                  <th style={styles.th}>Password</th>
                  <th style={styles.th}>Username</th>
                  <th style={styles.th}>TOTP Secret</th>
                  <th style={{ ...styles.th, width: 190 }}>Aksi</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => (
                  <tr key={i} style={{ borderBottom: '1px solid var(--border)' }}>
                    <td style={{ ...styles.td, color: 'var(--muted)' }}>{i + 1}</td>
                    <td style={styles.tdMono}>
                      <CopyCell
                        value={r.email}
                        onCopy={() => copyValue(r.email, 'Email')}
                      />
                    </td>
                    <td style={styles.tdMono}>
                      <CopyCell
                        value={r.password}
                        masked
                        onCopy={() => copyValue(r.password, 'Password')}
                      />
                    </td>
                    <td style={styles.tdMono}>
                      <CopyCell
                        value={r.username}
                        onCopy={() => copyValue(r.username, 'Username')}
                      />
                    </td>
                    <td style={styles.tdMono}>
                      {r.totp ? (
                        <CopyCell
                          value={r.totp}
                          masked
                          onCopy={() => copyValue(r.totp, 'TOTP secret')}
                        />
                      ) : (
                        <span style={{ color: 'var(--muted)' }}>—</span>
                      )}
                    </td>
                    <td style={styles.td}>
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                        <Button size="sm"
                          onClick={() => {
                            const line = `${r.email}----${r.password}----${r.username}----${r.totp || ''}`
                            navigator.clipboard.writeText(line).then(
                              () => notify('✓ Row copied'),
                              () => notify('✗ Clipboard failed'),
                            )
                          }}
                          title="Salin seluruh baris (email----password----username----totp)"
                        >
                          <Copy size={13} /> Copy
                        </Button>
                        {r.totp && (
                          <Button size="sm"
                            onClick={() => showTotpCode(r.totp, r.email)}
                            title="Generate kode 2FA saat ini dan salin ke clipboard"
                          >
                            <KeyRound size={13} /> Kode
                          </Button>
                        )}
                        {r.has_recovery && (
                          <Button size="sm"
                            onClick={() => viewRecoveryCodes(r.email)}
                            disabled={recoveryLoading}
                            title="View recovery codes for this account"
                          >
                            <ShieldCheck size={13} /> Recovery
                          </Button>
                        )}
                        <Button variant="destructive" size="sm"
                          onClick={() => setConfirm({ type: 'row', email: r.email, name: currentName })}
                        >
                          <Trash2 size={13} /> Delete
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* subtle overlay when refreshing rows while data is already shown */}
        {loadingRows && rows.length > 0 && (
          <div style={styles.tableRefresh} aria-hidden="true">
            <Spinner />
            <span style={{ marginLeft: 8, fontSize: 12, color: 'var(--muted)' }}>Loading…</span>
          </div>
        )}
      </Card>

      <Dialog
        open={!!confirm}
        onClose={() => setConfirm(null)}
        title={confirm?.type === 'file' ? 'Delete account file?' : 'Delete this account?'}
        footer={<><Button onClick={() => setConfirm(null)}>Cancel</Button><Button variant="destructive" onClick={confirm?.type === 'file' ? doDeleteFile : doDeleteRow}><Trash2 size={15} /> Delete</Button></>}
      >
        {confirm?.type === 'file' ? <>File <strong>{confirm.name}</strong> and all of its accounts will be permanently deleted.</> : <>Account <strong>{confirm?.email}</strong> will be deleted from {confirm?.name}. This action cannot be undone.</>}
      </Dialog>

      <Dialog
        open={!!recovery}
        onClose={() => setRecovery(null)}
        title="Recovery codes"
        footer={<><Button onClick={() => setRecovery(null)}>Close</Button><Button variant="primary" onClick={copyRecoveryCodes}><Copy size={15} /> Copy All</Button></>}
      >
        <p className="recovery-email">{recovery?.email}</p>
        <div className="recovery-codes">{recovery?.codes.map((code) => <code key={code}>{code}</code>)}</div>
        <p className="recovery-warning">Store these safely. Each recovery code can only be used once.</p>
      </Dialog>

      {toast && <div className="glass toast glass-strong" style={{ padding: '12px 26px', fontSize: 13.5 }}>{toast}</div>}

      <style>{accountsCSS}</style>
    </div>
  )
}

/**
 * CopyCell — displays a value with an inline copy button.
 * When `masked` is true the text is dots by default; click text to toggle
 * visibility. Clicking the button always copies the REAL value regardless of
 * mask state, so users don't have to reveal the password to copy it.
 */
function CopyCell({ value, onCopy, masked = false }) {
  const [show, setShow] = useState(!masked)
  const [copied, setCopied] = useState(false)
  const text = String(value ?? '')
  const display = masked && !show ? '•'.repeat(Math.min(12, text.length || 6)) : text

  async function handleCopy(e) {
    e.stopPropagation()
    if (onCopy) await onCopy()
    setCopied(true)
    setTimeout(() => setCopied(false), 900)
  }

  return (
    <span style={styles.copyCell}>
      <span
        style={{
          ...styles.copyText,
          cursor: masked ? 'pointer' : 'default',
          userSelect: masked && !show ? 'none' : 'text',
        }}
        onClick={masked ? () => setShow((v) => !v) : undefined}
        title={masked ? (show ? 'Click to hide' : 'Click to show') : undefined}
      >
        {display || <span style={{ color: 'var(--muted)' }}>—</span>}
      </span>
      {text && (
        <button
          type="button"
          className="copy-btn"
          onClick={handleCopy}
          title={copied ? 'Tersalin' : 'Salin ke clipboard'}
          aria-label="Salin"
        >
          {copied ? '✓' : '⧉'}
        </button>
      )}
    </span>
  )
}

/** First-load skeleton: 8 shimmering rows that mirror the real table shape. */
function TableSkeleton() {
  return (
    <div style={styles.skeletonWrap}>
      {Array.from({ length: 8 }).map((_, i) => (
        <div key={i} style={styles.skeletonRow}>
          <div style={styles.skeletonBarShort} />
          <div style={styles.skeletonBar} />
          <div style={styles.skeletonBar} />
          <div style={styles.skeletonBar} />
          <div style={styles.skeletonBar} />
          <div style={styles.skeletonBarShort} />
        </div>
      ))}
    </div>
  )
}

const styles = {
  wrap: { display: 'flex', flexDirection: 'column', gap: 14, flex: 1, maxWidth: 1100, width: '100%', margin: '0 auto', minHeight: 0 },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: {
    textAlign: 'left', padding: '12px 16px', fontSize: 11, fontWeight: 700,
    letterSpacing: 1, textTransform: 'uppercase', color: 'var(--muted)',
    borderBottom: '1px solid var(--border)', background: 'rgba(23,33,43,0.97)',
    position: 'sticky', top: 0, zIndex: 1,
  },
  td: { padding: '11px 16px', fontSize: 13 },
  tdMono: {
    padding: '11px 16px',
    fontFamily: "'SF Mono', Menlo, monospace",
    fontSize: 12.5,
  },
  copyCell: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    maxWidth: '100%',
  },
  copyText: {
    display: 'inline-block',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    maxWidth: 240,
    verticalAlign: 'middle',
  },
  overlay: {
    position: 'fixed', inset: 0, zIndex: 998,
    background: 'rgba(4,8,13,0.60)', backdropFilter: 'blur(8px)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    animation: 'fadeIn 0.2s ease',
  },
  dialog: { padding: 30, width: 400, animation: 'toastIn 0.25s cubic-bezier(0.34, 1.56, 0.64, 1)' },
  recoveryDialog: {
    padding: 24, width: 'min(460px, calc(100vw - 32px))',
    animation: 'toastIn 0.25s cubic-bezier(0.34, 1.56, 0.64, 1)',
  },
  recoveryCodes: {
    display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
    gap: 8, padding: 12, maxHeight: '42vh', overflowY: 'auto',
    border: '1px solid var(--glass-border)', borderRadius: 12,
    background: 'var(--bg-input)',
  },
  // refresh overlay on top of an existing table
  tableRefresh: {
    position: 'absolute', inset: 0, zIndex: 2,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    background: 'rgba(15,23,32,0.70)', backdropFilter: 'blur(3px)',
    animation: 'fadeIn 0.2s ease', pointerEvents: 'none',
  },
  // skeleton first-load layout
  skeletonWrap: {
    padding: '20px 16px', display: 'flex', flexDirection: 'column', gap: 10,
  },
  skeletonRow: {
    display: 'grid',
    gridTemplateColumns: '40px 1fr 1fr 1fr 1fr 120px',
    gap: 12, alignItems: 'center',
  },
  skeletonBar: {
    height: 14, borderRadius: 6,
    background: 'linear-gradient(90deg, rgba(255,255,255,0.04) 25%, rgba(255,255,255,0.10) 37%, rgba(255,255,255,0.04) 63%)',
    backgroundSize: '400% 100%',
    animation: 'shimmer 1.4s ease infinite',
  },
  skeletonBarShort: { height: 14, width: 60, borderRadius: 6, background: 'var(--border)' },
}

// injected copy-button CSS (hover/focus styling can't live in inline styles)
const accountsCSS = `
  .copy-btn {
    display: inline-flex; align-items: center; justify-content: center;
    width: 24px; height: 24px; padding: 0; flex-shrink: 0;
    border-radius: 6px; border: 1px solid transparent;
    background: var(--bg-card-hover);
    color: var(--muted);
    cursor: pointer;
    font-size: 12px;
    transition: all 0.15s ease;
    line-height: 1;
  }
  .copy-btn:hover {
    background: rgba(var(--accent-rgb),0.16);
    color: var(--text);
    border-color: rgba(var(--accent-rgb),0.40);
  }
  .copy-btn:active { transform: scale(0.9); }

  /* loading spinner — teal ring */
  .acc-spinner {
    width: 16px; height: 16px; flex-shrink: 0;
    border-radius: 50%;
    border: 2px solid rgba(var(--accent-rgb),0.25);
    border-top-color: var(--accent);
    animation: acc-spin 0.7s linear infinite;
    display: inline-block;
  }
  @keyframes acc-spin { to { transform: rotate(360deg); } }

  /* skeleton shimmer */
  @keyframes shimmer {
    0% { background-position: 100% 50%; }
    100% { background-position: 0 50%; }
  }

  /* responsive: collapse skeleton columns on narrow screens */
  @media (max-width: 640px) {
    .skeleton-row { grid-template-columns: 32px 1fr 1fr 120px !important; }
    .skeleton-row > :nth-child(4),
    .skeleton-row > :nth-child(5) { display: none; }
    .recovery-codes { grid-template-columns: 1fr !important; }
  }
`
