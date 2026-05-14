import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'react-toastify'
import { apiClient } from '../services/api'

/**
 * ProfileSetup — onboarding step 1 of 3.
 *
 * The post-pivot product treats contracts (not location) as the primary spine.
 * The required minimum here is name + email so the user can reach the
 * contracts step quickly. ZIP / city / state are optional fallback signals,
 * shown collapsed under a disclosure. Phone is also optional and only used
 * for SMS alerts on contract decisions (Phase 6).
 *
 * When a profile already exists, we render a summary card with an "edit"
 * affordance and a clear next-step button that respects the user's current
 * onboarding_state (jumps them to Contracts or Menu accordingly).
 */
export default function ProfileSetup() {
  const navigate = useNavigate()
  const [form, setForm] = useState({
    name: '',
    email: '',
    zip_code: '',
    city: '',
    state: '',
    phone_number: '',
    sms_alerts_opt_in: false,
  })
  const [loading, setLoading] = useState(false)
  const [existing, setExisting] = useState(null)
  const [showOptional, setShowOptional] = useState(false)
  const [editing, setEditing] = useState(false)

  useEffect(() => {
    apiClient.get('/api/profile').then(res => {
      if (res.data) setExisting(res.data)
    }).catch(() => {})
  }, [])

  const handleChange = e => setForm(f => ({ ...f, [e.target.name]: e.target.value }))

  const submitCreate = async e => {
    e.preventDefault()
    setLoading(true)
    try {
      const body = { ...form }
      Object.keys(body).forEach((k) => {
        if (k === 'sms_alerts_opt_in') return
        if (!body[k]) delete body[k]
      })
      body.sms_alerts_opt_in = Boolean(form.sms_alerts_opt_in)
      const { data } = await apiClient.post('/api/profile', body)
      toast.success('Profile saved!')
      setExisting(data)
      navigate('/contracts')
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to save profile')
    } finally {
      setLoading(false)
    }
  }

  const submitEdit = async e => {
    e.preventDefault()
    setLoading(true)
    try {
      const body = {}
      Object.entries(form).forEach(([k, v]) => {
        if (k === 'sms_alerts_opt_in') {
          body[k] = v
          return
        }
        if (v !== '') body[k] = v
      })
      const { data } = await apiClient.patch('/api/profile', body)
      toast.success('Profile updated')
      setExisting(data)
      setEditing(false)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Update failed')
    } finally {
      setLoading(false)
    }
  }

  const nextStepRoute = (state) => {
    switch (state) {
      case 'NEEDS_CONTRACTS': return { path: '/contracts', label: 'Set Up Contracts →' }
      case 'NEEDS_MENU':      return { path: '/menu',      label: 'Upload Your Menu →' }
      case 'COMPLETED':       return { path: '/procurement', label: 'Open Procurement →' }
      default:                return { path: '/contracts', label: 'Continue →' }
    }
  }

  if (existing && !editing) {
    const { path, label } = nextStepRoute(existing.onboarding_state)
    return (
      <div className="max-w-lg">
        <h1 className="text-2xl font-bold mb-2">Restaurant Profile</h1>
        <p className="text-sm text-slate-500 mb-6">
          Contracts are now the primary entry point — location is optional
          and only used as a fallback when discovering local vendors.
        </p>

        <div className="bg-white rounded-lg shadow-sm p-6 space-y-3 border border-slate-200">
          <Row label="Name" value={existing.name} />
          <Row label="Email" value={existing.email} />
          {existing.phone_number && <Row label="Phone (for SMS alerts)" value={existing.phone_number} />}
          {(existing.city || existing.state || existing.zip_code) && (
            <Row
              label="Location (fallback)"
              value={[existing.city, existing.state, existing.zip_code]
                .filter(Boolean).join(', ') || '—'}
            />
          )}
          <Row
            label="Onboarding"
            value={onboardingLabel(existing.onboarding_state)}
          />
        </div>

        <div className="mt-4 flex gap-2">
          <button
            onClick={() => navigate(path)}
            className="px-4 py-2 bg-emerald-600 text-white rounded-md text-sm font-medium hover:bg-emerald-700"
          >
            {label}
          </button>
          <button
            onClick={() => {
              setForm({
                name: existing.name || '',
                email: existing.email || '',
                zip_code: existing.zip_code || '',
                city: existing.city || '',
                state: existing.state || '',
                phone_number: existing.phone_number || '',
                sms_alerts_opt_in: Boolean(existing.sms_alerts_opt_in),
              })
              setEditing(true)
              setShowOptional(Boolean(
                existing.zip_code || existing.city || existing.state || existing.phone_number
              ))
            }}
            className="px-4 py-2 bg-white border border-slate-300 text-slate-700 rounded-md text-sm font-medium hover:bg-slate-50"
          >
            Edit Profile
          </button>
        </div>
      </div>
    )
  }

  const submitting = editing ? submitEdit : submitCreate

  return (
    <div className="max-w-lg">
      <h1 className="text-2xl font-bold mb-2">
        {editing ? 'Edit Restaurant Profile' : 'Set Up Your Restaurant'}
      </h1>
      <p className="text-sm text-slate-500 mb-6">
        We only need a name and an email to get started. The next step is
        to upload your existing supplier contracts (or skip if you don't
        have any yet).
      </p>

      <form
        onSubmit={submitting}
        className="bg-white rounded-lg shadow-sm p-6 space-y-4 border border-slate-200"
      >
        <Field
          label="Restaurant Name"
          name="name"
          value={form.name}
          onChange={handleChange}
          required
        />
        <Field
          label="Email"
          name="email"
          type="email"
          value={form.email}
          onChange={handleChange}
          required
          help="Used for vendor RFPs and contract decision alerts."
        />

        <button
          type="button"
          onClick={() => setShowOptional(s => !s)}
          className="text-xs text-emerald-700 font-medium hover:underline"
        >
          {showOptional ? '− Hide optional fields' : '+ Add location / phone (optional)'}
        </button>

        {showOptional && (
          <div className="space-y-4 pt-2 border-t border-slate-100">
            <Field
              label="Phone (for SMS alerts)"
              name="phone_number"
              value={form.phone_number}
              onChange={handleChange}
              placeholder="+1 555-555-5555"
              help="Optional. US numbers can be 10 digits; we normalize to +1…"
            />
            <label className="flex items-start gap-2 text-sm text-slate-700 cursor-pointer">
              <input
                type="checkbox"
                checked={form.sms_alerts_opt_in}
                onChange={(e) =>
                  setForm((f) => ({ ...f, sms_alerts_opt_in: e.target.checked }))
                }
                className="mt-1"
              />
              <span>
                Send <strong>SMS</strong> for actionable contract alerts (Twilio must be configured on the server).
              </span>
            </label>
            <Field
              label="ZIP Code (fallback for vendor discovery)"
              name="zip_code"
              value={form.zip_code}
              onChange={handleChange}
            />
            <div className="grid grid-cols-2 gap-4">
              <Field label="City" name="city" value={form.city} onChange={handleChange} />
              <Field
                label="State"
                name="state"
                value={form.state}
                onChange={handleChange}
                placeholder="CA"
              />
            </div>
          </div>
        )}

        <div className="flex gap-2 pt-2">
          <button
            type="submit"
            disabled={loading}
            className="flex-1 py-2 bg-emerald-600 text-white rounded-md font-medium hover:bg-emerald-700 disabled:opacity-50"
          >
            {loading
              ? 'Saving…'
              : editing ? 'Save Changes' : 'Save Profile & Continue →'}
          </button>
          {editing && (
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="px-4 py-2 bg-white border border-slate-300 text-slate-700 rounded-md font-medium hover:bg-slate-50"
            >
              Cancel
            </button>
          )}
        </div>
      </form>
    </div>
  )
}

function onboardingLabel(state) {
  switch (state) {
    case 'NEEDS_PROFILE':   return 'Profile needed'
    case 'NEEDS_CONTRACTS': return 'Step 2 of 3 — Contracts'
    case 'NEEDS_MENU':      return 'Step 3 of 3 — Menu'
    case 'COMPLETED':       return 'Complete'
    default:                return state
  }
}

function Field({ label, name, value, onChange, type = 'text', required, placeholder, help }) {
  return (
    <div>
      <label className="block text-sm font-medium text-slate-700 mb-1">
        {label}
        {!required && <span className="ml-1 text-xs font-normal text-slate-400">(optional)</span>}
      </label>
      <input
        type={type}
        name={name}
        value={value}
        onChange={onChange}
        required={required}
        placeholder={placeholder}
        className="w-full border border-slate-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500"
      />
      {help && <p className="mt-1 text-xs text-slate-500">{help}</p>}
    </div>
  )
}

function Row({ label, value }) {
  return (
    <div className="flex justify-between text-sm">
      <span className="text-slate-500">{label}</span>
      <span className="font-medium">{value || '—'}</span>
    </div>
  )
}
