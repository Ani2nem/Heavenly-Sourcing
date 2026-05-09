import { useState, useEffect } from 'react'
import { apiClient } from '../services/api'

export default function PurchaseHistory() {
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    apiClient.get('/api/purchase-history')
      .then(res => setHistory(res.data))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return <p className="text-slate-400 text-sm">Loading…</p>
  }

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-bold mb-6">Purchase History</h1>
      {history.length === 0 ? (
        <p className="text-slate-500 text-sm">No completed purchases yet.</p>
      ) : (
        <div className="bg-white border border-slate-200 rounded-lg shadow-sm overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 border-b border-slate-200">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-slate-600">Distributor</th>
                <th className="text-right px-4 py-3 font-medium text-slate-600">Total Cost</th>
                <th className="text-left px-4 py-3 font-medium text-slate-600">Date</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {history.map(row => (
                <tr key={row.id} className="even:bg-slate-50">
                  <td className="px-4 py-3 font-medium">{row.distributor_name}</td>
                  <td className="px-4 py-3 text-right">
                    {row.total_quoted_cost != null ? `$${row.total_quoted_cost.toFixed(2)}` : '—'}
                  </td>
                  <td className="px-4 py-3 text-slate-500">
                    {new Date(row.purchased_at).toLocaleDateString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
