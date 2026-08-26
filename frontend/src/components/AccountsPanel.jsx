import React, { useEffect, useState, useRef, useMemo, useCallback } from 'react'
import {
  Copy, Download, FileText, KeyRound, ShieldCheck, Trash2,
  Search, ChevronDown, ChevronLeft, ChevronRight, CheckSquare, Square,
  Eye, EyeOff, FolderOpen, X, ChevronsLeft, ChevronsRight, PanelRightOpen,
} from 'lucide-react'
import { api, getToken } from '../api.js'
import { Button, Card, Dialog, EmptyState, Spinner } from './ui.jsx'

const fmtSize = (n) => (n > 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`)
const fmtDate = (ts) => {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return d.toLocaleDateString('id-ID', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}
const truncate = (s, n = 20) => {
  if (!s) return ''
  return s.length > n ? s.slice(0, n) + '\u2026' : s
}

export default function AccountsPanel() {
  const [files, setFiles] = useState([])
  const [rows, setRows] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(1)
  const [perPage, setPerPage] = useState(25)
  const [toast, setToast] = useState('')
  const [confirm, setConfirm] = useState(null)
  const [recovery, setRecovery] = useState(null)
  const [recoveryLoading, setRecoveryLoading] = useState(false)
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState('all')
  const [fileFilter, setFileFilter] = useState('')
  const [selectedEmails, setSelectedEmails] = useState(new Set())
  const [selectAllAll, setSelectAllAll] = useState(false) // true = all matching results selected
  const [detailRow, setDetailRow] = useState(null)
  const [exportOpen, setExportOpen] = useState(false)
  const [fileDropOpen, setFileDropOpen] = useState(false)
  const [perPageOpen, setPerPageOpen] = useState(false)
  const exportRef = useRef(null)
  const fileDropRef = useRef(null)
  const perPageRef = useRef(null)
  const searchTimer = useRef(null)

  function notify(msg) { setToast(msg); setTimeout(() => setToast(''), 2200) }

  // load files list
  useEffect(() => {
    api.get('/api/accounts').then(d => setFiles(d.files || [])).catch(() => {})
  }, [])

  // load accounts with debounce on search
  const loadAccounts = useCallback((p = page, silent = false) => {
    if (!silent) setLoading(true)
    const params = new URLSearchParams({
      page: String(p), per_page: String(perPage), search, filter,
    })
    if (fileFilter) params.set('file', fileFilter)
    api.get(`/api/accounts/all?${params}`)
      .then(d => {
        setRows(d.rows || [])
        setTotal(d.total || 0)
        setPage(d.page || 1)
        setPages(d.pages || 1)
      })
      .catch(() => { setRows([]); setTotal(0) })
      .finally(() => setLoading(false))
  }, [page, perPage, search, filter, fileFilter])

  useEffect(() => { loadAccounts(1) }, [filter, fileFilter, perPage])

  // debounced search
  useEffect(() => {
    clearTimeout(searchTimer.current)
    searchTimer.current = setTimeout(() => loadAccounts(1), 300)
    return () => clearTimeout(searchTimer.current)
  }, [search])

  // background poll
  useEffect(() => {
    const t = setInterval(() => loadAccounts(page, true), 5000)
    return () => clearInterval(t)
  }, [page, loadAccounts])

  // close dropdowns on outside click
  useEffect(() => {
    const handler = (e) => {
      if (exportRef.current && !exportRef.current.contains(e.target)) setExportOpen(false)
      if (fileDropRef.current && !fileDropRef.current.contains(e.target)) setFileDropOpen(false)
      if (perPageRef.current && !perPageRef.current.contains(e.target)) setPerPageOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // reset selection on filter/file change (NOT on page change — persist across pages)
  useEffect(() => { setSelectedEmails(new Set()); setSelectAllAll(false) }, [filter, fileFilter, search])

  const currentPageEmails = rows.map(r => r.email)
  const allCurrentPageSelected = rows.length > 0 && rows.every(r => selectedEmails.has(r.email) || selectAllAll)
  const someSelected = selectAllAll || selectedEmails.size > 0

  function toggleSelectAll() {
    if (selectAllAll) {
      setSelectAllAll(false)
      setSelectedEmails(new Set())
    } else if (allCurrentPageSelected) {
      // all on current page selected → select ALL results
      setSelectAllAll(true)
    } else {
      // select all on current page
      const next = new Set(selectedEmails)
      rows.forEach(r => next.add(r.email))
      setSelectedEmails(next)
    }
  }

  function selectAllResults() {
    setSelectAllAll(true)
    setSelectedEmails(new Set())
  }

  function toggleSelect(email) {
    if (selectAllAll) {
      // switching from "select all" to individual — deselect this one
      setSelectAllAll(false)
      const next = new Set(rows.map(r => r.email))
      next.delete(email)
      setSelectedEmails(next)
    } else {
      const next = new Set(selectedEmails)
      if (next.has(email)) next.delete(email); else next.add(email)
      setSelectedEmails(next)
    }
  }

  function clearSelection() {
    setSelectedEmails(new Set())
    setSelectAllAll(false)
  }

  // get selected rows for export/copy
  function getSelectedRows() {
    if (selectAllAll) return rows // current page (API handles "all" for export)
    return rows.filter(r => selectedEmails.has(r.email))
  }

  async function copyValue(value, label) {
    if (!value) return
    try { await navigator.clipboard.writeText(String(value)); notify(`${label} copied`) }
    catch { notify('Clipboard failed') }
  }

  async function copyAllVisible() {
    const text = rows.map(r => `${r.email}----${r.password}----${r.username}----${r.totp || ''}`).join('\n')
    try { await navigator.clipboard.writeText(text); notify(`${rows.length} accounts copied`) }
    catch { notify('Clipboard failed') }
  }

  async function copySelected() {
    const sel = getSelectedRows()
    const text = sel.map(r => `${r.email}----${r.password}----${r.username}----${r.totp || ''}`).join('\n')
    try { await navigator.clipboard.writeText(text); notify(`${sel.length} accounts copied`) }
    catch { notify('Clipboard failed') }
  }

  function download(content, filename, mime) {
    const blob = new Blob([content], { type: mime })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url; a.download = filename; a.click()
    URL.revokeObjectURL(url); notify(`Exported ${filename}`)
  }

  function getExportRows() {
    // if rows are selected → export selected; otherwise export all on current page
    if (someSelected) return getSelectedRows()
    return rows
  }

  function exportTxt() {
    const data = getExportRows()
    download(data.map(r => `${r.email}----${r.password}----${r.username}----${r.totp || ''}`).join('\n'), 'github_accounts.txt', 'text/plain')
    setExportOpen(false)
  }
  function exportCsv() {
    const data = getExportRows()
    download(['email,password,username,totp_secret', ...data.map(r => `${r.email},${r.password},${r.username},${r.totp || ''}`)].join('\n'), 'github_accounts.csv', 'text/csv')
    setExportOpen(false)
  }
  function exportJson() {
    const data = getExportRows()
    download(JSON.stringify(data, null, 2), 'github_accounts.json', 'application/json')
    setExportOpen(false)
  }

  async function showTotpCode(secret, email) {
    try {
      const d = await api.get(`/api/totp?secret=${encodeURIComponent(secret)}`)
      const code = String(d.code || '')
      if (!code) throw new Error('empty code')
      try { await navigator.clipboard.writeText(code); notify(`${code} copied (${d.expires_in}s)`) }
      catch { notify(`${email}: ${code} (${d.expires_in}s)`) }
    } catch (e) { notify(e.message) }
  }

  async function viewRecoveryCodes(email) {
    setRecoveryLoading(true)
    try {
      const d = await api.get(`/api/accounts/recovery?email=${encodeURIComponent(email)}`)
      setRecovery({ email: d.email, codes: d.codes || [] })
    } catch (e) { notify(e.message) }
    finally { setRecoveryLoading(false) }
  }

  async function copyRecoveryCodes() {
    if (!recovery?.codes?.length) return
    try { await navigator.clipboard.writeText(recovery.codes.join('\n')); notify(`${recovery.codes.length} recovery codes copied`) }
    catch { notify('Clipboard failed') }
  }

  async function doDeleteRow(email, fileName) {
    try {
      await api.del('/api/accounts/row', { email, name: fileName })
      notify(`Account ${email} deleted`)
      if (detailRow?.email === email) setDetailRow(null)
      loadAccounts(page)
    } catch (e) { notify(e.message) }
  }

  async function doDeleteSelected() {
    const sel = getSelectedRows()
    let deleted = 0
    for (const r of sel) {
      try { await api.del('/api/accounts/row', { email: r.email, name: r.file }); deleted++ } catch {}
    }
    notify(`${deleted} accounts deleted`)
    clearSelection()
    if (detailRow && sel.some(r => r.email === detailRow.email)) setDetailRow(null)
    loadAccounts(page)
  }

  async function doDeleteFile(name) {
    try {
      await api.del(`/api/accounts/file?name=${encodeURIComponent(name)}`)
      notify(`File ${name} deleted`)
      setFileFilter('')
      loadAccounts(1)
      api.get('/api/accounts').then(d => setFiles(d.files || [])).catch(() => {})
    } catch (e) { notify(e.message) }
  }

  const currentFileLabel = fileFilter
    ? fileFilter.replace('github_accounts_', '').replace('.txt', '')
    : 'All Files'

  return (
    <div className="acc-layout">
      {/* Header */}
      <div className="acc-header">
        <div className="acc-header-left">
          <h2 className="acc-title">Registered Accounts</h2>
          <span className="acc-subtitle">
            {loading && total === 0 ? 'Loading...' : `${total} accounts${fileFilter ? ` in ${currentFileLabel}` : ` from ${files.length} files`}`}
          </span>
        </div>
        <div className="acc-header-actions">
          <Button onClick={copyAllVisible} disabled={!rows.length}><Copy size={14} /> Copy Page</Button>
          <div className="acc-dropdown" ref={exportRef}>
            <Button onClick={() => setExportOpen(v => !v)} disabled={!rows.length}>
              <Download size={14} /> Export <ChevronDown size={12} />
            </Button>
            {exportOpen && (
              <div className="acc-dropdown-menu">
                <button onClick={copyAllVisible}><Copy size={14} /> <span>Copy All (clipboard)</span></button>
                <button onClick={exportTxt}><FileText size={14} /> <span>Download TXT</span></button>
                <button onClick={exportCsv}><FileText size={14} /> <span>Download CSV</span></button>
                <button onClick={exportJson}><FileText size={14} /> <span>Download JSON</span></button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Toolbar: file filter + search + chips + pagination */}
      <Card className="acc-toolbar-card">
        <div className="acc-toolbar-row">
          {/* File dropdown */}
          <div className="acc-dropdown" ref={fileDropRef}>
            <button className="acc-file-trigger" onClick={() => setFileDropOpen(v => !v)}>
              <FolderOpen size={14} />
              <span>{currentFileLabel}</span>
              <ChevronDown size={12} />
            </button>
            {fileDropOpen && (
              <div className="acc-file-dropdown">
                <button className={`acc-file-option${!fileFilter ? ' active' : ''}`}
                  onClick={() => { setFileFilter(''); setFileDropOpen(false) }}>
                  <span className="acc-file-option-name">All Files</span>
                  <span className="acc-file-option-meta">{files.length} files</span>
                </button>
                <div className="acc-dropdown-divider" />
                {files.map(f => (
                  <button key={f.name} className={`acc-file-option${fileFilter === f.name ? ' active' : ''}`}
                    onClick={() => { setFileFilter(f.name); setFileDropOpen(false) }}>
                    <span className="acc-file-option-name">{f.name.replace('github_accounts_', '').replace('.txt', '')}</span>
                    <span className="acc-file-option-meta">{fmtSize(f.size)}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Search */}
          <div className="acc-search">
            <Search size={14} />
            <input type="text" placeholder="Search email or username..." value={search}
              onChange={e => setSearch(e.target.value)} />
            {search && <button className="acc-search-clear" onClick={() => setSearch('')}><X size={12} /></button>}
          </div>

          {/* Filter chips */}
          <div className="acc-filters">
            {[
              { key: 'all', label: 'All' },
              { key: 'has2fa', label: '2FA' },
              { key: 'no2fa', label: 'No 2FA' },
              { key: 'recovery', label: 'Recovery' },
            ].map(f => (
              <button key={f.key} className={`acc-filter-chip${filter === f.key ? ' active' : ''}`}
                onClick={() => setFilter(f.key)}>{f.label}</button>
            ))}
          </div>
        </div>
      </Card>

      {/* Bulk bar */}
      {someSelected && (
        <div className="acc-bulk-bar">
          <span className="acc-bulk-count">
            {selectAllAll ? `All ${total} results selected` : `${selectedEmails.size} selected`}
          </span>
          <Button size="sm" onClick={copySelected}><Copy size={13} /> Copy</Button>
          <Button size="sm" variant="destructive" onClick={() => setConfirm({ type: 'bulk' })}>
            <Trash2 size={13} /> Delete
          </Button>
          <Button size="sm" variant="ghost" onClick={clearSelection}>
            <X size={13} /> Clear
          </Button>
        </div>
      )}

      {/* Select all results banner */}
      {allCurrentPageSelected && !selectAllAll && total > rows.length && (
        <div className="acc-select-all-banner">
          All {rows.length} on this page selected.
          <button onClick={selectAllResults}>Select all {total} results</button>
        </div>
      )}

      {/* Main content: table + detail panel */}
      <div className={`acc-main${detailRow ? ' with-detail' : ''}`}>
        {/* Table + pagination column */}
        <div className="acc-table-col">
        <Card className="acc-table-card">
          {loading && rows.length === 0 ? (
            <div className="acc-skeleton">{Array.from({ length: 8 }).map((_, i) => (
              <div key={i} className="acc-skeleton-row">
                <div className="acc-skeleton-check" />
                <div className="acc-skeleton-bar" style={{ flex: 2 }} />
                <div className="acc-skeleton-bar" style={{ flex: 1 }} />
                <div className="acc-skeleton-bar" style={{ width: 60 }} />
              </div>
            ))}</div>
          ) : rows.length === 0 ? (
            <EmptyState icon={FileText}
              title={files.length === 0 ? 'No account files yet' : 'No matching accounts'}
              description={files.length === 0 ? 'Run a job from the Status page.' : search ? `No results for "${search}"` : undefined}
            />
          ) : (
            <>
              {/* Table header */}
              <div className="acc-thead">
                <button className="acc-check-btn" onClick={toggleSelectAll}>
                  {selectAllAll || allCurrentPageSelected ? <CheckSquare size={15} /> : someSelected ? <CheckSquare size={15} className="partial" /> : <Square size={15} />}
                </button>
                <span className="acc-th acc-th-idx">#</span>
                <span className="acc-th acc-th-email">Email</span>
                <span className="acc-th acc-th-user">Username</span>
                <span className="acc-th acc-th-status">Status</span>
                {!fileFilter && <span className="acc-th acc-th-file">File</span>}
                <span className="acc-th acc-th-action">Action</span>
              </div>

              {/* Table body */}
              <div className="acc-tbody">
                {rows.map((r, i) => {
                  const globalIdx = (page - 1) * perPage + i + 1
                  const isSelected = selectAllAll || selectedEmails.has(r.email)
                  const isDetail = detailRow?.email === r.email
                  return (
                    <div key={`${r.email}-${r.file}-${i}`}
                      className={`acc-tr${isSelected ? ' selected' : ''}${isDetail ? ' active' : ''}`}
                      onClick={() => setDetailRow(r)}
                    >
                      <button className="acc-check-btn" onClick={e => { e.stopPropagation(); toggleSelect(r.email) }}>
                        {isSelected ? <CheckSquare size={15} /> : <Square size={15} />}
                      </button>
                      <span className="acc-td acc-td-idx">{globalIdx}</span>
                      <span className="acc-td acc-td-email" title={r.email}>{truncate(r.email, 28)}</span>
                      <span className="acc-td acc-td-user" title={r.username}>{truncate(r.username, 16)}</span>
                      <span className="acc-td acc-td-status">
                        {r.totp && <span className="acc-badge acc-badge-2fa">2FA</span>}
                        {r.has_recovery && <span className="acc-badge acc-badge-recovery">Reco</span>}
                        {!r.totp && !r.has_recovery && <span className="acc-badge acc-badge-none">&mdash;</span>}
                      </span>
                      {!fileFilter && (
                        <span className="acc-td acc-td-file" title={r.file}>
                          {r.file?.replace('github_accounts_', '').replace('.txt', '')}
                        </span>
                      )}
                      <span className="acc-td acc-td-action">
                        <button className="acc-icon-btn" title="View detail" onClick={e => { e.stopPropagation(); setDetailRow(r) }}>
                          <PanelRightOpen size={13} />
                        </button>
                      </span>
                    </div>
                  )
                })}
              </div>
            </>
          )}

          {/* Loading overlay */}
          {loading && rows.length > 0 && (
            <div className="acc-loading-overlay"><Spinner /> <span>Loading...</span></div>
          )}
        </Card>

        {/* Pagination */}
        {total > 0 && (
          <div className="acc-pagination">
            <span className="acc-pagination-info">
              Showing {(page - 1) * perPage + 1}&ndash;{Math.min(page * perPage, total)} of {total}
            </span>
            <div className="acc-pagination-controls">
              <button disabled={page <= 1} onClick={() => loadAccounts(1)}><ChevronsLeft size={14} /></button>
              <button disabled={page <= 1} onClick={() => loadAccounts(page - 1)}><ChevronLeft size={14} /></button>
              {Array.from({ length: Math.min(pages, 7) }, (_, i) => {
                let p
                if (pages <= 7) p = i + 1
                else if (page <= 4) p = i + 1
                else if (page >= pages - 3) p = pages - 6 + i
                else p = page - 3 + i
                return (
                  <button key={p} className={p === page ? 'active' : ''} onClick={() => loadAccounts(p)}>{p}</button>
                )
              })}
              <button disabled={page >= pages} onClick={() => loadAccounts(page + 1)}><ChevronRight size={14} /></button>
              <button disabled={page >= pages} onClick={() => loadAccounts(pages)}><ChevronsRight size={14} /></button>
            </div>
            <div className="acc-per-page" ref={perPageRef}>
              <button className="acc-per-page-trigger" onClick={() => setPerPageOpen(v => !v)}>
                {perPage} / page <ChevronDown size={11} />
              </button>
              {perPageOpen && (
                <div className="acc-dropdown-menu acc-per-page-menu">
                  {[10, 25, 50, 100].map(n => (
                    <button key={n} className={n === perPage ? 'active' : ''}
                      onClick={() => { setPerPage(n); setPerPageOpen(false) }}>
                      {n} per page
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}

        </div> {/* end acc-table-col */}

        {/* Detail panel (right slide-in) */}
        {detailRow && (
          <div className="acc-detail-panel">
            <div className="acc-detail-header">
              <h3>Account Detail</h3>
              <button className="acc-icon-btn" onClick={() => setDetailRow(null)}><X size={16} /></button>
            </div>
            <div className="acc-detail-body">
              <DetailField label="Email" value={detailRow.email}
                onCopy={() => copyValue(detailRow.email, 'Email')} />
              <DetailField label="Password" value={detailRow.password} masked
                onCopy={() => copyValue(detailRow.password, 'Password')} />
              <DetailField label="Username" value={detailRow.username}
                onCopy={() => copyValue(detailRow.username, 'Username')} />
              <DetailField label="TOTP Secret" value={detailRow.totp || '\u2014'} masked={!!detailRow.totp}
                onCopy={detailRow.totp ? () => copyValue(detailRow.totp, 'TOTP') : undefined}
                extra={detailRow.totp ? (
                  <Button size="sm" onClick={() => showTotpCode(detailRow.totp, detailRow.email)}>
                    <KeyRound size={13} /> Generate
                  </Button>
                ) : null} />
              <DetailField label="Recovery" value={detailRow.has_recovery ? 'Available' : 'Not available'}
                onCopy={detailRow.has_recovery ? () => viewRecoveryCodes(detailRow.email) : undefined}
                extra={detailRow.has_recovery ? (
                  <Button size="sm" onClick={() => viewRecoveryCodes(detailRow.email)} disabled={recoveryLoading}>
                    <ShieldCheck size={13} /> View Codes
                  </Button>
                ) : null} />

              <div className="acc-detail-divider" />

              <div className="acc-detail-meta">
                <div className="acc-detail-meta-row">
                  <span className="acc-detail-meta-label">File</span>
                  <span className="acc-detail-meta-value" title={detailRow.file}>
                    {detailRow.file?.replace('github_accounts_', '').replace('.txt', '')}
                  </span>
                </div>
                <div className="acc-detail-meta-row">
                  <span className="acc-detail-meta-label">Created</span>
                  <span className="acc-detail-meta-value">{fmtDate(detailRow.file_mtime)}</span>
                </div>
              </div>

              <div className="acc-detail-divider" />

              <div className="acc-detail-bottom-actions">
                <Button onClick={() => {
                  const line = `${detailRow.email}----${detailRow.password}----${detailRow.username}----${detailRow.totp || ''}`
                  navigator.clipboard.writeText(line).then(() => notify('Row copied'), () => notify('Clipboard failed'))
                }}><Copy size={13} /> Copy Full Row</Button>
                <Button variant="destructive" onClick={() => setConfirm({ type: 'row', email: detailRow.email, name: detailRow.file })}>
                  <Trash2 size={13} /> Delete
                </Button>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Dialogs */}
      <Dialog open={!!confirm} onClose={() => setConfirm(null)}
        title={confirm?.type === 'file' ? 'Delete file?' : confirm?.type === 'bulk' ? `Delete ${selectedRows.size} accounts?` : 'Delete account?'}
        footer={<>
          <Button onClick={() => setConfirm(null)}>Cancel</Button>
          <Button variant="destructive" onClick={() => {
            if (confirm?.type === 'bulk') doDeleteSelected()
            else if (confirm?.type === 'row') doDeleteRow(confirm.email, confirm.name)
            else if (confirm?.type === 'file') doDeleteFile(confirm.name)
            setConfirm(null)
          }}><Trash2 size={15} /> Delete</Button>
        </>}>
        {confirm?.type === 'bulk' ? <>{selectedRows.size} accounts will be permanently deleted.</>
          : confirm?.type === 'row' ? <>Account <strong>{confirm?.email}</strong> will be deleted.</>
          : confirm ? <>File <strong>{confirm.name}</strong> and all accounts will be deleted.</> : null}
      </Dialog>

      <Dialog open={!!recovery} onClose={() => setRecovery(null)} title="Recovery Codes"
        footer={<>
          <Button onClick={() => setRecovery(null)}>Close</Button>
          <Button variant="primary" onClick={copyRecoveryCodes}><Copy size={15} /> Copy All</Button>
        </>}>
        <p className="recovery-email">{recovery?.email}</p>
        <div className="recovery-codes">{recovery?.codes?.map(c => <code key={c}>{c}</code>)}</div>
        <p className="recovery-warning">Store these safely. Each code can only be used once.</p>
      </Dialog>

      {toast && <div className="glass toast glass-strong" style={{ padding: '12px 26px', fontSize: 13.5 }}>{toast}</div>}
      <style>{accountsCSS}</style>
    </div>
  )
}

function DetailField({ label, value, masked = false, onCopy, extra }) {
  const [show, setShow] = useState(!masked)
  const text = String(value ?? '')
  const display = masked && !show ? '\u2022'.repeat(Math.min(16, text.length || 8)) : text

  return (
    <div className="acc-df">
      <span className="acc-df-label">{label}</span>
      <div className="acc-df-row">
        <span className="acc-df-value">{display || <span className="acc-df-empty">{'\u2014'}</span>}</span>
        <div className="acc-df-actions">
          {masked && text && text !== '\u2014' && (
            <button className="acc-icon-btn" onClick={() => setShow(v => !v)} title={show ? 'Hide' : 'Show'}>
              {show ? <EyeOff size={13} /> : <Eye size={13} />}
            </button>
          )}
          {onCopy && <button className="acc-icon-btn" onClick={onCopy} title="Copy"><Copy size={13} /></button>}
          {extra}
        </div>
      </div>
    </div>
  )
}

const accountsCSS = `
/* ===== Layout ===== */
.acc-layout { display:flex; flex-direction:column; gap:12px; flex:1; max-width:1200px; width:100%; margin:0 auto; min-height:0; }

/* ===== Header ===== */
.acc-header { display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:12px; }
.acc-header-left { display:flex; flex-direction:column; gap:2px; }
.acc-title { margin:0; font-size:19px; font-weight:800; color:var(--text-primary); letter-spacing:-0.3px; }
.acc-subtitle { font-size:12.5px; color:var(--text-muted); }
.acc-header-actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }

/* ===== Dropdown (shared) ===== */
.acc-dropdown { position:relative; }
.acc-dropdown-menu {
  position:absolute; top:calc(100% + 4px); left:0; z-index:100;
  min-width:200px; padding:4px;
  border:1px solid var(--border-strong); border-radius:12px;
  background:var(--bg-card); box-shadow:0 16px 48px rgba(0,0,0,0.50);
  animation:accDropIn 0.15s ease;
}
.acc-dropdown-menu button {
  display:flex; width:100%; align-items:center; gap:10px;
  padding:8px 12px; border:none; border-radius:8px;
  background:transparent; color:var(--text-secondary);
  font:inherit; font-size:13px; cursor:pointer; white-space:nowrap;
  transition:background 0.12s, color 0.12s;
}
.acc-dropdown-menu button span { flex:1; text-align:left; }
.acc-dropdown-menu button:hover { background:var(--bg-card-hover); color:var(--text-primary); }
.acc-dropdown-menu button.active { background:var(--accent-soft); color:var(--accent); }
.acc-dropdown-divider { height:1px; margin:4px 8px; background:var(--border); }

/* ===== File trigger ===== */
.acc-file-trigger {
  display:flex; align-items:center; gap:7px;
  padding:7px 12px; border:1px solid var(--border); border-radius:9px;
  background:var(--bg-card); color:var(--text-secondary);
  font:inherit; font-size:12.5px; font-weight:600; cursor:pointer;
  transition:border-color 0.15s, background 0.15s;
}
.acc-file-trigger:hover { border-color:rgba(var(--accent-rgb),0.40); background:var(--bg-card-hover); }
.acc-file-trigger span { color:var(--text-primary); }
.acc-file-dropdown {
  position:absolute; top:calc(100% + 4px); left:0; z-index:100;
  min-width:240px; max-height:300px; overflow-y:auto; padding:4px;
  border:1px solid var(--border-strong); border-radius:12px;
  background:var(--bg-card); box-shadow:0 16px 48px rgba(0,0,0,0.50);
  animation:accDropIn 0.15s ease;
}
.acc-file-option {
  display:flex; justify-content:space-between; align-items:center; gap:10px;
  width:100%; padding:8px 12px; border:none; border-radius:8px;
  background:transparent; color:var(--text-secondary);
  font:inherit; font-size:13px; cursor:pointer; text-align:left;
  transition:background 0.12s;
}
.acc-file-option:hover { background:var(--bg-card-hover); }
.acc-file-option.active { background:var(--accent-soft); color:var(--text-primary); }
.acc-file-option-name { font-weight:600; color:var(--text-primary); }
.acc-file-option-meta { font-size:11px; color:var(--text-muted); }

/* ===== Toolbar card ===== */
.acc-toolbar-card { padding:10px 14px; }
.acc-toolbar-row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
.acc-search {
  display:flex; align-items:center; gap:8px; flex:1; min-width:180px;
  padding:0 10px; height:34px;
  border:1px solid var(--border); border-radius:9px;
  background:var(--bg-input); transition:border-color 0.15s, box-shadow 0.15s;
}
.acc-search:focus-within { border-color:rgba(var(--accent-rgb),0.60); box-shadow:var(--ring); }
.acc-search svg { color:var(--text-muted); flex:none; }
.acc-search input { flex:1; border:none; background:transparent; color:var(--text-primary); font:inherit; font-size:13px; outline:none; }
.acc-search input::placeholder { color:var(--text-muted); }
.acc-search-clear {
  display:flex; align-items:center; justify-content:center;
  width:18px; height:18px; border:none; border-radius:50%;
  background:var(--bg-card-hover); color:var(--text-muted); cursor:pointer;
}
.acc-search-clear:hover { background:rgba(var(--danger-rgb),0.20); color:#FCA5A5; }
.acc-filters { display:flex; gap:4px; }
.acc-filter-chip {
  padding:5px 11px; border:1px solid var(--border); border-radius:999px;
  background:transparent; color:var(--text-muted);
  font:inherit; font-size:11.5px; font-weight:600; cursor:pointer;
  transition:all 0.15s;
}
.acc-filter-chip:hover { border-color:rgba(var(--accent-rgb),0.30); color:var(--text-secondary); }
.acc-filter-chip.active { border-color:rgba(var(--accent-rgb),0.50); background:var(--accent-soft); color:var(--accent); }

/* ===== Bulk bar ===== */
.acc-bulk-bar {
  display:flex; align-items:center; gap:8px;
  padding:10px 14px; border:1px solid rgba(var(--accent-rgb),0.30); border-radius:12px;
  background:var(--accent-soft); animation:accSlideIn 0.2s ease;
}
.acc-bulk-count { font-size:13px; font-weight:700; color:var(--accent); margin-right:4px; }

/* ===== Select all banner ===== */
.acc-select-all-banner {
  display:flex; align-items:center; gap:8px; justify-content:center;
  padding:8px 14px; border:1px solid var(--border); border-radius:10px;
  background:var(--bg-card); font-size:12.5px; color:var(--text-muted);
  animation:accSlideIn 0.15s ease;
}
.acc-select-all-banner button {
  border:none; background:transparent; color:var(--accent);
  font:inherit; font-size:12.5px; font-weight:600; cursor:pointer;
  text-decoration:underline; text-underline-offset:2px;
}
.acc-select-all-banner button:hover { color:#3ADDf2; }

/* ===== Main (table + detail) ===== */
.acc-main { display:flex; gap:12px; flex:1; min-height:0; }
.acc-table-col { display:flex; flex-direction:column; gap:10px; flex:1; min-width:0; min-height:0; }
.acc-table-card { flex:1; padding:0; overflow:hidden; display:flex; flex-direction:column; min-height:0; }

/* ===== Table ===== */
.acc-thead {
  display:grid; grid-template-columns:36px 40px 1fr 140px 100px auto 44px;
  align-items:center; padding:0 14px; height:40px;
  border-bottom:1px solid var(--border);
  background:rgba(23,33,43,0.97); position:sticky; top:0; z-index:2;
}
.acc-main.with-detail .acc-thead { grid-template-columns:36px 40px 1fr 120px 90px 44px; }
.acc-th { font-size:10.5px; font-weight:700; letter-spacing:0.8px; text-transform:uppercase; color:var(--text-muted); }
.acc-th-idx { text-align:center; }
.acc-th-status { text-align:center; }
.acc-th-action { text-align:center; }
.acc-th-file { }

.acc-tbody { flex:1; overflow-y:auto; }
.acc-tr {
  display:grid; grid-template-columns:36px 40px 1fr 140px 100px auto 44px;
  align-items:center; padding:0 14px; min-height:44px;
  border-bottom:1px solid var(--border); cursor:pointer;
  transition:background 0.1s;
}
.acc-main.with-detail .acc-tr { grid-template-columns:36px 40px 1fr 120px 90px 44px; }
.acc-tr:hover { background:var(--bg-card-hover); }
.acc-tr.selected { background:rgba(var(--accent-rgb),0.06); }
.acc-tr.active { background:rgba(var(--accent-rgb),0.10); border-left:2px solid var(--accent); }

.acc-td { font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.acc-td-idx { text-align:center; color:var(--text-muted); font-size:11.5px; }
.acc-td-email { font-family:'SF Mono',Menlo,monospace; font-size:12.5px; color:var(--text-primary); }
.acc-td-user { font-family:'SF Mono',Menlo,monospace; font-size:12px; color:var(--text-secondary); }
.acc-td-status { display:flex; gap:4px; justify-content:center; }
.acc-td-file { font-size:11px; color:var(--text-muted); }
.acc-td-action { text-align:center; }

/* ===== Badges ===== */
.acc-badge { display:inline-flex; padding:2px 7px; border-radius:999px; font-size:10px; font-weight:700; letter-spacing:0.3px; text-transform:uppercase; }
.acc-badge-2fa { color:#34D399; background:rgba(52,211,153,0.12); border:1px solid rgba(52,211,153,0.25); }
.acc-badge-recovery { color:#60A5FA; background:rgba(96,165,250,0.12); border:1px solid rgba(96,165,250,0.25); }
.acc-badge-none { color:var(--text-muted); background:var(--bg-card-hover); border:1px solid var(--border); }

/* ===== Checkbox ===== */
.acc-check-btn {
  display:flex; align-items:center; justify-content:center;
  width:28px; height:28px; border:none; border-radius:6px;
  background:transparent; color:var(--text-muted); cursor:pointer;
  transition:color 0.12s, background 0.12s; flex-shrink:0;
}
.acc-check-btn:hover { background:var(--bg-card-hover); color:var(--accent); }
.acc-check-btn .partial { opacity:0.5; }

/* ===== Icon button ===== */
.acc-icon-btn {
  display:inline-flex; align-items:center; justify-content:center;
  width:26px; height:26px; border:1px solid transparent; border-radius:6px;
  background:var(--bg-card-hover); color:var(--text-muted); cursor:pointer;
  transition:all 0.12s;
}
.acc-icon-btn:hover { background:rgba(var(--accent-rgb),0.15); color:var(--text-primary); border-color:rgba(var(--accent-rgb),0.30); }

/* ===== Pagination ===== */
.acc-pagination {
  display:flex; align-items:center; justify-content:space-between; gap:12px;
  padding:8px 0; flex-wrap:wrap;
}
.acc-pagination-info { font-size:12px; color:var(--text-muted); }
.acc-pagination-controls { display:flex; gap:2px; }
.acc-pagination-controls button {
  display:flex; align-items:center; justify-content:center;
  min-width:32px; height:32px; padding:0 6px;
  border:1px solid var(--border); border-radius:8px;
  background:var(--bg-card); color:var(--text-secondary);
  font:inherit; font-size:12px; font-weight:600; cursor:pointer;
  transition:all 0.12s;
}
.acc-pagination-controls button:hover:not(:disabled) { border-color:rgba(var(--accent-rgb),0.40); color:var(--text-primary); }
.acc-pagination-controls button.active { background:var(--accent); color:#071A21; border-color:var(--accent); }
.acc-pagination-controls button:disabled { opacity:0.35; cursor:not-allowed; }
.acc-per-page { position:relative; }
.acc-per-page-trigger {
  display:flex; align-items:center; gap:4px;
  padding:5px 10px; border:1px solid var(--border); border-radius:8px;
  background:var(--bg-card); color:var(--text-muted);
  font:inherit; font-size:11.5px; cursor:pointer;
}
.acc-per-page-trigger:hover { border-color:rgba(var(--accent-rgb),0.30); }
.acc-per-page-menu { right:0; left:auto; bottom:calc(100% + 4px); top:auto; min-width:130px; }

/* ===== Detail panel ===== */
.acc-detail-panel {
  width:340px; flex-shrink:0;
  border:1px solid var(--border); border-radius:16px;
  background:var(--bg-card); overflow:hidden;
  animation:accSlideRight 0.2s ease;
  display:flex; flex-direction:column;
}
.acc-detail-header {
  display:flex; justify-content:space-between; align-items:center;
  padding:14px 16px; border-bottom:1px solid var(--border);
}
.acc-detail-header h3 { margin:0; font-size:15px; font-weight:700; color:var(--text-primary); }
.acc-detail-body { padding:14px 16px; overflow-y:auto; flex:1; display:flex; flex-direction:column; gap:14px; }

.acc-df { display:flex; flex-direction:column; gap:4px; }
.acc-df-label { font-size:10.5px; font-weight:700; letter-spacing:0.5px; text-transform:uppercase; color:var(--text-muted); }
.acc-df-row { display:flex; align-items:center; gap:6px; }
.acc-df-value {
  flex:1; font-size:13px; font-family:'SF Mono',Menlo,monospace;
  color:var(--text-primary); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0;
}
.acc-df-empty { color:var(--text-muted); }
.acc-df-actions { display:flex; gap:4px; flex-shrink:0; }

.acc-detail-divider { height:1px; background:var(--border); }
.acc-detail-meta { display:flex; flex-direction:column; gap:8px; }
.acc-detail-meta-row { display:flex; justify-content:space-between; align-items:center; }
.acc-detail-meta-label { font-size:11.5px; color:var(--text-muted); }
.acc-detail-meta-value { font-size:12px; color:var(--text-secondary); font-family:'SF Mono',Menlo,monospace; }
.acc-detail-bottom-actions { display:flex; gap:8px; flex-wrap:wrap; }

/* ===== Skeleton ===== */
.acc-skeleton { padding:12px; display:flex; flex-direction:column; gap:6px; }
.acc-skeleton-row { display:flex; align-items:center; gap:12px; height:40px; }
.acc-skeleton-check { width:18px; height:18px; border-radius:4px; background:var(--border); }
.acc-skeleton-bar {
  height:12px; border-radius:6px;
  background:linear-gradient(90deg,rgba(255,255,255,0.04) 25%,rgba(255,255,255,0.10) 37%,rgba(255,255,255,0.04) 63%);
  background-size:400% 100%; animation:shimmer 1.4s ease infinite;
}

/* ===== Loading overlay ===== */
.acc-loading-overlay {
  position:absolute; inset:0; z-index:3;
  display:flex; align-items:center; justify-content:center; gap:8px;
  background:rgba(15,23,32,0.70); backdrop-filter:blur(3px);
  border-radius:16px; animation:fadeIn 0.2s ease; pointer-events:none;
}
.acc-loading-overlay span { font-size:12px; color:var(--text-muted); }

/* ===== Animations ===== */
@keyframes accDropIn { from{opacity:0;transform:translateY(-4px)} to{opacity:1;transform:none} }
@keyframes accSlideIn { from{opacity:0;transform:translateY(-6px)} to{opacity:1;transform:none} }
@keyframes accSlideRight { from{opacity:0;transform:translateX(16px)} to{opacity:1;transform:none} }
@keyframes shimmer { 0%{background-position:100% 50%} 100%{background-position:0 50%} }

/* ===== Responsive ===== */
@media (max-width:900px) {
  .acc-main { flex-direction:column; }
  .acc-detail-panel { width:100%; max-height:50vh; }
  .acc-thead, .acc-tr { grid-template-columns:36px 40px 1fr 100px 80px 44px !important; }
  .acc-th-file, .acc-td-file { display:none; }
}
@media (max-width:640px) {
  .acc-header { flex-direction:column; }
  .acc-toolbar-row { flex-direction:column; }
  .acc-search { min-width:100%; }
  .acc-filters { width:100%; overflow-x:auto; }
  .acc-pagination { flex-direction:column; align-items:stretch; }
  .acc-pagination-controls { justify-content:center; }
}
`
