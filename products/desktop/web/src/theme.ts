export type Theme = "light" | "dark" | "system";

const STORAGE_KEY = "covalent-theme";
const THEME_ORDER: Theme[] = ["light", "dark", "system"];

export function storedTheme(): Theme {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    if (value === "light" || value === "dark") return value;
  } catch {
    /* localStorage can be unavailable; follow the system. */
  }
  return "system";
}

export function resolveTheme(theme: Theme): "light" | "dark" {
  if (theme !== "system") return theme;
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle(
    "dark",
    resolveTheme(theme) === "dark",
  );
}

export function saveTheme(theme: Theme): void {
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* The class is still applied for this session. */
  }
  applyTheme(theme);
}

export function nextTheme(theme: Theme): Theme {
  return THEME_ORDER[(THEME_ORDER.indexOf(theme) + 1) % THEME_ORDER.length];
}

// While following the system, an OS-level switch has to re-resolve the theme.
export function watchSystemTheme(theme: Theme): () => void {
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const handler = () => {
    if (theme === "system") applyTheme("system");
  };
  media.addEventListener("change", handler);
  return () => media.removeEventListener("change", handler);
}
