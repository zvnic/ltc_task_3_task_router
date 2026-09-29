import type { ExpressionSpecification, Map as MapLibreMap, SymbolLayerSpecification } from 'maplibre-gl'

export const russianNameExpression: ExpressionSpecification = [
  'coalesce',
  ['get', 'name:ru'],
  ['get', 'name:ru-Latn'],
  ['get', 'name'],
  ['get', 'name:en'],
]

const houseNumberLayer: SymbolLayerSpecification = {
  id: 'house-numbers-ru',
  type: 'symbol',
  source: 'openmaptiles',
  'source-layer': 'housenumber',
  // Only when very zoomed in — active route uses marker captions instead.
  minzoom: 17,
  layout: {
    'text-field': ['get', 'housenumber'],
    'text-font': ['Noto Sans Regular'],
    'text-size': 11,
    'text-padding': 2,
    'text-optional': true,
    'text-allow-overlap': false,
    'text-ignore-placement': false,
  },
  paint: {
    'text-color': '#64748b',
    'text-halo-color': '#ffffff',
    'text-halo-width': 1.2,
    'text-opacity': 0.45,
  },
}

export function getHouseNumberLabel(address: string): string {
  // д.14 | д 83с 4 → д.83с | д.8 к 2 | д.31к2 | дом 12
  // Letter suffix sticks to the house number; extra digits only with explicit «к».
  const m = address.match(
    /(?:^|[\s,])(?:д\.?\s*|дом\s+)(\d+)([а-яА-Яa-zA-Z](?!\d))?((?:к|\s+к\.?\s*)\d+[а-яА-Яa-zA-Z]?)?/i,
  )
  if (m?.[1]) {
    const letter = m[2] ?? ''
    const corpus = (m[3] ?? '').replace(/\s+/g, '').replace(/к\./gi, 'к')
    return `д.${m[1]}${letter}${corpus}`
  }
  const nums = address.match(/\d+[а-яА-Яa-zA-Z]?/g)
  return nums?.at(-1) ? `д.${nums.at(-1)}` : ''
}

export function configureRussianBaseMap(map: MapLibreMap): {
  localizedLayerCount: number
  houseNumbersAdded: boolean
} {
  let localizedLayerCount = 0
  const style = map.getStyle()
  const layers = style?.layers ?? []

  for (const layer of layers) {
    if (layer.type !== 'symbol') continue
    const layout = layer.layout as Record<string, unknown> | undefined
    const textField = layout?.['text-field']
    if (textField == null) continue
    try {
      map.setLayoutProperty(layer.id, 'text-field', russianNameExpression)
      localizedLayerCount += 1
    } catch {
      // Ignore layers that reject the expression (e.g. non-name fields).
    }
  }

  let houseNumbersAdded = false
  if (map.getLayer('house-numbers-ru')) {
    try {
      map.setLayoutProperty('house-numbers-ru', 'text-allow-overlap', false)
      map.setLayoutProperty('house-numbers-ru', 'text-ignore-placement', false)
      map.setLayoutProperty('house-numbers-ru', 'text-optional', true)
      map.setLayoutProperty('house-numbers-ru', 'text-padding', 2)
      map.setLayoutProperty('house-numbers-ru', 'text-size', 11)
      map.setPaintProperty('house-numbers-ru', 'text-opacity', 0.45)
      // Keep base OSM numbers out until very close zoom.
      map.setLayerZoomRange('house-numbers-ru', 17, 24)
    } catch {
      // Style may not support all layout keys.
    }
    houseNumbersAdded = true
  } else if (style?.sources && 'openmaptiles' in style.sources) {
    try {
      map.addLayer(houseNumberLayer)
      houseNumbersAdded = true
    } catch {
      houseNumbersAdded = false
    }
  }

  return { localizedLayerCount, houseNumbersAdded }
}
