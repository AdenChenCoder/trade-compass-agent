export const NEW_CONTENT_WINDOW_MS = 24 * 60 * 60 * 1000;
const CLOCK_SKEW_TOLERANCE_MS = 5 * 60 * 1000;

export function isNewContent(
  createdAt: string | null | undefined,
  now = Date.now(),
): boolean {
  if (!createdAt) return false;
  const createdAtMs = Date.parse(createdAt);
  if (!Number.isFinite(createdAtMs)) return false;
  const ageMs = now - createdAtMs;
  return ageMs >= -CLOCK_SKEW_TOLERANCE_MS && ageMs < NEW_CONTENT_WINDOW_MS;
}
