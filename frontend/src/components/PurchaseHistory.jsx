import { useState, useEffect } from 'react'
import { toast } from 'react-toastify'
import { apiClient } from '../services/api'
import SupplyChainStagesGuide from './SupplyChainStagesGuide'

export default function PurchaseHistory() {
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(true)
  const [expandedId, setExpandedId] = useState(null)
  const [detailById, setDetailById] = useState({})            // cycle_id -> detail payload
  const [detailLoading, setDetailLoading] = useState({})      // cycle_id -> bool
  const [pinging, setPinging] = useState({})                  // `${cycleId}:${distId}` -> bool

  useEffect(() => {
    apiClient.get('/api/purchase-history')
      .then(res => setHistory(res.data))
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return <p className="text-slate-400 text-sm">Loading…</p>
  }

  const statusBadge = status => {
    if (status === 'COMPLETED') return 'bg-emerald-100 text-emerald-700'
    if (status === 'AWAITING_RECEIPT') return 'bg-yellow-100 text-yellow-700'
    return 'bg-slate-100 text-slate-600'
  }

  const toggleExpand = async (cycleId) => {
    if (expandedId === cycleId) {
      setExpandedId(null)
      return
    }
    setExpandedId(cycleId)
    if (!detailById[cycleId]) {
      setDetailLoading(prev => ({ ...prev, [cycleId]: true }))
      try {
        const { data } = await apiClient.get(`/api/purchase-history/${cycleId}`)
        setDetailById(prev => ({ ...prev, [cycleId]: data }))
      } catch (err) {
        toast.error(err.response?.data?.detail || 'Could not load details')
      } finally {
        setDetailLoading(prev => ({ ...prev, [cycleId]: false }))
      }
    }
  }

  const requestReceipt = async (cycleId, distributorId, distributorName) => {
    const key = `${cycleId}:${distributorId}`
    setPinging(prev => ({ ...prev, [key]: true }))
    try {
      const { data } = await apiClient.post(
        `/api/purchase-history/${cycleId}/vendors/${distributorId}/request-receipt`
      )
      toast.success(`Pinged ${distributorName} for the invoice.`)
      // Optimistically reflect "we just asked them" in the local detail
      setDetailById(prev => {
        const cur = prev[cycleId]
        if (!cur) return prev
        return {
          ...prev,
          [cycleId]: {
            ...cur,
            vendors: cur.vendors.map(v =>
              v.distributor_id === distributorId
                ? { ...v, last_invoice_ping_at: new Date().toISOString() }
                : v
            ),
          },
        }
      })
      // The backend returned a fresh days_since_po; nothing more to do.
      void data
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Could not send invoice request')
    } finally {
      setPinging(prev => ({ ...prev, [key]: false }))
    }
  }

  return (
    <div className="max-w-4xl space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Purchase History</h1>
        <p className="text-sm text-slate-600 mt-2 leading-relaxed max-w-3xl">
          After you approve a cart, formal <strong>POs</strong> go out by email. This list tracks{' '}
          <strong>delivery / invoice</strong>: we ingest vendor invoice or confirmation mail when your{' '}
          connected inbox receives it, match it to the PO, and show parsed totals here.{' '}
          <strong>Payment</strong> (Net 7 / 15 / 30, etc.) follows your supplier agreement — we surface documents,
          not accounting-system settlement.
        </p>
      </header>

      <SupplyChainStagesGuide emphasizeIds={['delivery', 'payment']} />

      {history.length === 0 ? (
        <p className="text-slate-500 text-sm">
          No cycles yet. When you <strong>approve a cart</strong> on Quotes, PO emails go out and the cycle
          lands here — including while invoices are still <strong>awaiting receipt</strong> from email parsing.
        </p>
      ) : (
        <div className="bg-white border border-slate-200 rounded-lg shadow-sm overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-slate-50 border-b border-slate-200">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-slate-600 w-8"></th>
                <th className="text-left px-4 py-3 font-medium text-slate-600">Distributor</th>
                <th className="text-right px-4 py-3 font-medium text-slate-600">Total Cost</th>
                <th className="text-left px-4 py-3 font-medium text-slate-600">Date</th>
                <th className="text-left px-4 py-3 font-medium text-slate-600">Status</th>
                <th className="text-left px-4 py-3 font-medium text-slate-600">Receipt / invoice</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {history.map(row => {
                const vendors = row.vendors || []
                const receipts = row.receipts || []
                const isExpanded = expandedId === row.id
                const detail = detailById[row.id]
                const isLoadingDetail = detailLoading[row.id]
                return (
                  <RowGroup
                    key={row.id}
                    row={row}
                    vendors={vendors}
                    receipts={receipts}
                    isExpanded={isExpanded}
                    detail={detail}
                    isLoadingDetail={isLoadingDetail}
                    pinging={pinging}
                    statusBadge={statusBadge}
                    onToggle={() => toggleExpand(row.id)}
                    onRequestReceipt={requestReceipt}
                  />
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ─── Row + expandable detail ────────────────────────────────────────────────

function RowGroup({
  row, vendors, receipts, isExpanded, detail, isLoadingDetail,
  pinging, statusBadge, onToggle, onRequestReceipt,
}) {
  return (
    <>
      <tr
        className={`align-top cursor-pointer transition-colors ${
          isExpanded ? 'bg-emerald-50' : 'hover:bg-slate-50'
        }`}
        onClick={onToggle}
      >
        <td className="px-4 py-3 text-slate-400">
          <span
            className={`inline-block transition-transform ${isExpanded ? 'rotate-90' : ''}`}
            aria-hidden
          >
            ▶
          </span>
        </td>
        <td className="px-4 py-3 font-medium">
          {vendors.length <= 1 ? (
            row.distributor_name
          ) : (
            <div className="space-y-0.5">
              {vendors.map(v => (
                <div key={v.po_id} className="text-sm">
                  {v.distributor_name}
                  <span className="text-xs text-slate-500 ml-1">
                    ${(v.total || 0).toFixed(2)}
                  </span>
                </div>
              ))}
            </div>
          )}
        </td>
        <td className="px-4 py-3 text-right">
          {row.total_quoted_cost != null ? `$${row.total_quoted_cost.toFixed(2)}` : '—'}
        </td>
        <td className="px-4 py-3 text-slate-500">
          {new Date(row.purchased_at).toLocaleDateString()}
        </td>
        <td className="px-4 py-3">
          <span className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${statusBadge(row.status)}`}>
            {row.status?.replace('_', ' ') || '—'}
          </span>
        </td>
        <td className="px-4 py-3 text-slate-500 text-xs">
          {receipts.length === 0 ? (
            <span className="text-slate-400">awaiting…</span>
          ) : (
            <div className="space-y-0.5">
              {receipts.map(r => (
                <div key={r.id}>
                  #{r.receipt_number || r.id.slice(0, 6)}
                  {r.total_amount != null && ` · $${r.total_amount.toFixed(2)}`}
                </div>
              ))}
              {vendors.length > receipts.length && (
                <div className="text-slate-400">
                  {vendors.length - receipts.length} pending…
                </div>
              )}
            </div>
          )}
        </td>
      </tr>

      {isExpanded && (
        <tr>
          <td colSpan={6} className="bg-slate-50 px-6 py-5 border-t border-slate-200">
            {isLoadingDetail || !detail ? (
              <p className="text-slate-400 text-sm">Loading order detail…</p>
            ) : (
              <CycleDetail
                detail={detail}
                pinging={pinging}
                onRequestReceipt={onRequestReceipt}
              />
            )}
          </td>
        </tr>
      )}
    </>
  )
}

function CycleDetail({ detail, pinging, onRequestReceipt }) {
  const cycleShort = detail.cycle_id.slice(0, 8)
  return (
    <div className="space-y-5">
      <div className="flex items-baseline gap-4 text-sm">
        <span className="font-mono text-slate-500">Cycle {cycleShort}…</span>
        <span className="text-slate-700">
          {detail.vendor_count} vendor{detail.vendor_count === 1 ? '' : 's'} ·{' '}
          {detail.ingredient_count} ingredient{detail.ingredient_count === 1 ? '' : 's'} ·{' '}
          <strong>${detail.grand_total.toFixed(2)}</strong> total
        </span>
      </div>

      {detail.vendors.length === 0 && (
        <p className="text-slate-500 text-sm">
          No approved vendors on this cycle. (Likely a record from before the multi-vendor flow.)
        </p>
      )}

      {detail.vendors.map(v => {
        const pingKey = `${detail.cycle_id}:${v.distributor_id}`
        const isPinging = !!pinging[pingKey]
        const hasReceipt = !!v.receipt
        return (
          <div
            key={v.po_id}
            className="bg-white border border-slate-200 rounded-lg p-4 shadow-sm"
          >
            <div className="flex items-start justify-between gap-4 mb-3">
              <div>
                <div className="font-semibold text-slate-800">
                  {v.distributor_name}
                  <span className="text-xs text-slate-400 font-mono ml-2">
                    PO #{v.po_id.slice(0, 6)}
                  </span>
                </div>
                <div className="text-xs text-slate-500 mt-0.5">
                  {v.items.length} ingredient{v.items.length === 1 ? '' : 's'} · approved{' '}
                  {v.approved_at ? new Date(v.approved_at).toLocaleDateString() : '—'}
                  {v.days_since_po != null && (
                    <span className="ml-1">({v.days_since_po}d ago)</span>
                  )}
                </div>
              </div>
              <div className="text-right">
                <div className="text-lg font-semibold text-slate-800">
                  ${(v.po_total || 0).toFixed(2)}
                </div>
              </div>
            </div>

            {/* Items the vendor won */}
            {v.items.length > 0 ? (
              <div className="border-t border-slate-100 pt-3">
                <div className="text-xs uppercase tracking-wider text-slate-400 font-medium mb-1.5">
                  Items in this PO
                </div>
                <table className="w-full text-sm">
                  <tbody className="divide-y divide-slate-100">
                    {v.items.map(it => (
                      <tr key={it.ingredient_id}>
                        <td className="py-1.5 text-slate-700">{it.ingredient_name}</td>
                        <td className="py-1.5 text-right text-slate-600 tabular-nums">
                          ${it.unit_price.toFixed(2)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="text-xs text-slate-400">No item-level breakdown available.</p>
            )}

            {/* Receipt status */}
            <div className="border-t border-slate-100 pt-3 mt-3 flex items-center justify-between gap-4">
              {hasReceipt ? (
                <div className="text-sm">
                  <span className="inline-block px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-100 text-emerald-700">
                    Invoice received
                  </span>
                  <span className="ml-2 text-slate-600">
                    #{v.receipt.receipt_number || v.receipt.id.slice(0, 6)}
                    {v.receipt.total_amount != null &&
                      ` · $${v.receipt.total_amount.toFixed(2)}`}
                    {v.receipt.received_at && (
                      <span className="text-slate-400 ml-2">
                        on {new Date(v.receipt.received_at).toLocaleDateString()}
                      </span>
                    )}
                  </span>
                  <p className="text-[11px] text-slate-500 mt-1.5">
                    Parsed automatically from the vendor email thread when your inbox connection receives it.
                  </p>
                </div>
              ) : (
                <div className="text-sm">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                    <span className="inline-block px-2 py-0.5 rounded-full text-xs font-medium bg-yellow-100 text-yellow-700">
                      Invoice pending
                    </span>
                    {v.last_invoice_ping_at && (
                      <span className="text-xs text-slate-500">(just pinged)</span>
                    )}
                  </div>
                  <p className="text-[11px] text-slate-600 mt-1.5 leading-relaxed">
                    We&apos;ll attach the receipt when an invoice or PO confirmation email arrives and parses cleanly.
                    Use Request invoice if the vendor hasn&apos;t emailed documentation yet.
                  </p>
                </div>
              )}
              {!hasReceipt && (
                <button
                  className="px-3 py-1.5 text-xs font-medium rounded-md border border-emerald-300 text-emerald-700 hover:bg-emerald-50 disabled:opacity-50 disabled:cursor-not-allowed"
                  disabled={isPinging}
                  onClick={() =>
                    onRequestReceipt(detail.cycle_id, v.distributor_id, v.distributor_name)
                  }
                >
                  {isPinging ? 'Sending…' : 'Request invoice'}
                </button>
              )}
            </div>
          </div>
        )
      })}

      {detail.unmatched_ingredients?.length > 0 && (
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-sm">
          <div className="font-medium text-amber-800 mb-1">
            Ingredients with no winning vendor
          </div>
          <ul className="list-disc list-inside text-amber-700 text-xs space-y-0.5">
            {detail.unmatched_ingredients.map(u => (
              <li key={u.ingredient_id}>{u.ingredient_name}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
