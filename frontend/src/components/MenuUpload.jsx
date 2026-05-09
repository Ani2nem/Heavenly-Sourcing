import { useState, useRef, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'react-toastify'
import { apiClient } from '../services/api'

const POLL_INTERVAL_MS = 3000
const MAX_FILE_MB = 25

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

  // clean up polling timer on unmount
  useEffect(() => () => clearInterval(pollRef.current), [])

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

  const isPending = phase === 'uploading' || phase === 'processing'

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

      {/* Progress card — visible while processing */}
      {isPending && (
        <div className="mt-4 bg-slate-50 border border-slate-200 rounded-lg px-4 py-3 flex items-start gap-3">
          <Spinner className="mt-0.5 flex-shrink-0" />
          <div className="min-w-0">
            <p className="text-sm font-medium text-slate-700">
              {phase === 'uploading' ? 'Uploading…' : 'Processing PDF…'}
            </p>
            {progress && (
              <p className="text-xs text-slate-500 mt-0.5 truncate">{progress}</p>
            )}
            {totalPages && (
              <p className="text-xs text-emerald-600 mt-1">
                {totalPages} pages detected, processing...
              </p>
            )}
          </div>
        </div>
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
