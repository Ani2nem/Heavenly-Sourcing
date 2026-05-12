import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'react-toastify'
import { apiClient } from '../services/api'

const UNIT_OPTIONS = ['lb', 'oz', 'fl oz', 'cup', 'tbsp', 'tsp', 'each', 'g', 'kg', 'ml', 'l']
const CATEGORY_OPTIONS = [
  'Proteins', 'Dairy', 'Produce', 'Bakery',
  'Condiments', 'Pantry', 'Dry Goods', 'Frozen',
]

export default function RecipeAccordion() {
  const navigate = useNavigate()
  const [dishes, setDishes] = useState([])
  const [forecasts, setForecasts] = useState({})
  const [expanded, setExpanded] = useState({})
  const [loading, setLoading] = useState(false)

  const loadRecipes = () => {
    apiClient.get('/api/menu/recipes/with-prices')
      .then(res => {
        setDishes(res.data)
        const initial = {}
        res.data.forEach(d => { initial[d.dish_id] = forecasts[d.dish_id] ?? 0 })
        setForecasts(initial)
      })
      .catch(() => toast.error('Could not load recipes'))
  }

  useEffect(() => {
    loadRecipes()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const toggle = id => setExpanded(e => ({ ...e, [id]: !e[id] }))
  const updateQty = (id, val) => setForecasts(f => ({ ...f, [id]: Math.max(0, parseInt(val) || 0) }))

  // Apply a partial dish update without re-fetching everything (snappy UX)
  const replaceDish = (dishId, updater) => {
    setDishes(prev => prev.map(d => d.dish_id === dishId ? updater(d) : d))
  }

  const handleAddIngredient = async (dishId, payload) => {
    try {
      const { data } = await apiClient.post(
        `/api/menu/dishes/${dishId}/ingredients`,
        payload,
      )
      replaceDish(dishId, d => ({
        ...d,
        ingredients: (() => {
          // If row already existed (qty bumped), replace it; otherwise append
          const idx = d.ingredients.findIndex(
            i => i.recipe_ingredient_id === data.row.recipe_ingredient_id
          )
          if (idx >= 0) {
            const copy = [...d.ingredients]
            copy[idx] = data.row
            return copy
          }
          return [...d.ingredients, data.row]
        })(),
      }))
      if (data.was_new_ingredient) {
        toast.info(`Added "${data.row.name}" — fetching USDA data in background.`)
      } else {
        toast.success(`Added "${data.row.name}".`)
      }
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Could not add ingredient')
    }
  }

  const handleEditIngredient = async (dishId, riId, payload) => {
    try {
      const { data } = await apiClient.patch(
        `/api/menu/recipe-ingredients/${riId}`,
        payload,
      )
      replaceDish(dishId, d => ({
        ...d,
        ingredients: d.ingredients.map(i =>
          i.recipe_ingredient_id === riId ? data.row : i
        ),
      }))
      if (data.was_new_ingredient) {
        toast.info(`Updated — fetching USDA data for "${data.row.name}".`)
      } else if (data.swapped_ingredient) {
        toast.success(`Swapped to "${data.row.name}".`)
      } else {
        toast.success('Quantity updated.')
      }
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Could not save edit')
    }
  }

  const handleDeleteIngredient = async (dishId, riId, name) => {
    if (!window.confirm(`Remove "${name}" from this dish?`)) return
    try {
      await apiClient.delete(`/api/menu/recipe-ingredients/${riId}`)
      replaceDish(dishId, d => ({
        ...d,
        ingredients: d.ingredients.filter(i => i.recipe_ingredient_id !== riId),
      }))
      toast.success(`Removed "${name}".`)
    } catch (err) {
      toast.error(err.response?.data?.detail || 'Could not delete')
    }
  }

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
      await apiClient.post('/api/procurement/cycle/initiate', {
        dish_forecasts,
      })
      toast.success('Cycle started — dispatching RFPs…')
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
    <div className="max-w-3xl">
      <h1 className="text-2xl font-bold mb-2">Forecast Your Dishes</h1>
      <p className="text-slate-500 text-sm mb-4">
        Set how many portions of each dish you'll serve this week, then start the procurement cycle.
        Expand a dish to review ingredients and USDA price trends — edit, add, or remove anything the parser got wrong.
      </p>
      <button
        type="button"
        onClick={loadRecipes}
        className="mb-4 text-xs text-emerald-700 underline"
      >
        Refresh USDA prices
      </button>

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
                <p className="text-xs font-medium text-slate-500 uppercase tracking-wide mb-2">
                  Ingredients & USDA Trend
                </p>
                {dish.ingredients.length ? (
                  <ul className="space-y-2 mb-3">
                    {dish.ingredients.map(ing => (
                      <IngredientRow
                        key={ing.recipe_ingredient_id}
                        ing={ing}
                        onSave={(payload) =>
                          handleEditIngredient(dish.dish_id, ing.recipe_ingredient_id, payload)
                        }
                        onDelete={() =>
                          handleDeleteIngredient(dish.dish_id, ing.recipe_ingredient_id, ing.name)
                        }
                      />
                    ))}
                  </ul>
                ) : (
                  <p className="text-sm text-slate-400 mb-3">No ingredients extracted.</p>
                )}
                <AddIngredientForm
                  onAdd={(payload) => handleAddIngredient(dish.dish_id, payload)}
                />
              </div>
            )}
          </div>
        ))}
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

// ─── Per-row UI ──────────────────────────────────────────────────────────────

function IngredientRow({ ing, onSave, onDelete }) {
  const [editing, setEditing] = useState(false)
  const [draftName, setDraftName] = useState(ing.name)
  const [draftQty, setDraftQty] = useState(ing.quantity ?? '')
  const [draftUnit, setDraftUnit] = useState(ing.unit || '')
  const [draftCat, setDraftCat] = useState(ing.category || '')
  const [saving, setSaving] = useState(false)
  const price = ing.usda_price || {}

  const startEdit = () => {
    setDraftName(ing.name)
    setDraftQty(ing.quantity ?? '')
    setDraftUnit(ing.unit || '')
    setDraftCat(ing.category || '')
    setEditing(true)
  }

  const cancel = () => setEditing(false)

  const save = async () => {
    if (!draftName.trim()) {
      toast.warn('Name is required')
      return
    }
    const qtyNum = draftQty === '' ? null : parseFloat(draftQty)
    if (draftQty !== '' && (Number.isNaN(qtyNum) || qtyNum < 0)) {
      toast.warn('Quantity must be a positive number')
      return
    }
    setSaving(true)
    try {
      await onSave({
        name: draftName.trim(),
        quantity: qtyNum,
        unit: draftUnit || null,
        category: draftCat || null,
      })
      setEditing(false)
    } finally {
      setSaving(false)
    }
  }

  if (editing) {
    return (
      <li className="text-sm bg-white rounded border border-emerald-200 p-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="text"
            value={draftName}
            onChange={e => setDraftName(e.target.value)}
            placeholder="Ingredient name"
            className="flex-1 min-w-[140px] border border-slate-300 rounded px-2 py-1 text-sm"
          />
          <input
            type="number"
            step="0.01"
            min="0"
            value={draftQty}
            onChange={e => setDraftQty(e.target.value)}
            placeholder="qty"
            className="w-20 border border-slate-300 rounded px-2 py-1 text-sm text-right"
          />
          <select
            value={draftUnit}
            onChange={e => setDraftUnit(e.target.value)}
            className="border border-slate-300 rounded px-2 py-1 text-sm bg-white"
          >
            <option value="">unit…</option>
            {UNIT_OPTIONS.map(u => <option key={u} value={u}>{u}</option>)}
          </select>
          <select
            value={draftCat}
            onChange={e => setDraftCat(e.target.value)}
            className="border border-slate-300 rounded px-2 py-1 text-sm bg-white"
          >
            <option value="">category…</option>
            {CATEGORY_OPTIONS.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
        <div className="flex items-center justify-end gap-2 mt-2">
          <button
            type="button"
            onClick={cancel}
            disabled={saving}
            className="px-2.5 py-1 text-xs text-slate-600 hover:text-slate-900"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={save}
            disabled={saving}
            className="px-3 py-1 text-xs font-medium rounded-md bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </li>
    )
  }

  return (
    <li className="text-sm text-slate-700 group">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-emerald-600">•</span>
          <span className="font-medium truncate">{ing.name}</span>
          {ing.quantity != null && ing.quantity !== '' && (
            <span className="text-slate-400 text-xs whitespace-nowrap">
              — {formatQty(ing.quantity)} {ing.unit || ''}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <PriceBadge summary={price} estimate={ing.usda_estimate} />
          <button
            type="button"
            onClick={startEdit}
            className="text-slate-400 hover:text-emerald-600 opacity-0 group-hover:opacity-100 transition-opacity"
            title="Edit ingredient"
            aria-label="Edit ingredient"
          >
            <PencilIcon />
          </button>
          <button
            type="button"
            onClick={onDelete}
            className="text-slate-400 hover:text-red-600 opacity-0 group-hover:opacity-100 transition-opacity"
            title="Remove from dish"
            aria-label="Remove from dish"
          >
            <TrashIcon />
          </button>
        </div>
      </div>
      {price.has_data && price.series?.length > 1 && (
        <Sparkline points={price.series.map(p => p.midpoint).filter(v => v != null)} />
      )}
    </li>
  )
}

function AddIngredientForm({ onAdd }) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [qty, setQty] = useState('')
  const [unit, setUnit] = useState('')
  const [cat, setCat] = useState('')
  const [saving, setSaving] = useState(false)

  const reset = () => {
    setName(''); setQty(''); setUnit(''); setCat('')
  }

  const submit = async () => {
    if (!name.trim()) {
      toast.warn('Name is required')
      return
    }
    const qtyNum = qty === '' ? null : parseFloat(qty)
    if (qty !== '' && (Number.isNaN(qtyNum) || qtyNum < 0)) {
      toast.warn('Quantity must be a positive number')
      return
    }
    setSaving(true)
    try {
      await onAdd({
        name: name.trim(),
        quantity: qtyNum,
        unit: unit || null,
        category: cat || null,
      })
      reset()
      setOpen(false)
    } finally {
      setSaving(false)
    }
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="text-xs text-emerald-700 hover:text-emerald-900 font-medium"
      >
        + Add ingredient
      </button>
    )
  }

  return (
    <div className="bg-white border border-emerald-200 rounded p-2.5">
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="text"
          value={name}
          onChange={e => setName(e.target.value)}
          placeholder="Ingredient name"
          autoFocus
          className="flex-1 min-w-[140px] border border-slate-300 rounded px-2 py-1 text-sm"
        />
        <input
          type="number"
          step="0.01"
          min="0"
          value={qty}
          onChange={e => setQty(e.target.value)}
          placeholder="qty"
          className="w-20 border border-slate-300 rounded px-2 py-1 text-sm text-right"
        />
        <select
          value={unit}
          onChange={e => setUnit(e.target.value)}
          className="border border-slate-300 rounded px-2 py-1 text-sm bg-white"
        >
          <option value="">unit…</option>
          {UNIT_OPTIONS.map(u => <option key={u} value={u}>{u}</option>)}
        </select>
        <select
          value={cat}
          onChange={e => setCat(e.target.value)}
          className="border border-slate-300 rounded px-2 py-1 text-sm bg-white"
        >
          <option value="">category…</option>
          {CATEGORY_OPTIONS.map(c => <option key={c} value={c}>{c}</option>)}
        </select>
      </div>
      <div className="flex items-center justify-end gap-2 mt-2">
        <button
          type="button"
          onClick={() => { reset(); setOpen(false) }}
          disabled={saving}
          className="px-2.5 py-1 text-xs text-slate-600 hover:text-slate-900"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={saving}
          className="px-3 py-1 text-xs font-medium rounded-md bg-emerald-600 text-white hover:bg-emerald-700 disabled:opacity-50"
        >
          {saving ? 'Adding…' : 'Add'}
        </button>
      </div>
    </div>
  )
}

// ─── Read-only bits (unchanged) ──────────────────────────────────────────────

function formatQty(q) {
  if (q == null) return ''
  // Show up to 3 decimals, strip trailing zeros
  const s = Number(q).toFixed(3)
  return s.replace(/\.?0+$/, '')
}

function PriceBadge({ summary, estimate }) {
  // Tier 1 — real USDA AMS Market News data.
  if (summary && summary.has_data) {
    const latest = summary.latest?.midpoint
    const fmt = v => v != null ? `$${v.toFixed(2)}/${summary.unit || 'lb'}` : '—'
    return (
      <span className="text-xs text-slate-600">
        {fmt(latest)}
        {summary.avg != null && (
          <span className="text-slate-400"> · avg {fmt(summary.avg)}</span>
        )}
        <span className="ml-1 text-[10px] text-emerald-700">USDA</span>
      </span>
    )
  }
  // Tier 2 — industry-estimate fallback (category midpoint, mass units only).
  // Visually distinct from real USDA so the user can't mistake one for the
  // other: amber colour + leading "~" + "industry est" tag.
  if (estimate && estimate.value != null) {
    const v = Number(estimate.value)
    const unit = estimate.unit || 'lb'
    const cat = estimate.category ? estimate.category.toLowerCase() : 'category'
    return (
      <span
        className="text-xs text-amber-700"
        title="No USDA AMS Market News data for this ingredient — falling back to a static category midpoint. Not USDA-sourced."
      >
        ~${v.toFixed(2)}/{unit}
        <span className="ml-1 text-[10px] text-amber-600">industry est · {cat}</span>
      </span>
    )
  }
  // Tier 3 — no signal at all.
  return <span className="text-xs text-slate-400">no USDA data</span>
}

function Sparkline({ points }) {
  if (!points || points.length < 2) return null
  const w = 120
  const h = 20
  const min = Math.min(...points)
  const max = Math.max(...points)
  const range = max - min || 1
  const stepX = w / (points.length - 1)
  const path = points
    .map((p, i) => `${i === 0 ? 'M' : 'L'} ${(i * stepX).toFixed(1)} ${(h - ((p - min) / range) * h).toFixed(1)}`)
    .join(' ')
  return (
    <svg width={w} height={h} className="mt-1 ml-5 text-emerald-600">
      <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
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

function PencilIcon() {
  return (
    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
      <path strokeLinecap="round" strokeLinejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
    </svg>
  )
}

function TrashIcon() {
  return (
    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="2">
      <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6M1 7h22M9 7V4a2 2 0 012-2h2a2 2 0 012 2v3" />
    </svg>
  )
}
