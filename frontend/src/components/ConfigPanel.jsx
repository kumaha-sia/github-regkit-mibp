import React, { useEffect, useState } from 'react'
import { Search, Save, FileText, Upload } from 'lucide-react'
import { api } from '../api.js'
import { Badge, Button, Card, Input } from './ui.jsx'

// `wide: true` -> field mengambil full width dalam grid group (mis. label
// panjang, teks bebas, secret string). Default = field kompak (setengah kolom).
const FIELDS = [
  { key: 'email_provider', label: 'Email Provider', group: 'Email Provider', type: 'select', options: [
    { value: 'litensi', label: 'Litensi (paid, API key required)' },
    { value: 'tempik', label: 'Tempik (free, self-hosted, no auth)' },
  ], wide: true },
  { key: 'litensi_api_id', label: 'Litensi API ID', group: 'Litensi Mail', showIf: { email_provider: 'litensi' } },
  { key: 'litensi_api_key', label: 'Litensi API Key', secret: true, group: 'Litensi Mail', wide: true, showIf: { email_provider: 'litensi' } },
  { key: 'litensi_site', label: 'Site (domain, e.g. github.com)', group: 'Litensi Mail', showIf: { email_provider: 'litensi' } },
  { key: 'litensi_zone', label: 'Zone (blank = automatic cheapest)', group: 'Litensi Mail', hasZoneChecker: true, wide: true, showIf: { email_provider: 'litensi' } },
  { key: 'tempik_api_base', label: 'Tempik API Base URL', group: 'Tempik Mail', wide: true, showIf: { email_provider: 'tempik' } },
  { key: 'tempik_domains', label: 'Tempik Domains (comma-separated)', group: 'Tempik Mail', wide: true, showIf: { email_provider: 'tempik' } },
  { key: 'register_count', label: 'Register Count', type: 'number', group: 'Registration' },
  { key: 'proxy_mode', label: 'Proxy Mode', group: 'Registration', type: 'select', options: [
    { value: 'single', label: 'Single proxy (one URL, DataImpulse sticky rotation)' },
    { value: 'list', label: 'Proxy list (file, sequential per account)' },
    { value: 'none', label: 'No proxy (use device IP directly)' },
  ], wide: true },
  { key: 'proxy', label: 'Proxy (http://user:pass@host:port)', secret: true, group: 'Registration', wide: true, showIf: { proxy_mode: 'single' } },
  { key: 'proxy_file', label: 'Proxy file path', group: 'Registration', wide: true, showIf: { proxy_mode: 'list' } },
  { key: 'delay_sec', label: 'Delay between accounts (seconds)', type: 'number', group: 'Registration' },
  { key: 'max_username_tries', label: 'Max username tries', type: 'number', group: 'Registration' },
  { key: 'otp_timeout_sec', label: 'OTP timeout (detik)', type: 'number', group: 'Registration' },
  { key: 'headless', label: 'Headless (no browser window, less stable)', type: 'checkbox', group: 'Registration', wide: true },
  { key: 'browser_profile_dir', label: 'Browser profile dir (DataDome trust)', group: 'Advanced', wide: true },
  { key: 'proxy_hard_block_retries', label: 'Proxy retries after DataDome hard block', type: 'number', group: 'Advanced' },
  { key: 'proxy_rate_limit_retries', label: 'IP rotation/retries after rate limit', type: 'number', group: 'Advanced' },
  { key: 'fresh_profile', label: 'Fresh browser per account — incognito-like with cloned DataDome cookie', type: 'checkbox', group: 'Advanced', wide: true },
  { key: 'repo_name', label: 'Repository name', group: 'Post-Signup Stages' },
  { key: 'create_repo', label: 'Create first repository after signup', type: 'checkbox', group: 'Post-Signup Stages', wide: true },
  { key: 'enable_2fa', label: 'Enable TOTP 2FA and save secret', type: 'checkbox', group: 'Post-Signup Stages', wide: true },
  { key: 'set_profile_status', label: 'Set profile status after 2FA', type: 'checkbox', group: 'Post-Signup Stages', wide: true },
  { key: 'profile_status', label: 'Profile status (blank = On vacation)', group: 'Post-Signup Stages' },
  { key: 'complete_profile', label: 'Complete name, bio, and location after 2FA', type: 'checkbox', group: 'Post-Signup Stages', wide: true },
  { key: 'profile_name', label: 'Profile name (blank = Random User)', group: 'Post-Signup Stages' },
  { key: 'profile_location', label: 'Profile location (blank = Random User)', group: 'Post-Signup Stages' },
  { key: 'profile_bio', label: 'Profile bio (blank = ZenQuotes)', group: 'Post-Signup Stages', wide: true },
  { key: 'notify_url', label: 'Webhook URL (blank = disabled)', group: 'Notifications', wide: true },
  { key: 'notify_token', label: 'Webhook token (Telegram chat_id / Bearer)', secret: true, group: 'Notifications', wide: true },
  { key: 'router_url', label: 'Router URL (blank = disabled)', group: 'Router', wide: true },
  { key: 'router_password', label: 'Router Password', secret: true, group: 'Router', wide: true },
]

// Group-level metadata: which column (kiri/kanan) di layout 2-kolom di layar lebar.
// Kelompokkan yang isinya lebih banyak di kiri, sisanya di kanan agar seimbang.
const GROUP_COLUMN = {
  'Email Provider': 'left',
  'Litensi Mail': 'left',
  'Tempik Mail': 'left',
  'Registration': 'left',
  'Advanced': 'right',
  'Post-Signup Stages': 'right',
  'Notifications': 'right',
  'Router': 'right',
}

export default function ConfigPanel() {
  const [cfg, setCfg] = useState(null)
  const [saved, setSaved] = useState('')
  const [busy, setBusy] = useState(false)

  // zone-check modal state
  const [zoneOpen, setZoneOpen] = useState(false)
  const [zoneLoading, setZoneLoading] = useState(false)
  const [zoneData, setZoneData] = useState(null)
  const [zoneError, setZoneError] = useState('')

  useEffect(() => {
    api.get('/api/config').then((d) => setCfg(d.config)).catch(() => {})
  }, [])

  if (!cfg) return <div style={{ color: 'var(--muted)', padding: 20 }}>Loading configuration…</div>

  function set(key, value) {
    setCfg((c) => ({ ...c, [key]: value }))
    setSaved('')
  }

  async function save() {
    setBusy(true)
    try {
      const patch = {}
      for (const f of FIELDS) {
        if (f.type === 'checkbox') patch[f.key] = !!cfg[f.key]
        else if (f.type === 'number') patch[f.key] = Number(cfg[f.key] ?? 0)
        else patch[f.key] = cfg[f.key] ?? ''
      }
      const d = await api.put('/api/config', patch)
      setCfg(d.config)
      setSaved('✓ Configuration saved')
    } catch (e) {
      setSaved('✗ ' + e.message)
    } finally {
      setBusy(false)
    }
  }

  async function checkZones() {
    setZoneOpen(true)
    setZoneLoading(true)
    setZoneError('')
    setZoneData(null)
    try {
      // pass current form values so the user can test BEFORE saving.
      // masked api-key ("zt***Py") is handled server-side (fallback to stored).
      const d = await api.post('/api/litensi/zones', {
        litensi_api_id: String(cfg.litensi_api_id ?? ''),
        litensi_api_key: String(cfg.litensi_api_key ?? ''),
        litensi_site: String(cfg.litensi_site ?? ''),
      })
      setZoneData(d)
    } catch (e) {
      setZoneError(e.message || 'Unable to retrieve zone list')
    } finally {
      setZoneLoading(false)
    }
  }

  function useZone(zone) {
    set('litensi_zone', zone)
    setZoneOpen(false)
  }

  const groups = [...new Set(FIELDS.map((f) => f.group))]
  const leftGroups = groups.filter((g) => GROUP_COLUMN[g] === 'left')
  const rightGroups = groups.filter((g) => GROUP_COLUMN[g] !== 'left')

  return (
    <div style={styles.wrap} className="config-layout">
      <div style={styles.columns} className="cfg-columns">
        <div style={styles.col}>
          {leftGroups.map((g) => (
            <GroupCard key={g} name={g} onCheckZones={checkZones} cfg={cfg} set={set} />
          ))}
        </div>
        <div style={styles.col}>
          {rightGroups.map((g) => (
            <GroupCard key={g} name={g} onCheckZones={checkZones} cfg={cfg} set={set} />
          ))}
        </div>
      </div>

      <Card style={styles.saveBar}>
        <Button variant="primary" size="lg" onClick={save} disabled={busy}>
          <Save size={16} />
          {busy ? 'Saving…' : 'Save configuration'}
        </Button>
        {saved && (
          <span style={{ fontSize: 13, color: saved.startsWith('✓') ? 'var(--ok)' : 'var(--danger)' }}>{saved}</span>
        )}
      </Card>

      {zoneOpen && (
        <ZoneModal
          loading={zoneLoading}
          error={zoneError}
          data={zoneData}
          currentZone={cfg.litensi_zone ?? ''}
          onClose={() => setZoneOpen(false)}
          onUse={useZone}
          onRefresh={checkZones}
        />
      )}

      <style>{layoutCSS}</style>
    </div>
  )
}

function GroupCard({ name, cfg, set, onCheckZones }) {
  const fields = FIELDS.filter((f) => {
    if (f.group !== name) return false
    // showIf: field only visible when another config field matches
    if (f.showIf) {
      for (const [k, v] of Object.entries(f.showIf)) {
        if ((cfg[k] || '') !== v) return false
      }
    }
    return true
  })
  if (fields.length === 0) return null
  return (
    <Card style={styles.card}>
      <div style={styles.groupTitle}>{name}</div>
      <div style={styles.fieldsGrid} className="cfg-fields">
        {fields.map((f) => (
          <div
            key={f.key}
            style={f.wide ? styles.fieldWide : styles.fieldHalf}
            className={f.wide ? 'cfg-field-wide' : 'cfg-field-half'}
          >
            <Field
              f={f}
              value={cfg[f.key]}
              onChange={(v) => set(f.key, v)}
              onCheckZones={f.hasZoneChecker ? onCheckZones : null}
            />
          </div>
        ))}
      </div>
      {name === 'Registration' && cfg.proxy_mode === 'list' && (
        <ProxyFileEditor />
      )}
    </Card>
  )
}

function Field({ f, value, onChange, onCheckZones }) {
  if (f.type === 'checkbox') {
    return (
      <label style={{ ...styles.field, flexDirection: 'row', alignItems: 'center', gap: 10 }}>
        <Input
          type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)}
          style={{ width: 16, height: 16, accentColor: 'var(--accent)' }}
        />
        <span style={{ fontSize: 13, color: 'var(--text)' }}>{f.label}</span>
      </label>
    )
  }
  if (f.type === 'select') {
    return (
      <label style={styles.field}>
        <span style={styles.label}>{f.label}</span>
        <select
          value={value ?? ''}
          onChange={(e) => onChange(e.target.value)}
          style={styles.select}
        >
          {(f.options || []).map((opt) => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      </label>
    )
  }
  if (f.type === 'textarea') {
    return (
      <label style={styles.field}>
        <span style={styles.label}>{f.label}</span>
        <textarea
          value={value ?? ''}
          onChange={(e) => onChange(e.target.value)}
          placeholder={f.placeholder || ''}
          rows={8}
          style={{
            width: '100%', minHeight: 120,
            border: '1px solid var(--border)', borderRadius: 10,
            padding: '10px 12px', outline: 'none',
            background: 'var(--bg-input)', color: 'var(--text-primary)',
            font: 'inherit', fontSize: 12.5, fontFamily: "'SF Mono', 'Fira Code', Menlo, monospace",
            resize: 'vertical',
          }}
        />
      </label>
    )
  }
  return (
    <label style={styles.field}>
      <span style={styles.label}>{f.label}</span>
      <div style={styles.inputRow}>
        <Input
          type={f.type === 'number' ? 'number' : 'text'}
          value={value ?? ''}
          onChange={(e) => onChange(e.target.value)}
          style={{ flex: 1, minWidth: 0 }}
        />
        {onCheckZones && <Button type="button" onClick={onCheckZones} className="config-check-zone" title="Retrieve Litensi zones for the configured site"><Search size={15} /> Check Zone</Button>}
      </div>
    </label>
  )
}

// injected via <style> — media queries can't live in inline styles
const layoutCSS = `
  /* Desktop lebar: 2 kolom sejajar */
  @media (min-width: 1024px) {
    .cfg-columns {
      grid-template-columns: 1fr 1fr !important;
    }
  }
  /* Tablet & narrower: 1 kolom */
  @media (max-width: 1023px) {
    .cfg-columns {
      grid-template-columns: 1fr !important;
    }
  }
  /* Dalam group card: field kompak jadi 2 kolom, field 'wide' full row */
  @media (min-width: 560px) {
    .cfg-fields {
      grid-template-columns: 1fr 1fr;
    }
    .cfg-field-half { grid-column: span 1; }
    .cfg-field-wide { grid-column: 1 / -1; }
  }
  @media (max-width: 559px) {
    .cfg-fields { grid-template-columns: 1fr; }
    .cfg-field-half, .cfg-field-wide { grid-column: 1 / -1; }
  }
`

function ProxyFileEditor() {
  const [content, setContent] = useState('')
  const [count, setCount] = useState(0)
  const [path, setPath] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.get('/api/proxies').then((d) => {
      setContent(d.content || '')
      setCount(d.count || 0)
      setPath(d.path || 'proxies.txt')
    }).catch(() => { setStatus('Failed to load proxy file') })
  }, [])

  async function save() {
    setBusy(true)
    try {
      const d = await api.put('/api/proxies', { content })
      setCount(d.count || 0)
      setPath(d.path || 'proxies.txt')
      setStatus(`Saved: ${d.count || 0} proxies in ${d.path}`)
    } catch (e) {
      setStatus('Error: ' + e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ marginTop: 14, borderTop: '1px solid var(--border)', paddingTop: 14 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
        <FileText size={15} style={{ color: 'var(--accent)' }} />
        <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)' }}>Proxy File Editor</span>
        <Badge tone="muted">{count} proxies</Badge>
        <span style={{ fontSize: 11, color: 'var(--muted)' }}>({path})</span>
      </div>
      <textarea
        value={content}
        onChange={(e) => { setContent(e.target.value); setStatus('') }}
        placeholder={'http://user:pass@host1:port\nhttp://user:pass@host2:port\n...'}
        rows={10}
        style={{
          width: '100%', minHeight: 160,
          border: '1px solid var(--border)', borderRadius: 10,
          padding: '10px 12px', outline: 'none',
          background: 'var(--bg-input)', color: 'var(--text-primary)',
          font: 'inherit', fontSize: 12,
          fontFamily: "'SF Mono', 'Fira Code', Menlo, monospace",
          resize: 'vertical', lineHeight: 1.6,
        }}
      />
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 8 }}>
        <Button size="sm" variant="primary" onClick={save} disabled={busy}>
          <Upload size={14} /> {busy ? 'Saving...' : 'Save proxy file'}
        </Button>
        {status && (
          <span style={{ fontSize: 12, color: status.startsWith('Saved') ? 'var(--ok)' : 'var(--danger)' }}>
            {status}
          </span>
        )}
      </div>
    </div>
  )
}

function ZoneModal({ loading, error, data, currentZone, onClose, onUse, onRefresh }) {
  const zones = (data?.zones || []).slice().sort((a, b) => {
    // in-stock first, then by price asc
    const sa = a.stock > 0 ? 0 : 1
    const sb = b.stock > 0 ? 0 : 1
    if (sa !== sb) return sa - sb
    return a.price - b.price
  })

  return (
    <div style={styles.modalBackdrop} onClick={onClose}>
      <div className="glass" style={styles.modal} onClick={(e) => e.stopPropagation()}>
        <div style={styles.modalHead}>
          <div>
            <div style={styles.modalTitle}>Litensi Zones</div>
            <div style={styles.modalSub}>
              {data?.site
                ? <>Site: <b style={{ color: 'var(--text)' }}>{data.site}</b></>
                : 'Available zones for your site'}
            </div>
          </div>
          <button className="glass-btn" onClick={onClose} style={{ padding: '6px 12px' }}>✕</button>
        </div>

        <div style={styles.modalBody}>
          {loading && <div style={styles.center}>Loading zones…</div>}
          {!loading && error && (
            <div style={styles.errorBox}>
              <div style={{ color: 'var(--danger)', fontWeight: 600, marginBottom: 6 }}>⚠ Failed</div>
              <div style={{ fontSize: 12.5, color: 'var(--muted)', wordBreak: 'break-word' }}>{error}</div>
            </div>
          )}
          {!loading && !error && data && zones.length === 0 && (
            <div style={styles.center}>No zones are available for this site.</div>
          )}
          {!loading && !error && zones.length > 0 && (
            <>
              <div style={styles.legend}>
                <span>Total: <b>{zones.length}</b></span>
                <span style={{ color: 'var(--ok)' }}>Available: <b>{zones.filter((z) => z.stock > 0).length}</b></span>
                {data.cheapest && (
                  <span style={{ color: 'var(--accent)' }}>Cheapest: <b>{data.cheapest}</b></span>
                )}
              </div>
              <div style={styles.tableWrap}>
                <table style={styles.table}>
                  <thead>
                    <tr>
                      <th style={styles.th}>Zone</th>
                      <th style={{ ...styles.th, textAlign: 'right' }}>Harga</th>
                      <th style={{ ...styles.th, textAlign: 'right' }}>Stok</th>
                      <th style={styles.th}></th>
                    </tr>
                  </thead>
                  <tbody>
                    {zones.map((z) => {
                      const isCurrent = currentZone && currentZone === z.zone
                      const isCheapest = data.cheapest && data.cheapest === z.zone
                      const outOfStock = z.stock <= 0
                      return (
                        <tr key={z.zone} style={outOfStock ? { opacity: 0.5 } : undefined}>
                          <td style={styles.td}>
                            <div style={styles.zoneCell}>
                              <span style={{ fontWeight: 700 }}>{z.zone || '—'}</span>
                              {isCurrent && <span className="badge accent" style={styles.tag}>current</span>}
                              {isCheapest && !isCurrent && <span className="badge accent" style={styles.tag}>termurah</span>}
                            </div>
                          </td>
                          <td style={{ ...styles.td, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                            {formatPrice(z.price)}
                          </td>
                          <td style={{ ...styles.td, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
                            <span style={{ color: outOfStock ? 'var(--danger)' : 'var(--ok)', fontWeight: 600 }}>
                              {formatPrice(Math.round(z.stock))}
                            </span>
                          </td>
                          <td style={{ ...styles.td, textAlign: 'right' }}>
                            <button
                              className="glass-btn"
                              disabled={outOfStock}
                              onClick={() => onUse(z.zone)}
                              style={{ padding: '6px 12px', fontSize: 12 }}
                            >
                              Use
                            </button>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>

        <div style={styles.modalFoot}>
          <button className="glass-btn" onClick={onRefresh} disabled={loading}>
            ⟳ Refresh
          </button>
          {!loading && !error && zones.length > 0 && (
            <button
              className="glass-btn primary"
              onClick={() => onUse('')}
              title="Clear the zone field and let the runner select the cheapest option"
            >
              Clear (automatic cheapest)
            </button>
          )}
          <div style={{ flex: 1 }} />
          <button className="glass-btn" onClick={onClose}>Close</button>
        </div>
      </div>
      <style>{modalCSS}</style>
    </div>
  )
}

function formatPrice(n) {
  if (!Number.isFinite(n)) return '—'
  try {
    // Litensi returns integer credits (e.g. 180). Format with thousands sep.
    return new Intl.NumberFormat('id-ID', { maximumFractionDigits: 0 }).format(n)
  } catch {
    return String(n)
  }
}

const styles = {
  wrap: {
    display: 'flex', flexDirection: 'column', gap: 14,
    maxWidth: 1200, width: '100%', margin: '0 auto',
  },
  columns: {
    display: 'grid',
    gridTemplateColumns: '1fr',  // overridden by media query
    gap: 14,
    alignItems: 'start',
  },
  col: { display: 'flex', flexDirection: 'column', gap: 14, minWidth: 0 },
  card: { padding: 22, minWidth: 0 },
  groupTitle: {
    fontSize: 11.5, fontWeight: 700, letterSpacing: 1.2, textTransform: 'uppercase',
    color: 'var(--accent)', marginBottom: 16,
  },
  fieldsGrid: {
    display: 'grid',
    gap: '14px 16px',  // row-gap, col-gap
  },
  fieldHalf: { minWidth: 0 },
  fieldWide: { minWidth: 0 },
  field: {
    display: 'flex', flexDirection: 'column', gap: 7,
    fontSize: 13, color: 'var(--muted)',
  },
  label: { fontWeight: 500 },
  select: {
    width: '100%', minHeight: 38,
    border: '1px solid var(--border)', borderRadius: 10,
    padding: '8px 12px', outline: 'none',
    background: 'var(--bg-input)', color: 'var(--text-primary)',
    font: 'inherit', fontSize: 13, cursor: 'pointer',
    transition: 'border-color 160ms ease, box-shadow 180ms ease',
  },
  inputRow: { display: 'flex', gap: 8, alignItems: 'stretch', flexWrap: 'wrap' },
  checkBtn: { padding: '10px 14px', fontSize: 12.5, whiteSpace: 'nowrap', flexShrink: 0 },
  saveBar: {
    padding: 18, display: 'flex', alignItems: 'center', gap: 14,
    position: 'sticky', bottom: 0, flexWrap: 'wrap', zIndex: 5,
  },

  // modal
  modalBackdrop: {
    position: 'fixed', inset: 0, zIndex: 100,
    background: 'rgba(0,0,0,0.55)',
    backdropFilter: 'blur(4px)',
    WebkitBackdropFilter: 'blur(4px)',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    padding: 16,
    animation: 'fadeIn 0.2s ease',
  },
  modal: {
    width: '100%', maxWidth: 640, maxHeight: '85vh',
    display: 'flex', flexDirection: 'column',
    padding: 0, overflow: 'hidden',
  },
  modalHead: {
    padding: '18px 22px 14px',
    display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 12,
    borderBottom: '1px solid var(--glass-border)',
  },
  modalTitle: { fontSize: 17, fontWeight: 800, letterSpacing: -0.3 },
  modalSub: { fontSize: 12.5, color: 'var(--muted)', marginTop: 4 },
  modalBody: { padding: '16px 22px', overflow: 'auto', flex: 1 },
  modalFoot: {
    padding: '14px 22px', display: 'flex', gap: 10, flexWrap: 'wrap',
    borderTop: '1px solid var(--glass-border)',
    background: 'var(--bg-input)',
  },
  center: { textAlign: 'center', padding: '32px 12px', color: 'var(--muted)', fontSize: 13.5 },
  errorBox: {
    padding: '14px 16px', borderRadius: 12,
    background: 'rgba(var(--danger-rgb),0.09)',
    border: '1px solid rgba(var(--danger-rgb),0.3)',
  },
  legend: {
    display: 'flex', gap: 16, flexWrap: 'wrap',
    fontSize: 12.5, color: 'var(--muted)', marginBottom: 12,
  },
  tableWrap: { overflowX: 'auto', margin: '0 -4px' },
  table: { width: '100%', borderCollapse: 'collapse', fontSize: 13 },
  th: {
    textAlign: 'left', fontWeight: 700, fontSize: 11,
    color: 'var(--muted)', textTransform: 'uppercase', letterSpacing: 0.5,
    padding: '8px 10px', borderBottom: '1px solid var(--glass-border)',
    position: 'sticky', top: 0,
    background: 'rgba(23,33,43,0.97)',
  },
  td: {
    padding: '10px', borderBottom: '1px solid var(--border)',
    verticalAlign: 'middle',
  },
  zoneCell: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' },
  tag: { fontSize: 10, padding: '2px 8px' },
}

const modalCSS = `
  @keyframes fadeIn { from { opacity: 0 } to { opacity: 1 } }
  @media (max-width: 520px) {
    .glass-btn { font-size: 12px; }
  }
`
