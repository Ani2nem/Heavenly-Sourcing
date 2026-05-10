import { useState, useRef, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'react-toastify'
import { apiClient } from '../services/api'

const POLL_INTERVAL_MS = 3000
const MAX_FILE_MB = 25

// Claude-Code-style rotating status lines. Cooking themed, deliberately silly so a long
// PDF parse feels like the chef is busy in the back rather than the app being broken.
const COOKING_QUIPS = [
  'Sharpening the knives',
  'Putting on the chef coat',
  'Asking the chef what is in the secret sauce',
  'Squinting at the menu in dim restaurant lighting',
  'Decoding the daily specials',
  'Whisking up the recipe details',
  'Tasting the marinara',
  'Garnishing with parsley',
  'Translating "al dente"',
  'Reading the small print on the wine list',
  'Identifying mystery cheeses',
  'Cataloguing the cheese situation',
  'Foraging for hidden ingredients',
  'Interrogating the pizza dough',
  'Bargaining with the sous chef',
  'Toasting the breadcrumbs',
  'Folding the calzones',
  'Convincing the cheese to behave',
  'Resolving the pesto identity crisis',
  'Untangling the spaghetti',
  'Renaming "12 inch Mediterranean" to just "Mediterranean"',
  'Measuring spoonfuls of inspiration',
  'Counting the basil leaves',
  'Asking is this fl oz or oz',
  'Plating the ingredients neatly',
  'Negotiating with the dough',
  'Locating the rogue parmesan',
  'Stirring the JSON pot',
  'Letting the dough proof',
  'Skimming the foam off the top',
  'Calibrating the oven',
  'Reducing the sauce',
  'Simmering on low heat',
  'Waiting for the timer to ding',
]

const QUIP_ROTATION_MS = 2200

export default function MenuUpload() {
  const navigate = useNavigate()
  const fileRef = useRef()
  const pollRef = useRef(null)

  const [dragging, setDragging] = useState(false)
  const [file, setFile] = useState(null)

  // upload phase: idle | uploading | processing | done | error
  const [phase, setPhase] = useState('idle')
  const [progress, setProgress] = useState('')
  const [totalPages, setTotalPages] = useState(null)
  const [quipIdx, setQuipIdx] = useState(() => Math.floor(Math.random() * COOKING_QUIPS.length))
  const [elapsed, setElapsed] = useState(0)

  // clean up polling timer on unmount
  useEffect(() => () => clearInterval(pollRef.current), [])

  const isPending = phase === 'uploading' || phase === 'processing'

  // Rotate the silly status quip every QUIP_ROTATION_MS while the upload is in flight.
  useEffect(() => {
    if (!isPending) return
    const id = setInterval(() => {
      setQuipIdx((i) => {
        // pick a different one each time so the same line never repeats back-to-back
        let next = Math.floor(Math.random() * COOKING_QUIPS.length)
        if (next === i) next = (next + 1) % COOKING_QUIPS.length
        return next
      })
    }, QUIP_ROTATION_MS)
    return () => clearInterval(id)
  }, [isPending])

  // Elapsed-seconds timer so the user can see something is moving even if the backend
  // progress string hasn't changed in a while.
  useEffect(() => {
    if (!isPending) {
      setElapsed(0)
      return
    }
    const startedAt = Date.now()
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000))
    }, 1000)
    return () => clearInterval(id)
  }, [isPending])

  const stopPolling = () => {
    clearInterval(pollRef.current)
    pollRef.current = null
  }

  const startPolling = useCallback((jobId) => {
    pollRef.current = setInterval(async () => {
      try {
        const { data } = await apiClient.get(`/api/menu/upload/status/${jobId}`)
        setProgress(data.progress || '')
        if (data.total_pages) setTotalPages(data.total_pages)

        if (data.status === 'completed') {
          stopPolling()
          setPhase('done')
          toast.success(
            `Parsed ${data.result.recipes.length} dish(es) across ${data.total_pages ?? '?'} pages.`
          )
          navigate('/procurement')
        } else if (data.status === 'failed') {
          stopPolling()
          setPhase('error')
          toast.error(`Parsing failed: ${data.error || 'unknown error'}`)
        }
      } catch {
        // transient network error — keep polling
      }
    }, POLL_INTERVAL_MS)
  }, [navigate])

  const handleFile = (f) => {
    const allowed = ['image/jpeg', 'image/png', 'image/webp', 'application/pdf']
    if (!allowed.includes(f.type)) {
      toast.error('Please upload a JPEG, PNG, WebP, or PDF file.')
      return
    }
    if (f.size > MAX_FILE_MB * 1024 * 1024) {
      toast.error(`File exceeds the ${MAX_FILE_MB} MB limit.`)
      return
    }
    setFile(f)
    setPhase('idle')
    setProgress('')
    setTotalPages(null)
  }

  const onDrop = (e) => {
    e.preventDefault()
    setDragging(false)
    const f = e.dataTransfer.files[0]
    if (f) handleFile(f)
  }

  const upload = async () => {
    if (!file || phase === 'uploading' || phase === 'processing') return
    setPhase('uploading')
    setProgress('Encoding file…')

    try {
      const base64 = await fileToBase64(file)
      setProgress('Sending to server…')

      const { data } = await apiClient.post(
        '/api/menu/upload',
        { base64_content: base64, mime_type: file.type },
        { timeout: 30_000 }   // 30 s for the initial POST (PDF returns immediately)
      )

      if (data.job_id) {
        // ── Async PDF path: switch to polling
        setPhase('processing')
        setProgress('PDF received — converting pages…')
        startPolling(data.job_id)
      } else {
        // ── Sync image path: already done
        setPhase('done')
        toast.success(
          `Parsed ${data.recipes.length} dish(es) with ${data.confidence_score ?? '?'}% confidence.`
        )
        navigate('/procurement')
      }
    } catch (err) {
      setPhase('error')
      const msg = err.response?.data?.detail || err.message || 'Upload failed'
      toast.error(msg)
    }
  }

  return (
    <div className="max-w-xl">
      <h1 className="text-2xl font-bold mb-6">Upload Your Menu</h1>

      {/* Drop zone */}
      <div
        className={`border-2 border-dashed rounded-lg p-12 text-center cursor-pointer transition-colors ${
          dragging
            ? 'border-emerald-500 bg-emerald-50'
            : 'border-slate-300 hover:border-emerald-400'
        } ${isPending ? 'pointer-events-none opacity-60' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onClick={() => !isPending && fileRef.current?.click()}
      >
        <input
          ref={fileRef}
          type="file"
          accept="image/jpeg,image/png,image/webp,application/pdf"
          className="hidden"
          onChange={(e) => e.target.files[0] && handleFile(e.target.files[0])}
        />
        {file ? (
          <div className="space-y-1">
            <p className="text-emerald-600 font-medium">{file.name}</p>
            <p className="text-sm text-slate-500">
              {(file.size / 1024).toFixed(1)} KB
              {file.type === 'application/pdf' && (
                <span className="ml-2 text-slate-400">· PDF — multi-page supported (up to 50 pages)</span>
              )}
            </p>
          </div>
        ) : (
          <div className="space-y-2 pointer-events-none">
            <UploadIcon />
            <p className="text-slate-600 font-medium">Drop your menu here, or click to browse</p>
            <p className="text-sm text-slate-400">JPEG, PNG, WebP, or PDF (up to {MAX_FILE_MB} MB · up to 50 pages)</p>
          </div>
        )}
      </div>

      {/* Cooking-themed progress card — visible while uploading or processing */}
      {isPending && (
        <CookingLoader
          phase={phase}
          quip={COOKING_QUIPS[quipIdx]}
          backendProgress={progress}
          totalPages={totalPages}
          elapsed={elapsed}
        />
      )}

      {/* Upload button */}
      {file && !isPending && phase !== 'done' && (
        <button
          onClick={upload}
          className="mt-4 w-full py-2 bg-emerald-600 text-white rounded-md font-medium hover:bg-emerald-700"
        >
          Upload &amp; Parse Menu
        </button>
      )}
    </div>
  )
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result.split(',')[1])
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}

function UploadIcon() {
  return (
    <svg className="mx-auto h-10 w-10 text-slate-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5}
        d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" />
    </svg>
  )
}

function Spinner({ className = '' }) {
  return (
    <svg className={`animate-spin h-4 w-4 text-emerald-600 ${className}`} fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor"
        d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  )
}

function ChefHatIcon({ className = '' }) {
  return (
    <svg
      className={className}
      viewBox="0 0 64 64"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M16 32c-5 0-9-4-9-9s4-9 9-9c1 0 2 0 3 .5C21 9 26 6 32 6s11 3 13 8.5c1-.5 2-.5 3-.5 5 0 9 4 9 9s-4 9-9 9v16H16V32z" />
      <path d="M16 48h32" />
      <path d="M22 32v8M32 30v10M42 32v8" />
    </svg>
  )
}

/**
 * Claude-Code-style loader: a big rotating quip, an animated chef hat, a row of
 * pulsing "stove burner" dots, and the real backend progress + elapsed time
 * underneath. Built so a 30–60 s GPT parse feels like the chef is busy in the
 * back rather than the app being broken.
 */
function CookingLoader({ phase, quip, backendProgress, totalPages, elapsed }) {
  return (
    <div className="mt-4 bg-gradient-to-br from-emerald-50 via-white to-amber-50 border border-emerald-200 rounded-xl px-5 py-5 shadow-sm">
      <div className="flex items-center gap-4">
        <div className="relative flex-shrink-0">
          <div className="absolute inset-0 rounded-full bg-emerald-100 animate-ping opacity-60" />
          <div className="relative h-12 w-12 rounded-full bg-white border border-emerald-200 flex items-center justify-center text-emerald-600">
            <ChefHatIcon className="h-7 w-7 animate-[wiggle_1.8s_ease-in-out_infinite]" />
          </div>
        </div>
        <div className="min-w-0 flex-1">
          <p className="text-xs uppercase tracking-wider text-emerald-700 font-semibold">
            {phase === 'uploading' ? 'Sending to the kitchen' : 'In the kitchen'}
          </p>
          <p className="text-base font-medium text-slate-800 mt-1 transition-opacity duration-500">
            {quip}
            <AnimatedEllipsis />
          </p>
        </div>
        <div className="text-xs text-slate-500 font-mono tabular-nums whitespace-nowrap">
          {formatElapsed(elapsed)}
        </div>
      </div>

      {/* Indeterminate "burner" bar — slides back and forth */}
      <div className="mt-4 h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
        <div className="h-full w-1/3 rounded-full bg-gradient-to-r from-emerald-400 via-amber-400 to-orange-400 animate-[slide_1.6s_ease-in-out_infinite]" />
      </div>

      {/* Real backend progress + page count, smaller text below */}
      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
        {backendProgress && <span className="truncate">{backendProgress}</span>}
        {totalPages && (
          <span className="text-emerald-700 font-medium">
            · {totalPages} {totalPages === 1 ? 'page' : 'pages'} in the oven
          </span>
        )}
      </div>

      {/* Inline keyframes (Tailwind v3 doesn't ship `wiggle` or `slide` by default) */}
      <style>{`
        @keyframes wiggle {
          0%, 100% { transform: rotate(-6deg); }
          50%      { transform: rotate(6deg); }
        }
        @keyframes slide {
          0%   { transform: translateX(-100%); }
          50%  { transform: translateX(200%); }
          100% { transform: translateX(-100%); }
        }
      `}</style>
    </div>
  )
}

function AnimatedEllipsis() {
  return (
    <span className="inline-flex ml-0.5 align-baseline">
      <span className="animate-[blink_1.4s_infinite] [animation-delay:0s]">.</span>
      <span className="animate-[blink_1.4s_infinite] [animation-delay:0.2s]">.</span>
      <span className="animate-[blink_1.4s_infinite] [animation-delay:0.4s]">.</span>
      <style>{`
        @keyframes blink {
          0%, 80%, 100% { opacity: 0.2; }
          40%           { opacity: 1; }
        }
      `}</style>
    </span>
  )
}

function formatElapsed(seconds) {
  if (seconds < 60) return `${seconds}s`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `${m}m ${s.toString().padStart(2, '0')}s`
}
