import { useState, useEffect, useCallback } from 'react'
import { toast } from 'react-toastify'
import { apiClient } from '../services/api'
import ComparisonMatrix from './ComparisonMatrix'

const STATUS_BADGE = {
  PENDING: 'bg-slate-100 text-slate-600',
  FOLLOW_UP_SENT: 'bg-yellow-100 text-yellow-700',
  RECEIVED: 'bg-blue-100 text-blue-700',
  APPROVED: 'bg-emerald-100 text-emerald-700',
  DECLINED: 'bg-rose-100 text-rose-700',
}

export default function QuoteTracker() {
  const [cycle, setCycle] = useState(null)
  const [comparison, setComparison] = useState(null)
  const [approving, setApproving] = useState(false)
  const [pinging, setPinging] = useState(null)

  const fetchCycle = useCallback(async () => {
    try {
      const cycleRes = await apiClient.get('/api/procurement/cycle/active')
      setCycle(cycleRes.data)
      if (cycleRes.data) {
        try {
          const cmpRes = await apiClient.get('/api/procurement/cycle/active/comparison')
          setComparison(cmpRes.data)
        } catch {
          setComparison(null)
        }
      } else {
        setComparison(null)
      }
    } catch {
      // silent
    }
  }, [])

  useEffect(() => {
    fetchCycle()
    const interval = setInterval(fetchCycle, 10000)
    return () => clearInterval(interval)
  }, [fetchCycle])

  const ping = async quoteId => {
    setPinging(quoteId)
    try {
      await apiClient.post(`/api/procurement/quotes/${quoteId}/ping`)
      toast.info('Follow-up email sent to vendor')
      fetchCycle()
    } catch {
      toast.error('Failed to send follow-up')
    } finally {
      setPinging(null)
    }
  }

  const approveOptimal = async () => {
    setApproving(true)
    try {
      const res = await apiClient.post('/api/procurement/cycle/active/approve-optimal')
      toast.success(
        `Sent ${res.data.pos.length} PO(s) — total $${res.data.grand_total.toFixed(2)}`
      )
      fetchCycle()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Approval failed')
    } finally {
      setApproving(false)
    }
  }

  if (!cycle) {
    return (
      <div className="text-slate-500 text-sm">
        No active procurement cycle. <a href="/procurement" className="text-emerald-600 underline">Start one.</a>
      </div>
    )
  }

  const receivedQuotes = cycle.quotes.filter(q => q.quote_status === 'RECEIVED')
  const topRec = receivedQuotes.find(q => q.recommendation_text)
  const canApprove = comparison?.rows?.length > 0 && cycle.status !== 'AWAITING_RECEIPT' && cycle.status !== 'COMPLETED'

  return (
    <div className="max-w-5xl space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Quote Tracker</h1>
          <p className="text-slate-500 text-sm mt-1">
            Cycle <code className="bg-slate-100 px-1 rounded text-xs">{cycle.cycle_id.slice(0, 8)}…</code>
            &nbsp;·&nbsp;<span className="capitalize">{cycle.status.toLowerCase().replace(/_/g, ' ')}</span>
            {comparison?.grand_total != null && (
              <>
                &nbsp;·&nbsp;<span className="font-medium text-slate-700">
                  Optimal cart total: ${comparison.grand_total.toFixed(2)}
                </span>
              </>
            )}
          </p>
        </div>

        {canApprove && (
          <button
            onClick={approveOptimal}
            disabled={approving}
            className="px-4 py-2 bg-emerald-600 text-white text-sm font-medium rounded shadow-sm hover:bg-emerald-700 disabled:opacity-50"
          >
            {approving ? 'Sending POs…' : 'Approve Optimal Cart'}
          </button>
        )}
      </div>

      {/* Discovery in progress — shown immediately after Procure is pressed
          while the background task is geocoding + hitting Google Places. Only
          flips to the empty-state banner if discovery actually finishes with
          0 distributors. */}
      {cycle.status === 'DISCOVERING_DISTRIBUTORS' && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 text-sm text-blue-800 flex items-start gap-3">
          <div className="mt-0.5 h-4 w-4 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />
          <div>
            <p className="font-medium">Finding local distributors…</p>
            <p className="mt-1 text-xs text-blue-700">
              Geocoding your zip and ladder-searching Google Places out to 50 miles.
              This usually takes 5–15 seconds.
            </p>
          </div>
        </div>
      )}

      {/* No-distributor banner — only shown AFTER discovery finished with 0 hits */}
      {cycle.status !== 'DISCOVERING_DISTRIBUTORS' &&
        cycle.distributor_count === 0 &&
        cycle.quotes.length === 0 && (
        <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-4 text-sm text-yellow-800">
          <p className="font-medium">No distributors found near your zip.</p>
          <p className="mt-1">
            Make sure both <code className="bg-yellow-100 px-1 rounded">Geocoding API</code> and{' '}
            <code className="bg-yellow-100 px-1 rounded">Places API (New)</code> are enabled in your
            Google Cloud project, and that <code className="bg-yellow-100 px-1 rounded">GOOGLE_PLACES_API_KEY</code>{' '}
            in <code>backend/.env</code> has access to both.
          </p>
        </div>
      )}

      {/* Awaiting receipt banner */}
      {cycle.status === 'AWAITING_RECEIPT' && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 text-sm text-blue-800">
          POs sent — waiting for vendors to email receipts. They'll appear in
          Purchase History as soon as they arrive.
        </div>
      )}

      {/* Recommendation card */}
      {topRec?.recommendation_text && (
        <div className="bg-emerald-50 border border-emerald-200 rounded-lg p-4">
          <p className="text-xs font-semibold text-emerald-700 uppercase tracking-wide mb-1">AI Recommendation</p>
          <p className="text-slate-700 text-sm leading-relaxed whitespace-pre-line">{topRec.recommendation_text}</p>
        </div>
      )}

      {/* Per-vendor summary table */}
      <div className="bg-white border border-slate-200 rounded-lg shadow-sm overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 border-b border-slate-200">
            <tr>
              <th className="text-left px-4 py-3 font-medium text-slate-600">Distributor</th>
              <th className="text-right px-4 py-3 font-medium text-slate-600">Win Rate</th>
              <th className="text-right px-4 py-3 font-medium text-slate-600">Items Won</th>
              <th className="text-right px-4 py-3 font-medium text-slate-600">Won Total</th>
              <th className="text-left px-4 py-3 font-medium text-slate-600">Status</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {cycle.quotes.length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-6 text-center text-slate-400">
                  Waiting for quotes — RFPs dispatched…
                </td>
              </tr>
            )}
            {cycle.quotes.map(q => {
              const v = comparison?.vendors?.find(v => v.distributor_id === q.distributor_id)
              const winRate = v ? (v.items_quoted ? v.items_won / v.items_quoted : 0) : null
              return (
                <tr key={q.quote_id} className="even:bg-slate-50">
                  <td className="px-4 py-3 font-medium">{q.distributor_name}</td>
                  <td className="px-4 py-3 text-right">
                    {winRate != null ? (
                      <span className="text-slate-600">
                        {(winRate * 100).toFixed(0)}%
                      </span>
                    ) : '—'}
                  </td>
                  <td className="px-4 py-3 text-right text-slate-600">
                    {v ? `${v.items_won} / ${v.items_quoted}` : '—'}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {v?.won_total != null ? `$${v.won_total.toFixed(2)}` : '—'}
                  </td>
                  <td className="px-4 py-3">
                    <span className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_BADGE[q.quote_status] || 'bg-slate-100 text-slate-600'}`}>
                      {q.quote_status.replace(/_/g, ' ')}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right whitespace-nowrap">
                    {(q.quote_status === 'PENDING' || q.quote_status === 'FOLLOW_UP_SENT') && (
                      <button
                        onClick={() => ping(q.quote_id)}
                        disabled={pinging === q.quote_id}
                        className="text-xs px-2 py-1 border border-slate-300 rounded hover:bg-slate-100 disabled:opacity-50"
                      >
                        {pinging === q.quote_id ? '…' : 'Ping'}
                      </button>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* Per-ingredient comparison matrix */}
      {comparison && <ComparisonMatrix data={comparison} />}
    </div>
  )
}
