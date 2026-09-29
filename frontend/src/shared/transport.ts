export type TransportPresentation = {
  icon: string
  label: string
  shortLabel: string
  dasharray: [number, number]
}

const FALLBACK_TRANSPORT: TransportPresentation = {
  icon: '📍',
  label: 'Режим не указан',
  shortLabel: 'Н/д',
  dasharray: [6, 4],
}

export const TRANSPORT_PRESENTATION: Record<string, TransportPresentation> = {
  car: { icon: '🚗', label: 'Автомобиль', shortLabel: 'Авто', dasharray: [1, 0] },
  public_transport: {
    icon: '🚌',
    label: 'Общественный транспорт',
    shortLabel: 'ОТ',
    dasharray: [5, 2],
  },
  walking: { icon: '🚶', label: 'Пешком', shortLabel: 'Пешком', dasharray: [1, 2] },
  bicycle: { icon: '🚲', label: 'Велосипед', shortLabel: 'Вело', dasharray: [3, 1.5] },
}

export function transportPresentation(code: string | null | undefined): TransportPresentation {
  return code ? (TRANSPORT_PRESENTATION[code] ?? { ...FALLBACK_TRANSPORT, label: code, shortLabel: code }) : FALLBACK_TRANSPORT
}

export function transportText(code: string | null | undefined, short = false): string {
  const item = transportPresentation(code)
  return `${item.icon} ${short ? item.shortLabel : item.label}`
}
