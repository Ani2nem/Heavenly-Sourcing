/**
 * ComparisonMatrix — per-ingredient × vendor pricing grid.
 *
 * Each row is one ingredient. Columns are vendors. The cheapest cell per row
 * is highlighted green. Single-source rows get a red badge so the operator
 * knows they have no fallback.
 *
 * The "Approve Optimal Cart" button lives on the parent (QuoteTracker) since
 * it's a cycle-level action, not a row-level one. Auto-trigger of price-match
 * outreach happens server-side as quotes arrive — there's no per-row button.
 */
export default function ComparisonMatrix({ data }) {
  if (!data) return null
  const { rows = [], vendors = [], grand_total, ingredient_count, ingredients_with_no_quotes = [] } = data

  if (rows.length === 0) {
    return (
      <div className="bg-white border border-slate-200 rounded-lg shadow-sm p-6 text-center text-slate-400 text-sm">
        No vendor quotes received yet — comparison matrix will populate as replies arrive.
      </div>
    )
  }

  // Stable column order (alphabetical) so the matrix doesn't reshuffle on every poll.
  const orderedVendors = [...vendors].sort((a, b) =>
    a.distributor_name.localeCompare(b.distributor_name)
  )

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-slate-800">Per-Ingredient Comparison</h2>
        <p className="text-xs text-slate-500 mt-1">
          Cheapest vendor per row is highlighted. The optimal cart picks the green cell on every
          row. <strong>This grid is bid evaluation for the current cycle only</strong> — not your master
          agreement (Net terms, SLAs, fixed vs index pricing live under Contracts).{' '}
          When a vendor&apos;s quote arrives that&apos;s more expensive than another vendor&apos;s
          quote on the same ingredient, the system automatically emails them asking if they can
          match the lower price. If they reply with a new price, the matrix updates live.
        </p>
      </div>

      <div className="bg-white border border-slate-200 rounded-lg shadow-sm overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 border-b border-slate-200">
            <tr>
              <th className="text-left px-4 py-3 font-medium text-slate-600 sticky left-0 bg-slate-50">
                Ingredient
              </th>
              {orderedVendors.map(v => (
                <th
                  key={v.distributor_id}
                  className="text-right px-4 py-3 font-medium text-slate-600"
                >
                  {v.distributor_name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {rows.map(row => {
              const offerByDist = Object.fromEntries(
                row.offers.map(o => [o.distributor_id, o])
              )
              return (
                <tr key={row.ingredient_id} className="even:bg-slate-50">
                  <td className="px-4 py-3 font-medium sticky left-0 bg-inherit">
                    {row.ingredient_name}
                    {row.single_source && (
                      <span className="ml-2 inline-block px-1.5 py-0.5 rounded text-[10px] font-semibold bg-rose-100 text-rose-700 align-middle">
                        single-source
                      </span>
                    )}
                  </td>
                  {orderedVendors.map(v => {
                    const o = offerByDist[v.distributor_id]
                    if (!o) {
                      return (
                        <td key={v.distributor_id} className="px-4 py-3 text-right text-slate-300">
                          —
                        </td>
                      )
                    }
                    return (
                      <td
                        key={v.distributor_id}
                        className={`px-4 py-3 text-right ${
                          o.is_winner
                            ? 'bg-emerald-50 text-emerald-700 font-semibold'
                            : 'text-slate-600'
                        }`}
                      >
                        ${o.unit_price.toFixed(2)}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
            <tr className="bg-slate-50 border-t-2 border-slate-200 font-semibold">
              <td className="px-4 py-3 sticky left-0 bg-slate-50">
                Grand Total ({ingredient_count} items)
              </td>
              {orderedVendors.map(v => (
                <td key={v.distributor_id} className="px-4 py-3 text-right text-slate-600">
                  ${(v.won_total || 0).toFixed(2)}
                </td>
              ))}
            </tr>
            <tr className="bg-emerald-50 border-t border-emerald-200 font-semibold">
              <td className="px-4 py-3 sticky left-0 bg-emerald-50 text-emerald-800">
                Optimal Cart Total
              </td>
              <td colSpan={orderedVendors.length} className="px-4 py-3 text-right text-emerald-800">
                ${(grand_total || 0).toFixed(2)}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      {ingredients_with_no_quotes.length > 0 && (
        <div className="bg-rose-50 border border-rose-200 rounded-lg p-4 text-sm text-rose-800">
          <p className="font-medium mb-1">No vendor quoted for {ingredients_with_no_quotes.length} item(s):</p>
          <p className="text-xs">{ingredients_with_no_quotes.join(', ')}</p>
        </div>
      )}
    </div>
  )
}
