import React, { useState, useEffect, useRef, useCallback } from 'react'
import client from '../api/client'

/**
 * SyncStatus — indicatore del sync AUTOMATICO (niente pulsante manuale).
 *
 * Il sync gira già da solo ogni ora (scheduler APScheduler nel backend): qui
 * si COMUNICA soltanto quando avverrà il prossimo e quando è avvenuto l'ultimo.
 *
 * Fonte dati: GET /sync/status → { last_sync, progress, scheduler }.
 * È l'endpoint più leggero che porta il blocco scheduler (identico a quello di
 * /api/system) SENZA i controlli d'integrità sul DB, e in più espone
 * progress.running (sync in corso ORA). Campi usati:
 *   - scheduler.next_run_times.hourly_sync_job → ISO tz-aware (Europe/Rome)
 *   - scheduler.running / scheduler.hourly_enabled → salute dello scheduler
 *   - last_sync.cases.last_sync → marker persistito di fine pipeline (UTC naive)
 *   - progress.running → un sync è in corso adesso
 *
 * variant="compact" (topbar) | "detailed" (dashboard).
 */

// Ogni quanto si rilegge lo stato dal server.
const FETCH_INTERVAL_MS = 60000
// Ogni quanto si ricalcola il countdown dai dati già in memoria (senza rete).
const TICK_INTERVAL_MS = 30000
// Sotto questa età l'ultimo sync è "fresco" → verde.
const FRESH_MAX_MIN = 90

/**
 * I timestamp persistiti (last_sync) sono UTC "naive" — datetime.utcnow()
 * .isoformat(), senza offset: vanno interpretati come UTC, non come ora locale.
 * next_run_times è invece già ISO con offset (tz-aware) → si parsa diretto.
 */
function parseServerDate(iso, { assumeUtc = false } = {}) {
  if (!iso) return null
  let s = iso
  if (assumeUtc && !/[zZ]$|[+-]\d{2}:?\d{2}$/.test(iso)) s = `${iso}Z`
  const d = new Date(s)
  return Number.isNaN(d.getTime()) ? null : d
}

function formatCountdown(mins) {
  if (mins <= 0) return 'a breve'
  if (mins < 60) return `tra ${mins} min`
  const h = Math.floor(mins / 60)
  const m = mins % 60
  return `tra ${h} h ${String(m).padStart(2, '0')} min`
}

function formatAgo(ageMin) {
  if (ageMin < 1) return 'adesso'
  if (ageMin < 60) return `${Math.round(ageMin)} min fa`
  if (ageMin < 1440) return `${Math.floor(ageMin / 60)} h fa`
  return `${Math.floor(ageMin / 1440)} g fa`
}

/** Normalizza lo stato grezzo + l'istante corrente in ciò che serve al render. */
function derive(status, now) {
  if (!status) return { mode: 'loading' }
  if (status.inProgress) return { mode: 'inProgress' }
  if (!status.running || !status.hourlyEnabled) return { mode: 'down' }

  const out = { mode: 'ok' }
  if (status.last) {
    const ageMin = (now - status.last.getTime()) / 60000
    out.fresh = ageMin >= -1 && ageMin <= FRESH_MAX_MIN
    out.agoText = formatAgo(Math.max(0, ageMin))
  }
  if (!status.next) {
    out.mode = 'starting'
    return out
  }
  const mins = Math.round((status.next.getTime() - now) / 60000)
  out.countdownText = formatCountdown(mins)
  out.nextTimeText = status.next.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' })
  return out
}

function ClockIcon({ className = '' }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  )
}

function SpinIcon({ className = '' }) {
  return (
    <svg className={`animate-spin-slow ${className}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
    </svg>
  )
}

function WarnIcon({ className = '' }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M5.07 19h13.86a2 2 0 001.71-3.03L13.71 4.03a2 2 0 00-3.42 0L3.36 15.97A2 2 0 005.07 19z" />
    </svg>
  )
}

export default function SyncStatus({ variant = 'compact' }) {
  const [status, setStatus] = useState(null)
  const [now, setNow] = useState(() => Date.now())
  const mounted = useRef(true)

  const fetchStatus = useCallback(async () => {
    try {
      const res = await client.get('/sync/status')
      if (!mounted.current) return
      const sched = res.data?.scheduler
      if (!sched) return // blocco assente: si conserva l'ultimo stato noto
      const nextIso = sched.next_run_times?.hourly_sync_job || null
      const lastIso = res.data?.last_sync?.cases?.last_sync || sched.last_sync?.cases || null
      setStatus({
        next: parseServerDate(nextIso),
        last: parseServerDate(lastIso, { assumeUtc: true }),
        running: sched.running === true,
        hourlyEnabled: sched.hourly_enabled !== false,
        inProgress: res.data?.progress?.running === true,
      })
      setNow(Date.now())
    } catch {
      // Cold start Render o hiccup di rete: il client axios ritenta già i
      // 502/503. Si conserva l'ultimo stato noto (o si resta in "loading").
    }
  }, [])

  useEffect(() => {
    mounted.current = true
    fetchStatus()
    const fetchId = setInterval(fetchStatus, FETCH_INTERVAL_MS)
    const tickId = setInterval(() => {
      if (mounted.current) setNow(Date.now())
    }, TICK_INTERVAL_MS)
    return () => {
      mounted.current = false
      clearInterval(fetchId)
      clearInterval(tickId)
    }
  }, [fetchStatus])

  const s = derive(status, now)

  // ── Tooltip completo (sempre disponibile anche quando il testo è corto) ──
  let title = 'Sincronizzazione automatica ogni ora'
  if (s.mode === 'inProgress') title = 'Sincronizzazione in corso'
  else if (s.mode === 'down') title = 'Sync automatico non attivo — controlla lo scheduler nella pagina Sistema'
  else if (s.mode === 'starting') title = 'Sync automatico in avvio…'
  else if (s.mode === 'ok') {
    title = `Prossimo sync automatico alle ${s.nextTimeText} (${s.countdownText})`
    if (s.agoText) title += ` · aggiornato ${s.agoText}`
  }

  // ── Variante COMPATTA (topbar) ──────────────────────────────────────────
  if (variant === 'compact') {
    let Icon = ClockIcon
    let tone = 'text-accent-teal'
    let textTone = 'text-txt-secondary'
    let short = '…'
    let full = 'Sincronizzazione automatica'

    if (s.mode === 'inProgress') {
      Icon = SpinIcon; tone = 'text-accent-teal'; textTone = 'text-accent-teal'
      short = 'in corso…'; full = 'Sincronizzazione in corso…'
    } else if (s.mode === 'down') {
      Icon = WarnIcon; tone = 'text-accent-amber'; textTone = 'text-accent-amber'
      short = 'Sync off'; full = 'Sync automatico non attivo'
    } else if (s.mode === 'starting') {
      tone = 'text-txt-muted'; textTone = 'text-txt-muted'
      short = 'in avvio…'; full = 'Sync in avvio…'
    } else if (s.mode === 'ok') {
      tone = 'text-accent-teal'; textTone = 'text-txt-secondary'
      short = s.countdownText; full = `Prossimo sync ${s.countdownText}`
    } else {
      tone = 'text-txt-muted'; textTone = 'text-txt-muted'
    }

    return (
      <div className="flex items-center gap-1.5 whitespace-nowrap" title={title}>
        <Icon className={`w-3.5 h-3.5 shrink-0 ${tone}`} />
        <span className={`text-xs ${textTone}`}>
          <span className="md:hidden">{short}</span>
          <span className="hidden md:inline">{full}</span>
        </span>
      </div>
    )
  }

  // ── Variante DETTAGLIATA (dashboard) ────────────────────────────────────
  if (s.mode === 'inProgress') {
    return (
      <div className="flex items-center gap-2 text-right" title={title}>
        <SpinIcon className="w-4 h-4 text-accent-teal shrink-0" />
        <span className="text-sm font-medium text-accent-teal">Sincronizzazione in corso…</span>
      </div>
    )
  }

  if (s.mode === 'down') {
    return (
      <div className="flex flex-col items-end gap-0.5" title={title}>
        <div className="flex items-center gap-2">
          <WarnIcon className="w-4 h-4 text-accent-amber shrink-0" />
          <span className="text-sm font-medium text-accent-amber">Sincronizzazione automatica non attiva</span>
        </div>
        <span className="text-xs text-txt-muted">Controlla lo scheduler nella pagina Sistema</span>
      </div>
    )
  }

  return (
    <div className="flex flex-col items-end gap-0.5" title={title}>
      <div className="flex items-center gap-2">
        <ClockIcon className="w-4 h-4 text-accent-teal shrink-0" />
        <span className="text-sm text-txt-secondary">Sincronizzazione automatica ogni ora</span>
      </div>
      <span className="text-xs text-txt-muted">
        {s.mode === 'ok' && s.nextTimeText
          ? <>Prossimo alle {s.nextTimeText} · {s.countdownText}</>
          : s.mode === 'starting'
            ? 'In avvio…'
            : 'Verifica prossimo sync…'}
        {s.agoText && (
          <>
            {' · '}
            <span className={s.fresh ? 'text-accent-green' : 'text-txt-muted'}>
              aggiornato {s.agoText}
            </span>
          </>
        )}
      </span>
    </div>
  )
}
