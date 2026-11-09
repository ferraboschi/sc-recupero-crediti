import React from 'react'

// Agente di vendita del cliente (Maggioni / Izzo / Scotti), accanto alla
// ragione sociale in ogni lista. La lista `agents` arriva già normalizzata dal
// server (backend/engine/agents.py: tag cliente Shopify, con gli alias decisi
// dall'owner — Rendina → Izzo, Sgariglia De Leo → Maggioni; se ce n'è più di
// uno si mostrano tutti).
//
// Stile UNICO e neutro, a solo bordo: il nome dell'agente è già scritto nella
// pillola, un colore non aggiungerebbe informazione e ogni tinta della palette
// ha già un significato (blu = I Contatto, viola = In Attesa/FatturaPro,
// ciano = hover dei link, ambra = warning…). Il bordo la distingue a colpo
// d'occhio dai badge di stato, che sono pieni.
const AGENT_STYLE = 'border-txt-secondary/60 text-txt-secondary'

export default function AgentBadges({ agents, className = '' }) {
  if (!Array.isArray(agents) || agents.length === 0) return null
  return (
    <span className={`inline-flex items-center gap-1 align-middle ${className}`} data-testid="agent-badges">
      {agents.map(agent => (
        <span
          key={agent}
          className={`sc-badge border bg-transparent font-medium leading-4 whitespace-nowrap ${AGENT_STYLE}`}
          title={`Agente: ${agent}`}
        >
          <span className="sr-only">Agente </span>
          {agent}
        </span>
      ))}
    </span>
  )
}
