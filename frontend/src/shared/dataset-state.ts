import { createContext, useContext } from 'react'
import type { UseQueryResult } from '@tanstack/react-query'

import type { DatasetSummary } from './types'

export type DatasetContextValue = {
  datasetsQuery: UseQueryResult<DatasetSummary[], Error>
  dataset: DatasetSummary | undefined
  selectDataset: (datasetId: string) => void
}

export const DatasetContext = createContext<DatasetContextValue | null>(null)

export function useActiveDataset() {
  const context = useContext(DatasetContext)
  if (!context) throw new Error('useActiveDataset must be used inside DatasetProvider')
  return context
}
