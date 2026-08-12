// Auto-label table cells for mobile card layout — reads each table's own header
// row so no template needs per-page markup. Tables whose first row has no <th>
// (e.g. nested breakdown tables) are left as plain scrollable tables.
(function () {
  function labelTables() {
    document.querySelectorAll("table").forEach(function (table) {
      if (table.dataset.labeled) return;
      var headerRow = table.rows[0];
      if (!headerRow || !headerRow.querySelector("th")) return;
      var labels = Array.from(headerRow.cells).map(function (c) {
        return c.textContent.trim();
      });
      table.classList.add("responsive-cards");
      for (var i = 1; i < table.rows.length; i++) {
        var row = table.rows[i];
        Array.from(row.cells).forEach(function (cell, idx) {
          if (labels[idx]) cell.setAttribute("data-label", labels[idx]);
        });
      }
      table.dataset.labeled = "1";
    });
  }
  document.addEventListener("DOMContentLoaded", labelTables);

  // Printing a P&L with collapsed drill-downs loses the transactions behind each
  // category. Expand everything for the print, then restore the screen state.
  var reopened = [];
  window.addEventListener("beforeprint", function () {
    reopened = [];
    document.querySelectorAll("details:not([open])").forEach(function (d) {
      d.open = true;
      reopened.push(d);
    });
  });
  window.addEventListener("afterprint", function () {
    reopened.forEach(function (d) { d.open = false; });
    reopened = [];
  });
})();
