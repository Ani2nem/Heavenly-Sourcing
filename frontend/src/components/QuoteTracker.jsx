import { useState, useEffect, useCallback } from 'react'
import { toast } from 'react-toastify'
import { apiClient } from '../services/api'

const STATUS_BADGE = {
  PENDING: 'bg-slate-100 text-slate-600',
  FOLLOW_UP_SENT: 'bg-yellow-100 text-yellow-700',
  RECEIVED: 'bg-blue-100 text-blue-700',
  APPROVED: 'bg-emerald-100 text-emerald-700',
}

export default function QuoteTracker() {
  const [cycle, setCycle] = useState(null)
  const [approving, setApproving] = useState(false)
  const [pinging, setPinging] = useState(null)

  const fetchCycle = useCallback(() => {
    apiClient.get('/api/procurement/cycle/active').then(res => {
      setCycle(res.data)
    }).catch(() => {})
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

  const approve = async distributorId => {
    setApproving(true)
    try {
      const res = await apiClient.post('/api/procurement/cycle/active/approve', {
        selected_distributor_id: distributorId,
      })
      toast.success('Purchase order sent! Cycle complete.')
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

  return (
    <div className="max-w-4xl space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Quote Tracker</h1>
        <p className="text-slate-500 text-sm mt-1">
          Cycle <code className="bg-slate-100 px-1 rounded text-xs">{cycle.cycle_id.slice(0, 8)}…</code>
          &nbsp;·&nbsp;{cycle.preferred_delivery_window} delivery
          &nbsp;·&nbsp;<span className="capitalize">{cycle.status.toLowerCase().replace('_', ' ')}</span>
        </p>
      </div>

      {/* Recommendation card */}
      {topRec?.recommendation_text && (
        <div className="bg-emerald-50 border border-emerald-200 rounded-lg p-4">
          <p className="text-xs font-semibold text-emerald-700 uppercase tracking-wide mb-1">AI Recommendation</p>
          <p className="text-slate-700 text-sm leading-relaxed">{topRec.recommendation_text}</p>
        </div>
      )}

      {/* Quote table */}
      <div className="bg-white border border-slate-200 rounded-lg shadow-sm overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 border-b border-slate-200">
            <tr>
              <th className="text-left px-4 py-3 font-medium text-slate-600">Distributor</th>
              <th className="text-right px-4 py-3 font-medium text-slate-600">Score</th>
              <th className="text-right px-4 py-3 font-medium text-slate-600">Total Price</th>
              <th className="text-left px-4 py-3 font-medium text-slate-600">Status</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {cycle.quotes.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-slate-400">
                  Waiting for quotes — RFPs dispatched…
                </td>
              </tr>
            )}
            {cycle.quotes.map(q => (
              <tr key={q.quote_id} className="even:bg-slate-50">
                <td className="px-4 py-3 font-medium">{q.distributor_name}</td>
                <td className="px-4 py-3 text-right">
                  {q.score != null ? (
                    <span className={`font-semibold ${q.score >= 80 ? 'text-emerald-600' : q.score >= 60 ? 'text-yellow-600' : 'text-slate-500'}`}>
                      {q.score.toFixed(1)}
                    </span>
                  ) : '—'}
                </td>
                <td className="px-4 py-3 text-right">
                  {q.total_quoted_price != null ? `$${q.total_quoted_price.toFixed(2)}` : '—'}
                </td>
                <td className="px-4 py-3">
                  <span className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_BADGE[q.quote_status] || 'bg-slate-100 text-slate-600'}`}>
                    {q.quote_status.replace('_', ' ')}
                  </span>
                </td>
                <td className="px-4 py-3 text-right space-x-2 whitespace-nowrap">
                  {q.quote_status === 'PENDING' || q.quote_status === 'FOLLOW_UP_SENT' ? (
                    <button
                      onClick={() => ping(q.quote_id)}
                      disabled={pinging === q.quote_id}
                      className="text-xs px-2 py-1 border border-slate-300 rounded hover:bg-slate-100 disabled:opacity-50"
                    >
                      {pinging === q.quote_id ? '…' : 'Ping'}
                    </button>
                  ) : null}
                  {q.quote_status === 'RECEIVED' && cycle.status !== 'COMPLETED' && (
                    <button
                      onClick={() => approve(q.distributor_id)}
                      disabled={approving}
                      className="text-xs px-3 py-1 bg-emerald-600 text-white rounded hover:bg-emerald-700 disabled:opacity-50"
                    >
                      {approving ? '…' : 'Approve & Purchase'}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Expanded line items for received quotes */}
      {receivedQuotes.filter(q => q.items?.length).map(q => (
        <div key={q.quote_id} className="bg-white border border-slate-200 rounded-lg shadow-sm overflow-hidden">
          <div className="px-4 py-3 bg-slate-50 border-b border-slate-200">
            <span className="font-medium text-sm">{q.distributor_name} — Line Items</span>
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-slate-500 text-xs uppercase tracking-wide border-b border-slate-100">
                <th className="px-4 py-2">Ingredient</th>
                <th className="px-4 py-2 text-right">Unit Price</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-50">
              {q.items.map((item, i) => (
                <tr key={i} className="even:bg-slate-50">
                  <td className="px-4 py-2">{item.ingredient_name}</td>
                  <td className="px-4 py-2 text-right">
                    {item.quoted_price_per_unit != null ? `$${item.quoted_price_per_unit.toFixed(2)}` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  )
}
