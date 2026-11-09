import React from 'react'

// Agente di vendita del cliente (Maggioni / Izzo / Scotti), accanto alla
// ragione sociale in ogni lista. La lista `agents` arriva già normalizzata dal
// server (backend/engine/agents.py: tag cliente Shopify, con gli alias decisi
// dall'owner — Rendina → Izzo, Sgariglia De Leo → Maggioni; se ce n'è più di
// uno si mostrano tutti). Pillola con solo bordo: si distingue a colpo d'occhio
// dai badge di stato (pieni). Un colore per agente, lo stesso ovunque.
const AGENT_STYLE = {
  Maggioni: 'border-accent-blue/50 text-accent-blue',
  Izzo: 'border-accent-cyan/50 text-accent-cyan',
  Scotti: 'border-accent-purple/50 text-accent-purple',
}
const FALLBACK_STYLE = 'border-txt-muted/50 text-txt-secondary'

export default function AgentBadges({ agents, className = '' }) {
  if (!Array.isArray(agents) || agents.length === 0) return null
  return (
    <span className={`inline-flex items-center gap-1 align-middle ${className}`} data-testid="agent-badges">
      {agents.map(agent => (
        <span
          key={agent}
          className={`sc-badge border bg-transparent font-medium leading-4 ${AGENT_STYLE[agent] || FALLBACK_STYLE}`}
          title={`Agente: ${agent}`}
        >
          {agent}
        </span>
      ))}
    </span>
  )
}
