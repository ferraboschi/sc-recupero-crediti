import React from 'react'

export default function StatsWidget({ label, value, subtitle, icon: Icon, trend, trendLabel, color = 'blue' }) {
  const colorClasses = {
    blue: 'border-accent-blue/30',
    green: 'border-accent-green/30',
    purple: 'border-accent-purple/30',
    orange: 'border-accent-amber/30',
    red: 'border-accent-red/30',
  }

  const valueColors = {
    blue: 'text-accent-blue',
    green: 'text-accent-green',
    purple: 'text-accent-purple',
    orange: 'text-accent-amber',
    red: 'text-accent-red',
  }

  return (
    <div className={`sc-kpi ${colorClasses[color]}`}>
      <p className="sc-kpi-label">{label}</p>
      <p className={`sc-kpi-value mt-2 ${valueColors[color] || 'text-txt-primary'}`}>{value}</p>
      {subtitle && <p className="text-xs text-txt-muted mt-1">{subtitle}</p>}
      {trend !== undefined && (
        <p className={`text-xs mt-2 ${trend >= 0 ? 'text-accent-green' : 'text-accent-red'}`}>
          {trend >= 0 ? '↑' : '↓'} {Math.abs(trend)}% {trendLabel && `(${trendLabel})`}
        </p>
      )}
    </div>
  )
}
