import {
  LngLatBounds,
  Map as MapLibreMap,
  Marker,
  NavigationControl,
  setWorkerUrl,
  type GeoJSONSource,
  type MapLayerMouseEvent,
  type StyleSpecification,
} from 'maplibre-gl'
import mapLibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'
import { useEffect, useMemo, useRef, useState } from 'react'

import { routeLocations, routeStopLocation } from '../../shared/route-geometry'
import { transportPresentation } from '../../shared/transport'
import type { DetailedEngineerRoute, EngineerRoute, ServiceRequest } from '../../shared/types'
import { Button } from '../../shared/ui'
import { configureRussianBaseMap, getHouseNumberLabel } from './map-style'
import { isEmergencyRequest, requestHighlightKind } from './request-kind'

setWorkerUrl(mapLibreWorkerUrl)

const mapStyleUrl = import.meta.env.VITE_MAP_STYLE_URL || 'https://tiles.openfreemap.org/styles/liberty'
const colors = ['#7C3AED', '#2563EB', '#059669', '#D97706', '#DB2777', '#0891B2']

const ROUTE_SOURCE_ID = 'routes'
const REQUEST_SOURCE_ID = 'requests'
const ROUTE_LAYER_IDS = [
  'route-lines-car',
  'route-lines-public-transport',
  'route-lines-walking',
  'route-lines-bicycle',
] as const
const ROUTE_LAYER_DEFINITIONS = [
  { id: ROUTE_LAYER_IDS[0], transport: 'car', dasharray: undefined },
  { id: ROUTE_LAYER_IDS[1], transport: 'public_transport', dasharray: [5, 2] },
  { id: ROUTE_LAYER_IDS[2], transport: 'walking', dasharray: [1, 2] },
  { id: ROUTE_LAYER_IDS[3], transport: 'bicycle', dasharray: [3, 1.5] },
] as const
const REQUEST_POINTS_LAYER_ID = 'request-points'
const REQUEST_LABELS_LAYER_ID = 'request-house-numbers'

const EMPTY_FEATURE_COLLECTION: GeoJSON.FeatureCollection = {
  type: 'FeatureCollection',
  features: [],
}

const EMPTY_ID_LIST: string[] = []

const blankStyle: StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#eef2f7' } }],
}

type SequenceMarkerKind = 'start' | 'stop' | 'finish'

type MapPayload = {
  dataKey: string
  fitKey: string
  markerKey: string
  routesData: GeoJSON.FeatureCollection
  requestsData: GeoJSON.FeatureCollection
  routes: EngineerRoute[]
  requests: ServiceRequest[]
  selectedEngineerId: string | null
  detailedRoute?: DetailedEngineerRoute
  focusedLegIndex: number | null
  focusedCoordinates: [number, number][]
}

function createSequenceMarker(
  label: string,
  kind: SequenceMarkerKind,
  emergency = false,
  caption?: string,
) {
  const element = document.createElement('div')
  element.dataset.routeSequenceMarker = kind
  if (emergency) element.dataset.requestKind = 'emergency'
  element.className = 'flex flex-col items-center drop-shadow-md'
  const housePart = caption ? `, ${caption}` : ''
  element.setAttribute(
    'aria-label',
    kind === 'start'
      ? 'Старт маршрута'
      : `${kind === 'finish' ? 'Финиш маршрута' : 'Визит'} ${label}${housePart}${emergency ? ', авария' : ''}`,
  )

  const badge = document.createElement('span')
  badge.className = emergency
    ? 'grid h-7 min-w-7 place-items-center rounded-full border-2 border-white bg-red-600 px-1 text-[11px] font-bold text-white ring-2 ring-red-200'
    : kind === 'start'
      ? 'grid h-6 min-w-6 place-items-center rounded-full border-2 border-white bg-slate-900 px-1 text-[10px] font-bold text-white'
      : kind === 'finish'
        ? 'grid h-7 min-w-7 place-items-center rounded-full border-2 border-white bg-emerald-600 px-1 text-[11px] font-bold text-white ring-2 ring-emerald-200'
        : 'grid h-6 min-w-6 place-items-center rounded-full border-2 border-white bg-violet-600 px-1 text-[10px] font-bold text-white'
  badge.textContent = label
  element.appendChild(badge)

  // Keep labels compact on dense city routes. The full house number remains in aria/title.
  const captionText = kind === 'start' ? 'Старт' : kind === 'finish' ? 'Финиш' : undefined
  if (captionText) {
    const captionEl = document.createElement('span')
    captionEl.className =
      kind === 'start'
        ? 'mt-0.5 rounded bg-slate-900 px-1.5 py-0.5 text-[10px] font-semibold text-white'
        : 'mt-0.5 rounded bg-emerald-700 px-1.5 py-0.5 text-[10px] font-semibold text-white'
    captionEl.textContent = captionText
    element.appendChild(captionEl)
  }
  element.title = element.getAttribute('aria-label') ?? ''
  return element
}

function requestLabel(kind: string) {
  if (kind === 'emergency') return 'А'
  if (kind === 'changed') return 'Δ'
  if (kind === 'protected') return '•'
  return ''
}

function formatLegDistance(meters: number) {
  if (meters < 1_000) return `${meters.toLocaleString('ru-RU')} м`
  return `${(meters / 1000).toLocaleString('ru-RU', { maximumFractionDigits: 1 })} км`
}

function formatNetworkDuration(seconds: number) {
  if (seconds > 0 && seconds < 60) return '<1 мин по сети'
  return `${Math.round(seconds / 60)} мин по сети`
}

export function RouteMap({
  routes,
  requests,
  selectedEngineerId,
  onSelectEngineer,
  startAddress,
  protectedRequestIds = EMPTY_ID_LIST,
  changedRequestIds = EMPTY_ID_LIST,
  detailedRoute,
  routingPending = false,
  routingError,
  onRetryRouting,
}: {
  routes: EngineerRoute[]
  requests: ServiceRequest[]
  selectedEngineerId: string | null
  onSelectEngineer: (id: string) => void
  startAddress?: string
  protectedRequestIds?: string[]
  changedRequestIds?: string[]
  detailedRoute?: DetailedEngineerRoute
  routingPending?: boolean
  routingError?: string
  onRetryRouting?: () => void
}) {
  const container = useRef<HTMLDivElement | null>(null)
  const mapRef = useRef<MapLibreMap | null>(null)
  const markersRef = useRef<Marker[]>([])
  const onSelectEngineerRef = useRef(onSelectEngineer)
  const layersReadyRef = useRef(false)
  const styleModeRef = useRef<'osm' | 'blank'>('osm')
  const lastDataKeyRef = useRef<string | null>(null)
  const lastFitKeyRef = useRef<string | null>(null)
  const lastMarkerKeyRef = useRef<string | null>(null)
  const pendingRef = useRef<MapPayload | null>(null)
  const interactionsBoundRef = useRef(false)
  const tilesUnavailableRef = useRef(false)

  const [mapReady, setMapReady] = useState(false)
  const [tilesUnavailable, setTilesUnavailable] = useState(false)
  const [mapLanguage, setMapLanguage] = useState<'pending' | 'ru' | 'fallback'>('pending')
  const [houseNumbersReady, setHouseNumbersReady] = useState(false)
  const [routeFocus, setRouteFocus] = useState<Record<string, number | 'overview'>>({})
  const [hiddenSequenceEngineerId, setHiddenSequenceEngineerId] = useState<string | null>(null)

  useEffect(() => {
    onSelectEngineerRef.current = onSelectEngineer
  }, [onSelectEngineer])

  const selectedRoute = routes.find((route) => route.engineer_id === selectedEngineerId)
  const requestById = useMemo(
    () => new Map(requests.map((request) => [request.id, request])),
    [requests],
  )
  const selectedRoutePoints = useMemo(
    () => selectedRoute ? buildRoutePoints(selectedRoute, requestById, startAddress) : [],
    [requestById, selectedRoute, startAddress],
  )
  const sequenceOpen = hiddenSequenceEngineerId !== selectedEngineerId
  const focusedLegIndex = useMemo(() => {
    if (!selectedRoute || !detailedRoute?.segments.length) return null
    const storedFocus = routeFocus[selectedRoute.engineer_id]
    if (storedFocus == null || storedFocus === 'overview') return null
    return Math.min(storedFocus, detailedRoute.segments.length - 1)
  }, [detailedRoute, routeFocus, selectedRoute])
  const focusedCoordinates = useMemo(
    () => focusedLegIndex == null
      ? detailedRoute?.geometry.coordinates ?? []
      : detailedRoute?.segments[focusedLegIndex]?.geometry.coordinates ?? [],
    [detailedRoute, focusedLegIndex],
  )
  const routesData = useMemo(() => {
    return {
      type: 'FeatureCollection' as const,
      features: routes.flatMap((route, index) => {
        if (
          route.engineer_id !== selectedEngineerId
          || detailedRoute?.engineer_id !== route.engineer_id
          || detailedRoute.routing_quality === 'estimated'
        ) return []
        const color = colors[index % colors.length] ?? '#7C3AED'
        if (focusedLegIndex == null) {
          return detailedRoute.segments.flatMap((segment, segmentIndex) => {
            if (segment.geometry.coordinates.length < 2) return []
            return [{
              type: 'Feature' as const,
              properties: {
                engineerId: route.engineer_id,
                selected: true,
                muted: false,
                color,
                transport: route.stops[segmentIndex]?.facts.travel_mode_from_previous ?? route.transport,
                routingQuality: detailedRoute.routing_quality,
              },
              geometry: segment.geometry,
            }]
          })
        }
        if (focusedCoordinates.length < 2) return []
        return [{
            type: 'Feature' as const,
            properties: {
              engineerId: route.engineer_id,
              selected: true,
              muted: false,
              color,
              transport: route.stops[focusedLegIndex]?.facts.travel_mode_from_previous ?? route.transport,
              routingQuality: detailedRoute.routing_quality,
            },
            geometry: { type: 'LineString' as const, coordinates: focusedCoordinates },
        }]
      }),
    }
  }, [detailedRoute, focusedCoordinates, focusedLegIndex, routes, selectedEngineerId])

  const requestsData = useMemo(() => {
    const protectedSet = new Set(protectedRequestIds)
    const changedSet = new Set(changedRequestIds)
    const requestEngineerId = new Map<string, string>()
    for (const route of routes) {
      for (const stop of route.stops) {
        if (stop.request_id) {
          requestEngineerId.set(stop.request_id, route.engineer_id)
        }
      }
    }
    const hasEngineerSelection = Boolean(selectedEngineerId)
    return {
      type: 'FeatureCollection' as const,
      features: requests.flatMap((request) => {
        const kind = requestHighlightKind({
          request,
          protectedIds: protectedSet,
          changedIds: changedSet,
          requestId: request.id,
        })
        const assignedEngineerId = requestEngineerId.get(request.id) ?? ''
        const selected =
          hasEngineerSelection && assignedEngineerId === selectedEngineerId
        const houseLabel = getHouseNumberLabel(request.address)
        // Labels only for the active engineer route; hide all when nothing is selected.
        const showLabel = selected && Boolean(houseLabel)
        // A focused route uses compact numbered DOM markers. Drawing the same
        // visits and every other request as circles underneath created visual noise.
        if (hasEngineerSelection) return []
        return [{
          type: 'Feature' as const,
          properties: {
            id: request.id,
            engineer_id: assignedEngineerId,
            selected,
            kind,
            show_label: showLabel,
            house_label: houseLabel,
            houseNumber: houseLabel,
            label: requestLabel(kind),
          },
          geometry: {
            type: 'Point' as const,
            coordinates: [request.coordinates.longitude, request.coordinates.latitude] as [number, number],
          },
        }]
      }),
    }
  }, [changedRequestIds, protectedRequestIds, requests, routes, selectedEngineerId])

  const dataKey = useMemo(
    () => JSON.stringify({ routes: routesData.features, requests: requestsData.features }),
    [requestsData, routesData],
  )

  const fitKey = useMemo(() => {
    return `${selectedEngineerId ?? ''}|${detailedRoute?.routing_method ?? ''}|${focusedLegIndex ?? 'overview'}|${JSON.stringify(focusedCoordinates)}`
  }, [detailedRoute, focusedCoordinates, focusedLegIndex, selectedEngineerId])

  const markerKey = useMemo(() => {
    const selected = routes.find((route) => route.engineer_id === selectedEngineerId)
    if (!selected) return ''
    return `${selected.engineer_id}|${selected.transport}|${focusedLegIndex ?? 'overview'}|${selected.stops.map((stop) => `${stop.request_id}:${stop.location?.longitude ?? ''},${stop.location?.latitude ?? ''}`).join(',')}`
  }, [focusedLegIndex, routes, selectedEngineerId])

  // MapLibre instance: create once, destroy only on unmount.
  useEffect(() => {
    if (!container.current || mapRef.current) return

    const map = new MapLibreMap({
      container: container.current,
      style: mapStyleUrl,
      center: [37.72, 55.73],
      zoom: 10.3,
      attributionControl: { compact: true },
    })
    mapRef.current = map
    styleModeRef.current = 'osm'
    layersReadyRef.current = false
    interactionsBoundRef.current = false

    map.addControl(new NavigationControl({ showCompass: false }), 'top-right')

    let styleLoaded = false
    let cancelled = false

    const finishStyleSetup = () => {
      if (cancelled || mapRef.current !== map || styleLoaded) return
      styleLoaded = true
      window.clearTimeout(fallbackTimer)

      if (styleModeRef.current === 'osm') {
        try {
          const baseMapConfiguration = configureRussianBaseMap(map)
          setMapLanguage(baseMapConfiguration.localizedLayerCount > 0 ? 'ru' : 'fallback')
          setHouseNumbersReady(baseMapConfiguration.houseNumbersAdded)
        } catch {
          setMapLanguage('fallback')
          setHouseNumbersReady(false)
        }
      } else {
        setMapLanguage('fallback')
        setHouseNumbersReady(false)
      }

      ensureMapLayers(map)
      layersReadyRef.current = true
      if (!interactionsBoundRef.current) {
        bindRouteInteraction(map, onSelectEngineerRef)
        interactionsBoundRef.current = true
      }
      setMapReady(true)

      if (pendingRef.current) {
        applyMapData(
          map,
          markersRef,
          lastDataKeyRef,
          lastFitKeyRef,
          lastMarkerKeyRef,
          pendingRef.current,
          tilesUnavailableRef.current,
        )
      }
      // Canvas may have been sized while layout still settling.
      map.resize()
    }

    // Hard-fail only: style never loads. Do not react to single tile errors.
    const fallbackTimer = window.setTimeout(() => {
      if (!cancelled && !styleLoaded && !tilesUnavailableRef.current) {
        tilesUnavailableRef.current = true
        setTilesUnavailable(true)
      }
    }, 8_000)

    map.once('load', finishStyleSetup)
    // style.load covers setStyle(blank) path; guard with styleLoaded flag.
    map.on('style.load', finishStyleSetup)

    // Debounced resize: continuous ResizeObserver → map.resize() itself flickers the canvas.
    let resizeFrame = 0
    const resizeObserver =
      typeof ResizeObserver !== 'undefined' && container.current
        ? new ResizeObserver(() => {
            if (mapRef.current !== map) return
            if (resizeFrame) cancelAnimationFrame(resizeFrame)
            resizeFrame = requestAnimationFrame(() => {
              resizeFrame = 0
              if (mapRef.current === map) map.resize()
            })
          })
        : null
    if (resizeObserver && container.current) resizeObserver.observe(container.current)

    return () => {
      cancelled = true
      window.clearTimeout(fallbackTimer)
      if (resizeFrame) cancelAnimationFrame(resizeFrame)
      resizeObserver?.disconnect()
      clearSequenceMarkers(markersRef)
      lastDataKeyRef.current = null
      lastFitKeyRef.current = null
      lastMarkerKeyRef.current = null
      layersReadyRef.current = false
      interactionsBoundRef.current = false
      if (mapRef.current === map) {
        unbindRouteInteraction(map)
        map.remove()
        mapRef.current = null
      }
      setMapReady(false)
    }
  }, [])

  // Blank basemap once if the OSM style never loads. Same Map instance.
  useEffect(() => {
    const map = mapRef.current
    if (!map || !tilesUnavailable) return
    if (styleModeRef.current === 'blank') return

    styleModeRef.current = 'blank'
    layersReadyRef.current = false
    interactionsBoundRef.current = false
    lastDataKeyRef.current = null
    lastFitKeyRef.current = null
    lastMarkerKeyRef.current = null

    const onStyleLoad = () => {
      if (mapRef.current !== map) return
      ensureMapLayers(map)
      layersReadyRef.current = true
      if (!interactionsBoundRef.current) {
        bindRouteInteraction(map, onSelectEngineerRef)
        interactionsBoundRef.current = true
      }
      setMapLanguage('fallback')
      setHouseNumbersReady(false)
      setMapReady(true)
      if (pendingRef.current) {
        applyMapData(
          map,
          markersRef,
          lastDataKeyRef,
          lastFitKeyRef,
          lastMarkerKeyRef,
          pendingRef.current,
          true,
        )
      }
      map.resize()
    }

    map.once('style.load', onStyleLoad)
    map.setStyle(blankStyle)
    return () => {
      map.off('style.load', onStyleLoad)
    }
  }, [tilesUnavailable])

  // Incremental GeoJSON / markers / bounds — never destroy the map.
  useEffect(() => {
    const payload: MapPayload = {
      dataKey,
      fitKey,
      markerKey,
      routesData,
      requestsData,
      routes,
      requests,
      selectedEngineerId,
      detailedRoute,
      focusedLegIndex,
      focusedCoordinates,
    }
    pendingRef.current = payload

    const map = mapRef.current
    if (!map || !mapReady || !layersReadyRef.current) return

    applyMapData(
      map,
      markersRef,
      lastDataKeyRef,
      lastFitKeyRef,
      lastMarkerKeyRef,
      payload,
      tilesUnavailable,
    )
  }, [
    dataKey,
    fitKey,
    mapReady,
    markerKey,
    requests,
    requestsData,
    routes,
    routesData,
    selectedEngineerId,
    detailedRoute,
    focusedCoordinates,
    focusedLegIndex,
    tilesUnavailable,
  ])

  const emergencyCount = requests.filter(isEmergencyRequest).length
  const replanHighlights = protectedRequestIds.length > 0 || changedRequestIds.length > 0
  const showTransitWidget = Boolean(
    selectedRoute?.transport === 'public_transport'
      && !routingPending
      && !detailedRoute,
  )

  return (
    <div
      className="relative h-[560px] w-full min-w-0 max-w-full overflow-hidden rounded-xl border border-slate-200 bg-slate-100 md:h-[620px] xl:h-[680px]"
      aria-label="Карта расчётных маршрутов"
      data-route-map
      data-map-engine={showTransitWidget ? 'yandex-widget' : 'maplibre'}
      data-map-ready={mapReady ? 'true' : 'false'}
      data-emergency-count={emergencyCount}
      data-replan-highlights={replanHighlights || undefined}
      data-map-language={mapLanguage}
      data-house-numbers={houseNumbersReady ? 'true' : 'false'}
      data-selected-transport={selectedRoute?.transport}
      data-routing-quality={showTransitWidget ? 'external' : detailedRoute?.routing_quality ?? (routingPending ? 'loading' : 'unavailable')}
      data-route-coordinate-count={focusedCoordinates.length}
      data-direction-arrows={detailedRoute ? 'true' : undefined}
    >
      <div ref={container} className="absolute inset-0" data-testid="route-map-canvas" />
      {showTransitWidget && selectedRoute ? (
        <TransitRouteWidget
          key={selectedRoute.engineer_id}
          route={selectedRoute}
          requests={requests}
          startAddress={startAddress}
        />
      ) : null}
      {!showTransitWidget && selectedRoute && detailedRoute ? (
        <RouteLegToolbar
          route={selectedRoute}
          detailedRoute={detailedRoute}
          focusedLegIndex={focusedLegIndex}
          onFocus={(focus) => setRouteFocus((current) => ({
            ...current,
            [selectedRoute.engineer_id]: focus,
          }))}
        />
      ) : null}
      {tilesUnavailable && !showTransitWidget ? (
        <FallbackRouteOverlay
          routes={routes}
          requests={requests}
          selectedEngineerId={selectedEngineerId}
          onSelectEngineer={onSelectEngineer}
          detailedRoute={detailedRoute}
          focusedLegIndex={focusedLegIndex}
          focusedCoordinates={focusedCoordinates}
        />
      ) : null}
      {!showTransitWidget && selectedRoute && focusedLegIndex == null && sequenceOpen ? (
        <RoutePointSequence
          route={selectedRoute}
          points={selectedRoutePoints}
          onSelectLeg={(index) => setRouteFocus((current) => ({
            ...current,
            [selectedRoute.engineer_id]: index,
          }))}
          onClose={() => setHiddenSequenceEngineerId(selectedRoute.engineer_id)}
        />
      ) : null}
      {!showTransitWidget && selectedRoute && focusedLegIndex != null ? (
        <RouteLegDetails
          route={selectedRoute}
          points={selectedRoutePoints}
          detailedRoute={detailedRoute}
          legIndex={focusedLegIndex}
          onSelectLeg={(index) => setRouteFocus((current) => ({
            ...current,
            [selectedRoute.engineer_id]: index,
          }))}
          onOverview={() => {
            setRouteFocus((current) => ({ ...current, [selectedRoute.engineer_id]: 'overview' }))
            setHiddenSequenceEngineerId(null)
          }}
        />
      ) : null}
      {!showTransitWidget && selectedRoute && focusedLegIndex == null && !sequenceOpen ? (
        <button
          type="button"
          onClick={() => setHiddenSequenceEngineerId(null)}
          className="absolute right-3 top-[108px] z-10 rounded-lg border border-violet-200 bg-white/95 px-3 py-2 text-xs font-semibold text-violet-700 shadow"
        >
          Показать точки
        </button>
      ) : null}
      {!showTransitWidget && !detailedRoute ? <div aria-label="Легенда типов заявок" className="absolute left-3 top-3 z-10 flex flex-wrap gap-2 rounded-lg bg-white/95 px-3 py-2 text-xs shadow">
        <span className="flex items-center gap-1.5">
          <span className="h-3 w-3 rounded-full border-2 border-violet-600 bg-white" />
          Обычная
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-3 w-3 rounded-full border-2 border-red-800 bg-red-600" />
          Авария
        </span>
        {replanHighlights && (
          <>
            <span className="flex items-center gap-1.5">
              <span className="h-3 w-3 rounded-full border-2 border-amber-700 bg-amber-500" />
              Изменено
            </span>
            <span className="flex items-center gap-1.5">
              <span className="h-3 w-3 rounded-full border-2 border-violet-800 bg-violet-500" />
              Защищено
            </span>
          </>
        )}
      </div> : null}
      {!showTransitWidget ? <div className="absolute bottom-3 left-3 z-10 flex max-w-[calc(100%_-_1.5rem)] flex-wrap items-center gap-2 rounded-lg bg-white/95 px-3 py-2 text-xs text-slate-600 shadow">
        {routingPending
          ? 'Получаем маршрут по транспортной сети…'
          : routingError
            ? <>
                <span role="alert">Точный маршрут не показан: {routingError}</span>
                {onRetryRouting ? (
                  <Button
                    type="button"
                    variant="secondary"
                    className="shrink-0 !px-2.5 !py-1 !text-xs shadow-none"
                    onClick={onRetryRouting}
                  >
                    Повторить
                  </Button>
                ) : null}
              </>
            : detailedRoute
              ? `${transportPresentation(selectedRoute?.transport).icon} ${transportPresentation(selectedRoute?.transport).label} · маршрут по OSM-сети · ${detailedRoute.routing_method}${tilesUnavailable ? ' · без подложки' : ''}${houseNumbersReady ? ' · номера домов у маркеров' : ''}`
              : 'Выберите инженера'}
      </div> : null}
    </div>
  )
}

function applyMapData(
  map: MapLibreMap,
  markersRef: { current: Marker[] },
  lastDataKeyRef: { current: string | null },
  lastFitKeyRef: { current: string | null },
  lastMarkerKeyRef: { current: string | null },
  payload: MapPayload,
  tilesUnavailable: boolean,
) {
  const routesSource = map.getSource(ROUTE_SOURCE_ID) as GeoJSONSource | undefined
  const requestsSource = map.getSource(REQUEST_SOURCE_ID) as GeoJSONSource | undefined
  if (!routesSource || !requestsSource) return

  // Avoid setData repaint flicker when TanStack Query returns identical geometry.
  if (lastDataKeyRef.current !== payload.dataKey) {
    routesSource.setData(payload.routesData)
    requestsSource.setData(payload.requestsData)
    lastDataKeyRef.current = payload.dataKey
  }
  applyRequestLayerPresentation(map, Boolean(payload.selectedEngineerId))

  if (lastMarkerKeyRef.current !== payload.markerKey) {
    clearSequenceMarkers(markersRef)
    const selectedRoute = payload.routes.find((route) => route.engineer_id === payload.selectedEngineerId)
    if (selectedRoute && !tilesUnavailable) {
      const requestById = new Map(payload.requests.map((request) => [request.id, request]))
      const locations = routeLocations(selectedRoute, requestById)
      const visibleSequences = payload.focusedLegIndex == null
        ? locations.map((_, index) => index)
        : [payload.focusedLegIndex, payload.focusedLegIndex + 1]
      for (const sequence of visibleSequences) {
        const location = locations[sequence]
        if (!location) continue
        if (sequence === 0) {
          markersRef.current.push(
            new Marker({
              element: createSequenceMarker('0', 'start'),
              anchor: 'bottom',
            })
              .setLngLat([location.longitude, location.latitude])
              .addTo(map),
          )
          continue
        }
        const stop = selectedRoute.stops[sequence - 1]
        if (!stop) continue
        const request = requestById.get(stop.request_id)
        if (!request) continue
        const kind = sequence === selectedRoute.stops.length ? 'finish' : 'stop'
        const emergency = isEmergencyRequest(request)
        const house = getHouseNumberLabel(request.address) || undefined
        markersRef.current.push(
          new Marker({
            element: createSequenceMarker(String(sequence), kind, emergency, house),
            anchor: 'bottom',
          })
            .setLngLat([location.longitude, location.latitude])
            .addTo(map),
        )
      }
    }
    lastMarkerKeyRef.current = payload.markerKey
  }

  if (lastFitKeyRef.current !== payload.fitKey) {
    const selectedRoute = payload.routes.find((route) => route.engineer_id === payload.selectedEngineerId)
    const requestById = new Map(payload.requests.map((request) => [request.id, request]))
    const focusPoints =
      payload.focusedCoordinates.length
        ? payload.focusedCoordinates.map(([longitude, latitude]) => ({
            longitude,
            latitude,
          }))
        : selectedRoute != null
          ? routeLocations(selectedRoute, requestById)
        : [
            ...payload.routes.map((route) => route.start_location),
            ...payload.requests.map((request) => request.coordinates),
          ]
    if (focusPoints.length > 0) {
      const first = focusPoints[0]!
      const bounds = focusPoints.slice(1).reduce(
        (current, point) => current.extend([point.longitude, point.latitude]),
        new LngLatBounds([first.longitude, first.latitude], [first.longitude, first.latitude]),
      )
      map.fitBounds(bounds, { padding: 64, maxZoom: 15.5, duration: 0 })
    }
    lastFitKeyRef.current = payload.fitKey
  }
}

function ensureMapLayers(map: MapLibreMap) {
  if (!map.getSource(ROUTE_SOURCE_ID)) {
    map.addSource(ROUTE_SOURCE_ID, {
      type: 'geojson',
      data: EMPTY_FEATURE_COLLECTION,
    })
  }
  if (!map.getSource(REQUEST_SOURCE_ID)) {
    map.addSource(REQUEST_SOURCE_ID, {
      type: 'geojson',
      data: EMPTY_FEATURE_COLLECTION,
    })
  }

  for (const definition of ROUTE_LAYER_DEFINITIONS) {
    if (map.getLayer(definition.id)) continue
    map.addLayer({
      id: definition.id,
      type: 'line',
      source: ROUTE_SOURCE_ID,
      filter: ['==', ['get', 'transport'], definition.transport],
      layout: { 'line-cap': 'round', 'line-join': 'round' },
      paint: {
        'line-color': ['coalesce', ['get', 'color'], '#7C3AED'],
        'line-width': ['case', ['==', ['get', 'selected'], true], 5.5, 3.2],
        'line-opacity': ['case', ['==', ['get', 'muted'], true], 0.28, 0.85],
        ...(definition.dasharray ? { 'line-dasharray': [...definition.dasharray] } : {}),
      },
    })
    const arrowLayerId = `${definition.id}-direction`
    if (!map.getLayer(arrowLayerId)) {
      map.addLayer({
        id: arrowLayerId,
        type: 'symbol',
        source: ROUTE_SOURCE_ID,
        filter: ['==', ['get', 'transport'], definition.transport],
        layout: {
          'symbol-placement': 'line',
          'symbol-spacing': 86,
          'text-field': '▶',
          'text-font': ['Noto Sans Regular'],
          'text-size': 14,
          'text-rotation-alignment': 'map',
          'text-pitch-alignment': 'map',
          'text-keep-upright': false,
          'text-allow-overlap': true,
          'text-ignore-placement': true,
        },
        paint: {
          'text-color': '#ffffff',
          'text-halo-color': ['coalesce', ['get', 'color'], '#7C3AED'],
          'text-halo-width': 2.5,
        },
      })
    }
  }

  if (!map.getLayer(REQUEST_POINTS_LAYER_ID)) {
    map.addLayer({
      id: REQUEST_POINTS_LAYER_ID,
      type: 'circle',
      source: REQUEST_SOURCE_ID,
      paint: {
        'circle-radius': [
          'case',
          ['get', 'selected'],
          9,
          [
            'match',
            ['get', 'kind'],
            'emergency',
            7,
            'changed',
            6.5,
            'protected',
            6,
            4.5,
          ],
        ],
        'circle-color': [
          'match',
          ['get', 'kind'],
          'emergency',
          '#DC2626',
          'changed',
          '#F59E0B',
          'protected',
          '#8B5CF6',
          '#7C3AED',
        ],
        'circle-opacity': ['case', ['get', 'selected'], 0.95, 0.55],
        'circle-stroke-color': [
          'case',
          ['get', 'selected'],
          '#0f172a',
          [
            'match',
            ['get', 'kind'],
            'emergency',
            '#991B1B',
            'changed',
            '#B45309',
            'protected',
            '#5B21B6',
            '#5B21B6',
          ],
        ],
        'circle-stroke-width': ['case', ['get', 'selected'], 2.2, 1.5],
      },
    })
  }

  if (!map.getLayer(REQUEST_LABELS_LAYER_ID)) {
    map.addLayer({
      id: REQUEST_LABELS_LAYER_ID,
      type: 'symbol',
      source: REQUEST_SOURCE_ID,
      filter: ['==', ['get', 'show_label'], true],
      layout: {
        'text-field': ['get', 'house_label'],
        'text-font': ['Noto Sans Regular'],
        'text-size': 12,
        'text-offset': [0, 1.15],
        'text-anchor': 'top',
        'text-allow-overlap': false,
        'text-ignore-placement': false,
        'text-optional': true,
        'text-padding': 4,
        'symbol-sort-key': ['case', ['get', 'selected'], 0, 1],
      },
      paint: {
        'text-color': [
          'match',
          ['get', 'kind'],
          'emergency',
          '#991B1B',
          'changed',
          '#B45309',
          'protected',
          '#5B21B6',
          '#0f172a',
        ],
        'text-halo-color': '#ffffff',
        'text-halo-width': 2,
      },
    })
  }
}

function applyRequestLayerPresentation(map: MapLibreMap, hasEngineerSelection: boolean) {
  if (map.getLayer(REQUEST_POINTS_LAYER_ID)) {
    map.setPaintProperty(REQUEST_POINTS_LAYER_ID, 'circle-opacity', [
      'case',
      ['get', 'selected'],
      0.95,
      hasEngineerSelection ? 0.25 : 0.55,
    ])
    map.setPaintProperty(REQUEST_POINTS_LAYER_ID, 'circle-radius', [
      'case',
      ['get', 'selected'],
      9,
      [
        'match',
        ['get', 'kind'],
        'emergency',
        hasEngineerSelection ? 6.5 : 9,
        'changed',
        hasEngineerSelection ? 5.5 : 8,
        'protected',
        hasEngineerSelection ? 5 : 7,
        hasEngineerSelection ? 4.2 : 6,
      ],
    ])
    map.setPaintProperty(REQUEST_POINTS_LAYER_ID, 'circle-stroke-color', [
      'case',
      ['get', 'selected'],
      '#0f172a',
      hasEngineerSelection
        ? '#cbd5e1'
        : [
            'match',
            ['get', 'kind'],
            'emergency',
            '#991B1B',
            'changed',
            '#B45309',
            'protected',
            '#5B21B6',
            '#5B21B6',
          ],
    ])
    map.setPaintProperty(
      REQUEST_POINTS_LAYER_ID,
      'circle-stroke-width',
      ['case', ['get', 'selected'], 2.2, hasEngineerSelection ? 1 : 2],
    )
  }

  if (map.getLayer(REQUEST_LABELS_LAYER_ID)) {
    // House numbers live on sequence marker captions when a route is focused.
    // Always hidden: house numbers are shown as sequence-marker captions on selection.
    map.setLayoutProperty(REQUEST_LABELS_LAYER_ID, 'visibility', 'none')
    map.setFilter(REQUEST_LABELS_LAYER_ID, ['==', ['get', 'show_label'], true])
    map.setLayoutProperty(REQUEST_LABELS_LAYER_ID, 'text-field', ['get', 'house_label'])
    map.setLayoutProperty(REQUEST_LABELS_LAYER_ID, 'text-allow-overlap', false)
    map.setLayoutProperty(REQUEST_LABELS_LAYER_ID, 'text-ignore-placement', false)
    map.setLayoutProperty(REQUEST_LABELS_LAYER_ID, 'text-optional', true)
    map.setLayoutProperty(REQUEST_LABELS_LAYER_ID, 'text-padding', 4)
    map.setLayoutProperty(REQUEST_LABELS_LAYER_ID, 'text-size', 12)
    map.setLayoutProperty(REQUEST_LABELS_LAYER_ID, 'text-offset', [0, 1.15])
    map.setLayoutProperty(
      REQUEST_LABELS_LAYER_ID,
      'symbol-sort-key',
      ['case', ['get', 'selected'], 0, 1],
    )
  }

  if (map.getLayer('house-numbers-ru')) {
    try {
      // Hide cluttering OSM house numbers while focusing a single engineer route.
      map.setLayoutProperty(
        'house-numbers-ru',
        'visibility',
        hasEngineerSelection ? 'none' : 'visible',
      )
      if (!hasEngineerSelection) {
        map.setPaintProperty('house-numbers-ru', 'text-opacity', 0.45)
      }
    } catch {
      // Base style may omit the layer on fallback tiles.
    }
  }
}
const routeInteractionHandlers = new WeakMap<
  MapLibreMap,
  {
    click: (event: MapLayerMouseEvent) => void
    enter: (event: MapLayerMouseEvent) => void
    leave: (event: MapLayerMouseEvent) => void
  }
>()

function bindRouteInteraction(
  map: MapLibreMap,
  onSelectEngineerRef: { current: (id: string) => void },
) {
  unbindRouteInteraction(map)

  const click = (event: MapLayerMouseEvent) => {
    const engineerId = event.features?.[0]?.properties?.engineerId
    if (typeof engineerId === 'string' && engineerId) {
      onSelectEngineerRef.current(engineerId)
    }
  }
  const enter = () => {
    map.getCanvas().style.cursor = 'pointer'
  }
  const leave = () => {
    map.getCanvas().style.cursor = ''
  }

  for (const layerId of ROUTE_LAYER_IDS) {
    map.on('click', layerId, click)
    map.on('mouseenter', layerId, enter)
    map.on('mouseleave', layerId, leave)
  }
  routeInteractionHandlers.set(map, { click, enter, leave })
}

function unbindRouteInteraction(map: MapLibreMap) {
  const existing = routeInteractionHandlers.get(map)
  if (!existing) return
  for (const layerId of ROUTE_LAYER_IDS) {
    map.off('click', layerId, existing.click)
    map.off('mouseenter', layerId, existing.enter)
    map.off('mouseleave', layerId, existing.leave)
  }
  routeInteractionHandlers.delete(map)
}

function clearSequenceMarkers(markersRef: { current: Marker[] }) {
  for (const marker of markersRef.current) {
    marker.remove()
  }
  markersRef.current = []
}

function transitWidgetUrl(points: Array<{ latitude: number; longitude: number }>) {
  const params = new URLSearchParams({
    mode: 'routes',
    rtext: points.map((point) => `${point.latitude},${point.longitude}`).join('~'),
    rtt: 'mt',
    lang: 'ru_RU',
  })
  return `https://yandex.ru/map-widget/v1/?${params.toString()}`
}

function formatRouteTime(value: string) {
  return value.slice(11, 16)
}

type RoutePoint = {
  sequence: number
  kind: 'start' | 'intermediate' | 'finish'
  title: string
  address: string
  externalId?: string
  plannedAt: string
}

function routePointTone(kind: RoutePoint['kind']) {
  if (kind === 'start') return 'bg-slate-900 text-white'
  if (kind === 'finish') return 'bg-emerald-700 text-white'
  return 'bg-violet-600 text-white'
}

function buildRoutePoints(
  route: EngineerRoute,
  requestById: Map<string, ServiceRequest>,
  startAddress?: string,
): RoutePoint[] {
  return [
    {
      sequence: 0,
      kind: 'start',
      title: 'Старт маршрута',
      address: startAddress ?? 'Стартовая точка бригады',
      // фактический выезд к первому визиту, а не начало смены
      plannedAt: formatRouteTime(route.stops[0]?.departure_at ?? route.departure_at),
    },
    ...route.stops.map((stop, index) => {
      const request = requestById.get(stop.request_id)
      const finish = index === route.stops.length - 1
      return {
        sequence: index + 1,
        kind: finish ? 'finish' as const : 'intermediate' as const,
        title: finish ? 'Финиш маршрута' : `Промежуточная точка ${index + 1}`,
        address: request?.address ?? `Заявка ${stop.request_id}`,
        externalId: request?.external_id,
        plannedAt: formatRouteTime(stop.service_start_at),
      }
    }),
  ]
}

function RoutePointSequence({
  route,
  points,
  onSelectLeg,
  onClose,
}: {
  route: EngineerRoute
  points: RoutePoint[]
  onSelectLeg: (index: number) => void
  onClose: () => void
}) {
  return (
    <div
      aria-label="Порядок точек маршрута"
      className="absolute bottom-14 left-3 right-3 z-10 max-h-[58%] overflow-y-auto rounded-xl border border-slate-200 bg-white/95 p-3 text-sm shadow-lg backdrop-blur-sm md:bottom-auto md:left-auto md:right-3 md:top-[108px] md:w-[380px] md:max-h-[calc(100%_-_180px)]"
      data-route-point-count={points.length}
    >
      <div className="sticky top-0 z-[1] flex items-start justify-between gap-3 bg-white/95 pb-2">
        <div>
          <div className="text-xs font-semibold uppercase tracking-wide text-violet-700">Порядок точек</div>
          <p className="mt-1 text-[11px] leading-4 text-slate-500">Нажмите на переезд между соседними точками, чтобы открыть его отдельно.</p>
        </div>
        <button type="button" onClick={onClose} className="shrink-0 rounded-md px-2 py-1 text-[11px] font-semibold text-slate-500 hover:bg-slate-100">
          Скрыть
        </button>
      </div>
      <div
        aria-label="Старт и финиш маршрута"
        className="mb-3 grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2 rounded-lg bg-slate-50 px-3 py-2"
      >
        <div className="min-w-0">
          <div className="text-[11px] font-bold text-slate-900">0 · Старт</div>
          <div className="truncate text-[10px] text-slate-500" title={points[0]?.address}>{points[0]?.address}</div>
        </div>
        <span className="font-bold text-violet-600">→</span>
        <div className="min-w-0 text-right">
          <div className="text-[11px] font-bold text-emerald-700">{points.at(-1)?.sequence} · Финиш</div>
          <div className="truncate text-[10px] text-slate-500" title={points.at(-1)?.address}>{points.at(-1)?.address}</div>
        </div>
      </div>
      <div className="mt-1">
        {points.map((point, index) => {
          const nextPoint = points[index + 1]
          const destinationStop = route.stops[index]
          const legMode = transportPresentation(
            destinationStop?.facts.travel_mode_from_previous ?? route.transport,
          )
          return (
            <div key={point.sequence}>
              <div className="flex items-start gap-3" data-route-point-kind={point.kind}>
                <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-full text-xs font-bold shadow-sm ${routePointTone(point.kind)}`}>
                  {point.sequence}
                </span>
                <div className="min-w-0 flex-1 pb-1">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                    <span className="font-semibold text-slate-800">{point.title}</span>
                    {point.externalId ? <span className="text-xs font-medium text-violet-700">Заявка {point.externalId}</span> : null}
                  </div>
                  <div className="mt-0.5 break-words text-xs leading-4 text-slate-600">{point.address}</div>
                  <div className="mt-0.5 text-[11px] font-medium text-slate-500">По плану: {point.plannedAt}</div>
                </div>
              </div>
              {nextPoint && destinationStop ? (
                <button
                  type="button"
                  onClick={() => onSelectLeg(index)}
                  aria-label={`Открыть переезд ${point.sequence} → ${nextPoint.sequence}`}
                  data-route-leg={`${point.sequence}-${nextPoint.sequence}`}
                  className="my-1 ml-3 flex min-h-9 w-[calc(100%_-_12px)] items-center gap-3 border-l-2 border-dashed border-violet-300 pl-7 text-left text-[11px] font-semibold text-violet-700 hover:text-violet-900"
                >
                  <span>↓</span>
                  <span>{legMode.icon} Переезд {point.sequence} → {nextPoint.sequence} · {destinationStop.travel_minutes_from_previous} мин</span>
                </button>
              ) : null}
            </div>
          )
        })}
      </div>
    </div>
  )
}

function RouteLegDetails({
  route,
  points,
  detailedRoute,
  legIndex,
  onSelectLeg,
  onOverview,
}: {
  route: EngineerRoute
  points: RoutePoint[]
  detailedRoute?: DetailedEngineerRoute
  legIndex: number
  onSelectLeg: (index: number) => void
  onOverview: () => void
}) {
  const stop = route.stops[legIndex]
  const origin = points[legIndex]
  const destination = points[legIndex + 1]
  if (!stop || !origin || !destination) return null
  const segment = detailedRoute?.segments[legIndex]
  const modeCode = stop.facts.travel_mode_from_previous ?? route.transport
  const mode = transportPresentation(modeCode)
  const isShortCarWalk = stop.facts.travel_mode_reason === 'short_car_leg_walk'
  const isShortTransitWalk = stop.facts.travel_mode_reason === 'short_transit_leg_walk'
  return (
    <div
      aria-label="Детали выбранного переезда"
      className="absolute bottom-14 left-3 right-3 z-10 max-h-[48%] overflow-y-auto rounded-xl border border-slate-200 bg-white/95 p-4 text-sm shadow-lg backdrop-blur-sm md:bottom-auto md:left-auto md:right-3 md:top-[108px] md:w-[380px] md:max-h-[calc(100%_-_180px)]"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-xs font-semibold uppercase tracking-wide text-violet-700">
            Переезд {origin.sequence} → {destination.sequence}
          </div>
          <p className="mt-1 text-[11px] text-slate-500">Показаны соседние точки и режим этого плеча.</p>
        </div>
        <button type="button" onClick={onOverview} className="shrink-0 rounded-md border border-violet-200 bg-white px-2 py-1 text-[11px] font-semibold text-violet-700 hover:bg-violet-50">
          Все точки
        </button>
      </div>
      <div className="mt-3 rounded-lg bg-slate-50 p-3">
        <div className="font-semibold text-slate-800">{mode.icon} {mode.label}</div>
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-slate-600">
          <span>{stop.travel_minutes_from_previous} мин по плану</span>
          <span>{formatLegDistance(stop.distance_meters_from_previous)}</span>
          {segment ? <span>{formatNetworkDuration(segment.duration_seconds)}</span> : null}
        </div>
        {isShortCarWalk ? (
          <p className="mt-2 text-xs font-medium text-violet-700">Короткий переход до 500 м выполняется пешком: автомобильная бригада не совершает микропоездку между соседними объектами.</p>
        ) : null}
        {isShortTransitWalk ? (
          <p className="mt-2 text-xs font-medium text-violet-700">Пешком не дольше, чем дойти до остановки и дождаться транспорта, — бригада идёт пешком.</p>
        ) : null}
      </div>
      <div className="mt-3 space-y-1 text-slate-700">
        {[origin, destination].map((point, index) => (
          <div key={point.sequence}>
            {index === 1 ? <div className="ml-4 min-h-8 border-l-2 border-dashed border-violet-300 pl-7 text-[11px] font-semibold leading-8 text-violet-700">↓ {mode.label}</div> : null}
            <div className="flex items-start gap-3">
              <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-full text-xs font-bold ${routePointTone(point.kind)}`}>{point.sequence}</span>
              <div className="min-w-0">
                <div className="font-semibold">{index === 0 ? 'Откуда' : 'Куда'} · {point.title}</div>
                <div className="mt-0.5 break-words text-xs text-slate-600">{point.address}</div>
                <div className="mt-0.5 text-[11px] text-slate-500">По плану: {point.plannedAt}</div>
              </div>
            </div>
          </div>
        ))}
      </div>
      <div className="mt-4 flex flex-wrap gap-2" aria-label="Соседние переезды маршрута">
        {legIndex > 0 ? <button type="button" onClick={() => onSelectLeg(legIndex - 1)} className="rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-[11px] font-semibold text-slate-700 hover:bg-slate-50">← {legIndex - 1} → {legIndex}</button> : null}
        {legIndex < route.stops.length - 1 ? <button type="button" onClick={() => onSelectLeg(legIndex + 1)} className="rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-[11px] font-semibold text-slate-700 hover:bg-slate-50">{legIndex + 1} → {legIndex + 2} →</button> : null}
      </div>
    </div>
  )
}

function TransitRouteWidget({
  route,
  requests,
  startAddress,
}: {
  route: EngineerRoute
  requests: ServiceRequest[]
  startAddress?: string
}) {
  const [routeFocus, setRouteFocus] = useState<number | 'overview'>('overview')
  const [loadedSource, setLoadedSource] = useState<string | null>(null)
  const [sequenceOpen, setSequenceOpen] = useState(true)
  const requestById = useMemo(
    () => new Map(requests.map((request) => [request.id, request])),
    [requests],
  )
  const points = useMemo(() => routeLocations(route, requestById), [requestById, route])
  const legs = useMemo(
    () => points.slice(0, -1).map((origin, index) => ({
      origin,
      destination: points[index + 1]!,
      fromSequence: index,
      toSequence: index + 1,
      destinationRequest: index < route.stops.length ? requestById.get(route.stops[index]!.request_id) : undefined,
    })),
    [points, requestById, route.stops],
  )
  const routePoints = useMemo(
    () => buildRoutePoints(route, requestById, startAddress),
    [requestById, route, startAddress],
  )

  const safeIndex = typeof routeFocus === 'number'
    ? Math.min(routeFocus, Math.max(legs.length - 1, 0))
    : 0
  const selectedLeg = legs[safeIndex]
  if (!selectedLeg) return null
  const isOverview = routeFocus === 'overview'
  const selectedPoints = isOverview ? points : [selectedLeg.origin, selectedLeg.destination]
  const source = transitWidgetUrl(selectedPoints)
  const lastStop = route.stops.at(-1)
  const destinationRequest = isOverview
    ? requestById.get(lastStop?.request_id ?? '')
    : selectedLeg.destinationRequest
  const destinationSequence = isOverview ? route.stops.length : selectedLeg.toSequence
  const destinationLabel = destinationRequest
    ? `заявка ${destinationRequest.external_id}`
    : `визит ${destinationSequence}`
  const destinationStop = isOverview ? lastStop : route.stops[safeIndex]
  // stop.departure_at — выезд на плечо, которое ведёт в этот визит
  const departureAt = isOverview
    ? route.stops[0]?.departure_at ?? route.departure_at
    : route.stops[safeIndex]?.departure_at ?? route.departure_at
  const arrivalAt = isOverview ? route.finish_at : destinationStop?.arrival_at
  const originPoint = routePoints[safeIndex]!
  const destinationPoint = routePoints[safeIndex + 1]!
  const externalUrl = source.replace('/map-widget/v1/', '/maps/')

  return (
    <div
      className="absolute inset-0 z-20 bg-slate-100"
      aria-label="Маршрут общественного транспорта"
      data-transit-route-widget
      data-route-view={isOverview ? 'overview' : 'leg'}
    >
      <iframe
        key={source}
        title={isOverview
          ? `Общественный транспорт: весь маршрут 0 → ${route.stops.length}`
          : `Общественный транспорт: ${selectedLeg.fromSequence} → ${selectedLeg.toSequence}`}
        src={source}
        width={760}
        height={520}
        className="absolute inset-0 h-full w-full border-0"
        loading="eager"
        referrerPolicy="strict-origin-when-cross-origin"
        allowFullScreen
        data-widget-loaded={loadedSource === source ? 'true' : 'false'}
        onLoad={() => setLoadedSource(source)}
      />
      {loadedSource !== source ? (
        <div className="pointer-events-none absolute inset-0 z-[5] grid place-items-center bg-slate-100 text-sm font-medium text-slate-600">
          Загружаем маршрут общественного транспорта…
        </div>
      ) : null}
      {isOverview && sequenceOpen ? (
        <RoutePointSequence
          route={route}
          points={routePoints}
          onSelectLeg={setRouteFocus}
          onClose={() => setSequenceOpen(false)}
        />
      ) : null}
      {!isOverview ? <div
        aria-label="Детали выбранного переезда"
        className="absolute bottom-14 left-3 right-3 z-10 max-h-[42%] overflow-y-auto rounded-xl border border-slate-200 bg-white/95 p-4 text-sm shadow-lg backdrop-blur-sm md:bottom-auto md:left-auto md:right-3 md:top-[108px] md:w-[360px] md:max-h-[calc(100%_-_180px)]"
      >
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="text-xs font-semibold uppercase tracking-wide text-violet-700">
              Переезд {selectedLeg.fromSequence} → {selectedLeg.toSequence}
            </div>
            <p className="mt-1 text-[11px] text-slate-500">Показаны две соседние точки выбранного перемещения.</p>
          </div>
          <button
            type="button"
            onClick={() => {
              setRouteFocus('overview')
              setSequenceOpen(true)
            }}
            className="shrink-0 rounded-md border border-violet-200 bg-white px-2 py-1 text-[11px] font-semibold text-violet-700 hover:bg-violet-50"
          >
            Все точки
          </button>
        </div>
        <div className="mt-3 space-y-1 text-slate-700">
          <div className="flex items-start gap-3" data-selected-transit-point="origin">
            <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-full text-xs font-bold ${routePointTone(originPoint.kind)}`}>
              {originPoint.sequence}
            </span>
            <div className="min-w-0">
              <div className="font-semibold">Откуда · {originPoint.title}</div>
              <div className="mt-0.5 break-words text-xs text-slate-600">{originPoint.address}</div>
              <div className="mt-0.5 text-[11px] text-slate-500">Выезд: {formatRouteTime(departureAt)}</div>
            </div>
          </div>
          <div className="ml-4 min-h-9 border-l-2 border-dashed border-violet-300 pl-7 text-[11px] font-semibold leading-9 text-violet-700">
            ↓ Переезд {originPoint.sequence} → {destinationPoint.sequence}
          </div>
          <div className="flex items-start gap-3" data-selected-transit-point="destination">
            <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-full text-xs font-bold ${routePointTone(destinationPoint.kind)}`}>
              {destinationPoint.sequence}
            </span>
            <div className="min-w-0">
              <div className="font-semibold">Куда · {destinationPoint.title}</div>
              <div className="mt-0.5 break-words text-xs text-slate-600">{destinationPoint.address}</div>
              <div className="mt-0.5 text-[11px] text-slate-500">Прибытие: {arrivalAt ? formatRouteTime(arrivalAt) : '—'}</div>
            </div>
          </div>
        </div>
        <div className="mt-4 flex flex-wrap gap-2" aria-label="Соседние переезды маршрута ОТ">
          {safeIndex > 0 ? (
            <button type="button" onClick={() => setRouteFocus(safeIndex - 1)} className="rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-[11px] font-semibold text-slate-700 hover:bg-slate-50">
              ← {safeIndex - 1} → {safeIndex}
            </button>
          ) : null}
          {safeIndex < legs.length - 1 ? (
            <button type="button" onClick={() => setRouteFocus(safeIndex + 1)} className="rounded-md border border-slate-200 bg-white px-2.5 py-1.5 text-[11px] font-semibold text-slate-700 hover:bg-slate-50">
              {safeIndex + 1} → {safeIndex + 2} →
            </button>
          ) : null}
          <a
            href={externalUrl}
            target="_blank"
            rel="noreferrer"
            className="rounded-md bg-violet-600 px-2.5 py-1.5 text-[11px] font-semibold text-white hover:bg-violet-700"
          >
            Открыть в Яндексе ↗
          </a>
        </div>
      </div> : null}
      <div className="absolute left-3 right-3 top-3 z-10 rounded-xl border border-slate-200 bg-white/95 p-2 shadow-lg backdrop-blur-sm">
        <div className="flex items-center justify-between gap-3 px-1">
          <div className="min-w-0">
            <div className="text-xs font-semibold text-slate-800">
              🚌 {route.engineer_name}: {isOverview
                ? `весь маршрут 0 → ${route.stops.length}`
                : `переезд ${selectedLeg.fromSequence} → ${selectedLeg.toSequence}`}
            </div>
            <div className="truncate text-[11px] text-slate-600">
              {isOverview
                ? `Старт 0 → Финиш ${route.stops.length} · ${route.stops.length} визитов`
                : `${selectedLeg.fromSequence === 0 ? 'Офис' : `визит ${selectedLeg.fromSequence}`} → ${destinationLabel}`}
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            {isOverview && !sequenceOpen ? (
              <button
                type="button"
                onClick={() => setSequenceOpen(true)}
                className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-[11px] font-semibold text-slate-700 hover:bg-slate-50"
              >
                Показать точки
              </button>
            ) : null}
            <a
              href={externalUrl}
              target="_blank"
              rel="noreferrer"
              className="rounded-lg border border-violet-200 bg-white px-2.5 py-1.5 text-[11px] font-semibold text-violet-700 hover:bg-violet-50"
            >
              Открыть крупно ↗
            </a>
          </div>
        </div>
        <div className="mt-2 flex gap-1 overflow-x-auto pb-0.5" aria-label="Переезды маршрута ОТ">
          <button
            type="button"
            aria-pressed={isOverview}
            onClick={() => {
              setRouteFocus('overview')
              setSequenceOpen(true)
            }}
            className={`shrink-0 rounded-md px-2.5 py-1 text-[11px] font-semibold ${
              isOverview
                ? 'bg-violet-600 text-white'
                : 'border border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
            }`}
          >
            Весь маршрут
          </button>
          {legs.map((leg, index) => (
            <button
              key={`${leg.fromSequence}-${leg.toSequence}`}
              type="button"
              aria-pressed={!isOverview && index === safeIndex}
              onClick={() => setRouteFocus(index)}
              className={`shrink-0 rounded-md px-2.5 py-1 text-[11px] font-semibold ${
                !isOverview && index === safeIndex
                  ? 'bg-violet-600 text-white'
                  : 'border border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
              }`}
            >
              {leg.fromSequence} → {leg.toSequence}
            </button>
          ))}
        </div>
      </div>
      <div className="absolute bottom-3 left-3 right-3 z-10 flex flex-wrap items-center justify-between gap-2 rounded-lg bg-white/95 px-3 py-2 text-[11px] text-slate-600 shadow">
        <span>{isOverview
          ? `Показан весь маршрут через ${points.length} точек. Источник маршрута: Яндекс Карты.`
          : 'Показан один переезд. Источник маршрута: Яндекс Карты.'}</span>
        <span className="font-semibold text-slate-800">Направление: {isOverview
          ? `0 → ${route.stops.length}`
          : `${selectedLeg.fromSequence} → ${selectedLeg.toSequence}`}</span>
      </div>
    </div>
  )
}

function RouteLegToolbar({
  route,
  detailedRoute,
  focusedLegIndex,
  onFocus,
}: {
  route: EngineerRoute
  detailedRoute: DetailedEngineerRoute
  focusedLegIndex: number | null
  onFocus: (focus: number | 'overview') => void
}) {
  const modeCode = focusedLegIndex == null
    ? route.transport
    : route.stops[focusedLegIndex]?.facts.travel_mode_from_previous ?? route.transport
  const transport = transportPresentation(modeCode)
  return (
    <div
      className="absolute left-3 right-14 top-3 z-10 rounded-xl border border-slate-200 bg-white/95 p-2 shadow-lg backdrop-blur-sm"
      aria-label="Выбор участка маршрута"
    >
      <div className="flex items-center justify-between gap-3 px-1">
        <div className="truncate text-xs font-semibold text-slate-800">
          {transport.icon} {route.engineer_name} · {focusedLegIndex == null
            ? 'весь маршрут'
            : `переезд ${focusedLegIndex} → ${focusedLegIndex + 1}`}
        </div>
        <div className="flex shrink-0 items-center gap-3">
          <div aria-label="Легенда типов заявок" className="flex items-center gap-2 text-[10px] text-slate-600">
            <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-full border-2 border-violet-600 bg-white" />Обычная</span>
            <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-full border border-red-800 bg-red-600" />Авария</span>
          </div>
          <div className="text-[11px] font-semibold text-violet-700">
            {focusedLegIndex == null
              ? `Направление 0 → ${route.stops.length}`
              : `Направление ${focusedLegIndex} → ${focusedLegIndex + 1}`}
          </div>
        </div>
      </div>
      <div className="mt-2 flex gap-1 overflow-x-auto pb-0.5" aria-label="Переезды выбранного маршрута">
        <button
          type="button"
          aria-pressed={focusedLegIndex == null}
          onClick={() => onFocus('overview')}
          className={`shrink-0 rounded-md px-2.5 py-1 text-[11px] font-semibold ${
            focusedLegIndex == null
              ? 'bg-violet-600 text-white'
              : 'border border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
          }`}
        >
          Весь маршрут
        </button>
        {detailedRoute.segments.map((segment, index) => (
          <button
            key={segment.sequence}
            type="button"
            aria-pressed={focusedLegIndex === index}
            onClick={() => onFocus(index)}
            className={`shrink-0 rounded-md px-2.5 py-1 text-[11px] font-semibold ${
              focusedLegIndex === index
                ? 'bg-violet-600 text-white'
                : 'border border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
            }`}
          >
            {index} → {index + 1}
          </button>
        ))}
      </div>
    </div>
  )
}

function FallbackRouteOverlay({
  routes,
  requests,
  selectedEngineerId,
  onSelectEngineer,
  detailedRoute,
  focusedLegIndex,
  focusedCoordinates,
}: {
  routes: EngineerRoute[]
  requests: ServiceRequest[]
  selectedEngineerId: string | null
  onSelectEngineer: (id: string) => void
  detailedRoute?: DetailedEngineerRoute
  focusedLegIndex: number | null
  focusedCoordinates: [number, number][]
}) {
  const requestById = new Map(requests.map((request) => [request.id, request]))
  const selectedRoute = routes.find((route) => route.engineer_id === selectedEngineerId)
  const points = selectedRoute
    ? focusedCoordinates.map(([longitude, latitude]) => ({ longitude, latitude }))
    : [
        ...routes.flatMap((route) => routeLocations(route, requestById)),
        ...requests.map((request) => request.coordinates),
      ]
  if (points.length === 0) return null

  const minLon = Math.min(...points.map((point) => point.longitude))
  const maxLon = Math.max(...points.map((point) => point.longitude))
  const minLat = Math.min(...points.map((point) => point.latitude))
  const maxLat = Math.max(...points.map((point) => point.latitude))
  const longitudeRange = Math.max(maxLon - minLon, 0.01)
  const latitudeRange = Math.max(maxLat - minLat, 0.01)

  const project = (longitude: number, latitude: number) => ({
    x: ((longitude - minLon) / longitudeRange) * 920 + 40,
    y: ((maxLat - latitude) / latitudeRange) * 400 + 30,
  })

  const selectedStartPoint = selectedRoute && (focusedLegIndex == null || focusedLegIndex === 0)
    ? project(selectedRoute.start_location.longitude, selectedRoute.start_location.latitude)
    : null
  const selectedStops =
    selectedRoute?.stops.flatMap((stop, index) => {
      const sequence = index + 1
      if (
        focusedLegIndex != null
        && sequence !== focusedLegIndex
        && sequence !== focusedLegIndex + 1
      ) return []
      const request = requestById.get(stop.request_id)
      const location = routeStopLocation(stop, requestById)
      if (!request || !location) return []
      const houseLabel = getHouseNumberLabel(request.address)
      return [
        {
          point: project(location.longitude, location.latitude),
          label: String(stop.sequence),
          kind: index === selectedRoute.stops.length - 1 ? ('finish' as const) : ('stop' as const),
          emergency: isEmergencyRequest(request),
          houseLabel,
        },
      ]
    }) ?? []

  return (
    <svg
      role="img"
      aria-label="Расчётные маршруты на нейтральной подложке"
      className="pointer-events-auto absolute inset-0 z-[5] h-full w-full bg-slate-100/90"
      viewBox="0 0 1000 460"
      preserveAspectRatio="xMidYMid meet"
    >
      <defs>
        <marker id="selected-route-arrowhead" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 z" fill="#0f172a" stroke="#ffffff" strokeWidth="0.75" />
        </marker>
      </defs>
      {routes.map((route, index) => {
        if (route.engineer_id !== selectedEngineerId || detailedRoute?.engineer_id !== route.engineer_id) return null
        const pathPoints = focusedCoordinates.map(([longitude, latitude]) => project(longitude, latitude))
        if (pathPoints.length < 2) return null
        const d = pathPoints.map((point, pointIndex) => `${pointIndex === 0 ? 'M' : 'L'}${point.x} ${point.y}`).join(' ')
        const selected = route.engineer_id === selectedEngineerId
        return (
          <path
            key={route.engineer_id}
            d={d}
            fill="none"
            stroke={colors[index % colors.length] ?? '#7C3AED'}
            strokeWidth={selected ? 5 : 3}
            strokeDasharray={transportPresentation(route.transport).dasharray.join(' ')}
            strokeLinecap="round"
            strokeLinejoin="round"
            opacity={selectedEngineerId && !selected ? 0.28 : 0.9}
            markerEnd="url(#selected-route-arrowhead)"
            className="cursor-pointer"
            data-route-transport={route.transport}
            onClick={() => onSelectEngineer(route.engineer_id)}
          />
        )
      })}
      {!selectedRoute ? requests.map((request) => {
        const point = project(request.coordinates.longitude, request.coordinates.latitude)
        const emergency = isEmergencyRequest(request)
        return (
          <g key={request.id} opacity={0.9}>
            <circle
              data-request-kind={emergency ? 'emergency' : 'normal'}
              cx={point.x}
              cy={point.y}
              r={emergency ? 7 : 5}
              fill={emergency ? '#DC2626' : '#7C3AED'}
              stroke={emergency ? '#991B1B' : '#5B21B6'}
              strokeWidth={1.5}
            />
            {emergency ? (
              <text x={point.x} y={point.y + 4} textAnchor="middle" fill="#fff" fontSize="11" fontWeight="700">
                А
              </text>
            ) : null}
          </g>
        )
      }) : null}
      {selectedStartPoint && (
        <g
          data-route-sequence-marker="start"
          transform={`translate(${selectedStartPoint.x} ${selectedStartPoint.y})`}
        >
          <title>Старт маршрута</title>
          <circle r="13" fill="#0f172a" stroke="#fff" strokeWidth="3" />
          <text y="4" textAnchor="middle" fill="#fff" fontSize="11" fontWeight="700">0</text>
        </g>
      )}
      {selectedStops.map(({ point, label, kind, emergency, houseLabel }) => (
        <g
          key={`${kind}-${label}`}
          data-route-sequence-marker={kind}
          data-request-kind={emergency ? 'emergency' : 'normal'}
          transform={`translate(${point.x} ${point.y})`}
        >
          <title>
            {kind === 'finish' ? `Финиш маршрута, визит ${label}` : `Визит ${label}`}
            {houseLabel ? `, ${houseLabel}` : ''}
            {emergency ? ', авария' : ''}
          </title>
          <circle
            r={kind === 'finish' || emergency ? 15 : 13}
            fill={emergency ? '#DC2626' : kind === 'finish' ? '#059669' : '#7c3aed'}
            stroke="#fff"
            strokeWidth="3"
          />
          <text y="4" textAnchor="middle" fill="#fff" fontSize="11" fontWeight="700">
            {label}
          </text>
          {houseLabel ? (
            <text
              y="28"
              textAnchor="middle"
              fill={kind === 'finish' ? '#047857' : '#0f172a'}
              fontSize="10"
              fontWeight="700"
              stroke="#fff"
              strokeWidth="3"
              paintOrder="stroke"
            >
              {houseLabel}
            </text>
          ) : null}
        </g>
      ))}
    </svg>
  )
}
