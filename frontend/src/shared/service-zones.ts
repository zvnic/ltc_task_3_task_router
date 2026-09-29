export const serviceZoneVisuals = {
  vostok: {
    indicator: 'bg-amber-400',
    selected: 'border-amber-300 bg-amber-50 text-amber-950 ring-amber-200',
    idle: 'border-slate-200 text-slate-700 hover:border-amber-200 hover:bg-amber-50/70',
  },
  yugo_vostok: {
    indicator: 'bg-teal-500',
    selected: 'border-teal-300 bg-teal-50 text-teal-950 ring-teal-200',
    idle: 'border-slate-200 text-slate-700 hover:border-teal-200 hover:bg-teal-50/70',
  },
  yugocenter: {
    indicator: 'bg-violet-500',
    selected: 'border-violet-300 bg-violet-50 text-violet-950 ring-violet-200',
    idle: 'border-slate-200 text-slate-700 hover:border-violet-200 hover:bg-violet-50/70',
  },
} as const

const fallbackVisual = {
  indicator: 'bg-slate-400',
  selected: 'border-slate-300 bg-slate-100 text-slate-950 ring-slate-200',
  idle: 'border-slate-200 text-slate-700 hover:border-slate-300 hover:bg-slate-50',
}

export function serviceZoneVisual(zoneCode: string | null) {
  if (zoneCode && zoneCode in serviceZoneVisuals) {
    return serviceZoneVisuals[zoneCode as keyof typeof serviceZoneVisuals]
  }
  return fallbackVisual
}
