import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState, type ReactNode } from 'react'

import { apiClient } from './api'
import { DatasetContext } from './dataset-state'

const STORAGE_KEY = 'beeline-routes-active-dataset'

export function DatasetProvider({ children }: { children: ReactNode }) {
  const datasetsQuery = useQuery({ queryKey: ['datasets'], queryFn: apiClient.datasets })
  const [selectedId, setSelectedId] = useState(() => localStorage.getItem(STORAGE_KEY) ?? '')
  const dataset = useMemo(() => {
    const datasets = datasetsQuery.data ?? []
    return (
      datasets.find((item) => item.id === selectedId)
      ?? datasets.find((item) => item.service_zone === 'vostok')
      ?? datasets[0]
    )
  }, [datasetsQuery.data, selectedId])

  useEffect(() => {
    if (datasetsQuery.data === undefined) return
    if (dataset && dataset.id !== selectedId) {
      localStorage.setItem(STORAGE_KEY, dataset.id)
      return
    }
    if (!dataset && selectedId) {
      localStorage.removeItem(STORAGE_KEY)
    }
  }, [dataset, datasetsQuery.data, selectedId])

  function selectDataset(datasetId: string) {
    setSelectedId(datasetId)
    if (datasetId) localStorage.setItem(STORAGE_KEY, datasetId)
    else localStorage.removeItem(STORAGE_KEY)
  }

  return (
    <DatasetContext.Provider value={{ datasetsQuery, dataset, selectDataset }}>
      {children}
    </DatasetContext.Provider>
  )
}
