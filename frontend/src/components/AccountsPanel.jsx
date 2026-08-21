import React, { useEffect, useState } from 'react'
import { api, getToken } from '../api.js'

const fmtSize = (n) => (n > 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`)

export default function AccountsPanel() {
  const [files, setFiles] = useState([])
  const [selected, setSelected] = useState(null)
  const [rows, setRows] = useState([])
  const [toast, setToast] = useState('')
  const [confirm, setConfirm] = useState(null) // {type:'row'|'file', ...}

  function notify(msg) {
    setToast(msg)
    setTimeout(() => setToast(''), 2200)
  }

  const loadFiles = () => api.get('/api/accounts').then((d) => setFiles(d.files || [])).catch(() => {})
  const currentName = selected || files[0]?.name || ''

  const loadRows = (name) => {
    if (!name) { setRows([]); return }
    api.get(`/api/accounts/preview?name=${encodeURIComponent(name)}`)
      .then((d) => setRows(d.rows || []))
      .catch(() => setRows([]))
  }

  useEffect(() => {
    const t = setInterval(loadFiles, 3000)
    return () => clearInterval(t)
  }, [])

  // load preview rows whenever selection changes (or newest file arrives)
  useEffect(() => {
    loadRows(currentName)
  }, [selected, files.length])

  async function copyAll() {
    const text = rows.map((r) => `${r.email}----${r.password}----${r.username}----${r.totp || ''}`).join('\n')
    try {
      await navigator.clipboard.writeText(text)
      notify(`✓ ${rows.length} akun disalin ke clipboard`)
    } catch {
      notify('✗ Clipboard gagal')
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
      .catch(() => notify('✗ Download gagal'))
  }

  async function doDeleteRow() {
    const { email } = confirm
    try {
      await api.del(`/api/accounts/row`, { email, name: currentName })
      notify(`✓ Akun ${email} dihapus`)
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
      notify(`🔑 ${email}: kode ${d.code} (berlaku ${d.expires_in}s)`)
    } catch (e) {
      notify('✗ ' + e.message)
    }
  }

  async function doDeleteFile() {
    const { name } = confirm
    try {
      await api.del(`/api/accounts/file?name=${encodeURIComponent(name)}`)
      notify(`✓ File ${name} dihapus`)
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
      <div className="glass" style={{ padding: 20 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 14, flexWrap: 'wrap', gap: 12 }}>
          <div>
            <div style={{ fontSize: 19, fontWeight: 800 }}>Akun Terdaftar</div>
            <div style={{ fontSize: 12.5, color: 'var(--muted)', marginTop: 3 }}>
              {rows.length} akun {currentName && `· ${currentName}`}
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button className="glass-btn" onClick={copyAll} disabled={!rows.length}>⧉ Copy Semua</button>
            <button className="glass-btn" onClick={exportTxt} disabled={!rows.length}>TXT</button>
            <button className="glass-btn" onClick={exportCsv} disabled={!rows.length}>CSV</button>
            <button className="glass-btn" onClick={exportJson} disabled={!rows.length}>JSON</button>
            <button className="glass-btn primary" onClick={downloadRaw} disabled={!files.length}>⬇ Download File</button>
            {files.length > 1 && (
              <button
                className="glass-btn danger"
                onClick={() => setConfirm({ type: 'file', name: currentName })}
                disabled={!currentName}
              >
                🗑 Hapus File
              </button>
            )}
          </div>
        </div>

        {files.length > 1 && (
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
            {files.slice(0, 8).map((f) => {
              const active = currentName === f.name
              return (
                <button
                  key={f.name}
                  className="glass-btn"
                  style={active ? { borderColor: 'rgba(0,173,181,0.55)', background: 'rgba(0,173,181,0.16)', fontSize: 12, padding: '6px 12px' } : { fontSize: 12, padding: '6px 12px' }}
                  onClick={() => setSelected(f.name)}
                >
                  {f.name.replace('github_accounts_', '').replace('.txt', '')}
                  <span style={{ color: 'var(--muted)', marginLeft: 4 }}>{fmtSize(f.size)}</span>
                </button>
              )
            })}
          </div>
        )}
      </div>

      {/* table */}
      <div className="glass" style={{ flex: 1, padding: 0, overflow: 'hidden', minHeight: 200 }}>
        {rows.length === 0 ? (
          <div style={{ padding: 60, textAlign: 'center', color: 'var(--muted)', fontSize: 13.5 }}>
            Belum ada akun — jalankan job dari tab Status
          </div>
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
                  <tr key={i} style={{ borderBottom: '1px solid rgba(238,238,238,0.05)' }}>
                    <td style={{ ...styles.td, color: 'var(--muted)' }}>{i + 1}</td>
                    <td style={{ ...styles.td, fontFamily: "'SF Mono', Menlo, monospace", fontSize: 12.5 }}>{r.email}</td>
                    <td style={{ ...styles.td, fontFamily: "'SF Mono', Menlo, monospace", fontSize: 12.5 }}>
                      <Masked value={r.password} />
                    </td>
                    <td style={{ ...styles.td, fontFamily: "'SF Mono', Menlo, monospace", fontSize: 12.5 }}>{r.username}</td>
                    <td style={{ ...styles.td, fontFamily: "'SF Mono', Menlo, monospace", fontSize: 12.5 }}>
                      {r.totp ? <Masked value={r.totp} /> : <span style={{ color: 'var(--muted)' }}>—</span>}
                    </td>
                    <td style={styles.td}>
                      <div style={{ display: 'flex', gap: 6 }}>
                        <button
                          className="glass-btn"
                          style={{ fontSize: 11, padding: '4px 12px' }}
                          onClick={() => {
                            navigator.clipboard.writeText(`${r.email}----${r.password}----${r.username}----${r.totp || ''}`)
                            notify('✓ Baris disalin')
                          }}
                        >
                          Copy
                        </button>
                        {r.totp && (
                          <button
                            className="glass-btn"
                            style={{ fontSize: 11, padding: '4px 12px', borderColor: 'rgba(0,173,181,0.45)' }}
                            onClick={() => showTotpCode(r.totp, r.email)}
                          >
                            🔑 Kode
                          </button>
                        )}
                        <button
                          className="glass-btn danger"
                          style={{ fontSize: 11, padding: '4px 12px' }}
                          onClick={() => setConfirm({ type: 'row', email: r.email, name: currentName })}
                        >
                          Hapus
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* confirm dialog (liquid glass) */}
      {confirm && (
        <div style={styles.overlay} onClick={() => setConfirm(null)}>
          <div className="glass glass-strong" style={styles.dialog} onClick={(e) => e.stopPropagation()}>
            <div style={{ fontSize: 34, textAlign: 'center', marginBottom: 8 }}>
              {confirm.type === 'file' ? '🗑' : '⚠️'}
            </div>
            <div style={{ fontSize: 16, fontWeight: 700, textAlign: 'center', marginBottom: 8 }}>
              {confirm.type === 'file' ? 'Hapus file akun?' : 'Hapus akun ini?'}
            </div>
            <div style={{ fontSize: 13, color: 'var(--muted)', textAlign: 'center', marginBottom: 20, lineHeight: 1.5 }}>
              {confirm.type === 'file' ? (
                <>File <b style={{ color: 'var(--danger)' }}>{confirm.name}</b> beserta semua
                akun di dalamnya akan dihapus permanen.</>
              ) : (
                <>Akun <b style={{ color: 'var(--danger)' }}>{confirm.email}</b> akan dihapus
                dari {confirm.name}. Tindakan tidak bisa dibatalkan.</>
              )}
            </div>
            <div style={{ display: 'flex', gap: 10, justifyContent: 'center' }}>
              <button className="glass-btn" style={{ minWidth: 110 }} onClick={() => setConfirm(null)}>
                Batal
              </button>
              <button
                className="glass-btn danger"
                style={{ minWidth: 110 }}
                onClick={confirm.type === 'file' ? doDeleteFile : doDeleteRow}
              >
                Hapus
              </button>
            </div>
          </div>
        </div>
      )}

      {toast && <div className="glass toast glass-strong" style={{ padding: '12px 26px', fontSize: 13.5 }}>{toast}</div>}
    </div>
  )
}

function Masked({ value }) {
  const [show, setShow] = useState(false)
  return (
    <span
      style={{ cursor: 'pointer', userSelect: 'none' }}
      onClick={() => setShow(!show)}
      title="Klik untuk tampilkan"
    >
      {show ? value : '•'.repeat(Math.min(12, value.length))}
    </span>
  )
}

const styles = {
  wrap: { display: 'flex', flexDirection: 'column', gap: 14, flex: 1, maxWidth: 1100, width: '100%', margin: '0 auto', minHeight: 0 },
  table: { width: '100%', borderCollapse: 'collapse' },
  th: {
    textAlign: 'left', padding: '12px 16px', fontSize: 11, fontWeight: 700,
    letterSpacing: 1, textTransform: 'uppercase', color: 'var(--muted)',
    borderBottom: '1px solid rgba(238,238,238,0.08)', background: 'rgba(34,40,49,0.35)',
    position: 'sticky', top: 0, zIndex: 1,
  },
  td: { padding: '11px 16px', fontSize: 13 },
  overlay: {
    position: 'fixed', inset: 0, zIndex: 998,
    background: 'rgba(34,40,49,0.55)', backdropFilter: 'blur(6px)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    animation: 'fadeIn 0.2s ease',
  },
  dialog: { padding: 30, width: 400, animation: 'toastIn 0.25s cubic-bezier(0.34, 1.56, 0.64, 1)' },
}
