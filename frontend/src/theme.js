// Light / dark / follow-the-system theme, remembered per browser.
export const NEXT_THEME = { system: "light", light: "dark", dark: "system" };

export function storedTheme() {
  try {
    return window.localStorage.getItem("iv-theme");
  } catch {
    return null; // storage can be blocked (private mode, strict settings)
  }
}

export function saveTheme(theme) {
  try {
    window.localStorage.setItem("iv-theme", theme);
  } catch {
    // ignore: the choice just will not be remembered
  }
}

export function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}
