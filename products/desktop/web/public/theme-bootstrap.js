// Applied before first paint so a dark theme never flashes light. Kept as an
// external file because the app's CSP allows only file-based scripts.
(function () {
  try {
    var stored = localStorage.getItem("covalent-theme");
    var dark =
      stored === "dark" ||
      (stored !== "light" &&
        window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.classList.toggle("dark", dark);
  } catch (error) {
    /* Fall back to the light theme. */
  }
})();
