import { useEffect, useState, useCallback, useRef } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { toast } from 'react-toastify'
import { apiClient } from '../services/api'

/**
 * Contracts page — step 2 of the onboarding wizard, and a permanent
 * top-nav page after onboarding.
 *
 * Layout:
 *   - Header card with three call-to-action tiles:
 *       (1) Upload a PDF contract     → opens the upload picker
 *       (2) Seed a demo contract      → for testing without a real PDF
 *       (3) I don't have any yet      → advances onboarding to menu step
 *   - List of existing contracts (when any).
 *   - When an extraction is in flight, opens the ContractVerifier modal so
 *     the manager can review and confirm each field before persisting.
 */
export default function Contracts() {
  const navigate = useNavigate()
  const [profile, setProfile] = useState(null)
  const [contracts, setContracts] = useState([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  // Active extraction we're walking the user through. Null = no modal.
  const [draft, setDraft] = useState(null)

  const fetchAll = useCallback(async () => {
    try {
      const [profileRes, contractsRes] = await Promise.all([
        apiClient.get('/api/profile'),
        apiClient.get('/api/contracts'),
      ])
      setProfile(profileRes.data)
      setContracts(contractsRes.data || [])
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to load contracts')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
  }, [fetchAll])

  if (loading) {
    return <p className="text-slate-500">Loading…</p>
  }

  if (!profile) {
    return (
      <div className="max-w-lg">
        <h1 className="text-2xl font-bold mb-4">Contracts</h1>
        <p className="text-sm text-slate-600">
          Create your restaurant profile first, then come back here to upload
          contracts.
        </p>
        <button
          onClick={() => navigate('/')}
          className="mt-3 px-4 py-2 bg-emerald-600 text-white rounded-md text-sm font-medium hover:bg-emerald-700"
        >
          Go to Profile Setup →
        </button>
      </div>
    )
  }

  const onboardingState = profile.onboarding_state
  const isOnboarding = onboardingState === 'NEEDS_CONTRACTS'

  return (
    <div className="max-w-4xl space-y-6">
      <header className="space-y-3">
        <div>
          <h1 className="text-2xl font-bold">Contracts</h1>
          <p className="text-sm text-slate-500 mt-1">
            Register negotiated umbrella agreements with suppliers — scope,
            term dates, pricing model (fixed vs index-tied), payment Net /
            minimums, delivery expectations (often documented like SLAs),
            renewal, exclusivity, and similar clauses live here as structured
            terms.
          </p>
          <p className="text-sm text-slate-500 mt-2">
            Day-to-day buying stays separate: weekly RFPs, PO lines,
            deliveries, and AP invoices run through procurement — contracts give
            the baseline rules those cycles plug into.
          </p>
        </div>
        <details className="bg-slate-50 border border-slate-200 rounded-lg px-4 py-3 text-sm text-slate-700">
          <summary className="cursor-pointer font-medium text-slate-800 select-none">
            What belongs on this screen vs procurement?
          </summary>
          <ul className="mt-2 ml-4 list-disc space-y-1 text-slate-600">
            <li>
              <strong className="font-medium text-slate-700">Here:</strong>{' '}
              master agreement row — supplier × scope/category × term window ×{' '}
              <code className="text-xs bg-slate-100 px-1 rounded">pricing_structure</code>{' '}
              plus extracted clauses (payment terms, MOQs, delivery cadence,
              rebates, renewal notice, etc.).
            </li>
            <li>
              <strong className="font-medium text-slate-700">
                Weekly spot flow:
              </strong>{' '}
              <Link to="/procurement" className="text-emerald-700 font-medium hover:underline">
                Procurement
              </Link>{' '}
              (forecast) →{' '}
              <Link to="/quotes" className="text-emerald-700 font-medium hover:underline">
                Quotes
              </Link>{' '}
              (RFP &amp; approve PO) →{' '}
              <Link to="/history" className="text-emerald-700 font-medium hover:underline">
                Purchase History
              </Link>{' '}
              (parsed invoices vs PO). Payment stays in your AP stack; we reference Net-style terms in PO copy.
            </li>
          </ul>
        </details>
      </header>

      <ActionTiles
        busy={busy}
        onUpload={(file) => handleUpload(file, setDraft, setBusy)}
        onSeedDemo={() => handleSeedDemo(setBusy, fetchAll)}
        onSkip={() => handleSkip(setBusy, navigate)}
        showSkipPrompt={isOnboarding}
      />

      {contracts.length > 0 && (
        <ContractList
          contracts={contracts}
          onVerify={(c) => handleVerify(c.id, fetchAll, setBusy)}
          onOpen={(c) => setDraft({ ...c, _readonly: true })}
          onRenewalComplete={fetchAll}
        />
      )}

      {isOnboarding && contracts.length === 0 && (
        <div className="bg-emerald-50 border border-emerald-200 rounded-lg p-4 text-sm text-emerald-900">
          <strong>Tip:</strong> if you want to see the full flow end-to-end
          before plugging in your real contracts, click <em>Seed a demo
          contract</em> — it creates a realistic Sysco-shaped agreement you
          can walk through verification on.
        </div>
      )}

      {isOnboarding && contracts.some((c) => c.manager_verified) && (
        <div className="bg-white border border-emerald-200 rounded-lg p-4 flex items-center justify-between">
          <p className="text-sm text-slate-700">
            ✅ Contracts step done. Final stop: upload your menu so we can
            forecast demand against these contracts.
          </p>
          <button
            onClick={() => navigate('/menu')}
            className="px-4 py-2 bg-emerald-600 text-white rounded-md text-sm font-medium hover:bg-emerald-700"
          >
            Continue to Menu Upload →
          </button>
        </div>
      )}

      {draft && (
        <ContractVerifierModal
          initial={draft}
          readOnly={!!draft._readonly}
          onClose={() => setDraft(null)}
          onSaved={(saved) => {
            setDraft(null)
            toast.success('Contract saved — please review and verify')
            fetchAll()
            if (!saved.manager_verified && !draft._readonly) {
              // Open the verifier again, this time pointing at the persisted row.
              setDraft({ ...saved, _readonly: false })
            }
          }}
        />
      )}
    </div>
  )
}


// ─── Action tiles ────────────────────────────────────────────────────────────

function ActionTiles({ busy, onUpload, onSeedDemo, onSkip, showSkipPrompt }) {
  const fileRef = useRef()
  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
      <Tile
        title="Upload a contract"
        body="PDF, TXT, or pasted text. We extract the key terms and let you verify before saving."
        cta={busy ? 'Working…' : 'Choose a file'}
        disabled={busy}
        onClick={() => fileRef.current?.click()}
        accent="emerald"
      >
        <input
          ref={fileRef}
          type="file"
          className="hidden"
          accept="application/pdf,text/plain,text/markdown"
          onChange={(e) => {
            const f = e.target.files?.[0]
            if (f) onUpload(f)
            e.target.value = ''
          }}
        />
      </Tile>

      <Tile
        title="Seed a demo contract"
        body="Generate a believable Sysco-style agreement so you can see the verifier and renewal flow without a real PDF."
        cta={busy ? 'Working…' : 'Seed demo'}
        disabled={busy}
        onClick={onSeedDemo}
        accent="amber"
      />

      <Tile
        title="I don't have any contracts yet"
        body={
          showSkipPrompt
            ? 'Skip ahead to menu upload — we will help you sign your first contracts after we know what you cook.'
            : 'Skip the contracts step. You can still come back to upload them later.'
        }
        cta={busy ? 'Working…' : 'Skip for now'}
        disabled={busy}
        onClick={onSkip}
        accent="slate"
      />
    </div>
  )
}


function Tile({ title, body, cta, onClick, disabled, accent = 'emerald', children }) {
  const accents = {
    emerald: 'border-emerald-200 hover:border-emerald-400',
    amber:   'border-amber-200 hover:border-amber-400',
    slate:   'border-slate-200 hover:border-slate-400',
  }
  const button = {
    emerald: 'bg-emerald-600 hover:bg-emerald-700 text-white',
    amber:   'bg-amber-600 hover:bg-amber-700 text-white',
    slate:   'bg-slate-700 hover:bg-slate-800 text-white',
  }
  return (
    <div className={`bg-white border ${accents[accent]} rounded-lg p-5 flex flex-col`}>
      <h3 className="font-semibold text-slate-800">{title}</h3>
      <p className="text-xs text-slate-500 mt-1 flex-1">{body}</p>
      <button
        onClick={onClick}
        disabled={disabled}
        className={`mt-4 px-3 py-2 rounded-md text-sm font-medium ${button[accent]} disabled:opacity-50`}
      >
        {cta}
      </button>
      {children}
    </div>
  )
}


// ─── Handlers ────────────────────────────────────────────────────────────────

async function handleUpload(file, setDraft, setBusy) {
  setBusy(true)
  try {
    const base64 = await fileToBase64(file)
    const { data } = await apiClient.post('/api/contracts/upload', {
      base64_content: base64,
      mime_type: file.type || 'application/pdf',
      filename: file.name,
    })
    setDraft({ ...data, _isExtraction: true })
  } catch (err) {
    toast.error(err.response?.data?.detail || 'Upload failed')
  } finally {
    setBusy(false)
  }
}

async function handleSeedDemo(setBusy, fetchAll) {
  setBusy(true)
  try {
    await apiClient.post('/api/contracts/seed-demo')
    toast.success('Demo contract created — please review and verify it')
    fetchAll()
  } catch (err) {
    toast.error(err.response?.data?.detail || 'Seed failed')
  } finally {
    setBusy(false)
  }
}

async function handleSkip(setBusy, navigate) {
  setBusy(true)
  try {
    await apiClient.post('/api/contracts/skip')
    toast.success('OK — skipping to menu upload')
    navigate('/menu')
  } catch (err) {
    toast.error(err.response?.data?.detail || 'Skip failed')
  } finally {
    setBusy(false)
  }
}

async function handleVerify(id, fetchAll, setBusy) {
  setBusy(true)
  try {
    await apiClient.post(`/api/contracts/${id}/verify`)
    toast.success('Contract verified')
    fetchAll()
  } catch (err) {
    toast.error(err.response?.data?.detail || 'Verify failed')
  } finally {
    setBusy(false)
  }
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result.split(',')[1])
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}


// ─── Contract list ──────────────────────────────────────────────────────────

function midpointLabel(snapshot) {
  if (!snapshot || typeof snapshot !== 'object') return ''
  const m = snapshot.avg_quote_midpoint
  if (typeof m === 'number' && m > 0) return `~$${m.toFixed(2)} mid`
  const parsed = snapshot.parsed
  const items = parsed?.pricing_items
  if (Array.isArray(items) && items.length > 0) return '(priced)'
  return ''
}

function formatApiError(err) {
  const d = err.response?.data?.detail
  if (d == null) return err.message || 'Request failed'
  if (typeof d === 'string') return d
  if (typeof d === 'object' && d.error != null) {
    return typeof d.error === 'string' ? d.error : JSON.stringify(d.error)
  }
  try {
    return JSON.stringify(d)
  } catch {
    return 'Request failed'
  }
}

function DecisionBoardModal({ contract, onClose, onComplete }) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState(null)

  useEffect(() => {
    if (!contract) return undefined
    let cancelled = false
    setLoading(true)
    apiClient
      .get(`/api/contracts/${contract.id}/decision-board`)
      .then((res) => {
        if (!cancelled) setData(res.data)
      })
      .catch((err) => {
        if (!cancelled) toast.error(formatApiError(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [contract])

  const award = async (nid) => {
    if (!window.confirm('Record this negotiation as the winning award for this contract?')) return
    setBusyId(nid)
    try {
      await apiClient.post(`/api/contracts/${contract.id}/award`, {
        negotiation_id: nid,
      })
      toast.success('Award saved — counterparty updated')
      onComplete?.()
      onClose()
    } catch (err) {
      toast.error(formatApiError(err))
    } finally {
      setBusyId(null)
    }
  }

  if (!contract) return null

  const rows = data?.negotiations || []

  return (
    <div className="fixed inset-0 bg-slate-900/60 z-50 flex items-center justify-center p-4">
      <div className="bg-white max-w-2xl w-full max-h-[90vh] overflow-auto rounded-lg shadow-xl p-6 space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h3 className="text-lg font-semibold text-slate-800">Contract decision board</h3>
            <p className="text-xs text-slate-500 mt-1">
              Phase 5 — compare parsed negotiation signals, then record the formal award. This updates
              the contract&apos;s incumbent vendor.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600 text-2xl leading-none"
          >
            ×
          </button>
        </div>

        {loading ? (
          <p className="text-sm text-slate-500">Loading decision data…</p>
        ) : (
          <table className="w-full text-xs border border-slate-200 rounded-md overflow-hidden">
            <thead className="bg-slate-50 text-slate-600">
              <tr>
                <th className="text-left px-3 py-2">Vendor</th>
                <th className="text-left px-3 py-2">Intent</th>
                <th className="text-left px-3 py-2">Status</th>
                <th className="text-right px-3 py-2">Mid $</th>
                <th className="text-right px-3 py-2">Trust</th>
                <th className="text-right px-3 py-2"> </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {rows.map((row) => (
                <tr key={row.negotiation_id}>
                  <td className="px-3 py-2 font-medium text-slate-800">{row.vendor_name || '—'}</td>
                  <td className="px-3 py-2 text-slate-600">{row.intent}</td>
                  <td className="px-3 py-2 text-slate-600">{row.status}</td>
                  <td className="px-3 py-2 text-right font-mono">
                    {row.latest_quote_midpoint != null
                      ? `$${Number(row.latest_quote_midpoint).toFixed(2)}`
                      : '—'}
                  </td>
                  <td className="px-3 py-2 text-right font-mono">
                    {row.trust_score != null ? `${Math.round(row.trust_score)}` : '—'}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      disabled={busyId === row.negotiation_id || row.status === 'CLOSED_WON'}
                      onClick={() => award(row.negotiation_id)}
                      className="px-2 py-1 rounded bg-violet-600 text-white font-medium hover:bg-violet-700 disabled:opacity-40"
                    >
                      {busyId === row.negotiation_id ? 'Saving…' : 'Award'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

function RenewalModal({ contract, onClose, onComplete }) {
  const [force, setForce] = useState(false)
  const [skipEmail, setSkipEmail] = useState(false)
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    setBusy(true)
    try {
      const { data } = await apiClient.post(
        `/api/contracts/${contract.id}/start-renewal`,
        {},
        { params: { force, skip_email: skipEmail } },
      )
      const sent = data.emails_sent ?? 0
      const comp = data.competitors_discovered ?? 0
      toast.success(
        skipEmail
          ? `Renewal cycle recorded (dry run — ${comp} competitor rows)`
          : `Renewal started — ${sent} outbound email(s), ${comp} competitor vendor(s)`,
      )
      onComplete?.()
      onClose()
    } catch (err) {
      toast.error(formatApiError(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-slate-900/60 z-40 flex items-center justify-center p-4">
      <div className="bg-white max-w-md w-full rounded-lg shadow-xl p-6 space-y-4">
        <h3 className="text-lg font-semibold text-slate-800">Start renewal cycle</h3>
        <p className="text-xs text-slate-600">
          Emails the incumbent for renewal pricing and up to four competitor RFPs (when discovery
          succeeds). Requires an active verified contract with vendor and end date — unless you
          force.
        </p>
        <label className="flex items-start gap-2 text-sm text-slate-700 cursor-pointer">
          <input
            type="checkbox"
            checked={force}
            onChange={(e) => setForce(e.target.checked)}
            className="mt-0.5"
          />
          <span>
            <strong>Force</strong> — bypass the renewal calendar window and duplicate-cycle guard.
          </span>
        </label>
        <label className="flex items-start gap-2 text-sm text-slate-700 cursor-pointer">
          <input
            type="checkbox"
            checked={skipEmail}
            onChange={(e) => setSkipEmail(e.target.checked)}
            className="mt-0.5"
          />
          <span>
            <strong>Skip outbound email</strong> — persist negotiations only (dry run / CI).
          </span>
        </label>
        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 border border-slate-300 text-slate-700 rounded-md text-sm font-medium hover:bg-slate-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={busy}
            className="px-4 py-2 bg-emerald-600 text-white rounded-md text-sm font-medium hover:bg-emerald-700 disabled:opacity-50"
          >
            {busy ? 'Starting…' : 'Start renewal'}
          </button>
        </div>
      </div>
    </div>
  )
}

function NegotiationsPanel({ contractId }) {
  const [payload, setPayload] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    apiClient
      .get(`/api/contracts/${contractId}/negotiations`)
      .then((res) => {
        if (!cancelled) setPayload(res.data)
      })
      .catch((err) => {
        if (!cancelled) toast.error(formatApiError(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [contractId])

  if (loading) {
    return <p className="text-xs text-slate-500 py-3">Loading negotiations…</p>
  }

  const negs = payload?.negotiations || []
  if (negs.length === 0) {
    return (
      <p className="text-xs text-slate-500 py-3 italic">
        No negotiation threads yet — start a renewal cycle to email the incumbent and competitor
        distributors.
      </p>
    )
  }

  return (
    <div className="space-y-4 py-3">
      {negs.map((n) => (
        <div key={n.id} className="border border-slate-200 rounded-md overflow-hidden">
          <div className="px-3 py-2 bg-slate-50 border-b border-slate-200 flex flex-wrap gap-2 items-center justify-between">
            <p className="text-xs font-semibold text-slate-800">
              {n.vendor_name || 'Vendor'}{' '}
              <span className="font-normal text-slate-500">· {n.intent}</span>
            </p>
            <span className="text-[10px] px-2 py-0.5 rounded bg-white border border-slate-200 text-slate-600">
              {n.status} · rounds {n.rounds_used}/{n.max_rounds}
            </span>
          </div>
          <ul className="divide-y divide-slate-100 text-xs">
            {(n.rounds || []).map((r) => (
              <li key={r.id} className="px-3 py-2 grid grid-cols-12 gap-2">
                <span className="col-span-1 font-mono text-slate-500">{r.round_index}</span>
                <span className="col-span-2 text-slate-600">{r.direction}</span>
                <span className="col-span-2 text-slate-600">{r.status}</span>
                <span className="col-span-7 text-slate-700 truncate" title={r.subject || ''}>
                  {r.subject || '—'}
                  {midpointLabel(r.offer_snapshot) && (
                    <span className="ml-2 text-emerald-700 font-medium">
                      {midpointLabel(r.offer_snapshot)}
                    </span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  )
}

function ContractList({ contracts, onVerify, onOpen, onRenewalComplete }) {
  const [expandedId, setExpandedId] = useState(null)
  const [renewalContract, setRenewalContract] = useState(null)
  const [decisionContract, setDecisionContract] = useState(null)

  return (
    <div className="bg-white border border-slate-200 rounded-lg shadow-sm overflow-hidden">
      <div className="px-5 py-3 border-b border-slate-200 bg-slate-50">
        <h2 className="text-sm font-semibold text-slate-700">
          Your contracts ({contracts.length})
        </h2>
      </div>
      <ul className="divide-y divide-slate-100">
        {contracts.map((c) => (
          <li key={c.id} className="px-5 py-4">
            <div className="flex items-center gap-4">
              <div className="flex-1 min-w-0">
                <p className="font-medium text-slate-800 truncate">{c.nickname}</p>
                <p className="text-xs text-slate-500 mt-0.5">
                  {c.vendor?.name || 'No vendor set'} ·{' '}
                  {c.primary_category || 'No category'} ·{' '}
                  {c.pricing_structure}
                  {c.end_date && (
                    <span className="ml-2">
                      ends <span className="font-medium">{c.end_date}</span>
                    </span>
                  )}
                  {c.renewal_cycle_started_at && (
                    <span className="ml-2 text-emerald-700 font-medium">
                      · Renewal cycle started
                    </span>
                  )}
                </p>
              </div>
              <StatusPill verified={c.manager_verified} status={c.status} />
              <div className="flex flex-wrap gap-2 justify-end">
                <button
                  type="button"
                  onClick={() => setExpandedId((id) => (id === c.id ? null : c.id))}
                  className="px-3 py-1.5 text-xs font-medium text-slate-700 border border-slate-300 rounded hover:bg-slate-50"
                >
                  {expandedId === c.id ? 'Hide negotiations' : 'Negotiations'}
                </button>
                <button
                  type="button"
                  onClick={() => setDecisionContract(c)}
                  disabled={!c.manager_verified}
                  title={
                    c.manager_verified
                      ? 'Pick winning vendor after renewal replies'
                      : 'Verify contract first'
                  }
                  className="px-3 py-1.5 text-xs font-medium text-white bg-violet-600 hover:bg-violet-700 rounded disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  Decision board
                </button>
                <button
                  type="button"
                  onClick={() => onOpen(c)}
                  className="px-3 py-1.5 text-xs font-medium text-slate-700 border border-slate-300 rounded hover:bg-slate-50"
                >
                  View
                </button>
                {!c.manager_verified && (
                  <button
                    type="button"
                    onClick={() => onVerify(c)}
                    className="px-3 py-1.5 text-xs font-medium text-white bg-emerald-600 hover:bg-emerald-700 rounded"
                  >
                    Verify
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => setRenewalContract(c)}
                  disabled={!renewalEligible(c)}
                  title={renewalDisabledReason(c)}
                  className="px-3 py-1.5 text-xs font-medium text-white bg-indigo-600 hover:bg-indigo-700 rounded disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  Start renewal
                </button>
              </div>
            </div>
            {expandedId === c.id && (
              <NegotiationsPanel contractId={c.id} />
            )}
          </li>
        ))}
      </ul>
      {renewalContract && (
        <RenewalModal
          contract={renewalContract}
          onClose={() => setRenewalContract(null)}
          onComplete={onRenewalComplete}
        />
      )}
      {decisionContract && (
        <DecisionBoardModal
          contract={decisionContract}
          onClose={() => setDecisionContract(null)}
          onComplete={onRenewalComplete}
        />
      )}
    </div>
  )
}

function renewalEligible(c) {
  if (!c.manager_verified || !c.vendor || !c.end_date) return false
  if (!['ACTIVE', 'EXPIRING_SOON'].includes(c.status)) return false
  return true
}

function renewalDisabledReason(c) {
  if (!c.manager_verified) return 'Verify the contract first'
  if (!c.vendor) return 'Link a vendor before renewal outreach'
  if (!c.end_date) return 'Add an end date so we know the renewal window'
  if (!['ACTIVE', 'EXPIRING_SOON'].includes(c.status)) {
    return 'Renewal is only available for Active or Expiring-soon contracts'
  }
  return 'Start renewal cycle'
}


function StatusPill({ verified, status }) {
  if (verified) {
    return (
      <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-100 text-emerald-700">
        Verified ✓ · {status}
      </span>
    )
  }
  return (
    <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-amber-100 text-amber-700">
      Awaiting verification · {status}
    </span>
  )
}


// ─── Verifier modal ─────────────────────────────────────────────────────────

function ContractVerifierModal({ initial, readOnly, onClose, onSaved }) {
  const [draft, setDraft] = useState(() => normaliseDraft(initial))
  const [saving, setSaving] = useState(false)
  const [schema, setSchema] = useState(null)

  const lowConfidence = new Set(initial.low_confidence_fields || [])

  useEffect(() => {
    let cancelled = false
    apiClient
      .get('/api/contracts/schema')
      .then(({ data }) => {
        if (!cancelled) setSchema(data)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [])

  const update = (path, value) =>
    setDraft((d) => setByPath(d, path, value))

  const onSave = async () => {
    setSaving(true)
    try {
      const body = {
        nickname: draft.nickname,
        vendor: draft.vendor,
        primary_category: draft.primary_category,
        category_coverage: draft.category_coverage,
        start_date: draft.start_date || null,
        end_date: draft.end_date || null,
        pricing_structure: draft.pricing_structure,
        line_items: draft.line_items,
        extracted_terms: draft.extracted_terms,
        raw_text: draft.raw_text || null,
        raw_filename: draft.raw_filename || null,
        source: draft.source || 'MANUAL_ENTRY',
      }
      const { data } = await apiClient.post('/api/contracts', body)
      onSaved(data)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-slate-900/60 z-40 flex items-center justify-center p-4">
      <div className="bg-white max-w-3xl w-full max-h-[90vh] rounded-lg shadow-xl flex flex-col">
        <header className="px-6 py-4 border-b border-slate-200 flex items-center justify-between">
          <div>
            <h2 className="text-lg font-bold text-slate-800">
              {readOnly ? draft.nickname : 'Verify Contract'}
            </h2>
            {!readOnly && (
              <p className="text-xs text-slate-500 mt-0.5">
                Review each field. Items highlighted in amber were flagged by
                the extractor as low confidence.
              </p>
            )}
            {!readOnly && (
              <p className="text-xs text-slate-600 mt-2 leading-relaxed">
                You&apos;re confirming one umbrella agreement: negotiated rules
                for pricing mechanics, payment / credit (e.g. Net terms),
                operational delivery expectations (SLA-style where written),
                renewal and exclusivity — not individual weekly POs or
                invoices.
              </p>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600 text-2xl leading-none"
          >
            ×
          </button>
        </header>

        <div className="flex-1 overflow-auto px-6 py-4 space-y-6">
          <Section title="Basics">
            <LField
              label="Nickname"
              value={draft.nickname}
              onChange={(v) => update(['nickname'], v)}
              readOnly={readOnly}
            />
            {schema?.allowed_categories?.length ? (
              <SelectField
                label="Primary category"
                value={draft.primary_category || ''}
                options={mergeEnumOptions(
                  schema.allowed_categories,
                  draft.primary_category,
                )}
                onChange={(v) => update(['primary_category'], v)}
                flagged={lowConfidence.has('primary_category')}
                readOnly={readOnly}
              />
            ) : (
              <LField
                label="Primary Category"
                value={draft.primary_category || ''}
                onChange={(v) => update(['primary_category'], v)}
                flagged={lowConfidence.has('primary_category')}
                readOnly={readOnly}
              />
            )}
            {schema?.allowed_pricing_structures?.length ? (
              <>
                <SelectField
                  label="Pricing structure"
                  value={draft.pricing_structure || ''}
                  options={mergeEnumOptions(
                    schema.allowed_pricing_structures,
                    draft.pricing_structure,
                  )}
                  onChange={(v) => update(['pricing_structure'], v)}
                  flagged={lowConfidence.has('pricing_structure')}
                  readOnly={readOnly}
                />
                {schema.pricing_structure_descriptions?.[
                  draft.pricing_structure
                ] && (
                  <p className="text-[11px] text-slate-600 -mt-1 leading-snug">
                    {schema.pricing_structure_descriptions[
                      draft.pricing_structure
                    ]}
                  </p>
                )}
              </>
            ) : (
              <LField
                label="Pricing Structure"
                value={draft.pricing_structure}
                onChange={(v) => update(['pricing_structure'], v)}
                flagged={lowConfidence.has('pricing_structure')}
                readOnly={readOnly}
              />
            )}
            <div className="grid grid-cols-2 gap-3">
              <LField
                label="Start date"
                value={draft.start_date || ''}
                onChange={(v) => update(['start_date'], v)}
                flagged={lowConfidence.has('start_date')}
                placeholder="YYYY-MM-DD"
                readOnly={readOnly}
              />
              <LField
                label="End date"
                value={draft.end_date || ''}
                onChange={(v) => update(['end_date'], v)}
                flagged={lowConfidence.has('end_date')}
                placeholder="YYYY-MM-DD"
                readOnly={readOnly}
              />
            </div>
          </Section>

          <Section title="Vendor">
            <LField
              label="Vendor name"
              value={draft.vendor?.name || ''}
              onChange={(v) => update(['vendor', 'name'], v)}
              flagged={lowConfidence.has('vendor.name')}
              readOnly={readOnly}
            />
            <div className="grid grid-cols-2 gap-3">
              <LField
                label="Primary domain"
                value={draft.vendor?.primary_domain || ''}
                onChange={(v) => update(['vendor', 'primary_domain'], v)}
                placeholder="sysco.com"
                readOnly={readOnly}
              />
              <LField
                label="Service region"
                value={draft.vendor?.service_region || ''}
                onChange={(v) => update(['vendor', 'service_region'], v)}
                placeholder="Bay Area / national"
                readOnly={readOnly}
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <LField
                label="HQ city"
                value={draft.vendor?.headquarters_city || ''}
                onChange={(v) => update(['vendor', 'headquarters_city'], v)}
                readOnly={readOnly}
              />
              <LField
                label="HQ state"
                value={draft.vendor?.headquarters_state || ''}
                onChange={(v) => update(['vendor', 'headquarters_state'], v)}
                readOnly={readOnly}
              />
            </div>
          </Section>

          <Section title={`Line items (${(draft.line_items || []).length})`}>
            {(draft.line_items || []).length === 0 ? (
              <p className="text-xs text-slate-500 italic">
                Contract is not itemized — pricing is described as a methodology
                rather than per-SKU. That's normal for broadline foodservice
                contracts; the comparison agent will work at category level.
              </p>
            ) : (
              <div className="space-y-2">
                {(draft.line_items || []).map((li, idx) => (
                  <div key={idx} className="border border-slate-200 rounded p-3 text-xs space-y-1">
                    <p className="font-medium text-slate-800">{li.sku_name}</p>
                    <p className="text-slate-500">
                      {li.pack_description || '—'} · {li.unit_of_measure || '—'} ·{' '}
                      {li.fixed_price != null
                        ? `$${Number(li.fixed_price).toFixed(2)} fixed`
                        : (li.price_formula || 'no pricing')}
                    </p>
                  </div>
                ))}
              </div>
            )}
          </Section>

          <Section
            title="Negotiated terms"
            subtitle={
              <span className="normal-case font-normal text-slate-500">
                Grouped like operators read agreements: pricing mechanics,
                payment &amp; minimums, delivery &amp; service commitments,
                renewal &amp; exclusivity. Anything outside our standard keys
                lands under Other — please review.
              </span>
            }
          >
            <SectionedTermsTable
              terms={draft.extracted_terms || {}}
              termSections={schema?.term_sections}
              termKeyLabels={schema?.term_key_labels}
              lowConfidence={lowConfidence}
              readOnly={readOnly}
              onChange={(key, next) =>
                setDraft((d) => ({
                  ...d,
                  extracted_terms: { ...d.extracted_terms, [key]: next },
                }))
              }
            />
          </Section>
        </div>

        <footer className="px-6 py-4 border-t border-slate-200 flex items-center justify-between">
          <p className="text-xs text-slate-500">
            {readOnly
              ? 'Read-only view. Click Verify on the list to confirm.'
              : 'Saving creates the contract in DRAFT. Click "Verify" on the list afterwards.'}
          </p>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="px-4 py-2 border border-slate-300 text-slate-700 rounded-md text-sm font-medium hover:bg-slate-50"
            >
              Close
            </button>
            {!readOnly && (
              <button
                onClick={onSave}
                disabled={saving}
                className="px-4 py-2 bg-emerald-600 text-white rounded-md text-sm font-medium hover:bg-emerald-700 disabled:opacity-50"
              >
                {saving ? 'Saving…' : 'Save Draft'}
              </button>
            )}
          </div>
        </footer>
      </div>
    </div>
  )
}


function Section({ title, subtitle, children }) {
  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-500">
          {title}
        </h3>
        {subtitle && (
          <p className="text-[11px] text-slate-500 mt-1 leading-relaxed">
            {subtitle}
          </p>
        )}
      </div>
      {children}
    </section>
  )
}

/** Ensure current extraction value appears even if not in canonical enum list. */
function mergeEnumOptions(allowed, current) {
  const cur = (current || '').trim()
  const base = (allowed || []).map((v) => ({ value: v, label: v }))
  if (!cur) return base
  const has = base.some((o) => o.value === cur)
  if (has) return base
  return [{ value: cur, label: `${cur} (from extraction)` }, ...base]
}

function SelectField({ label, value, options, onChange, flagged, readOnly }) {
  const ring = flagged
    ? 'border-amber-300 focus:ring-amber-400'
    : 'border-slate-300 focus:ring-emerald-500'
  return (
    <div>
      <label className="block text-xs font-medium text-slate-600 mb-1">
        {label}
        {flagged && (
          <span className="ml-2 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-100 text-amber-700">
            verify
          </span>
        )}
      </label>
      <select
        value={value || ''}
        onChange={(e) => onChange(e.target.value)}
        disabled={readOnly}
        className={`w-full border ${ring} rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 ${readOnly ? 'bg-slate-50' : 'bg-white'}`}
      >
        <option value="">—</option>
        {(options || []).map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  )
}


function LField({ label, value, onChange, flagged, placeholder, readOnly }) {
  const ring = flagged
    ? 'border-amber-300 focus:ring-amber-400'
    : 'border-slate-300 focus:ring-emerald-500'
  return (
    <div>
      <label className="block text-xs font-medium text-slate-600 mb-1">
        {label}
        {flagged && (
          <span className="ml-2 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-100 text-amber-700">
            verify
          </span>
        )}
      </label>
      <input
        value={value || ''}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        readOnly={readOnly}
        className={`w-full border ${ring} rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 ${readOnly ? 'bg-slate-50' : ''}`}
      />
    </div>
  )
}


function SectionedTermsTable({
  terms,
  termSections,
  termKeyLabels,
  lowConfidence,
  readOnly,
  onChange,
}) {
  const keys = Object.keys(terms || {})
  if (keys.length === 0) {
    return (
      <p className="text-xs text-slate-500 italic">
        No negotiated terms were extracted. After save you can extend JSON /
        re-upload — typical fields include Net-style payment days, MOQs,
        delivery windows, index references, and renewal notice.
      </p>
    )
  }

  // Fallback: flat list until schema loads or if backend sends no sections.
  if (!termSections?.length) {
    return (
      <div className="border border-slate-200 rounded-md divide-y divide-slate-100">
        {keys.sort().map((k) => (
          <TermRow
            key={k}
            termKey={k}
            entry={terms[k] || {}}
            displayLabel={termKeyLabels?.[k]}
            lowConfidence={lowConfidence}
            readOnly={readOnly}
            onChange={onChange}
          />
        ))}
      </div>
    )
  }

  const sectioned = new Set()
  for (const sec of termSections) {
    for (const k of sec.keys || []) sectioned.add(k)
  }

  const blocks = []
  for (const sec of termSections) {
    const secKeys = (sec.keys || []).filter((k) => keys.includes(k))
    if (secKeys.length === 0) continue
    blocks.push(
      <div
        key={sec.id}
        className="border border-slate-200 rounded-md overflow-hidden mb-3 last:mb-0"
      >
        <div className="bg-slate-50 px-3 py-2 border-b border-slate-200">
          <h4 className="text-xs font-semibold text-slate-800">{sec.label}</h4>
          {sec.blurb && (
            <p className="text-[11px] text-slate-600 mt-0.5 leading-snug">
              {sec.blurb}
            </p>
          )}
        </div>
        <div className="divide-y divide-slate-100">
          {secKeys.map((k) => (
            <TermRow
              key={k}
              termKey={k}
              entry={terms[k] || {}}
              displayLabel={termKeyLabels?.[k]}
              lowConfidence={lowConfidence}
              readOnly={readOnly}
              onChange={onChange}
            />
          ))}
        </div>
      </div>,
    )
  }

  const otherKeys = keys.filter((k) => !sectioned.has(k)).sort()
  if (otherKeys.length > 0) {
    blocks.push(
      <div
        key="_other"
        className="border border-amber-100 rounded-md overflow-hidden"
      >
        <div className="bg-amber-50 px-3 py-2 border-b border-amber-100">
          <h4 className="text-xs font-semibold text-amber-900">
            Other extracted clauses
          </h4>
          <p className="text-[11px] text-amber-900/80 mt-0.5 leading-snug">
            Keys outside our standard checklist — confirm meaning before
            relying on them.
          </p>
        </div>
        <div className="divide-y divide-slate-100">
          {otherKeys.map((k) => (
            <TermRow
              key={k}
              termKey={k}
              entry={terms[k] || {}}
              displayLabel={termKeyLabels?.[k]}
              lowConfidence={lowConfidence}
              readOnly={readOnly}
              onChange={onChange}
            />
          ))}
        </div>
      </div>,
    )
  }

  return <div className="space-y-0">{blocks}</div>
}

function TermRow({
  termKey,
  entry,
  displayLabel,
  lowConfidence,
  readOnly,
  onChange,
}) {
  const needsVerification =
    entry.needs_verification ||
    lowConfidence.has(`extracted_terms.${termKey}`)
  return (
    <div className="px-3 py-2 grid grid-cols-12 gap-2 items-start text-sm">
      <div className="col-span-4">
        <p className="text-xs font-medium text-slate-800">
          {displayLabel || termKey}
          {needsVerification && (
            <span className="ml-2 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-amber-100 text-amber-700">
              verify
            </span>
          )}
        </p>
        {displayLabel && (
          <p className="font-mono text-[10px] text-slate-400 mt-0.5">{termKey}</p>
        )}
      </div>
      <div className="col-span-7">
        <input
          value={
            typeof entry.value === 'object'
              ? JSON.stringify(entry.value)
              : (entry.value ?? '')
          }
          onChange={(e) =>
            onChange(termKey, {
              ...entry,
              value: coerceTermValue(e.target.value, entry.value),
            })
          }
          readOnly={readOnly}
          className={`w-full border border-slate-300 rounded px-2 py-1 text-xs ${readOnly ? 'bg-slate-50' : ''}`}
        />
        {entry.notes && (
          <p className="mt-1 text-[11px] text-slate-500 italic">{entry.notes}</p>
        )}
      </div>
      <button
        type="button"
        onClick={() =>
          onChange(termKey, {
            ...entry,
            needs_verification: !needsVerification,
          })
        }
        disabled={readOnly}
        className="col-span-1 text-[10px] text-slate-400 hover:text-emerald-600 disabled:opacity-40"
        title="Toggle verify flag"
      >
        {needsVerification ? '✓' : '⚠'}
      </button>
    </div>
  )
}


// ─── Helpers ────────────────────────────────────────────────────────────────

function normaliseDraft(initial) {
  return {
    nickname: initial.nickname || '',
    vendor: initial.vendor
      ? { ...initial.vendor }
      : { name: '', primary_domain: '', headquarters_city: '', headquarters_state: '', service_region: '' },
    primary_category: initial.primary_category || '',
    category_coverage: initial.category_coverage || [],
    start_date: initial.start_date || '',
    end_date: initial.end_date || '',
    pricing_structure: initial.pricing_structure || 'FIXED',
    line_items: initial.line_items || [],
    extracted_terms: initial.extracted_terms || {},
    raw_text: initial.raw_text || '',
    raw_filename: initial.raw_filename || '',
    source: initial.source || 'MANUAL_ENTRY',
  }
}

function setByPath(obj, path, value) {
  const [head, ...tail] = path
  if (tail.length === 0) return { ...obj, [head]: value }
  return { ...obj, [head]: setByPath(obj[head] || {}, tail, value) }
}

function coerceTermValue(text, previous) {
  // Try to preserve the original JS type so booleans / numbers stay typed.
  if (previous === true || previous === false) {
    return /^(true|yes|1)$/i.test(text.trim())
  }
  if (typeof previous === 'number') {
    const n = Number(text)
    return Number.isFinite(n) ? n : text
  }
  // Try parsing JSON arrays/objects when the user is editing those.
  if (Array.isArray(previous) || (previous && typeof previous === 'object')) {
    try { return JSON.parse(text) } catch { /* fall through */ }
  }
  return text
}
