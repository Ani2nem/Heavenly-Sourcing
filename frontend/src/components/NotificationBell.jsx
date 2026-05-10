import { useEffect, useRef, useState } from 'react'
import { apiClient } from '../services/api'

function formatRelative(iso) {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  const diff = Math.max(0, Date.now() - t)
  const mins = Math.round(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.round(hrs / 24)
  return `${days}d ago`
}

export default function NotificationBell() {
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState([])
  const [unread, setUnread] = useState(0)
  const ref = useRef(null)

  const fetchItems = () => {
    apiClient.get('/api/notifications/recent?limit=15')
      .then(res => {
        setItems(res.data?.items || [])
        setUnread(res.data?.unread_count || 0)
      })
      .catch(() => {})
  }

  useEffect(() => {
    fetchItems()
    const interval = setInterval(fetchItems, 10000)
    return () => clearInterval(interval)
  }, [])

  useEffect(() => {
    const onClick = e => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [])

  const markAllRead = async () => {
    try {
      await apiClient.post('/api/notifications/mark-all-read')
      fetchItems()
    } catch {
      /* swallow */
    }
  }

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="relative w-9 h-9 flex items-center justify-center rounded-md hover:bg-slate-800 text-slate-200"
        aria-label="Notifications"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" className="w-5 h-5">
          <path strokeLinecap="round" strokeLinejoin="round" d="M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 01-5.714 0m5.714 0a3 3 0 11-5.714 0" />
        </svg>
        {unread > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1 rounded-full bg-emerald-500 text-white text-[10px] font-semibold flex items-center justify-center">
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute left-full top-0 ml-2 w-80 bg-white text-slate-800 rounded-lg shadow-lg border border-slate-200 z-50">
          <div className="flex items-center justify-between px-3 py-2 border-b border-slate-100">
            <span className="text-sm font-semibold">Notifications</span>
            <button
              type="button"
              onClick={markAllRead}
              disabled={unread === 0}
              className="text-xs text-emerald-700 disabled:text-slate-300 hover:underline"
            >
              Mark all read
            </button>
          </div>
          <div className="max-h-80 overflow-y-auto">
            {items.length === 0 && (
              <div className="px-3 py-6 text-center text-sm text-slate-400">
                No notifications yet.
              </div>
            )}
            {items.map(n => (
              <div
                key={n.id}
                className={`px-3 py-2 border-b border-slate-50 last:border-b-0 ${
                  n.is_read ? 'bg-white' : 'bg-emerald-50/40'
                }`}
              >
                <div className="flex items-start gap-2">
                  {!n.is_read && (
                    <span className="mt-1.5 w-1.5 h-1.5 rounded-full bg-emerald-500 flex-shrink-0" />
                  )}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-xs font-semibold text-slate-700 truncate">{n.title}</span>
                      <span className="text-[10px] text-slate-400 flex-shrink-0">{formatRelative(n.created_at)}</span>
                    </div>
                    <p className="text-xs text-slate-600 mt-0.5 leading-snug break-words">{n.message}</p>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
