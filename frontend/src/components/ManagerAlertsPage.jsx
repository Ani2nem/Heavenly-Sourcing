import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'react-toastify'
import { apiClient } from '../services/api'

/**
 * Manager alerts inbox — Phase 5/6 actionable items (renewal outreach, awards).
 */
export default function ManagerAlertsPage() {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const { data } = await apiClient.get('/api/alerts/manager')
      setRows(data || [])
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to load alerts')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const markRead = async (id) => {
    try {
      await apiClient.patch(`/api/alerts/manager/${id}/read`)
      load()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Update failed')
    }
  }

  if (loading) return <p className="text-slate-500">Loading alerts…</p>

  return (
    <div className="max-w-3xl space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Manager alerts</h1>
        <p className="text-sm text-slate-500 mt-1">
          Contract renewal outreach, awards, and escalations — separate from the weekly ingredient bid cycle on{' '}
          <Link to="/quotes" className="text-emerald-700 font-medium hover:underline">Quotes</Link>
          {' / '}
          <Link to="/procurement" className="text-emerald-700 font-medium hover:underline">Procurement</Link>.
          SMS fires when you opt in on your profile and Twilio is configured.
        </p>
      </header>
      {rows.length === 0 ? (
        <p className="text-sm text-slate-500">No alerts yet.</p>
      ) : (
        <ul className="space-y-3">
          {rows.map((a) => (
            <li
              key={a.id}
              className={`border rounded-lg p-4 text-sm ${
                a.is_read ? 'border-slate-200 bg-white' : 'border-violet-200 bg-violet-50/40'
              }`}
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="font-semibold text-slate-800">{a.title}</p>
                  <p className="text-slate-600 mt-1 whitespace-pre-wrap">{a.body}</p>
                  <p className="text-[10px] text-slate-400 mt-2">
                    {a.severity}
                    {a.delivered_sms_at && ` · SMS delivered ${a.delivered_sms_at}`}
                  </p>
                  {a.action_url && (
                    <a
                      href={a.action_url}
                      className="inline-block mt-2 text-xs text-emerald-700 font-medium hover:underline"
                    >
                      {a.action_label || 'Open'} →
                    </a>
                  )}
                </div>
                {!a.is_read && (
                  <button
                    type="button"
                    onClick={() => markRead(a.id)}
                    className="shrink-0 px-2 py-1 text-xs border border-slate-300 rounded hover:bg-white"
                  >
                    Mark read
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
