import { useEffect, useRef } from 'react'
import { toast } from 'react-toastify'
import { apiClient } from '../services/api'

export default function NotificationToast() {
  const seenIds = useRef(new Set())

  useEffect(() => {
    const poll = () => {
      apiClient.get('/api/notifications')
        .then(res => {
          const unread = res.data?.unread || []
          unread.forEach(n => {
            if (!seenIds.current.has(n.id)) {
              seenIds.current.add(n.id)
              toast.info(`${n.title}: ${n.message}`, { toastId: n.id })
            }
          })
        })
        .catch(() => {})
    }

    poll()
    const interval = setInterval(poll, 10000)
    return () => clearInterval(interval)
  }, [])

  return null
}
