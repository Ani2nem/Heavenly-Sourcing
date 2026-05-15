import { Link } from 'react-router-dom'

/**
 * Maps production buying stages to what HeavenlySourcing actually automates.
 * Shown as a collapsible on Procurement, Quotes, and Purchase History.
 */
const STAGES = [
  {
    id: 'rfp',
    title: 'RFP / bid',
    intent: 'Ask vendors for pricing and term signals for this cycle.',
    inApp: (
      <>
        Automated weekly <strong>RFP email</strong> to distributors plus the{' '}
        <Link to="/quotes" className="text-emerald-700 font-medium hover:underline">
          Quotes
        </Link>{' '}
        comparison grid — framed as evaluation only (not a binding commitment).
      </>
    ),
  },
  {
    id: 'contract',
    title: 'Negotiation / master agreement',
    intent:
      'SLAs, Net-style payment terms, fixed vs index-linked pricing — the umbrella deal.',
    inApp: (
      <>
        <Link to="/contracts" className="text-emerald-700 font-medium hover:underline">
          Contracts
        </Link>{' '}
        capture verified agreements, renewal outreach, and award decisions —{' '}
        <strong>separate</strong> from each week&apos;s spot ingredient RFP.
      </>
    ),
  },
  {
    id: 'po',
    title: 'Purchase order',
    intent:
      'Chef or manager locks actual buy quantities; distributor receives a formal PO.',
    inApp: (
      <>
        Forecast portions on{' '}
        <Link to="/procurement" className="text-emerald-700 font-medium hover:underline">
          Procurement
        </Link>
        , compare quotes, then <strong>Approve optimal cart</strong> — we email PO
        lines referencing the cycle (quantities from your winning quotes).
      </>
    ),
  },
  {
    id: 'delivery',
    title: 'Delivery / invoice',
    intent: 'Drop ticket or invoice tied back to the PO for reconciliation.',
    inApp: (
      <>
        Vendor replies that look like invoices or PO confirmations are{' '}
        <strong>parsed from email</strong> (IMAP-connected inbox). Receipts attach to
        the cycle and surface on{' '}
        <Link to="/history" className="text-emerald-700 font-medium hover:underline">
          Purchase History
        </Link>
        ; you can chase missing paperwork with <strong>Request invoice</strong>.
      </>
    ),
  },
  {
    id: 'payment',
    title: 'Payment',
    intent:
      'Settlement follows agreed credit terms after delivery — not a prepaid lump sum for the contract term.',
    inApp: (
      <>
        PO email copy references <strong>Net-style terms</strong> from your supplier
        relationship. HeavenlySourcing does <strong>not</strong> integrate with
        QuickBooks / accounting — AP stays in your books; we focus on sourcing,
        POs, and matching inbound invoices.
      </>
    ),
  },
]

export default function SupplyChainStagesGuide({
  emphasizeIds = [],
  defaultOpen = false,
  className = '',
}) {
  const emphasis = new Set(emphasizeIds)
  return (
    <details
      open={defaultOpen}
      className={`rounded-lg border border-slate-200 bg-slate-50 text-sm text-slate-700 ${className}`}
    >
      <summary className="cursor-pointer select-none px-4 py-3 font-medium text-slate-800 hover:bg-slate-100/80 rounded-lg">
        How weekly buying fits together (RFP → PO → receipt → payment)
      </summary>
      <div className="px-4 pb-4 pt-0 space-y-3 border-t border-slate-200/80">
        <p className="text-xs text-slate-600 pt-3 leading-relaxed">
          Restaurant procurement is a chain of stages. Below is what each step means
          in the real world and what this app automates today.
        </p>
        <ul className="space-y-3">
          {STAGES.map((s) => (
            <li
              key={s.id}
              className={`rounded-md pl-3 pr-2 py-2 border border-transparent ${
                emphasis.has(s.id)
                  ? 'bg-emerald-50/90 border-emerald-200/80 shadow-sm'
                  : 'bg-white border-slate-100'
              }`}
            >
              <div className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                {s.title}
              </div>
              <p className="text-xs text-slate-600 mt-1 leading-relaxed">{s.intent}</p>
              <p className="text-xs text-slate-800 mt-1.5 leading-relaxed border-t border-slate-100 pt-1.5">
                <span className="font-medium text-slate-600">In the app: </span>
                {s.inApp}
              </p>
            </li>
          ))}
        </ul>
      </div>
    </details>
  )
}
