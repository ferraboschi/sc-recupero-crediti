import React from 'react'

// Badge dello stadio di avanzamento di UNA fattura (definizione nel server).
const STAGE_STYLE = {
  none: 'bg-[rgba(148,163,184,0.15)] text-txt-secondary',
  first: 'bg-accent-teal/15 text-accent-teal',
  second: 'bg-accent-amber/15 text-accent-amber',
  lawyer: 'bg-accent-purple/15 text-accent-purple',
  in_incasso: 'bg-accent-teal/15 text-accent-teal',
  insoluto: 'bg-accent-red text-dark-bg',
  sospetto: 'bg-accent-amber/25 text-accent-amber',
}

export const CHANNEL_LABELS = { whatsapp_copy: 'WhatsApp', whatsapp_link: 'WhatsApp', email_copy: 'Email', email: 'Email', phone: 'Telefono' }

export default function StageBadge({ stage, label, className = '' }) {
  if (!stage) return <span className="text-txt-muted">—</span>
  return <span className={`sc-badge ${STAGE_STYLE[stage] || STAGE_STYLE.none} ${className}`}>{label || stage}</span>
}
