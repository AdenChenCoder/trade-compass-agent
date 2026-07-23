/**
 * Pure helpers for when AgentPage should call GET /sessions/{id}.
 * Session ids are persisted only after the backend assigns one (first turn
 * or explicit sidebar selection), never for a blank "新对话" on mount.
 */

export function shouldFetchStoredSessionOnMount(
  storedSessionId: string | null | undefined,
): storedSessionId is string {
  if (storedSessionId == null) return false;
  return storedSessionId.trim().length > 0;
}

export function prependUniqueMessages<T extends { id: string }>(
  current: T[],
  older: T[],
): T[] {
  const existing = new Set(current.map((message) => message.id));
  return [...older.filter((message) => !existing.has(message.id)), ...current];
}

export function isNearMessageListBottom(
  scrollHeight: number,
  scrollTop: number,
  clientHeight: number,
  threshold = 120,
): boolean {
  return scrollHeight - scrollTop - clientHeight <= threshold;
}
