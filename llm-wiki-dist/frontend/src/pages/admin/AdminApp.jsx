import { useEffect, useState } from 'react'

const API = (() => {
  const path = window.location.pathname
  const i = path.indexOf('/admin')
  return (import.meta.env.VITE_ADMIN_API_BASE || `${i < 0 ? '' : path.slice(0, i)}/admin/api`).replace(/\/$/, '')
})()

async function request(path, options = {}, password = '') {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-Admin-Password': password, ...(options.headers || {}) },
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || response.statusText)
  return data
}

const fieldClass = 'mt-1 w-full rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm outline-none focus:border-blue-500'

export default function AdminApp() {
  const [password, setPassword] = useState('')
  const [loggedIn, setLoggedIn] = useState(false)
  const [status, setStatus] = useState(null)
  const [form, setForm] = useState({ name: 'local', url: '', api_token: '', mode: 'attach', root_path: '/', write_path: '/inbox' })
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  const load = async (secret = password) => {
    setBusy('load')
    setError('')
    try {
      const next = await request('/status', {}, secret)
      setStatus(next)
      const c = next.connection
      if (c) setForm((old) => ({ ...old, ...c, api_token: '', name: c.name || old.name }))
      setLoggedIn(true)
    } catch (e) {
      setLoggedIn(false)
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  useEffect(() => { if (password) load(password) }, [])

  const save = async () => {
    setBusy('save'); setError('')
    try {
      const payload = { ...form, api_token: form.api_token || undefined }
      const data = await request(`/connections/${encodeURIComponent(form.name || 'local')}`, { method: 'POST', body: JSON.stringify(payload) }, password)
      setStatus((old) => ({ ...old, connection: data }))
      setForm((old) => ({ ...old, api_token: '' }))
      await load()
    } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  const action = async (name, path, options = {}) => {
    setBusy(name); setError('')
    try { await request(path, options, password); await load() } catch (e) { setError(e.message) } finally { setBusy('') }
  }

  if (!loggedIn) return (
    <main className="grid min-h-screen place-items-center bg-slate-100 p-6">
      <form onSubmit={(e) => { e.preventDefault(); load() }} className="w-full max-w-sm rounded-2xl bg-white p-6 shadow-sm">
        <h1 className="text-xl font-bold text-slate-900">LLM-Wiki Admin</h1>
        <p className="mt-2 text-sm text-slate-500">管理者パスワードを入力してください。</p>
        <label className="mt-5 block text-sm font-semibold text-slate-700">Admin password<input autoFocus type="password" value={password} onChange={(e) => setPassword(e.target.value)} className={fieldClass} /></label>
        {error && <p className="mt-3 text-sm text-red-700">{error}</p>}
        <button disabled={!password || busy} className="mt-5 w-full rounded-xl bg-blue-600 px-4 py-2 text-sm font-bold text-white disabled:opacity-50">{busy ? 'Loading…' : 'Login'}</button>
      </form>
    </main>
  )

  const connection = status?.connection
  return (
    <main className="min-h-screen bg-slate-100 p-6 text-slate-900">
      <div className="mx-auto max-w-4xl space-y-5">
        <header className="flex items-center justify-between">
          <div><h1 className="text-2xl font-bold">LLM-Wiki Admin</h1><p className="text-sm text-slate-500">One engine, with GROWI as the source of truth.</p></div>
          <button onClick={() => { setLoggedIn(false); setPassword('') }} className="rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm">Log out</button>
        </header>

        {error && <div className="rounded-xl border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</div>}

        <section className="rounded-2xl bg-white p-5 shadow-sm">
          <h2 className="text-lg font-bold">Engine</h2>
          <p className="mt-2 text-sm text-slate-600">Stage: <b>{status?.stage || 'not_started'}</b>{status?.error ? ` — ${status.error}` : ''}</p>
          <p className="mt-1 text-xs text-slate-500">Data root: {status?.data_root || '—'}</p>
          <div className="mt-4 flex flex-wrap gap-2">{(status?.teams || []).map((team) => <a key={team} href={status.urls?.[team]} className="rounded-full bg-blue-50 px-3 py-1 text-sm font-semibold text-blue-700">{team}</a>)}</div>
          <button onClick={() => action('sync', '/sync', { method: 'POST' })} disabled={!!busy} className="mt-5 rounded-xl bg-blue-600 px-4 py-2 text-sm font-bold text-white disabled:opacity-50">{busy === 'sync' ? 'Syncing…' : 'Sync now'}</button>
        </section>

        <section className="rounded-2xl bg-white p-5 shadow-sm">
          <h2 className="text-lg font-bold">GROWI connection</h2>
          <p className="mt-1 text-sm text-slate-500">Token is write-only; an existing token is shown as configured.</p>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <label className="text-sm font-semibold">Name<input value={form.name} readOnly className={`${fieldClass} bg-slate-50`} /></label>
            <label className="text-sm font-semibold">URL<input value={form.url || ''} onChange={(e) => setForm({ ...form, url: e.target.value })} className={fieldClass} /></label>
            <label className="text-sm font-semibold">API token<input type="password" placeholder={connection?.has_token ? '•••••••• configured' : ''} value={form.api_token} onChange={(e) => setForm({ ...form, api_token: e.target.value })} className={fieldClass} /></label>
            <label className="text-sm font-semibold">Mode<select value={form.mode} onChange={(e) => setForm({ ...form, mode: e.target.value })} className={fieldClass}><option value="attach">attach</option><option value="own">own</option></select></label>
            <label className="text-sm font-semibold">Root path<input value={form.root_path} onChange={(e) => setForm({ ...form, root_path: e.target.value })} className={fieldClass} /></label>
            <label className="text-sm font-semibold">Write path<input value={form.write_path} onChange={(e) => setForm({ ...form, write_path: e.target.value })} className={fieldClass} /></label>
          </div>
          <div className="mt-5 flex flex-wrap gap-2">
            <button onClick={save} disabled={!!busy || !form.url || !form.api_token} className="rounded-xl bg-blue-600 px-4 py-2 text-sm font-bold text-white disabled:opacity-50">{busy === 'save' ? 'Saving…' : 'Save'}</button>
            <button onClick={() => action('test', `/connections/${encodeURIComponent(form.name)}/test`)} disabled={!!busy || !connection} className="rounded-xl border border-slate-300 px-4 py-2 text-sm font-bold disabled:opacity-50">Test</button>
            <button onClick={() => action('resync', `/connections/${encodeURIComponent(form.name)}/resync`, { method: 'POST' })} disabled={!!busy || !connection} className="rounded-xl border border-slate-300 px-4 py-2 text-sm font-bold disabled:opacity-50">Resync</button>
            <button onClick={() => window.confirm('GROWI remains untouched; the local index is dropped. Continue?') && action('detach', `/connections/${encodeURIComponent(form.name)}`, { method: 'DELETE' })} disabled={!!busy || !connection} className="rounded-xl border border-red-200 px-4 py-2 text-sm font-bold text-red-700 disabled:opacity-50">Detach</button>
          </div>
        </section>
      </div>
    </main>
  )
}
