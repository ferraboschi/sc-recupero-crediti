import React, { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import client from '../api/client'

// Revisione dei DUPLICATI anagrafici: schede diverse che sono la stessa
// azienda (stessa P.IVA). L'auto-merge del sync fonde già i sicuri (P.IVA
// italiana checksum-valida + nome corrispondente); qui restano quelli da
// confermare a mano — nome non corrispondente ("la P.IVA coincide, controlla
// il nome") o P.IVA estera. Unire = un solo messaggio recupera tutte le
// fatture della stessa azienda. Desktop only.

export default function MergeDuplicati() {
  const navigate = useNavigate()
  const [clusters, setClusters] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(() => new Set()) // id membri da unire
  const [busyPiva, setBusyPiva] = useState(null)
  const [lastResult, setLastResult] = useState(null)

  const load = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const res = await client.get('/customers/merge-suggestions')
      const list = res.data.clusters || []
      setClusters(list)
      // Pre-seleziona i membri NON-survivor il cui nome CORRISPONDE: sono i
      // sicuri, il grosso passa in un clic. I "controlla nome" restano da
      // spuntare a mano.
      const pre = new Set()
      for (const cl of list) {
        for (const m of cl.members) {
          if (!m.is_survivor && m.corresponds) pre.add(m.id)
        }
      }
      setSelected(pre)
    } catch (err) {
      console.error(err)
      setError('Errore nel caricamento dei duplicati')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const toggle = (id) => setSelected(prev => {
    const n = new Set(prev)
    if (n.has(id)) n.delete(id)
    else n.add(id)
    return n
  })

  const selectedInCluster = (cl) =>
    cl.members.filter(m => !m.is_survivor && selected.has(m.id)).map(m => m.id)

  const mergeCluster = async (cl) => {
    const dupIds = selectedInCluster(cl)
    if (dupIds.length === 0) return
    const survivorName = cl.members.find(m => m.is_survivor)?.nome
    if (!window.confirm(
      `Unisco ${dupIds.length} scheda${dupIds.length === 1 ? '' : 'e'} in "${survivorName}".\n\n`
      + 'Fatture, contatti e pratiche passano alla scheda sopravvissuta; le '
      + 'altre restano nel sistema come "fuse" (nascoste dagli elenchi). La '
      + 'P.IVA coincide su tutte.'
    )) return
    try {
      setBusyPiva(cl.piva)
      const res = await client.post('/customers/merge', {
        survivor_id: cl.survivor_id,
        duplicate_ids: dupIds,
      })
      setLastResult(res.data)
      await load()
    } catch (err) {
      console.error(err)
      setError('Errore durante l\'unione delle schede')
    } finally {
      setBusyPiva(null)
    }
  }

  return (
    <div className="space-y-6">
      {/* Intestazione + ritorno */}
      <div>
        <button
          onClick={() => navigate('/customers')}
          className="text-sm text-txt-secondary hover:text-accent-teal transition-colors mb-2 flex items-center gap-1"
        >
          <span aria-hidden="true">←</span> Torna ai clienti
        </button>
        <h2 className="text-lg font-semibold text-txt-primary">Duplicati da unire</h2>
        <p className="text-sm text-txt-secondary mt-1 max-w-2xl">
          Schede diverse con la <strong>stessa P.IVA</strong>: sono la stessa
          azienda. Unendole, un solo &quot;Copia Messaggio&quot; recupera tutte
          le sue fatture. I casi sicuri (P.IVA italiana + nome corrispondente)
          li unisce già in automatico il sync: qui restano quelli da
          confermare — nome diverso o P.IVA estera.
        </p>
      </div>

      {/* Esito dell'ultima unione */}
      {lastResult && (
        <div className="rounded-xl border border-accent-green/30 bg-accent-green/5 p-4">
          <p className="text-sm text-accent-green font-semibold">
            {lastResult.merged} scheda{lastResult.merged === 1 ? '' : 'e'} unit{lastResult.merged === 1 ? 'a' : 'e'} nella sopravvissuta.
          </p>
          {Array.isArray(lastResult.skipped) && lastResult.skipped.length > 0 && (
            <p className="text-xs text-txt-secondary mt-1">
              Saltate: {lastResult.skipped.length} (P.IVA diversa o già fuse).
            </p>
          )}
        </div>
      )}

      {loading ? (
        <div className="sc-card flex items-center justify-center h-64">
          <svg className="animate-spin-slow w-8 h-8 text-accent-teal" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
        </div>
      ) : error ? (
        <div className="sc-card p-6 text-accent-red">{error}</div>
      ) : clusters.length === 0 ? (
        <div className="sc-card p-10 text-center">
          <p className="text-txt-primary font-medium">Nessun duplicato da unire</p>
          <p className="text-sm text-txt-muted mt-1">
            Ogni azienda ha una sola scheda. I duplicati sicuri vengono uniti in automatico dal sync.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          {clusters.map(cl => {
            const selCount = selectedInCluster(cl).length
            const busy = busyPiva === cl.piva
            return (
              <div key={cl.piva} className="sc-card overflow-hidden">
                <div className="px-5 py-3 border-b border-dark-border flex items-center justify-between gap-4">
                  <div className="min-w-0">
                    <p className="text-sm font-semibold text-txt-primary font-mono truncate">P.IVA {cl.piva}</p>
                    <p className="text-xs text-txt-muted">
                      {cl.checksum_backed ? 'P.IVA italiana verificata' : 'P.IVA estera (solo formato)'} · {cl.members.length} schede
                    </p>
                  </div>
                  <button
                    onClick={() => mergeCluster(cl)}
                    disabled={selCount === 0 || busy}
                    className="sc-btn-primary text-sm font-bold shrink-0 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    {busy ? 'Unisco…' : `Unisci (${selCount})`}
                  </button>
                </div>
                <div className="divide-y divide-dark-border">
                  {cl.members.map(m => (
                    <div key={m.id} className="px-5 py-3 flex items-center gap-4">
                      {m.is_survivor ? (
                        <span className="sc-badge bg-accent-teal/15 text-accent-teal shrink-0" title="La scheda che resta: le altre confluiscono qui">
                          Sopravvissuta
                        </span>
                      ) : (
                        <input
                          type="checkbox"
                          checked={selected.has(m.id)}
                          onChange={() => toggle(m.id)}
                          aria-label={`Unisci ${m.nome}`}
                          className="h-4 w-4 accent-accent-teal cursor-pointer shrink-0"
                        />
                      )}
                      <div className="flex-1 min-w-0">
                        <button
                          onClick={() => navigate(`/customers/${m.id}`)}
                          className="text-sm font-medium text-accent-teal hover:text-accent-cyan text-left truncate block max-w-full"
                          title="Apri la scheda cliente"
                        >
                          {m.nome || `Cliente #${m.id}`}
                        </button>
                        <p className="text-xs text-txt-muted">
                          {m.phone || 'senza telefono'} · {m.invoice_count} fatture · {m.overdue_count} scadute
                        </p>
                      </div>
                      {!m.is_survivor && (
                        m.corresponds ? (
                          <span className="sc-badge bg-accent-teal/15 text-accent-teal shrink-0" title="Il nome corrisponde alla sopravvissuta">
                            nome ok
                          </span>
                        ) : (
                          <span className="sc-badge bg-accent-amber/15 text-accent-amber shrink-0" title="La P.IVA coincide ma il nome è diverso: controlla prima di unire">
                            controlla nome
                          </span>
                        )
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
