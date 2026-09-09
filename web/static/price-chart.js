/* Lokaler, responsiver SVG-Renderer für gespeicherte Kursdaten. */
(function () {
  "use strict";
  function init() {
    const chart = document.querySelector("[data-price-chart]");
    if (!chart) return;
    const source = document.getElementById(chart.dataset.pointsId);
    if (!source) return;
    let points;
    try { points = JSON.parse(source.textContent || "[]"); } catch (_) { points = []; }
    if (!Array.isArray(points) || points.length < 2) return;
    const svg = chart.querySelector("svg"), line = chart.querySelector("[data-chart-line]"), status = chart.querySelector("[data-chart-status], [data-chart-labels]");
    if (!svg || !line) return;
    const purchaseLine = chart.querySelector("[data-purchase-line]");
    const currency = chart.dataset.currency || ((document.querySelector(".title-line .subtitle") || {}).textContent || "").split("·").pop().trim();
    const width = 900, height = 320, pad = { left: 94, right: 30, top: 20, bottom: 58 };
    const plotW = width - pad.left - pad.right, plotH = height - pad.top - pad.bottom;
    const ns = "http://www.w3.org/2000/svg";
    function text(x, y, value, anchor) {
      const node = document.createElementNS(ns, "text");
      node.setAttribute("x", x); node.setAttribute("y", y); node.setAttribute("text-anchor", anchor || "middle"); node.textContent = value; return node;
    }
    function element(name, attrs) {
      const node = document.createElementNS(ns, name);
      Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value)); return node;
    }
    function formatDate(value, range) {
      const date = new Date(value + "T00:00:00Z");
      const options = range <= 31 ? { day: "numeric", month: "short" } : range <= 365 ? { month: "short", year: "2-digit" } : { month: "short", year: "numeric" };
      return new Intl.DateTimeFormat("de-DE", options).format(date);
    }
    function formatPrice(value) { return Number(value).toLocaleString("de-DE", { maximumFractionDigits: 2 }) + (currency ? " " + currency : ""); }
    function draw(range) {
      const cutoff = range === "Max" ? null : new Date(Date.now() - range * 86400000);
      const visible = points.filter(point => !cutoff || new Date(point.date + "T00:00:00Z") >= cutoff);
      const values = visible.map(point => Number(point.close)).filter(Number.isFinite);
      if (values.length < 2) { if (status) status.textContent = "Für diesen Zeitraum sind noch nicht genügend Daten vorhanden."; return; }
      const purchase = Number(chart.dataset.purchasePrice), allValues = Number.isFinite(purchase) ? values.concat([purchase]) : values;
      const rawMin = Math.min(...allValues), rawMax = Math.max(...allValues), margin = (rawMax - rawMin || Math.max(rawMax * .01, 1)) * .08;
      const min = rawMin - margin, max = rawMax + margin, span = max - min || 1;
      const x = index => pad.left + (index / (visible.length - 1)) * plotW, y = value => pad.top + (1 - (value - min) / span) * plotH;
      line.setAttribute("points", visible.map((point, index) => `${x(index)},${y(Number(point.close))}`).join(" "));
      chart.querySelectorAll(".chart-generated").forEach(node => node.remove());
      const grid = element("g", { "class": "chart-generated" });
      for (let tick = 0; tick < 5; tick += 1) { const value = max - (span * tick / 4), yy = y(value); grid.appendChild(element("line", { x1: pad.left, x2: pad.left + plotW, y1: yy, y2: yy, "class": "chart-gridline" })); grid.appendChild(text(pad.left - 8, yy + 4, formatPrice(value), "end")); }
      const dateSpan = (new Date(visible[visible.length - 1].date) - new Date(visible[0].date)) / 86400000, tickCount = Math.min(6, visible.length);
      for (let tick = 0; tick < tickCount; tick += 1) { const index = Math.round(tick * (visible.length - 1) / (tickCount - 1 || 1)), xx = x(index); grid.appendChild(element("line", { x1: xx, x2: xx, y1: pad.top, y2: pad.top + plotH, "class": "chart-gridline-vertical" })); grid.appendChild(text(xx, pad.top + plotH + 22, formatDate(visible[index].date, dateSpan), "middle")); }
      svg.insertBefore(grid, line);
      if (purchaseLine && Number.isFinite(purchase)) { const purchaseY = y(purchase); purchaseLine.setAttribute("x1", pad.left); purchaseLine.setAttribute("x2", pad.left + plotW); purchaseLine.setAttribute("y1", purchaseY); purchaseLine.setAttribute("y2", purchaseY); purchaseLine.hidden = false; const label = text(pad.left + 5, Math.max(pad.top + 14, purchaseY - 5), "Kauf " + formatPrice(purchase), "start"); label.setAttribute("class", "chart-purchase-label chart-generated"); svg.appendChild(label); }
      const last = visible[visible.length - 1], lastX = x(visible.length - 1), lastY = y(Number(last.close)), labelX = Math.min(pad.left + plotW - 4, Math.max(pad.left + 4, lastX - 6)), labelBelow = lastY < pad.top + 22, lastLabel = text(labelX, labelBelow ? lastY + 17 : lastY - 8, "Aktuell " + formatPrice(last.close), labelX > pad.left + plotW - 45 ? "end" : "start"); lastLabel.setAttribute("class", "chart-current-label chart-generated"); svg.appendChild(lastLabel);
      if (status) status.textContent = visible[0].date + " – " + last.date;
      svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    }
    chart.parentElement.querySelectorAll("[data-chart-range]").forEach(button => { button.type = "button"; });
    chart.parentElement.addEventListener("click", event => { const button = event.target.closest("[data-chart-range]"); if (!button || !chart.parentElement.contains(button)) return; chart.parentElement.querySelectorAll("[data-chart-range]").forEach(item => item.classList.remove("active")); button.classList.add("active"); draw(button.dataset.chartRange === "Max" ? "Max" : Number(button.dataset.chartRange)); });
    draw("Max");
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
}());
