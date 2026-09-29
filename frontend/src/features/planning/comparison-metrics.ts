export type DistanceChange = {
  label: string
  value: string
  tone: 'positive' | 'negative' | 'neutral'
}

function formatKilometers(meters: number) {
  return `${(Math.abs(meters) / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} км`
}

export function describeDistanceChange(
  savingMeters: number,
  savingPercent: number | null,
): DistanceChange {
  const percent = savingPercent == null
    ? ''
    : ` · ${Math.abs(savingPercent).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%`
  if (savingMeters > 0) {
    return {
      label: 'Пробег меньше базового варианта',
      value: `${formatKilometers(savingMeters)}${percent}`,
      tone: 'positive',
    }
  }
  if (savingMeters < 0) {
    return {
      label: 'Пробег больше базового варианта',
      value: `${formatKilometers(savingMeters)}${percent}`,
      tone: 'negative',
    }
  }
  return { label: 'Пробег относительно базового варианта', value: 'без изменения', tone: 'neutral' }
}
