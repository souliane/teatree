// Stamp a compare-page cell with the outcome of its own write.
//
// The write endpoint is the settings grid's `settings/set/<key>/`, and it answers with the
// GRID's row — whose columns are not the compare table's. So the control discards the response
// (`hx-swap="none"`) and the outcome is stamped from the request instead. Without this a saved
// value and a refused one look identical, and a config value nobody watched land is not a change
// anyone can rely on.
(function () {
  "use strict";

  var REFUSAL_SELECTOR = ".banner" + "-red";
  var MAX_REASON = 160;

  function refusalReason(xhr) {
    if (!xhr || !xhr.responseText) {
      return "refused";
    }
    var parsed = new DOMParser().parseFromString(xhr.responseText, "text/html");
    var banner = parsed.querySelector(REFUSAL_SELECTOR);
    return banner ? banner.textContent.trim() : xhr.responseText.trim().slice(0, MAX_REASON);
  }

  document.addEventListener("htmx:afterRequest", function (event) {
    var control = event.target;
    if (!control || !control.closest) {
      return;
    }
    var cell = control.closest(".compare-cell");
    var slot = cell && cell.querySelector(".cell-status");
    if (!slot) {
      return;
    }
    if (event.detail.successful) {
      slot.dataset.state = "saved";
      slot.textContent = "✓ saved";
      return;
    }
    slot.dataset.state = "refused";
    slot.textContent = "✕ " + refusalReason(event.detail.xhr);
  });
})();
