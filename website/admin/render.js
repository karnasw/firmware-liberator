// The admin view's HTML: every value written in through esc(), so nothing a
// report or a readout carries can be read as markup. Charts are inline SVG,
// each mark with a <title> the browser shows on hover, and every figure
// beside it in text or a table too. The palette is the /stats page's
// (website/src/pages/stats.astro), checked for colour-blind readers and
// contrast, in the theme the browser asks for.

export const esc = (value) => String(value ?? "").replace(/[&<>"']/g,
  (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));

export const number = (value) => (value === null || value === undefined ? "–" : Number(value).toLocaleString("en"));

export function bytes(value) {
  const units = ["B", "KB", "MB", "GB"];
  let size = Number(value) || 0;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit && size < 10 ? 1 : 0)} ${units[unit]}`;
}

export const tile = (label, value, note = "") =>
  `<div class="tile"><div class="tile-label">${esc(label)}</div><div class="tile-value">${esc(value)}</div>`
  + (note ? `<div class="tile-note">${esc(note)}</div>` : "") + "</div>";

// A meter: `used` of `of`, its fill a warning past `warn` of it.
export function meter(label, used, of, shown, warn = 0.75) {
  const share = of ? Math.min(1, used / of) : 0;
  return `<div class="meter-row"><div class="meter-head"><span>${esc(label)}</span><span>${esc(shown)}</span></div>`
    + `<div class="meter" role="img" aria-label="${esc(`${label}: ${shown}`)}"><span class="${share >= warn ? "fill warn" : "fill"}"`
    + ` style="width: ${(100 * share).toFixed(1)}%"></span></div></div>`;
}

export function table(columns, rows, empty = "None.") {
  if (!rows.length) {
    return `<p class="none">${esc(empty)}</p>`;
  }
  const head = columns.map(([name, , kind]) => `<th${kind === "number" ? ' class="number"' : ""}>${esc(name)}</th>`).join("");
  const body = rows.map((row) => `<tr>${columns.map(([, key, kind]) => {
    const value = typeof key === "function" ? key(row) : row[key];
    if (kind === "html") {
      return `<td>${value}</td>`;   // made here, from esc()'d parts
    }
    const kept = kind === "number" || kind === "text" ? ` class="${kind}"` : "";
    return `<td${kept}>${esc(kind === "number" ? number(value) : value)}</td>`;
  }).join("")}</tr>`).join("");
  return `<div class="scroll"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

// Charts are drawn at about the width they are shown at, `width` (FULL in a
// card across the page, HALF in one of two), so their text and marks come out
// at the sizes meant, not stretched or shrunk with the card.
export const FULL = 1040;
export const HALF = 480;

// A line over days, one series: `points` [{day, value}], a dashed `limit`
// it is held under, if any, and how a value is said.
export function lineChart(points, { limit = null, say = number, label = "", width = HALF } = {}) {
  if (points.length < 2) {
    return '<p class="none">Not enough days yet.</p>';
  }
  const [W, H, left, bottom, top] = [width, 200, 64, 26, 14];
  const most = Math.max(limit ?? 0, ...points.map((point) => point.value)) || 1;
  const x = (index) => left + ((W - left - 8) * index) / (points.length - 1);
  const y = (value) => top + (H - top - bottom) * (1 - value / most);
  const path = points.map((point, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(point.value).toFixed(1)}`).join(" ");
  const last = points.at(-1);
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">`
    + [0, most / 2, most].map((tick) => `<line class="grid" x1="${left}" x2="${W}" y1="${y(tick)}" y2="${y(tick)}"/>`
      + `<text class="tick" x="${left - 8}" y="${y(tick) + 4}" text-anchor="end">${esc(say(tick))}</text>`).join("")
    + (limit ? `<line class="limit" x1="${left}" x2="${W}" y1="${y(limit)}" y2="${y(limit)}"/>`
      + `<text class="tick" x="${W}" y="${y(limit) - 6}" text-anchor="end">limit ${esc(say(limit))}</text>` : "")
    + `<path class="line" d="${path}"/>`
    + points.map((point, index) => `<circle class="hit" cx="${x(index)}" cy="${y(point.value)}" r="10"><title>${esc(`${point.day}: ${say(point.value)}`)}</title></circle>`).join("")
    + `<circle class="dot" cx="${x(points.length - 1)}" cy="${y(last.value)}" r="4"/>`
    + `<text class="tick" x="${left}" y="${H - 6}">${esc(points[0].day)}</text>`
    + `<text class="tick" x="${W}" y="${H - 6}" text-anchor="end">${esc(last.day)}</text></svg>`;
}

// Lines over days on one scale, one a series: `days` [{day, values: {name: n}}],
// `names` in the legend's order, each in its series' colour.
export function lines(days, names, label = "", width = FULL) {
  if (days.length < 2) {
    return '<p class="none">Not enough days yet.</p>';
  }
  const [W, H, left, bottom, top] = [width, 220, 64, 26, 14];
  const most = Math.max(1, ...days.flatMap((day) => names.map((name) => day.values[name] ?? 0)));
  const x = (index) => left + ((W - left - 8) * index) / (days.length - 1);
  const y = (value) => top + (H - top - bottom) * (1 - value / most);
  return `<ul class="legend">${names.map((name, at) => `<li><span class="swatch series-${at + 1}"></span>${esc(name)}</li>`).join("")}</ul>`
    + `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">`
    + [0, most / 2, most].map((tick) => `<line class="grid" x1="${left}" x2="${W}" y1="${y(tick)}" y2="${y(tick)}"/>`
      + `<text class="tick" x="${left - 8}" y="${y(tick) + 4}" text-anchor="end">${esc(number(Math.round(tick)))}</text>`).join("")
    + names.map((name, at) => {
      const values = days.map((day) => day.values[name] ?? 0);
      return `<path class="line series-${at + 1}" d="${values.map((value, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(" ")}"/>`
        + values.map((value, index) => `<circle class="hit" cx="${x(index).toFixed(1)}" cy="${y(value).toFixed(1)}" r="6">`
          + `<title>${esc(`${days[index].day}: ${number(value)} ${name}`)}</title></circle>`).join("")
        + `<circle class="dot series-${at + 1}" cx="${x(days.length - 1)}" cy="${y(values.at(-1))}" r="4"/>`;
    }).join("")
    + `<text class="tick" x="${left}" y="${H - 6}">${esc(days[0].day)}</text>`
    + `<text class="tick" x="${W}" y="${H - 6}" text-anchor="end">${esc(days.at(-1).day)}</text></svg>`;
}

// Stacked columns a day: `days` [{day, parts: {name: n}}], `names` in stack
// order, each drawn in its series' colour, or in the class `kinds` gives it.
export function columns(days, names, label = "", width = HALF, kinds = names.map((_, at) => `series-${at + 1}`)) {
  if (!days.length) {
    return '<p class="none">Nothing yet.</p>';
  }
  const [W, H, left, bottom, top] = [width, 180, 40, 24, 16];
  const total = (day) => names.reduce((sum, name) => sum + (day.parts[name] ?? 0), 0);
  const most = Math.max(1, ...days.map(total));
  const band = (W - left) / days.length;
  const bar = Math.min(24, band * 0.6);
  const y = (value) => top + (H - top - bottom) * (1 - value / most);
  const marks = days.map((day, index) => {
    let base = 0;
    const x = left + band * index + (band - bar) / 2;
    return names.map((name, at) => {
      const value = day.parts[name] ?? 0;
      if (!value) return "";
      const from = y(base);
      base += value;
      const height = Math.max(1, from - y(base) - (base > value ? 2 : 0));
      return `<rect class="mark ${kinds[at]}" x="${x}" y="${y(base)}" width="${bar}" height="${height}" rx="2">`
        + `<title>${esc(`${day.day}: ${number(value)} ${name}`)}</title></rect>`;
    }).join("");
  }).join("");
  return `<ul class="legend">${names.map((name, at) => `<li><span class="swatch ${kinds[at]}"></span>${esc(name)}</li>`).join("")}</ul>`
    + `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">`
    + `<line class="grid" x1="${left}" x2="${W}" y1="${y(0)}" y2="${y(0)}"/>`
    + `<text class="tick" x="${left - 6}" y="${y(most) + 4}" text-anchor="end">${esc(number(most))}</text>`
    + marks
    + `<text class="tick" x="${left}" y="${H - 6}">${esc(days[0].day)}</text>`
    + `<text class="tick" x="${W}" y="${H - 6}" text-anchor="end">${esc(days.at(-1).day)}</text></svg>`;
}

// One small chart a model, its series as lines over `months` (["2026-11", ...]),
// all on one scale - the most any model's series reached - so the charts read
// against each other: `models` [{model, series: {name: {month: n}}}], `names`
// in the legend's order.
export function trends(models, months, names) {
  if (!models.length || months.length < 2) {
    return '<p class="none">Nothing yet.</p>';
  }
  const [W, H, left, bottom, top] = [320, 120, 34, 22, 12];
  const most = Math.max(1, ...models.flatMap(({ series }) => names.flatMap((name) => Object.values(series[name] ?? {}))));
  const x = (index) => left + ((W - left - 6) * index) / (months.length - 1);
  const y = (value) => top + (H - top - bottom) * (1 - value / most);
  const chart = ({ model, series }) => `<figure class="trend"><figcaption>${esc(model)}</figcaption>`
    + `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(model)}, month by month">`
    + `<line class="grid" x1="${left}" x2="${W}" y1="${y(0)}" y2="${y(0)}"/>`
    + `<text class="tick" x="${left - 6}" y="${y(most) + 4}" text-anchor="end">${esc(number(most))}</text>`
    + names.map((name, at) => {
      const values = months.map((month) => series[name]?.[month] ?? 0);
      return `<path class="line series-${at + 1}" d="${values.map((value, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(" ")}"/>`
        + values.map((value, index) => (value ? `<circle class="hit" cx="${x(index).toFixed(1)}" cy="${y(value).toFixed(1)}" r="6">`
          + `<title>${esc(`${model}, ${months[index]}: ${number(value)} ${name}`)}</title></circle>` : "")).join("");
    }).join("")
    + `<text class="tick" x="${left}" y="${H - 6}">${esc(months[0])}</text>`
    + `<text class="tick" x="${W}" y="${H - 6}" text-anchor="end">${esc(months.at(-1))}</text></svg></figure>`;
  return `<ul class="legend">${names.map((name, at) => `<li><span class="swatch series-${at + 1}"></span>${esc(name)}</li>`).join("")}</ul>`
    + `<div class="trends">${models.map(chart).join("")}</div>`;
}

// A table whose cells are shaded by a share, 0 to 1: `rows` and `cols` its
// labels, `cell(row, col)` {share, text} or null for none, `corner` the first
// header. The share is said in each cell's text too, so the shade only helps.
export function heat(rows, cols, cell, corner = "", empty = "None.") {
  if (!rows.length || !cols.length) {
    return `<p class="none">${esc(empty)}</p>`;
  }
  const td = (row, col) => {
    const value = cell(row, col);
    if (!value) {
      return "<td></td>";
    }
    const share = Math.max(0, Math.min(1, value.share));
    // A failure at all shows: the lightest shade is a tenth.
    const shade = share ? Math.round(10 + 70 * share) : 0;
    return `<td class="number heat" style="background: color-mix(in srgb, var(--critical) ${shade}%, transparent)">`
      + `${esc(value.text)}</td>`;
  };
  return `<div class="scroll"><table><thead><tr><th>${esc(corner)}</th>`
    + cols.map((col) => `<th class="number">${esc(col)}</th>`).join("") + "</tr></thead><tbody>"
    + rows.map((row) => `<tr><th>${esc(row)}</th>${cols.map((col) => td(row, col)).join("")}</tr>`).join("")
    + "</tbody></table></div>";
}

export const STYLE = `
:root { color-scheme: dark;
  --plane: #0d1117; --surface: #161b22; --ink: #e6edf3; --ink-2: #9198a1; --muted: #7d8590; --grid: #262c36;
  --ring: rgba(255,255,255,0.10); --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
  --warning: #fab219; --critical: #d03b3b; --link: #4493f8; }
@media (prefers-color-scheme: light) { :root { color-scheme: light;
  --plane: #ffffff; --surface: #f6f8fa; --ink: #1f2328; --ink-2: #59636e; --muted: #6e7781; --grid: #e1e4e8;
  --ring: rgba(31,35,40,0.10); --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --link: #0969da; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--plane); color: var(--ink); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1120px; margin: 0 auto; padding: 28px 20px 60px; }
a { color: var(--link); }
h1 { font-size: 26px; margin: 0; } h2 { font-size: 17px; margin: 0 0 4px; } h3 { font-size: 15px; margin: 18px 0 4px; }
.top { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; flex-wrap: wrap; }
.who { color: var(--muted); font-size: 13px; }
.sub { color: var(--ink-2); font-size: 13px; margin: 0 0 14px; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 18px 20px; margin: 16px 0; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; } .grid2 .card { margin: 0; }
@media (max-width: 800px) { .grid2 { grid-template-columns: 1fr; } }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 16px; }
.tile-label { color: var(--ink-2); font-size: 13px; } .tile-value { font-size: 26px; font-weight: 600; }
.tile-note { color: var(--muted); font-size: 12px; }
.meter-row { margin: 10px 0; } .meter-head { display: flex; justify-content: space-between; font-size: 13px; color: var(--ink-2); }
.meter { height: 8px; border-radius: 4px; background: var(--grid); margin-top: 4px; overflow: hidden; }
.meter .fill { display: block; height: 100%; background: var(--series-1); border-radius: 4px; }
.meter .fill.warn { background: var(--warning); }
svg { width: 100%; height: auto; display: block; overflow: visible; }
.grid { stroke: var(--grid); stroke-width: 1; } .tick { fill: var(--muted); font-size: 11px; }
.limit { stroke: var(--critical); stroke-width: 1; stroke-dasharray: 4 4; }
.line { fill: none; stroke: var(--series-1); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.dot { fill: var(--series-1); stroke: var(--surface); stroke-width: 2; } .hit { fill: transparent; }
.hit:hover { fill: var(--ring); }
.mark.series-1, .swatch.series-1 { fill: var(--series-1); background: var(--series-1); }
.mark.series-2, .swatch.series-2 { fill: var(--series-2); background: var(--series-2); }
.mark.series-3, .swatch.series-3 { fill: var(--series-3); background: var(--series-3); }
.mark.muted, .swatch.muted { fill: var(--muted); background: var(--muted); }
.mark.critical, .swatch.critical { fill: var(--critical); background: var(--critical); }
.line.series-2 { stroke: var(--series-2); } .line.series-3 { stroke: var(--series-3); }
.dot.series-2 { fill: var(--series-2); } .dot.series-3 { fill: var(--series-3); }
.trends { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px 20px; }
.trend { margin: 0; } .trend figcaption { font-size: 13px; font-weight: 600; margin-bottom: 2px; }
.mark:hover { filter: brightness(1.15); }
.legend { display: flex; gap: 14px; list-style: none; padding: 0; margin: 0 0 8px; font-size: 13px; color: var(--ink-2); }
.legend li { display: flex; align-items: center; gap: 6px; } .swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
.scroll { overflow-x: auto; } table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--ink-2); font-weight: 500; } .number { text-align: right; font-variant-numeric: tabular-nums; }
td.text { max-width: 360px; overflow-wrap: anywhere; color: var(--ink-2); }
.none { color: var(--muted); } td.heat { white-space: nowrap; }
select { font: inherit; font-size: 12px; background: var(--plane); color: var(--ink); border: 1px solid var(--ring); border-radius: 6px; }
form.inline { display: inline; }
button { font: inherit; font-size: 12px; padding: 3px 10px; border-radius: 6px; border: 1px solid var(--ring);
  background: var(--plane); color: var(--ink); cursor: pointer; } button.danger { color: var(--critical); }
.pill { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 10px; border: 1px solid var(--ring); color: var(--ink-2); }
`;
