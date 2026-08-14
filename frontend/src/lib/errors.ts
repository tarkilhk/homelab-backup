export function getErrorMessage(error: unknown, fallback = 'Request failed'): string {
  return error instanceof Error && error.message ? error.message : fallback
}

export function getErrorStatus(error: unknown): number | undefined {
  if (typeof error !== 'object' || error === null || !('status' in error)) return undefined
  return typeof error.status === 'number' ? error.status : undefined
}
