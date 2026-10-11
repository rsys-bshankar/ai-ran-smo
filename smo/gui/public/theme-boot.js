/* Applies the cached console preferences (theme, accent, text size, motion, language) to <html> before the app's script runs, so the first paint is already
   in the user's theme (no flash of the wrong one). A plain file and not an inline script: the console's Content-Security-Policy allows scripts from
   'self' only (security-headers.conf). The real copy comes from the GUI BFF and is re-applied by src/shell/ThemeProvider.tsx; this file only reads
   the browser copy, and only accepts values from the closed sets of src/data/preferences.ts. */
(function () {
  var root = document.documentElement, p = {};
  try { p = JSON.parse(window.localStorage.getItem("smo.prefs") || "{}") || {}; } catch (e) { p = {}; }
  var theme = p.theme === "light" || p.theme === "dark" ? p.theme : "dark";
  if (p.theme === "system" && window.matchMedia) theme = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  root.setAttribute("data-theme", theme);
  root.setAttribute("data-accent", ["volt", "blue", "teal", "amber", "radisys"].indexOf(p.accent) >= 0 ? p.accent : "volt");
  root.setAttribute("data-size", ["s", "m", "l", "xl"].indexOf(p.size) >= 0 ? p.size : "m");
  if (p.reduceMotion === true) root.setAttribute("data-motion", "reduce");
  root.setAttribute("lang", p.language === "de" ? "de" : "en");
})();
