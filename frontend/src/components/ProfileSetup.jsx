import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'react-toastify'
import { apiClient } from '../services/api'

export default function ProfileSetup() {
  const navigate = useNavigate()
  const [form, setForm] = useState({ name: '', zip_code: '', city: '', state: '', email: '' })
  const [loading, setLoading] = useState(false)
  const [existing, setExisting] = useState(null)

  useEffect(() => {
    apiClient.get('/api/profile').then(res => {
      if (res.data) setExisting(res.data)
    }).catch(() => {})
  }, [])

  const handleChange = e => setForm(f => ({ ...f, [e.target.name]: e.target.value }))

  const handleSubmit = async e => {
    e.preventDefault()
    setLoading(true)
    try {
      await apiClient.post('/api/profile', form)
      toast.success('Profile saved!')
      navigate('/menu')
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to save profile')
    } finally {
      setLoading(false)
    }
  }

  if (existing) {
    return (
      <div className="max-w-lg">
        <h1 className="text-2xl font-bold mb-6">Restaurant Profile</h1>
        <div className="bg-white rounded-lg shadow-sm p-6 space-y-3 border border-slate-200">
          <Row label="Name" value={existing.name} />
          <Row label="Location" value={`${existing.city}, ${existing.state} ${existing.zip_code}`} />
          <Row label="Email" value={existing.email} />
        </div>
        <button
          onClick={() => navigate('/menu')}
          className="mt-4 px-4 py-2 bg-emerald-600 text-white rounded-md text-sm font-medium hover:bg-emerald-700"
        >
          Continue to Menu Upload →
        </button>
      </div>
    )
  }

  return (
    <div className="max-w-lg">
      <h1 className="text-2xl font-bold mb-6">Set Up Your Restaurant</h1>
      <form onSubmit={handleSubmit} className="bg-white rounded-lg shadow-sm p-6 space-y-4 border border-slate-200">
        <Field label="Restaurant Name" name="name" value={form.name} onChange={handleChange} required />
        <Field label="ZIP Code" name="zip_code" value={form.zip_code} onChange={handleChange} required />
        <div className="grid grid-cols-2 gap-4">
          <Field label="City" name="city" value={form.city} onChange={handleChange} required />
          <Field label="State" name="state" value={form.state} onChange={handleChange} required placeholder="CA" />
        </div>
        <Field label="Email" name="email" type="email" value={form.email} onChange={handleChange} required />
        <button
          type="submit"
          disabled={loading}
          className="w-full py-2 bg-emerald-600 text-white rounded-md font-medium hover:bg-emerald-700 disabled:opacity-50"
        >
          {loading ? 'Saving…' : 'Save Profile & Continue'}
        </button>
      </form>
    </div>
  )
}

function Field({ label, name, value, onChange, type = 'text', required, placeholder }) {
  return (
    <div>
      <label className="block text-sm font-medium text-slate-700 mb-1">{label}</label>
      <input
        type={type}
        name={name}
        value={value}
        onChange={onChange}
        required={required}
        placeholder={placeholder}
        className="w-full border border-slate-300 rounded-md px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500"
      />
    </div>
  )
}

function Row({ label, value }) {
  return (
    <div className="flex justify-between text-sm">
      <span className="text-slate-500">{label}</span>
      <span className="font-medium">{value}</span>
    </div>
  )
}
