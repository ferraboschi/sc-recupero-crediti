import React, { useState, useEffect, useCallback, useMemo } from 'react'
import {
  ResponsiveContainer,
  ComposedChart,
  Area,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
} from 'recharts'
import client from '../api/client'

/*
 * La CASCATA di riconciliazione + il GRAFICO dell'evoluzione.
 *
 * Sostituisce i numeri scollegati che non tornavano ("674.378" in testa,
 * "460.932 in gestione", "197.033 recuperato" — tre risposte a tre domande
 * diverse). Qui i numeri chiudono a vista perché è il BACKEND a garantire
 * l'identità scaduto_totale = non_abbinati + esclusi + contestati + in_incasso + lavorabile.
 *
 * REGOLA FERREA: non si ricalcola nulla lato client (era proprio il ricalcolo
 * a generare il bug). Si mostrano i numeri del backend, punto.
 *
 * Colori = significato (un colore, un senso):
 *   rosso  = scaduto/urgente        → Scaduto totale
 *   ambra  = lavorabile/da inseguire → Lavorabile
 *   verde  = incasso presunto        → Presunto incassato
 *   muto   = tolto dalla lavorazione → Non abbinati / Esclusi / Contestati
 */

// Etichette degli stati pratica del lavorabile (stessa lingua del backend).
const STATO_LABELS = {
  idle: 'Da Gestire',
  first_contact: 'I Contatto',
  second_contact: 'II Contatto',
  lawyer: 'Avvocato',
  waiting: 'In Attesa',
  archived: 'Archiviato',
  sconosciuto: 'Sconosciuto',
}

// Ordine di visualizzazione degli stati (sconosciuto in coda, è l'anomalia).
const STATO_ORDER = [
  'idle',
  'first_contact',
  'second_contact',
  'lawyer',
  'waiting',
  'archived',
  'sconosciuto',
]

// Tono muto per ogni chip di stato: sono suddivisioni, non semafori.
const STATO_CHIP = 'bg-dark-surface border border-dark-border text-txt-secondary'

const GIORNI_OPTIONS = [30, 90, 180]

// Serie da tracciare nel grafico. Un colore, un significato.
const CHART_SERIES = [
  { key: 'scaduto_totale', label: 'Scaduto totale', color: '#f87171' }, // accent-red
  { key: 'lavorabile', label: 'Lavorabile', color: '#fbbf24' }, // accent-amber
  { key: 'recuperato_certo', label: 'Presunto incassato (cassa)', color: '#4ade80' }, // accent-green — gli assegni in mano NON sono cassa: serie a parte (recuperato_assegni)
]

// Tratteggio dei segmenti STIMATI (storico ricostruito dalle date fattura).
const STIMA_DASH = '6 4'

/*
 * Ogni serie logica si sdoppia in due chiavi: `<key>_stima` (tratteggiata)
 * e `<key>_vero` (piena). Un punto vero ADIACENTE a un punto stimato entra
 * anche nella serie stimata: è il PONTE che fa toccare tratteggio e linea
 * piena senza buchi visivi alla transizione stima→vero (e attorno a un
 * eventuale giorno stimato in mezzo a punti veri, se un sync è saltato).
 */
function buildChartData(serie) {
  return serie.map((p, i) => {
    const inStima =
      p.stimato || serie[i - 1]?.stimato || serie[i + 1]?.stimato
    const out = { ...p }
    for (const s of CHART_SERIES) {
      out[`${s.key}_stima`] = inStima ? p[s.key] : null
      out[`${s.key}_vero`] = p.stimato ? null : p[s.key]
    }
    return out
  })
}

export default function RiconciliazioneCascata({ formatCurrency }) {
  const [recon, setRecon] = useState(null)
  const [reconLoading, setReconLoading] = useState(true)
  const [reconError, setReconError] = useState(false)

  const [giorni, setGiorni] = useState(90)
  const [serie, setSerie] = useState(null)
  const [evoLoading, setEvoLoading] = useState(true)
  const [evoError, setEvoError] = useState(false)

  const fetchRecon = useCallback(async () => {
    try {
      setReconLoading(true)
      setReconError(false)
      const res = await client.get('/dashboard/riconciliazione')
      setRecon(res.data)
    } catch (err) {
      console.error('Errore riconciliazione:', err)
      setReconError(true)
    } finally {
      setReconLoading(false)
    }
  }, [])

  const fetchEvoluzione = useCallback(async (g) => {
    try {
      setEvoLoading(true)
      setEvoError(false)
      const res = await client.get('/dashboard/evoluzione', { params: { giorni: g } })
      // La serie può essere VUOTA (storico appena partito): è un caso valido,
      // non un errore — lo gestisce il placeholder, non il ramo di errore.
      setSerie(Array.isArray(res.data?.serie) ? res.data.serie : [])
    } catch (err) {
      console.error('Errore evoluzione:', err)
      setEvoError(true)
      setSerie(null)
    } finally {
      setEvoLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchRecon()
  }, [fetchRecon])

  useEffect(() => {
    fetchEvoluzione(giorni)
  }, [giorni, fetchEvoluzione])

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <Cascata
        recon={recon}
        loading={reconLoading}
        error={reconError}
        onRetry={fetchRecon}
        formatCurrency={formatCurrency}
      />
      <Evoluzione
        serie={serie}
        loading={evoLoading}
        error={evoError}
        giorni={giorni}
        setGiorni={setGiorni}
        onRetry={() => fetchEvoluzione(giorni)}
        formatCurrency={formatCurrency}
      />
    </div>
  )
}

/* ── La cascata ──────────────────────────────────────────────────── */

function Cascata({ recon, loading, error, onRetry, formatCurrency }) {
  if (loading) {
    return (
      <div className="sc-card p-5">
        <SectionTitle>Riconciliazione</SectionTitle>
        <div className="mt-4 space-y-3 animate-pulse">
          <div className="h-8 bg-dark-surface rounded w-2/3" />
          <div className="h-4 bg-dark-surface rounded w-1/2" />
          <div className="h-4 bg-dark-surface rounded w-1/2" />
          <div className="h-4 bg-dark-surface rounded w-1/2" />
          <div className="h-8 bg-dark-surface rounded w-2/3 mt-4" />
        </div>
      </div>
    )
  }

  if (error || !recon?.cascata) {
    return (
      <div className="sc-card p-5">
        <SectionTitle>Riconciliazione</SectionTitle>
        <div className="mt-4 text-center py-6">
          <p className="text-txt-secondary text-sm mb-3">
            Non è stato possibile caricare la riconciliazione.
          </p>
          <button onClick={onRetry} className="sc-btn-secondary text-xs">
            Riprova
          </button>
        </div>
      </div>
    )
  }

  const c = recon.cascata
  const rec = recon.recuperato || {}
  const certo = rec.certo
  const stimato = rec.storico_stimato

  const deduzioni = [c.non_abbinati, c.esclusi, c.contestati, c.in_incasso].filter(Boolean)
  const perStato = c.lavorabile?.per_stato || {}

  return (
    <div className="sc-card p-5">
      <div className="flex items-center justify-between gap-2 mb-4">
        <SectionTitle>Riconciliazione</SectionTitle>
        <span className="text-[10px] text-txt-muted hidden sm:block">
          i conti chiudono
        </span>
      </div>

      {/* Scaduto totale — la cima della cascata */}
      <div className="flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-txt-primary">
            {c.scaduto_totale?.label || 'Scaduto totale'}
          </p>
          <p className="text-xs text-txt-muted">
            {c.scaduto_totale?.fatture ?? 0} fatture
          </p>
        </div>
        <p className="text-xl sm:text-2xl font-bold text-accent-red whitespace-nowrap tabular-nums">
          {formatCurrency(c.scaduto_totale?.importo || 0)}
        </p>
      </div>

      {/* Le deduzioni — tono muto: escono dalla lavorazione, non sono successi */}
      <div className="mt-3 space-y-1.5">
        {deduzioni.map((d) => (
          <div
            key={d.label}
            className="flex items-baseline justify-between gap-3 group"
            title={d.descrizione}
          >
            <div className="min-w-0 flex items-baseline gap-2">
              <span className="text-txt-muted shrink-0">−</span>
              <span className="text-sm text-txt-secondary">{d.label}</span>
              <span className="text-xs text-txt-muted shrink-0 hidden sm:inline">
                {d.fatture} fatt.
              </span>
            </div>
            <p className="text-sm font-medium text-txt-secondary whitespace-nowrap tabular-nums">
              −{formatCurrency(d.importo || 0)}
            </p>
          </div>
        ))}
      </div>

      {/* La riga risultato — il lavorabile, ciò che il motore insegue davvero */}
      <div className="mt-3 pt-3 border-t border-dark-border">
        <div className="flex items-baseline justify-between gap-3">
          <div className="min-w-0 flex items-baseline gap-2">
            <span className="text-accent-amber shrink-0">=</span>
            <div className="min-w-0">
              <p className="text-sm font-semibold text-txt-primary">
                {c.lavorabile?.label || 'Lavorabile'}
              </p>
              <p className="text-xs text-txt-muted">
                {c.lavorabile?.fatture ?? 0} fatture
              </p>
            </div>
          </div>
          <p className="text-lg sm:text-xl font-bold text-accent-amber whitespace-nowrap tabular-nums">
            {formatCurrency(c.lavorabile?.importo || 0)}
          </p>
        </div>

        {/* Il lavorabile per stato pratica */}
        <div className="mt-3 flex flex-wrap gap-1.5">
          {STATO_ORDER.map((stato) => {
            const s = perStato[stato]
            if (!s || (s.fatture || 0) <= 0) return null
            // Il bucket 'sconosciuto' è un'ANOMALIA da sanare: tono d'avviso.
            const isSconosciuto = stato === 'sconosciuto'
            return (
              <span
                key={stato}
                title={
                  `${formatCurrency(s.importo || 0)} · ${s.clienti || 0} clienti` +
                  (isSconosciuto ? ' — stato pratica non riconosciuto, da sanare' : '')
                }
                className={`sc-badge inline-flex items-center gap-1.5 ${
                  isSconosciuto
                    ? 'bg-accent-amber/10 border border-accent-amber/40 text-accent-amber'
                    : STATO_CHIP
                }`}
              >
                {isSconosciuto && <span aria-hidden="true">⚠</span>}
                {STATO_LABELS[stato] || stato}
                {/* L'unità è esplicita: i chip contano FATTURE, la KPI
                    "Clienti da Gestire" conta aziende — senza "fatt." i due
                    numeri sembravano lo stesso dato in disaccordo. */}
                <span className="font-bold">{s.fatture}</span>
                <span className="text-[10px] text-txt-muted">fatt.</span>
              </span>
            )
          })}
        </div>
      </div>

      {/* Il presunto incassato — verde: sparito dalla lista da incassare dopo
          il sollecito. È dedotto per assenza (FatturaPro), non un pagamento
          osservato: l'etichetta e la nota lo dicono senza spacciarlo per certo. */}
      {certo && (
        <div className="mt-4 pt-4 border-t border-dark-border">
          <div className="flex items-baseline justify-between gap-3">
            <div className="min-w-0">
              <p className="text-sm font-semibold text-accent-green">
                {certo.label || 'Presunto incassato'}
                <span className="ml-2 text-[10px] font-normal text-txt-muted uppercase tracking-wide">
                  presunto
                </span>
              </p>
              <p className="text-xs text-txt-muted">
                {certo.fatture ?? 0} fatture · sparite dalla lista dopo il sollecito
              </p>
            </div>
            <p className="text-lg sm:text-xl font-bold text-accent-green whitespace-nowrap tabular-nums">
              {formatCurrency(certo.importo || 0)}
            </p>
          </div>

          {/* La nota onesta: dedotto per assenza, non verificato */}
          {certo.nota && (
            <p className="mt-1.5 text-[11px] leading-snug text-txt-muted">
              {certo.nota}
            </p>
          )}

          {/* In incasso da assegni (decisione owner Q2): conta dalla
              registrazione ma NON è cassa — sotto-voce separata, con totale. */}
          {rec.in_incasso_assegni && (rec.in_incasso_assegni.fatture || 0) > 0 && (
            <div className="mt-3 bg-dark-surface border border-dark-border rounded-lg px-3 py-2.5">
              <div className="flex items-baseline justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-semibold text-accent-teal">{rec.in_incasso_assegni.label || 'In incasso da assegni'}</p>
                  <p className="text-xs text-txt-muted">
                    {rec.in_incasso_assegni.fatture} fatture · assegni in mano, non ancora cassa
                  </p>
                </div>
                <p className="text-base font-bold text-accent-teal whitespace-nowrap tabular-nums">
                  {formatCurrency(rec.in_incasso_assegni.importo || 0)}
                </p>
              </div>
              {rec.in_incasso_assegni.nota && (
                <p className="mt-1.5 text-[11px] leading-snug text-txt-muted">{rec.in_incasso_assegni.nota}</p>
              )}
              {rec.totale && (
                <p className="mt-2 text-xs text-txt-secondary">
                  Recuperato totale (incassato + in incasso):{' '}
                  <strong className="tabular-nums text-txt-primary">{formatCurrency(rec.totale.importo || 0)}</strong>
                </p>
              )}
            </div>
          )}

          {/* Lo storico stimato — MAI sommato al certo, con la sua nota onesta */}
          {stimato && (stimato.fatture || 0) > 0 && (
            <div className="mt-3 bg-dark-surface border border-dark-border rounded-lg px-3 py-2.5">
              <div className="flex items-baseline justify-between gap-3">
                <div className="min-w-0 flex items-baseline gap-2 flex-wrap">
                  <span className="text-txt-muted shrink-0">+</span>
                  <span className="text-sm text-txt-secondary">
                    {stimato.label || 'Storico stimato'}
                  </span>
                  <span className="sc-badge bg-[rgba(148,163,184,0.15)] text-txt-muted shrink-0">
                    stima
                  </span>
                </div>
                <p className="text-sm font-medium text-txt-secondary whitespace-nowrap tabular-nums">
                  {formatCurrency(stimato.importo || 0)}
                </p>
              </div>
              {stimato.nota && (
                <p className="mt-1.5 text-[11px] leading-snug text-txt-muted">
                  {stimato.nota}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ── Il grafico dell'evoluzione ──────────────────────────────────── */

function Evoluzione({ serie, loading, error, giorni, setGiorni, onRetry, formatCurrency }) {
  const hasData = Array.isArray(serie) && serie.length > 0
  const hasStima = hasData && serie.some((p) => p.stimato)
  const chartData = useMemo(
    () => (hasData ? buildChartData(serie) : []),
    [serie, hasData]
  )

  return (
    <div className="sc-card p-5 flex flex-col">
      <div className="flex items-center justify-between gap-2 mb-4 flex-wrap">
        <SectionTitle>Evoluzione</SectionTitle>
        <div className="flex flex-wrap items-center justify-end gap-1">
          {GIORNI_OPTIONS.map((g) => (
            <button
              key={g}
              onClick={() => setGiorni(g)}
              className={`px-2.5 py-1 rounded-full text-xs font-medium transition-colors ${
                giorni === g
                  ? 'bg-accent-teal text-dark-bg'
                  : 'bg-dark-card text-txt-secondary border border-dark-border hover:bg-dark-cardHover'
              }`}
            >
              {g}gg
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 min-h-[260px]">
        {loading ? (
          <div className="h-[260px] flex items-center justify-center">
            <p className="text-txt-muted text-sm">Caricamento…</p>
          </div>
        ) : error ? (
          <div className="h-[260px] flex flex-col items-center justify-center text-center">
            <p className="text-txt-secondary text-sm mb-3">
              Non è stato possibile caricare l'andamento.
            </p>
            <button onClick={onRetry} className="sc-btn-secondary text-xs">
              Riprova
            </button>
          </div>
        ) : !hasData ? (
          <div className="h-[260px] flex flex-col items-center justify-center text-center px-4">
            <p className="text-txt-secondary text-sm font-medium">
              Lo storico parte da oggi
            </p>
            <p className="text-txt-muted text-xs mt-1.5 max-w-xs">
              Il grafico si popola col passare dei sync: ogni giorno registra
              uno scatto dello scaduto.
            </p>
          </div>
        ) : (
          <EvoluzioneChart data={chartData} formatCurrency={formatCurrency} />
        )}
      </div>

      {/* Legenda: la stessa lettura di colori della cascata */}
      {hasData && (
        <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1">
          {CHART_SERIES.map((s) => (
            <span key={s.key} className="inline-flex items-center gap-1.5 text-xs text-txt-secondary">
              <span
                className="w-2.5 h-2.5 rounded-sm shrink-0"
                style={{ backgroundColor: s.color }}
              />
              {s.label}
            </span>
          ))}
        </div>
      )}

      {/* La nota onesta della stima: c'è solo se il tratteggio c'è */}
      {hasStima && (
        <p className="mt-1.5 inline-flex items-center gap-1.5 text-[11px] leading-snug text-txt-muted">
          <span
            aria-hidden="true"
            className="w-4 shrink-0 border-t-2 border-dashed border-txt-muted"
          />
          tratteggio = storico stimato dalle date fattura, con la classificazione di oggi
        </p>
      )}
    </div>
  )
}

function EvoluzioneChart({ data, formatCurrency }) {
  // Asse Y compatto (in migliaia): l'importo pieno vive nel tooltip.
  const formatAxis = (v) => {
    if (v == null) return ''
    if (Math.abs(v) >= 1000) return `${Math.round(v / 1000)}k`
    return `${v}`
  }

  // Data breve italiana per l'asse X (campo 'data' → ISO "YYYY-MM-DD").
  const formatTick = (d) => {
    if (!d) return ''
    const dt = new Date(`${d}T00:00:00`)
    if (Number.isNaN(dt.getTime())) return d
    return dt.toLocaleDateString('it-IT', { day: '2-digit', month: 'short' })
  }

  /*
   * Ogni serie logica è DUE elementi: la variante `_stima` (tratteggiata,
   * riempimento più tenue) e la variante `_vero` (piena). I null spezzano
   * l'elemento dove l'altra variante prende il testimone; il punto-ponte
   * (buildChartData) fa combaciare i due tratti senza buchi.
   */
  return (
    <ResponsiveContainer width="100%" height={260}>
      <ComposedChart data={data} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="gradScaduto" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#f87171" stopOpacity={0.35} />
            <stop offset="100%" stopColor="#f87171" stopOpacity={0.02} />
          </linearGradient>
          <linearGradient id="gradLavorabile" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#fbbf24" stopOpacity={0.3} />
            <stop offset="100%" stopColor="#fbbf24" stopOpacity={0.02} />
          </linearGradient>
          <linearGradient id="gradScadutoStima" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#f87171" stopOpacity={0.16} />
            <stop offset="100%" stopColor="#f87171" stopOpacity={0.02} />
          </linearGradient>
          <linearGradient id="gradLavorabileStima" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#fbbf24" stopOpacity={0.14} />
            <stop offset="100%" stopColor="#fbbf24" stopOpacity={0.02} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#263545" vertical={false} />
        <XAxis
          dataKey="data"
          tickFormatter={formatTick}
          tick={{ fill: '#64748b', fontSize: 11 }}
          stroke="#263545"
          minTickGap={24}
        />
        <YAxis
          tickFormatter={formatAxis}
          tick={{ fill: '#64748b', fontSize: 11 }}
          stroke="#263545"
          width={44}
        />
        <Tooltip content={<ChartTooltip formatCurrency={formatCurrency} formatTick={formatTick} />} />
        <Area
          type="monotone"
          dataKey="scaduto_totale_stima"
          name="Scaduto totale (stima)"
          stroke="#f87171"
          strokeWidth={2}
          strokeDasharray={STIMA_DASH}
          fill="url(#gradScadutoStima)"
        />
        <Area
          type="monotone"
          dataKey="scaduto_totale_vero"
          name="Scaduto totale"
          stroke="#f87171"
          strokeWidth={2}
          fill="url(#gradScaduto)"
        />
        <Area
          type="monotone"
          dataKey="lavorabile_stima"
          name="Lavorabile (stima)"
          stroke="#fbbf24"
          strokeWidth={2}
          strokeDasharray={STIMA_DASH}
          fill="url(#gradLavorabileStima)"
        />
        <Area
          type="monotone"
          dataKey="lavorabile_vero"
          name="Lavorabile"
          stroke="#fbbf24"
          strokeWidth={2}
          fill="url(#gradLavorabile)"
        />
        <Line
          type="monotone"
          dataKey="recuperato_certo_stima"
          name="Presunto incassato (stima)"
          stroke="#4ade80"
          strokeWidth={2}
          strokeDasharray={STIMA_DASH}
          dot={false}
        />
        <Line
          type="monotone"
          dataKey="recuperato_certo_vero"
          name="Presunto incassato"
          stroke="#4ade80"
          strokeWidth={2}
          dot={false}
        />
      </ComposedChart>
    </ResponsiveContainer>
  )
}

function ChartTooltip({ active, payload, label, formatCurrency, formatTick }) {
  if (!active || !payload || payload.length === 0) return null
  // Le serie sdoppiate (stima/vero) duplicherebbero le righe: si legge il
  // PUNTO intero e si mostrano le tre serie logiche, una volta sola.
  const punto = payload[0]?.payload || {}
  return (
    <div className="bg-dark-card border border-dark-border rounded-lg px-3 py-2 shadow-lg">
      <p className="text-xs font-semibold text-txt-primary mb-1">
        {formatTick(label)}
        {punto.stimato && (
          <span className="ml-2 text-[10px] font-normal text-txt-muted uppercase tracking-wide">
            stima
          </span>
        )}
      </p>
      {CHART_SERIES.map((s) => (
        <div key={s.key} className="flex items-center justify-between gap-4 text-xs">
          <span className="inline-flex items-center gap-1.5 text-txt-secondary">
            <span
              className="w-2 h-2 rounded-sm shrink-0"
              style={{ backgroundColor: s.color }}
            />
            {s.label}
          </span>
          <span className="font-medium text-txt-primary tabular-nums">
            {formatCurrency(punto[s.key] || 0)}
          </span>
        </div>
      ))}
    </div>
  )
}

function SectionTitle({ children }) {
  return <h2 className="text-base font-bold text-txt-primary">{children}</h2>
}
