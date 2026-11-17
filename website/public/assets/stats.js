// The /stats page's tooltips (src/pages/stats.astro): what a mark's data-tip
// says, beside the pointer or the focused mark. Every figure is on the page
// without it: a label, a value at a bar's tip, or a table.
(function () {
  var tip = document.querySelector(".stats .tip");
  if (!tip) return;
  function show(mark, x, y) {
    tip.textContent = mark.getAttribute("data-tip");
    tip.hidden = false;
    var width = tip.offsetWidth, height = tip.offsetHeight;
    var left = Math.min(Math.max(8, x - width / 2), window.innerWidth - width - 8);
    var top = y - height - 12 < 8 ? y + 18 : y - height - 12;
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }
  function hide() { tip.hidden = true; }
  document.querySelectorAll(".stats [data-tip]").forEach(function (mark) {
    mark.addEventListener("pointermove", function (event) { show(mark, event.clientX, event.clientY); });
    mark.addEventListener("pointerleave", hide);
    mark.addEventListener("focus", function () {
      var box = mark.getBoundingClientRect();
      show(mark, box.left + box.width / 2, box.top);
    });
    mark.addEventListener("blur", hide);
  });
  window.addEventListener("scroll", hide, { passive: true });
})();
