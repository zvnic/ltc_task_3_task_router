import type { ImportIssue, ImportPreview } from '../../shared/api'

export function canSkipDuplicateRows(preview: ImportPreview | null): boolean {
  return Boolean(
    preview
      && preview.errors.length > 0
      && preview.errors.every((issue) => issue.code === 'duplicate_id'),
  )
}

export function importIssueText(issue: ImportIssue): string {
  if (issue.code === 'duplicate_id') {
    const repeated = issue.external_id ? `ID ${issue.external_id}` : 'ID заявки'
    const origin = issue.first_line ? ` уже встречался в строке ${issue.first_line}` : ' уже встречался выше'
    return `Строка ${issue.line}: ${repeated}${origin}.`
  }
  if (issue.code === 'invalid_datetime') {
    return `Строка ${issue.line}: некорректная дата или время${issue.value ? ` «${issue.value}»` : ''}.`
  }
  if (issue.code === 'missing_id') return `Строка ${issue.line}: не указан ID заявки.`
  if (issue.code === 'window_reversed') {
    return `Строка ${issue.line}: начало временного окна позже окончания.`
  }
  if (issue.code === 'invalid_gigabit') {
    return `Строка ${issue.line}: в поле гигабитного подключения ожидается «Да» или «Нет».`
  }
  return `Строка ${issue.line}: ошибка ${issue.code}.`
}
