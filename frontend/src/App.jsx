import { BrowserRouter, Routes, Route, NavLink, Navigate } from 'react-router-dom'
import { ToastContainer } from 'react-toastify'
import ProfileSetup from './components/ProfileSetup'
import MenuUpload from './components/MenuUpload'
import RecipeAccordion from './components/RecipeAccordion'
import QuoteTracker from './components/QuoteTracker'
import PurchaseHistory from './components/PurchaseHistory'
import NotificationToast from './components/NotificationToast'
import NotificationBell from './components/NotificationBell'

const navItems = [
  { to: '/', label: 'Profile' },
  { to: '/menu', label: 'Menu Upload' },
  { to: '/procurement', label: 'Procurement' },
  { to: '/history', label: 'Purchase History' },
]

function Sidebar() {
  return (
    <aside className="w-60 min-h-screen bg-slate-900 text-slate-100 flex flex-col flex-shrink-0">
      <div className="px-4 py-5 border-b border-slate-700 flex items-center justify-between">
        <span className="text-lg font-bold tracking-tight text-white">HeavenlySourcing</span>
        <NotificationBell />
      </div>
      <nav className="flex-1 px-3 py-4 space-y-1">
        {navItems.map(({ to, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `flex items-center px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                isActive
                  ? 'bg-emerald-600 text-white'
                  : 'text-slate-300 hover:bg-slate-800 hover:text-white'
              }`
            }
          >
            {label}
          </NavLink>
        ))}
      </nav>
    </aside>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="flex min-h-screen">
        <Sidebar />
        <main className="flex-1 p-8 overflow-auto">
          <NotificationToast />
          <Routes>
            <Route path="/" element={<ProfileSetup />} />
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
