export const APPEARANCE_KEY = "trade-compass.appearance.v1";

export const appearances = [
  { id: "ember", name: "朱砂橙", palette: "暖白 × 朱砂橙", canvas: "#faf7f1" },
  { id: "glacier", name: "冰川青", palette: "深墨 × 冰川青", canvas: "#111f24" },
  { id: "berry", name: "莓果红", palette: "雾白 × 莓果红", canvas: "#faf3f5" },
  { id: "volt", name: "电光蓝", palette: "银灰 × 电光蓝", canvas: "#f1f3f5" },
] as const;

export type Appearance = "classic" | typeof appearances[number]["id"];
const listeners = new Set<() => void>();
let current: Appearance = "classic";

export function parseAppearance(value: string | null): Appearance {
  return appearances.some(theme => theme.id === value) ? value as Appearance : "classic";
}

export function getAppearance(): Appearance { return current; }
export function subscribeAppearance(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function applyAppearance(value: Appearance) {
  current = value;
  document.documentElement.dataset.theme = value;
  document.documentElement.classList.toggle("dark", value === "glacier");
  document.querySelector('meta[name="theme-color"]')?.setAttribute(
    "content", appearances.find(theme => theme.id === value)?.canvas ?? "#f5f6fa",
  );
  listeners.forEach(listener => listener());
}

// Called before the first React render, including the mobile startup screen.
// The preference is local to this origin, independent of accounts and pairing.
export function initializeAppearance() {
  let saved: string | null = null;
  try { saved = localStorage.getItem(APPEARANCE_KEY); } catch { /* Private/storage-disabled browsing. */ }
  applyAppearance(parseAppearance(saved));
  const onStorage = (event: StorageEvent) => {
    if (event.key !== APPEARANCE_KEY && event.key !== null) return;
    if (event.storageArea && event.storageArea !== localStorage) return;
    applyAppearance(parseAppearance(event.newValue));
  };
  window.addEventListener("storage", onStorage);
  return () => window.removeEventListener("storage", onStorage);
}

// A failed write must not prevent the current page from applying the choice.
export function setAppearance(value: Appearance): boolean {
  const next = parseAppearance(value);
  applyAppearance(next);
  try {
    localStorage.setItem(APPEARANCE_KEY, next);
    return true;
  } catch { return false; }
}
