import { useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, NavLink, Navigate } from 'react-router-dom'
import { ToastContainer } from 'react-toastify'
import ProfileSetup from './components/ProfileSetup'
import MenuUpload from './components/MenuUpload'
import RecipeAccordion from './components/RecipeAccordion'
import QuoteTracker from './components/QuoteTracker'
import PurchaseHistory from './components/PurchaseHistory'
import NotificationToast from './components/NotificationToast'
import NotificationBell from './components/NotificationBell'
import Contracts from './components/Contracts'
import Vendors from './components/Vendors'
import ManagerAlertsPage from './components/ManagerAlertsPage'
import { apiClient } from './services/api'

/**
 * App shell — sidebar nav + routes.
 *
 * Onboarding is gated by `RestaurantProfile.onboarding_state`. We surface
 * that state in the sidebar with a small "Step N/3" pill so the user
 * always knows where they are, and the nav items disable steps that
 * aren't reachable yet (e.g. you can't open the Procurement page until
 * you've at least skipped the contracts step).
 */

const allNavItems = [
  { to: '/',           label: 'Profile',         step: 'NEEDS_PROFILE' },
  { to: '/alerts',     label: 'Alerts',          step: 'NEEDS_CONTRACTS' },
  { to: '/contracts',  label: 'Contracts',       step: 'NEEDS_CONTRACTS' },
  { to: '/vendors',    label: 'Vendors',         step: 'NEEDS_CONTRACTS' },
  { to: '/menu',       label: 'Menu Upload',     step: 'NEEDS_MENU' },
  { to: '/procurement',label: 'Procurement',     step: 'COMPLETED' },
  { to: '/quotes',     label: 'Quotes',          step: 'COMPLETED' },
  { to: '/history',    label: 'Purchase History',step: 'COMPLETED' },
]

const STATE_RANK = {
  NEEDS_PROFILE:   0,
  NEEDS_CONTRACTS: 1,
  NEEDS_MENU:      2,
  COMPLETED:       3,
}


function Sidebar({ onboardingState }) {
  const reached = STATE_RANK[onboardingState] ?? 0
  return (
    <aside className="w-60 min-h-screen bg-slate-900 text-slate-100 flex flex-col flex-shrink-0">
      <div className="px-4 py-5 border-b border-slate-700 flex items-center justify-between">
        <span className="text-lg font-bold tracking-tight text-white">HeavenlySourcing</span>
        <NotificationBell />
      </div>

      <OnboardingPill state={onboardingState} />

      <nav className="flex-1 px-3 py-4 space-y-1">
        {allNavItems.map(({ to, label, step }) => {
          const stepRank = STATE_RANK[step] ?? 0
          const locked = stepRank > reached
          return (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              onClick={(e) => locked && e.preventDefault()}
              className={({ isActive }) =>
                `flex items-center justify-between px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                  locked
                    ? 'text-slate-600 cursor-not-allowed'
                    : isActive
                    ? 'bg-emerald-600 text-white'
                    : 'text-slate-300 hover:bg-slate-800 hover:text-white'
                }`
              }
            >
              <span>{label}</span>
              {locked && <LockIcon />}
            </NavLink>
          )
        })}
      </nav>
    </aside>
  )
}


function OnboardingPill({ state }) {
  if (state === 'COMPLETED' || !state) return null
  const label = {
    NEEDS_PROFILE:   'Step 1 of 3 — Profile',
    NEEDS_CONTRACTS: 'Step 2 of 3 — Contracts',
    NEEDS_MENU:      'Step 3 of 3 — Menu',
  }[state] || state
  return (
    <div className="mx-3 mt-3 px-3 py-2 bg-emerald-900/40 border border-emerald-700 rounded-md text-xs text-emerald-100">
      {label}
    </div>
  )
}


function LockIcon() {
  return (
    <svg className="h-3 w-3" viewBox="0 0 20 20" fill="currentColor">
      <path fillRule="evenodd" d="M10 1a4.5 4.5 0 00-4.5 4.5V9H5a2 2 0 00-2 2v6a2 2 0 002 2h10a2 2 0 002-2v-6a2 2 0 00-2-2h-.5V5.5A4.5 4.5 0 0010 1zm3 8V5.5a3 3 0 10-6 0V9h6z" clipRule="evenodd" />
    </svg>
  )
}


export default function App() {
  const [onboardingState, setOnboardingState] = useState('NEEDS_PROFILE')

  useEffect(() => {
    const refresh = () => {
      apiClient.get('/api/profile').then(res => {
        setOnboardingState(res.data?.onboarding_state || 'NEEDS_PROFILE')
      }).catch(() => setOnboardingState('NEEDS_PROFILE'))
    }
    refresh()
    // Re-check on focus so the sidebar pill updates after wizard transitions.
    window.addEventListener('focus', refresh)
    // Cheap heartbeat in case the user navigates between pages without a
    // window-focus event (happens with SPA route changes).
    const interval = setInterval(refresh, 8000)
    return () => {
      window.removeEventListener('focus', refresh)
      clearInterval(interval)
    }
  }, [])

  return (
    <BrowserRouter>
      <div className="flex min-h-screen">
        <Sidebar onboardingState={onboardingState} />
        <main className="flex-1 p-8 overflow-auto">
          <NotificationToast />
          <Routes>
            <Route path="/" element={<ProfileSetup />} />
            <Route path="/alerts" element={<ManagerAlertsPage />} />
            <Route path="/contracts" element={<Contracts />} />
            <Route path="/vendors" element={<Vendors />} />
            <Route path="/menu" element={<MenuUpload />} />
            <Route path="/procurement" element={<RecipeAccordion />} />
            <Route path="/quotes" element={<QuoteTracker />} />
            <Route path="/history" element={<PurchaseHistory />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
      <ToastContainer position="top-right" autoClose={5000} hideProgressBar={false} />
    </BrowserRouter>
  )
}
