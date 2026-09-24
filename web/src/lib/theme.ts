export type Theme = "dark" | "light" | "system";

const KEY = "jarvis-theme";

export function storedTheme(): Theme {
  try {
    const value = localStorage.getItem(KEY);
    return value === "light" || value === "dark" || value === "system" ? value : "dark";
  } catch {
    return "dark";
  }
}

export function applyTheme(theme: Theme): void {
  const dark =
    theme === "dark" ||
    (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", dark ? "#0b0e14" : "#f7f8fa");
}

export function saveTheme(theme: Theme): void {
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    // private mode: the theme just won't persist
  }
  applyTheme(theme);
}
