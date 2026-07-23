const LEGACY_AGENT_SESSION_STORAGE_KEY = "trade-compass-agent-session-id";
export const AGENT_SESSION_STORAGE_KEY = "trade-compass-session-id";

function normalizeStoredSessionId(value: string | null): string | null {
  if (!value) return null;
  const trimmed = value.trim();
  return trimmed || null;
}

export function getStoredSessionId(): string | null {
  try {
    const current = normalizeStoredSessionId(
      localStorage.getItem(AGENT_SESSION_STORAGE_KEY),
    );
    if (current) return current;
    const legacy = normalizeStoredSessionId(
      localStorage.getItem(LEGACY_AGENT_SESSION_STORAGE_KEY),
    );
    if (legacy) {
      localStorage.setItem(AGENT_SESSION_STORAGE_KEY, legacy);
      localStorage.removeItem(LEGACY_AGENT_SESSION_STORAGE_KEY);
      return legacy;
    }
    return null;
  } catch {
    return null;
  }
}

export function setStoredSessionId(sessionId: string): void {
  try {
    localStorage.setItem(AGENT_SESSION_STORAGE_KEY, sessionId);
    localStorage.removeItem(LEGACY_AGENT_SESSION_STORAGE_KEY);
  } catch {
    // ignore quota / private mode
  }
}

export function clearStoredSessionId(): void {
  try {
    localStorage.removeItem(AGENT_SESSION_STORAGE_KEY);
    localStorage.removeItem(LEGACY_AGENT_SESSION_STORAGE_KEY);
  } catch {
    // ignore
  }
}
