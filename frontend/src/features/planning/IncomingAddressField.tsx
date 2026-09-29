import { useId, useState, type KeyboardEvent } from 'react'

import { apiClient } from '../../shared/api'
import type { Coordinates, GeocodeCandidate } from '../../shared/types'
import { Button } from '../../shared/ui'
import {
  addressFromDirectory,
  FAR_FROM_OFFICE_KM,
  findDirectoryAddress,
  formatCoordinates,
  nearestDistrict,
  parseCoordinates,
  type IncomingAddress,
  type ZoneAddress,
} from './incoming-address'
import { straightKm } from './plan-explain'

type SearchState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'done'; items: GeocodeCandidate[]; attribution: string; picked: number }
  | { status: 'error'; message: string }

const ORIGIN_TEXT = {
  directory: 'Координата из справочника зоны',
  geocoder: 'Координата найдена по адресу в OpenStreetMap',
  manual: 'Координата введена вручную',
} as const

function kmLabel(km: number) {
  return `${km.toLocaleString('ru-RU', { maximumFractionDigits: 1 })} км от офиса`
}

/**
 * Адрес новой заявки: любой текст. Координата — из справочника зоны (адрес совпал с
 * заявкой зоны), из поиска по адресу («Найти») или вручную «широта, долгота». Без
 * координаты заявку не отправить: подставлять чужую точку нельзя — бригада поедет
 * не туда, а пересчёт покажет неверную дорогу.
 */
export function IncomingAddressField({
  addresses,
  office,
  value,
  onChange,
}: {
  addresses: ZoneAddress[]
  office?: Coordinates
  value: IncomingAddress
  onChange: (value: IncomingAddress) => void
}) {
  const listId = useId()
  const [manualText, setManualText] = useState('')
  const [search, setSearch] = useState<SearchState>({ status: 'idle' })
  const coordinatesText = value.origin === 'manual'
    ? manualText
    : value.coordinates ? formatCoordinates(value.coordinates) : ''
  const officeKm = office && value.coordinates ? straightKm(office, value.coordinates) : null

  function changeAddress(text: string) {
    setSearch({ status: 'idle' })
    const known = findDirectoryAddress(addresses, text)
    if (known) {
      onChange(addressFromDirectory(known))
      return
    }
    // Координата относилась к прежнему адресу — сбрасываем, а не везём заявку не туда.
    setManualText('')
    onChange({ address: text, district: '', coordinates: null, origin: 'manual', coordinateSource: 'verified' })
  }

  function changeCoordinates(text: string) {
    setManualText(text)
    const coordinates = parseCoordinates(text)
    onChange({
      ...value,
      district: value.district || (coordinates ? nearestDistrict(addresses, coordinates) : ''),
      coordinates,
      origin: 'manual',
      coordinateSource: 'verified',
    })
  }

  function pick(items: GeocodeCandidate[], index: number, attribution: string) {
    const candidate = items[index]
    if (!candidate) return
    setSearch({ status: 'done', items, attribution, picked: index })
    onChange({
      address: value.address,
      district: candidate.district,
      coordinates: candidate.coordinates,
      origin: 'geocoder',
      coordinateSource: 'geocoded',
    })
  }

  async function find() {
    const query = value.address.trim()
    if (query.length < 3) {
      setSearch({ status: 'error', message: 'Введите адрес: улица и дом, для области — ещё и город.' })
      return
    }
    setSearch({ status: 'loading' })
    try {
      const result = await apiClient.geocode(query)
      if (result.items.length) pick(result.items, 0, result.attribution)
      else setSearch({ status: 'done', items: [], attribution: result.attribution, picked: -1 })
    } catch (error) {
      setSearch({ status: 'error', message: error instanceof Error ? error.message : 'Поиск адреса не удался.' })
    }
  }

  function addressKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key !== 'Enter') return
    // Enter в поле адреса — поиск, а не отправка формы без координаты.
    event.preventDefault()
    if (!findDirectoryAddress(addresses, event.currentTarget.value)) void find()
  }

  const coordinatesInvalid = coordinatesText.trim() !== '' && !value.coordinates
  return (
    <div className="space-y-3">
      <div>
        <label htmlFor={`${listId}-address`} className="block text-sm font-medium">Адрес</label>
        <div className="mt-1 flex gap-2">
          <input
            id={`${listId}-address`}
            list={`${listId}-directory`}
            value={value.address}
            onChange={(event) => changeAddress(event.target.value)}
            onKeyDown={addressKeyDown}
            readOnly={search.status === 'loading'}
            required
            autoComplete="off"
            placeholder="Улица, дом; для области — город"
            className="min-w-0 flex-1 rounded-lg border px-3 py-2"
          />
          <Button type="button" variant="secondary" onClick={() => void find()} disabled={search.status === 'loading' || value.address.trim().length < 3}>
            {search.status === 'loading' ? 'Ищем…' : 'Найти'}
          </Button>
        </div>
        <datalist id={`${listId}-directory`}>
          {addresses.map((item) => (
            <option key={item.id} value={item.address} label={item.officeKm === null ? item.district : `${item.district} · ${kmLabel(item.officeKm)}`} />
          ))}
        </datalist>
        <span className="mt-1 block text-xs text-muted">
          Любой адрес. Подсказки — адреса заявок зоны, ближние к офису первыми: аварию надо закончить за 100 минут.
          Для нового адреса нажмите «Найти» или вставьте координаты из карты.
        </span>
      </div>
      {search.status === 'error' && <div role="alert" className="rounded-lg bg-red-50 p-2 text-xs text-red-700">{search.message}</div>}
      {search.status === 'done' && search.items.length === 0 && (
        <div role="status" className="rounded-lg bg-amber-50 p-2 text-xs text-amber-900">
          Адрес не найден в OpenStreetMap. Уточните улицу, дом и город или вставьте координаты из карты.
        </div>
      )}
      {search.status === 'done' && search.items.length > 0 && (
        <div className="rounded-lg border border-slate-200 p-2 text-xs">
          <div className="font-medium text-slate-700">{search.items.length > 1 ? 'Найдено — выберите нужный дом:' : 'Найдено:'}</div>
          <ul className="mt-1 space-y-1">
            {search.items.map((item, index) => (
              <li key={`${item.coordinates.latitude},${item.coordinates.longitude},${index}`}>
                <button
                  type="button"
                  aria-pressed={index === search.picked}
                  onClick={() => pick(search.items, index, search.attribution)}
                  className={`w-full rounded px-2 py-1 text-left ${index === search.picked ? 'bg-violet-50 font-semibold text-violet-900' : 'hover:bg-slate-50'}`}
                >
                  {item.address}
                </button>
              </li>
            ))}
          </ul>
          <div className="mt-1 text-muted">Данные {search.attribution}</div>
        </div>
      )}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-[3fr_2fr]">
        <label className="text-sm font-medium">Координаты (широта, долгота)
          <input
            value={coordinatesText}
            onChange={(event) => changeCoordinates(event.target.value)}
            inputMode="decimal"
            autoComplete="off"
            placeholder="55.707821, 37.751864"
            aria-invalid={coordinatesInvalid || undefined}
            className={`mt-1 w-full rounded-lg border px-3 py-2 ${coordinatesInvalid ? 'border-red-400' : ''}`}
          />
        </label>
        <label className="text-sm font-medium">Район
          <input value={value.district} onChange={(event) => onChange({ ...value, district: event.target.value })} required className="mt-1 w-full rounded-lg border px-3 py-2" />
        </label>
      </div>
      <div className="text-xs" aria-live="polite">
        {coordinatesInvalid && <span className="text-red-700">Не удалось прочитать координаты: нужно «55.707821, 37.751864» — сначала широта.</span>}
        {!coordinatesInvalid && !value.coordinates && (
          <span className="text-amber-800">Координаты не заданы — нажмите «Найти» или вставьте «широта, долгота» из Яндекс Карт.</span>
        )}
        {value.coordinates && (
          <span className="text-muted">
            {ORIGIN_TEXT[value.origin]}
            {value.origin === 'directory' && value.coordinateSource === 'synthetic' ? ' (демо-точка района)' : ''}
            {value.origin === 'directory' && value.coordinateSource === 'geocoded' ? ' (дом найден в OpenStreetMap)' : ''}
            {officeKm !== null ? ` · ${kmLabel(officeKm)} по прямой` : ''}
          </span>
        )}
        {officeKm !== null && officeKm > FAR_FROM_OFFICE_KM && (
          <span className="mt-1 block text-red-700">Точка дальше {FAR_FROM_OFFICE_KM} км от офиса — проверьте адрес и порядок координат.</span>
        )}
      </div>
    </div>
  )
}
