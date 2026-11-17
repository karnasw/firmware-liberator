// The dark and light themes (src/layouts/Page.astro): loaded in each page's
// head, after both stylesheets, so the reader's choice is on before the page
// is drawn; the button is wired once the page is there. A file of its own,
// not inline, so the site's Content-Security-Policy (public/_headers) allows
// scripts from the site alone.
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  document.getElementById("dark").disabled = theme === "light";
  document.getElementById("light").disabled = theme !== "light";
}

var theme = "dark";
try { theme = localStorage.getItem("theme") || theme; } catch (e) {}
setTheme(theme);

document.addEventListener("DOMContentLoaded", function () {
  var button = document.getElementById("theme");
  function label() {
    button.textContent = document.documentElement.dataset.theme === "light" ? "☾︎" : "☀︎";
  }
  button.addEventListener("click", function () {
    var chosen = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    setTheme(chosen);
    label();
    try { localStorage.setItem("theme", chosen); } catch (e) {}
  });
  label();
});
