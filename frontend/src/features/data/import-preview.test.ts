import { describe, expect, it } from 'vitest'

import type { ImportPreview } from '../../shared/api'
import { canSkipDuplicateRows, importIssueText } from './import-preview'

function preview(errors: ImportPreview['errors']): ImportPreview {
  return {
    total_rows: 2,
    valid_count: 1,
    skipped_empty: 0,
    errors,
    service_zone: null,
    preview: [],
  }
}

describe('предпросмотр импорта', () => {
  it('предлагает пропуск только когда все ошибки — дубликаты', () => {
    const duplicate = { line: 57, first_line: 50, code: 'duplicate_id', external_id: '305898293' }

    expect(canSkipDuplicateRows(preview([duplicate]))).toBe(true)
    expect(canSkipDuplicateRows(preview([duplicate, { line: 60, code: 'invalid_datetime' }]))).toBe(false)
    expect(canSkipDuplicateRows(preview([]))).toBe(false)
  })

  it('показывает обе строки и повторяющийся ID', () => {
    expect(
      importIssueText({
        line: 57,
        first_line: 50,
        code: 'duplicate_id',
        external_id: '305898293',
      }),
    ).toBe('Строка 57: ID 305898293 уже встречался в строке 50.')
  })
})
