import { AnimatePresence, motion } from 'framer-motion'
import { useLocation } from 'react-router-dom'

export default function PageTransition({ children }: { children: React.ReactNode }) {
  const location = useLocation()
  const prefersReduced = typeof window !== 'undefined' && window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  return (
    <AnimatePresence mode="wait">
      <motion.div
        key={location.pathname}
        initial={prefersReduced ? false : { opacity: 0, y: 8 }}
        animate={prefersReduced ? {} : { opacity: 1, y: 0 }}
        exit={prefersReduced ? {} : { opacity: 0, y: -8 }}
        transition={{ duration: 0.18 }}
      >
        {children}
      </motion.div>
    </AnimatePresence>
  )
}


