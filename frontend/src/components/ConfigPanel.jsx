import React, { useEffect, useState } from 'react'
import { api } from '../api.js'

const FIELDS = [
  { key: 'litensi_api_id', label: 'Litensi API ID', group: 'Litensi Mail' },
  { key: 'litensi_api_key', label: 'Litensi API Key', secret: true, group: 'Litensi Mail' },
  { key: 'litensi_site', label: 'Site (domain, contoh: github.com)', group: 'Litensi Mail' },
  { key: 'litensi_zone', label: 'Zone (kosong = otomatis termurah)', group: 'Litensi Mail' },
  { key: 'register_count', label: 'Register Count', type: 'number', group: 'Registration' },
  { key: 'proxy', label: 'Proxy (http://user:pass@host:port)', secret: true, group: 'Registration' },
  { key: 'headless', label: 'Headless (tanpa window — kurang stabil)', type: 'checkbox', group: 'Registration' },
  { key: 'delay_sec', label: 'Delay antar akun (detik)', type: 'number', group: 'Registration' },
  { key: 'max_username_tries', label: 'Max username tries', type: 'number', group: 'Registration' },
  { key: 'otp_timeout_sec', label: 'OTP timeout (detik)', type: 'number', group: 'Registration' },
  { key: 'browser_profile_dir', label: 'Browser profile dir (DataDome trust)', group: 'Advanced' },
  { key: 'create_repo', label: 'Buat repository pertama setelah signup', type: 'checkbox', group: 'Post-Signup Stages' },
  { key: 'repo_name', label: 'Nama repository', group: 'Post-Signup Stages' },
  { key: 'enable_2fa', label: 'Aktifkan 2FA (TOTP) + simpan secret', type: 'checkbox', group: 'Post-Signup Stages' },
]

export default function ConfigPanel() {
  const [cfg, setCfg] = useState(null)
  const [saved, setSaved] = useState('')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api.get('/api/config').then((d) => setCfg(d.config)).catch(() => {})
  }, [])

  if (!cfg) return <div style={{ color: 'var(--muted)', padding: 20 }}>Loading config…</div>

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
      setSaved('✓ Config tersimpan')
    } catch (e) {
      setSaved('✗ ' + e.message)
    } finally {
      setBusy(false)
    }
  }

  const groups = [...new Set(FIELDS.map((f) => f.group))]

  return (
    <div style={styles.wrap}>
      {groups.map((g) => (
        <div className="glass" key={g} style={{ padding: 22 }}>
          <div style={styles.groupTitle}>{g}</div>
          {FIELDS.filter((f) => f.group === g).map((f) => (
            <Field key={f.key} f={f} value={cfg[f.key]} onChange={(v) => set(f.key, v)} />
          ))}
        </div>
      ))}

      <div className="glass" style={{ padding: 18, display: 'flex', alignItems: 'center', gap: 14, position: 'sticky', bottom: 0 }}>
        <button className="glass-btn primary" style={{ padding: '12px 36px', fontSize: 14 }} onClick={save} disabled={busy}>
          {busy ? 'Menyimpan…' : 'Simpan Config'}
        </button>
        {saved && (
          <span style={{ fontSize: 13, color: saved.startsWith('✓') ? 'var(--ok)' : 'var(--danger)' }}>{saved}</span>
        )}
      </div>
    </div>
  )
}

function Field({ f, value, onChange }) {
  if (f.type === 'checkbox') {
    return (
      <label style={{ ...styles.field, flexDirection: 'row', alignItems: 'center', gap: 10 }}>
        <input
          type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)}
          style={{ width: 16, height: 16, accentColor: '#00adb5' }}
        />
        <span style={{ fontSize: 13, color: 'var(--text)' }}>{f.label}</span>
      </label>
    )
  }
  return (
    <label style={styles.field}>
      <span style={styles.label}>{f.label}</span>
      <input
        type={f.type === 'number' ? 'number' : 'text'}
        className="glass-input"
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value)}
      />
    </label>
  )
}

const styles = {
  wrap: { display: 'flex', flexDirection: 'column', gap: 14, maxWidth: 720, width: '100%', margin: '0 auto' },
  groupTitle: {
    fontSize: 11.5, fontWeight: 700, letterSpacing: 1.2, textTransform: 'uppercase',
    color: 'var(--accent)', marginBottom: 16,
  },
  field: { display: 'flex', flexDirection: 'column', gap: 7, marginBottom: 15, fontSize: 13, color: 'var(--muted)' },
  label: { fontWeight: 500 },
}
