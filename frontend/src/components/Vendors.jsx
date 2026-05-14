import { useEffect, useState, useCallback } from 'react'
import { toast } from 'react-toastify'
import { apiClient } from '../services/api'

/**
 * Vendors page — list every supplier the system knows about for this
 * restaurant, plus a manual-add form so the manager can drop in a vendor
 * the Places-based discovery missed (or a referral from another operator).
 *
 * Public-signal cards are shown with a "3rd-party only — verify" badge
 * and are never blended into the first-party trust score.
 */
export default function Vendors() {
  const [vendors, setVendors] = useState([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)

  const fetchAll = useCallback(async () => {
    try {
      const { data } = await apiClient.get('/api/vendors')
      setVendors(data || [])
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to load vendors')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchAll() }, [fetchAll])

  return (
    <div className="max-w-5xl space-y-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Vendors</h1>
          <p className="text-sm text-slate-500 mt-1">
            All suppliers the comparison agent considers — incumbents from
            your contracts, vendors discovered by Google Places, and any
            vendor you've added by hand.
          </p>
        </div>
        <button
          onClick={() => setShowForm((s) => !s)}
          className="px-4 py-2 bg-emerald-600 text-white rounded-md text-sm font-medium hover:bg-emerald-700"
        >
          {showForm ? 'Cancel' : 'Add Vendor'}
        </button>
      </header>

      {showForm && (
        <VendorForm
          onCancel={() => setShowForm(false)}
          onSaved={() => {
            setShowForm(false)
            fetchAll()
          }}
        />
      )}

      {loading ? (
        <p className="text-slate-500">Loading…</p>
      ) : vendors.length === 0 ? (
        <div className="bg-white border border-slate-200 rounded-lg p-8 text-center text-slate-500 text-sm">
          No vendors yet. Once you upload a contract, the counterparty
          appears here automatically. You can also add vendors manually
          using the button above.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {vendors.map((v) => (
            <VendorCard key={v.id} vendor={v} onChanged={fetchAll} />
          ))}
        </div>
      )}
    </div>
  )
}


function VendorForm({ onCancel, onSaved }) {
  const [form, setForm] = useState({
    name: '',
    contact_email: '',
    contact_name: '',
    contact_phone: '',
    primary_domain: '',
    service_region: '',
    internal_alias: '',
    internal_notes: '',
  })
  const [saving, setSaving] = useState(false)

  const change = (e) => setForm((f) => ({ ...f, [e.target.name]: e.target.value }))

  const submit = async (e) => {
    e.preventDefault()
    setSaving(true)
    try {
      const body = { ...form }
      Object.keys(body).forEach((k) => { if (!body[k]) delete body[k] })
      await apiClient.post('/api/vendors', body)
      toast.success('Vendor added — pending domain verification')
      onSaved()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Add failed')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form
      onSubmit={submit}
      className="bg-white border border-slate-200 rounded-lg shadow-sm p-5 space-y-3"
    >
      <h3 className="font-semibold text-slate-800">Add a vendor</h3>
      <p className="text-xs text-slate-500">
        Manually-added vendors start in <strong>PENDING_DOMAIN_CHECK</strong>{' '}
        — the renewal agent won't RFP them until a domain check or operator
        approval verifies they're real.
      </p>

      <div className="grid grid-cols-2 gap-3">
        <FormField label="Name" name="name" value={form.name} onChange={change} required />
        <FormField label="Primary domain" name="primary_domain" value={form.primary_domain} onChange={change} placeholder="acme-foods.com" />
        <FormField label="Contact email" name="contact_email" type="email" value={form.contact_email} onChange={change} />
        <FormField label="Contact name" name="contact_name" value={form.contact_name} onChange={change} />
        <FormField label="Contact phone" name="contact_phone" value={form.contact_phone} onChange={change} />
        <FormField label="Service region" name="service_region" value={form.service_region} onChange={change} placeholder="Bay Area" />
        <FormField label="Internal alias" name="internal_alias" value={form.internal_alias} onChange={change} placeholder="Our cheese guy" />
      </div>
      <FormField label="Notes" name="internal_notes" value={form.internal_notes} onChange={change} />

      <div className="flex gap-2 justify-end">
        <button
          type="button"
          onClick={onCancel}
          className="px-4 py-2 border border-slate-300 text-slate-700 rounded-md text-sm font-medium hover:bg-slate-50"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={saving}
          className="px-4 py-2 bg-emerald-600 text-white rounded-md text-sm font-medium hover:bg-emerald-700 disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save Vendor'}
        </button>
      </div>
    </form>
  )
}


function FormField({ label, name, value, onChange, type = 'text', required, placeholder }) {
  return (
    <div>
      <label className="block text-xs font-medium text-slate-600 mb-1">{label}</label>
      <input
        type={type}
        name={name}
        value={value}
        onChange={onChange}
        required={required}
        placeholder={placeholder}
        className="w-full border border-slate-300 rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500"
      />
    </div>
  )
}


function VendorCard({ vendor, onChanged }) {
  const [signals, setSignals] = useState(vendor.public_signals)
  const [loadingSignals, setLoadingSignals] = useState(false)

  const fetchSignals = async () => {
    setLoadingSignals(true)
    try {
      const { data } = await apiClient.get(`/api/vendors/${vendor.id}/public-signals`)
      setSignals(data.public_signals)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to load public signals')
    } finally {
      setLoadingSignals(false)
    }
  }

  const verify = async () => {
    try {
      await apiClient.post(`/api/vendors/${vendor.id}/verify`)
      toast.success('Vendor marked verified')
      onChanged()
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Verify failed')
    }
  }

  const link = vendor.link || {}
  const trust = vendor.trust_score

  return (
    <div className="bg-white border border-slate-200 rounded-lg shadow-sm p-5 space-y-3">
      <div className="flex items-start justify-between">
        <div className="min-w-0">
          <h3 className="font-semibold text-slate-800 truncate">{vendor.name}</h3>
          <p className="text-xs text-slate-500 mt-0.5">
            {vendor.primary_domain || 'no domain'} ·{' '}
            {vendor.service_region || 'no region set'}
          </p>
        </div>
        <VerifyPill status={link.verification_status} onClick={verify} />
      </div>

      {(vendor.supplied_categories || []).length > 0 && (
        <div className="flex flex-wrap gap-1">
          {vendor.supplied_categories.map((c) => (
            <span
              key={c}
              className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-slate-100 text-slate-700"
            >
              {c}
            </span>
          ))}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 text-xs">
        <div>
          <p className="text-slate-500 uppercase tracking-wider text-[10px] font-semibold">
            Your data (first-party)
          </p>
          {trust ? (
            <p className="mt-1">
              <strong>{trust.trust_score?.toFixed?.(0) ?? '—'}</strong> /100 ·{' '}
              {trust.deliveries_total} deliveries
            </p>
          ) : (
            <p className="mt-1 text-slate-400 italic">No data yet</p>
          )}
        </div>
        <div>
          <p className="text-slate-500 uppercase tracking-wider text-[10px] font-semibold">
            3rd party (verify)
          </p>
          {signals ? (
            <div className="mt-1 text-slate-700 space-y-0.5">
              <p>BBB <span className="font-mono">{signals.bbb?.rating || '—'}</span></p>
              <p>D&amp;B credit <span className="font-mono">{signals.dnb?.credit_score || '—'}</span></p>
              <p>Yelp <span className="font-mono">{signals.yelp_b2b?.stars || '—'}★</span></p>
              <p className="text-[10px] text-slate-400">
                Yelp data: {signals.yelp_b2b?.source || '—'} · {signals.evidence || ''}
              </p>
            </div>
          ) : (
            <button
              onClick={fetchSignals}
              disabled={loadingSignals}
              className="mt-1 text-emerald-700 hover:underline"
            >
              {loadingSignals ? 'Loading…' : 'Pull signals'}
            </button>
          )}
        </div>
      </div>

      {link.contact_email && (
        <p className="text-xs text-slate-500 border-t border-slate-100 pt-2">
          Contact: <span className="font-mono">{link.contact_email}</span>
          {link.contact_name && <span className="ml-2">({link.contact_name})</span>}
        </p>
      )}
    </div>
  )
}


function VerifyPill({ status, onClick }) {
  if (status === 'VERIFIED') {
    return (
      <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-emerald-100 text-emerald-700 whitespace-nowrap">
        Verified ✓
      </span>
    )
  }
  if (status === 'AUTO_TRUSTED') {
    return (
      <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-slate-100 text-slate-700 whitespace-nowrap">
        Auto-trusted
      </span>
    )
  }
  return (
    <button
      onClick={onClick}
      className="px-2 py-0.5 rounded text-[10px] font-semibold bg-amber-100 text-amber-700 hover:bg-amber-200 whitespace-nowrap"
      title="Click to mark this vendor as verified"
    >
      Pending · verify
    </button>
  )
}
