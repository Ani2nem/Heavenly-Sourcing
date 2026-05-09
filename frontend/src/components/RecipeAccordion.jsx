import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'react-toastify'
import { apiClient } from '../services/api'

export default function RecipeAccordion() {
  const navigate = useNavigate()
  const [dishes, setDishes] = useState([])
  const [forecasts, setForecasts] = useState({})
  const [expanded, setExpanded] = useState({})
  const [window_, setWindow] = useState('Morning')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    apiClient.get('/api/menu/recipes').then(res => {
      setDishes(res.data)
      const initial = {}
      res.data.forEach(d => { initial[d.dish_id] = 0 })
      setForecasts(initial)
    }).catch(() => toast.error('Could not load recipes'))
  }, [])

  const toggle = id => setExpanded(e => ({ ...e, [id]: !e[id] }))
  const updateQty = (id, val) => setForecasts(f => ({ ...f, [id]: Math.max(0, parseInt(val) || 0) }))

  const submit = async () => {
    const dish_forecasts = Object.fromEntries(
      Object.entries(forecasts).filter(([, v]) => v > 0)
    )
    if (!Object.keys(dish_forecasts).length) {
      toast.warn('Set a forecast quantity for at least one dish')
      return
    }
    setLoading(true)
    try {
      const res = await apiClient.post('/api/procurement/cycle/initiate', {
        dish_forecasts,
        preferred_delivery_window: window_,
      })
      toast.success(`Cycle started — dispatching RFPs…`)
      navigate('/quotes')
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Failed to start cycle')
    } finally {
      setLoading(false)
    }
  }

  if (!dishes.length) {
    return (
      <div className="text-slate-500 text-sm">
        No recipes found. <a href="/menu" className="text-emerald-600 underline">Upload a menu first.</a>
      </div>
    )
  }

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-bold mb-2">Forecast Your Dishes</h1>
      <p className="text-slate-500 text-sm mb-6">
        Set how many portions of each dish you'll serve this week, then start the procurement cycle.
      </p>

      <div className="space-y-2 mb-6">
        {dishes.map(dish => (
          <div key={dish.dish_id} className="bg-white border border-slate-200 rounded-lg overflow-hidden shadow-sm">
            <button
              className="w-full flex items-center justify-between px-4 py-3 text-left"
              onClick={() => toggle(dish.dish_id)}
            >
              <div>
                <span className="font-medium">{dish.name}</span>
                {dish.base_price && (
                  <span className="ml-2 text-sm text-slate-400">${dish.base_price.toFixed(2)}</span>
                )}
              </div>
              <div className="flex items-center gap-3">
                <input
                  type="number"
                  min="0"
                  value={forecasts[dish.dish_id] ?? 0}
                  onClick={e => e.stopPropagation()}
                  onChange={e => updateQty(dish.dish_id, e.target.value)}
                  className="w-20 border border-slate-300 rounded px-2 py-1 text-sm text-right focus:outline-none focus:ring-1 focus:ring-emerald-500"
                  placeholder="qty"
                />
                <span className="text-slate-400 text-xs">portions</span>
                <Chevron open={expanded[dish.dish_id]} />
              </div>
            </button>
            {expanded[dish.dish_id] && (
              <div className="border-t border-slate-100 px-4 py-3 bg-slate-50">
                <p className="text-xs font-medium text-slate-500 uppercase tracking-wide mb-2">Ingredients</p>
                {dish.ingredients.length ? (
                  <ul className="space-y-1">
                    {dish.ingredients.map((ing, i) => (
                      <li key={i} className="text-sm text-slate-700 flex gap-2">
                        <span className="text-emerald-600">•</span>
                        {ing.name}
                        {ing.quantity != null && ing.quantity !== '' && (
                          <span className="text-slate-400">— {ing.quantity} {ing.unit || ''}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-sm text-slate-400">No ingredients extracted.</p>
                )}
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="bg-white border border-slate-200 rounded-lg p-4 mb-4 shadow-sm">
        <label className="block text-sm font-medium text-slate-700 mb-2">Preferred Delivery Window</label>
        <div className="flex gap-3">
          {['Morning', 'Afternoon'].map(w => (
            <button
              key={w}
              onClick={() => setWindow(w)}
              className={`px-4 py-2 rounded-md text-sm font-medium border transition-colors ${
                window_ === w
                  ? 'bg-emerald-600 text-white border-emerald-600'
                  : 'border-slate-300 text-slate-600 hover:border-emerald-400'
              }`}
            >
              {w}
            </button>
          ))}
        </div>
      </div>

      <button
        onClick={submit}
        disabled={loading}
        className="w-full py-3 bg-emerald-600 text-white rounded-md font-semibold hover:bg-emerald-700 disabled:opacity-50"
      >
        {loading ? 'Starting cycle…' : 'Start Procurement Cycle →'}
      </button>
    </div>
  )
}

function Chevron({ open }) {
  return (
    <svg
      className={`w-4 h-4 text-slate-400 transition-transform ${open ? 'rotate-180' : ''}`}
      fill="none" viewBox="0 0 24 24" stroke="currentColor"
    >
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
    </svg>
  )
}
