"use strict";

const ZONES = ["SE1", "SE2", "SE3", "SE4"];
const ZONE_NAMES = { SE1: "Luleå", SE2: "Sundsvall", SE3: "Stockholm", SE4: "Malmö" };
const TZ = "Europe/Stockholm";
const SVG_NS = "http://www.w3.org/2000/svg";
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const pocketQuery = window.matchMedia("(max-width: 720px)");
const stackQuery = window.matchMedia("(max-width: 1100px)");

const state = { data: null, zone: "SE3" };

/* Formatting */

const price = (v) => (v == null ? "n/a" : v.toFixed(2));
const err = (v) => (v == null ? "n/a" : v.toFixed(3));
const pct = (v, digits = 0) => `${(v * 100).toFixed(digits)}%`;
const sek = (v) => `${Math.round(v).toLocaleString("en-GB")} SEK`;
const count = (v) => v.toLocaleString("en-GB");

function dateFrom(isoDate) {
  // "2026-10-04" is a Stockholm calendar day; noon UTC keeps it on that day in any zone.
  return new Date(`${isoDate.slice(0, 10)}T12:00:00Z`);
}
const fmtDay = (d, opts) => new Intl.DateTimeFormat("en-GB", { timeZone: TZ, ...opts }).format(d);
const longDay = (iso) => fmtDay(dateFrom(iso), { weekday: "long", day: "numeric", month: "long" });
const shortDay = (iso) => fmtDay(dateFrom(iso), { weekday: "short", day: "numeric", month: "short" });
const clock = (ts) => fmtDay(new Date(ts), { hour: "2-digit", minute: "2-digit", hour12: false });
const monthLabel = (m) => fmtDay(dateFrom(`${m}-15`), { month: "short", year: "numeric" });
const hourLabel = (h) => String(h).padStart(2, "0");

/* Small DOM helpers */

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "style") node.setAttribute("style", v);
    else node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) {
    if (child == null) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function svg(tag, attrs = {}, parent) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.append(node);
  return node;
}

function setText(id, text) {
  document.getElementById(id).textContent = text;
}

/* Tooltip */

function makeTooltip(container) {
  const tip = el("div", { class: "tooltip", role: "status" });
  container.append(tip);
  return {
    show(title, rows, x, y) {
      tip.replaceChildren(el("div", { class: "t-title", text: title }));
      for (const row of rows) {
        const label = el("span", {}, [row.key ? el("i", { class: `key ${row.key}` }) : null, row.label]);
        tip.append(el("div", { class: "t-row" }, [label, el("strong", { text: row.value })]));
      }
      const box = container.getBoundingClientRect();
      const w = tip.offsetWidth || 180;
      const h = tip.offsetHeight || 80;
      const left = Math.min(Math.max(x + 14, 0), box.width - w);
      const top = Math.max(y - h - 12, 0);
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
      tip.classList.add("on");
    },
    hide() { tip.classList.remove("on"); },
  };
}

/* Scales */

function linear(d0, d1, r0, r1) {
  const k = (r1 - r0) / (d1 - d0 || 1);
  return (v) => r0 + (v - d0) * k;
}

function niceMax(v) {
  const step = v > 2 ? 1 : v > 1 ? 0.5 : v > 0.4 ? 0.1 : 0.05;
  return Math.ceil(v / step) * step;
}

function ticks(lo, hi, n = 4) {
  const raw = (hi - lo) / n;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}

/* Header */

function renderFreshness(d) {
  const node = document.getElementById("freshness");
  const parts = [el("span", {}, ["Prices through ", el("strong", { text: shortDay(d.pipeline.last_day) })])];
  if (d.tomorrow) {
    parts.push(el("br"));
    parts.push(el("span", {}, ["Forecast for ", el("strong", { text: shortDay(d.tomorrow.date) })]));
  }
  node.replaceChildren(...parts);
  setText("generated", `Data generated ${fmtDay(new Date(d.generated_at), {
    weekday: "short", day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false,
  })} Stockholm time.`);
}

/* Accuracy */

const MODEL_NAMES = {
  gbm: "Elmorgon",
  gbm_no_weather: "Elmorgon without weather",
  naive_day: "Same hour, day before",
  naive_week: "Same hour, week before",
};

function renderClaim(d) {
  const bt = d.backtest;
  const ours = bt.overall.gbm;
  const base = bt.overall.naive_day;
  setText("claim", `Elmorgon's forecasts miss by ${pct(ours.skill_vs_naive_day)} less than repeating the day before.`);
  setText("fig-skill", pct(ours.skill_vs_naive_day));
  setText("fig-days", count(bt.period.days));
  setText("fig-coverage", pct(ours.coverage_80));
  const from = fmtDay(dateFrom(bt.period.first_day), { month: "long", year: "numeric" });
  const to = fmtDay(dateFrom(bt.period.last_day), { month: "long", year: "numeric" });
  setText("claim-support",
    `Average error ${err(ours.mae)} SEK/kWh against ${err(base.mae)}, over ${count(bt.period.days)} days ` +
    `from ${from} to ${to}, every hour in all four zones. Retrained monthly, and only ever tested on days it had not seen.`);
}

function renderRanked(d) {
  const table = document.getElementById("ranked");
  const rows = Object.entries(d.backtest.overall).sort((a, b) => a[1].mae - b[1].mae);
  const max = Math.max(...rows.map(([, m]) => m.mae));
  const head = el("thead", {}, el("tr", {}, [
    el("th", { scope: "col", text: "Model" }),
    el("th", { scope: "col", class: "val", text: "SEK/kWh" }),
    el("th", { scope: "col" }, el("span", { class: "visually-hidden", text: "Error drawn to scale" })),
  ]));
  const body = el("tbody");
  for (const [key, m] of rows) {
    const bar = el("div", { class: "meter" }, el("span", { style: `width:${(m.mae / max) * 100}%` }));
    body.append(el("tr", { class: key === "gbm" ? "is-us" : "" }, [
      el("td", { text: MODEL_NAMES[key] || key }),
      el("td", { class: "val num", text: err(m.mae) }),
      el("td", { class: "bar" }, bar),
    ]));
  }
  table.append(head, body);
  const ours = d.backtest.overall.gbm;
  const before = ours.coverage_80_uncalibrated;
  setText("ranked-note",
    `Elmorgon's 80% range contained the actual price ${pct(ours.coverage_80, 1)} of the time` +
    (before ? ` (${pct(before, 1)} before calibration).` : "."));
}

function renderMonthly(d) {
  const months = d.backtest.monthly_mae;
  const host = document.getElementById("monthly-chart");
  host.replaceChildren();
  const W = Math.max(300, host.clientWidth || 1180), H = W < 600 ? 220 : 160, M = { t: 16, r: 16, b: 30, l: 44 };
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, "aria-hidden": "true" });
  const yMax = niceMax(Math.max(...months.flatMap((m) => [m.gbm, m.naive_day])));
  const x = linear(0, months.length - 1, M.l, W - M.r);
  const y = linear(0, yMax, H - M.b, M.t);

  const grid = svg("g", { class: "grid" }, root);
  const axis = svg("g", { class: "axis" }, root);
  for (const t of ticks(0, yMax, 4)) {
    svg("line", { x1: M.l, x2: W - M.r, y1: y(t), y2: y(t), class: t === 0 ? "base" : "" }, grid);
    svg("text", { x: M.l - 8, y: y(t) + 4, "text-anchor": "end" }, axis).textContent = t.toFixed(2);
  }
  months.forEach((m, i) => {
    if (m.month.endsWith("-01")) {
      svg("text", { x: x(i), y: H - 8, "text-anchor": "middle" }, axis).textContent = m.month.slice(0, 4);
    }
  });

  const switchAt = months.findIndex((m) => m.month === "2025-10");
  if (switchAt > 0) {
    const a = svg("g", { class: "annot" }, root);
    svg("line", { x1: x(switchAt), x2: x(switchAt), y1: M.t, y2: H - M.b }, a);
    svg("text", { x: x(switchAt) + 6, y: M.t + 10 }, a).textContent = W < 600 ? "15-min market" : "15-minute market starts";
  }

  const path = (key) => months.map((m, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(m[key]).toFixed(1)}`).join("");
  svg("path", { d: path("naive_day"), fill: "none", stroke: "var(--series-day-before)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, root);
  svg("path", { d: path("gbm"), fill: "none", stroke: "var(--series-elmorgon)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, root);

  const last = months.length - 1;
  svg("circle", { cx: x(last), cy: y(months[last].gbm), r: 4, fill: "var(--series-elmorgon)", class: "dot" }, root);
  svg("circle", { cx: x(last), cy: y(months[last].naive_day), r: 4, fill: "var(--series-day-before)", class: "dot" }, root);

  const cross = svg("line", { class: "crosshair", y1: M.t, y2: H - M.b, visibility: "hidden" }, root);
  const dots = ["gbm", "naive_day"].map((k) =>
    svg("circle", { r: 4, class: "dot", fill: k === "gbm" ? "var(--series-elmorgon)" : "var(--series-day-before)", visibility: "hidden" }, root));
  host.append(root);
  const tip = makeTooltip(host);

  const hit = svg("rect", { x: M.l, y: M.t, width: W - M.l - M.r, height: H - M.t - M.b, fill: "transparent" }, root);
  const move = (evt) => {
    const box = root.getBoundingClientRect();
    const scale = W / box.width;
    const px = (evt.clientX - box.left) * scale;
    const i = Math.max(0, Math.min(last, Math.round((px - M.l) / ((W - M.l - M.r) / last))));
    const m = months[i];
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    ["gbm", "naive_day"].forEach((k, j) => {
      dots[j].setAttribute("cx", x(i)); dots[j].setAttribute("cy", y(m[k])); dots[j].setAttribute("visibility", "visible");
    });
    tip.show(monthLabel(m.month), [
      { key: "elmorgon", label: "Elmorgon", value: err(m.gbm) },
      { key: "day-before", label: "Same hour, day before", value: err(m.naive_day) },
    ], x(i) / scale, y(Math.max(m.gbm, m.naive_day)) / scale);
  };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerleave", () => {
    tip.hide(); cross.setAttribute("visibility", "hidden"); dots.forEach((dot) => dot.setAttribute("visibility", "hidden"));
  });
}

function renderMonthlyTable(d) {
  const months = d.backtest.monthly_mae;
  const table = document.getElementById("monthly-table");
  table.append(el("thead", {}, el("tr", {}, [
    el("th", { text: "Month" }), el("th", { class: "num", text: "Elmorgon" }),
    el("th", { class: "num", text: "No weather" }), el("th", { class: "num", text: "Day before" }),
    el("th", { class: "num", text: "Week before" }),
  ])));
  const body = el("tbody");
  for (const m of months) {
    body.append(el("tr", {}, [
      el("td", { text: monthLabel(m.month) }), el("td", { class: "num", text: err(m.gbm) }),
      el("td", { class: "num", text: err(m.gbm_no_weather) }), el("td", { class: "num", text: err(m.naive_day) }),
      el("td", { class: "num", text: err(m.naive_week) }),
    ]));
  }
  table.append(body);
}

/* Tomorrow: the timetable */

function priceBins(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const q = (p) => sorted[Math.min(sorted.length - 1, Math.floor(p * sorted.length))];
  return [1, 2, 3, 4, 5, 6].map((i) => q(i / 7));
}
const binOf = (v, cuts) => cuts.filter((c) => v >= c).length;

function hourColumns(t) {
  // Columns follow the real hours of the delivery day: 23, 24 or 25 of them.
  const seen = new Map();
  return t.zones[ZONES[0]].map((h) => {
    const n = (seen.get(h.hour) || 0) + 1;
    seen.set(h.hour, n);
    return { label: hourLabel(h.hour) + (n > 1 ? "b" : ""), hour: h.hour, start: h.start };
  });
}

function cellTooltipRows(z, h) {
  return [
    { key: "elmorgon", label: "Median forecast", value: price(h.q50) },
    { key: "band", label: "80% range", value: `${price(h.q10)} to ${price(h.q90)}` },
    { key: "day-before", label: `Day before (${latestLabel()})`, value: price(h.naive) },
  ];
}

function renderTimetable(d) {
  const t = d.tomorrow;
  const table = document.getElementById("timetable");
  table.replaceChildren();
  const cols = hourColumns(t);
  const cuts = priceBins(ZONES.flatMap((z) => t.zones[z].map((h) => h.q50)));
  const cheapest = Object.fromEntries(ZONES.map((z) => {
    const idx = t.zones[z].map((h, i) => [h.q50, i]).sort((a, b) => a[0] - b[0]).slice(0, 3).map(([, i]) => i);
    return [z, new Set(idx)];
  }));
  const pocket = pocketQuery.matches;
  table.classList.toggle("pocket", pocket);
  table.setAttribute("aria-label", `Median forecast for ${longDay(t.date)}, SEK/kWh, by zone and hour`);

  const cells = [];
  const makeCell = (z, i, colIndex) => {
    const h = t.zones[z][i];
    const bin = binOf(h.q50, cuts);
    const td = el("td", { text: price(h.q50), "data-zone": z, "data-i": i });
    if (cheapest[z].has(i)) {
      td.classList.add("cheap");
      td.setAttribute("title", "One of the three cheapest hours");
    }
    td.dataset.bin = bin;
    td.style.transitionDelay = reducedMotion ? "0ms" : `${colIndex * 16}ms`;
    cells.push(td);
    return td;
  };

  const blockStart = (c, i) => i > 0 && [6, 12, 18].includes(c.hour) && !c.label.endsWith("b");
  const validBand = (span) => el("tr", { class: "valid" }, el("th", { colspan: span, scope: "colgroup" },
    el("div", { class: "valid-row" }, [
      el("strong", { text: `Valid for ${longDay(t.date)}` }),
      el("span", { text: `Issued ${shortDay(t.issued_at)} at ${clock(t.issued_at)}, before these prices were published` }),
    ])));
  if (!pocket) {
    // Column widths come from <col>: with a fixed layout the first row (the band) would set them.
    table.append(el("colgroup", {}, [el("col", { class: "col-zone" }), ...cols.map(() => el("col"))]));
    table.append(el("thead", {}, [validBand(cols.length + 1), el("tr", {}, [
      el("th", { scope: "col", text: "Zone" }),
      ...cols.map((c, i) => el("th", { scope: "col", text: c.label, class: blockStart(c, i) ? "block" : "" })),
    ])]));
    const body = el("tbody");
    for (const z of ZONES) {
      body.append(el("tr", {}, [
        el("th", { scope: "row" }, el("span", { class: "zone-label" }, [
          el("span", { class: `badge ${z}`, text: z }), el("small", { text: ZONE_NAMES[z] }),
        ])),
        ...cols.map((c, i) => {
          const td = makeCell(z, i, i);
          if (blockStart(c, i)) td.classList.add("block");
          return td;
        }),
      ]));
    }
    table.append(body);
  } else {
    table.append(el("colgroup", {}, [el("col", { class: "col-hour" }), ...ZONES.map(() => el("col"))]));
    table.append(el("thead", {}, [validBand(ZONES.length + 1), el("tr", {}, [
      el("th", { scope: "col", text: "Hour" }),
      ...ZONES.map((z) => el("th", { scope: "col" }, el("span", { class: `badge ${z}`, text: z }))),
    ])]));
    const body = el("tbody");
    cols.forEach((c, i) => {
      body.append(el("tr", { class: blockStart(c, i) ? "block-row" : "" },
        [el("th", { scope: "row", text: c.label }), ...ZONES.map((z) => makeCell(z, i, i))]));
    });
    table.append(body);
  }

  const paint = () => cells.forEach((td) => {
    const bin = +td.dataset.bin;
    td.style.backgroundColor = `var(--p${bin})`;
    td.classList.toggle("dark", bin >= 5);
  });
  if (reducedMotion) paint();
  else requestAnimationFrame(() => requestAnimationFrame(paint));

  // The tooltip lives outside the scrolling wrapper, so its overflow can never clip it.
  const host = document.getElementById("timetable-body");
  if (!host._tip) {
    host.style.position = "relative";
    host._tip = makeTooltip(host);
  }
  const tip = host._tip;
  table.onpointermove = (evt) => {
    const td = evt.target.closest("td");
    if (!td) { tip.hide(); return; }
    const z = td.dataset.zone, i = +td.dataset.i;
    const h = t.zones[z][i];
    const box = host.getBoundingClientRect();
    const cell = td.getBoundingClientRect();
    tip.show(`${z} ${ZONE_NAMES[z]}, ${cols[i].label}:00`, cellTooltipRows(z, h),
      cell.right - box.left, cell.top - box.top);
  };
  table.onpointerleave = () => tip.hide();
  table.onclick = (evt) => {
    const td = evt.target.closest("td");
    if (td) selectZone(td.dataset.zone, true);
  };
}

const average = (values) => (values.length ? values.reduce((s, v) => s + v, 0) / values.length : null);

// A small step line of tomorrow's forecast, on a scale shared by all four cards.
function sparkline(values, color, hi) {
  const W = 300, H = 44;
  const x = linear(0, values.length, 0, W);
  const y = linear(Math.min(0, ...values), hi, H - 2, 3);
  const line = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}L${x(i + 1).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" });
  svg("path", { d: `${line}L${W},${H}L0,${H}Z`, fill: color, "fill-opacity": 0.12 }, root);
  svg("path", { d: line, fill: "none", stroke: color, "stroke-width": 1.5, "vector-effect": "non-scaling-stroke" }, root);
  return root;
}

// The four price areas, as on statsskuld.se: tomorrow's forecast average, the latest actual
// average and the shape of tomorrow. Market intervals in a day are equal, so a plain mean
// is the daily average.
function renderAreas(d) {
  const t = d.tomorrow;
  const actual = d.latest_actual;
  const box = document.getElementById("areas");
  const tabs = document.getElementById("area-tabs");
  const hi = t ? Math.max(...ZONES.flatMap((z) => t.zones[z].map((h) => h.q50))) : 1;
  for (const z of ZONES) {
    const forecast = t ? average(t.zones[z].map((h) => h.q50)) : null;
    const known = actual && actual.zones[z] ? average(actual.zones[z].map((p) => p.price)) : null;
    const card = el("button", { type: "button", class: "area", "data-zone": z, "aria-pressed": String(z === state.zone) }, [
      el("span", { class: "area-name" }, [el("span", { class: `badge ${z}`, text: z }), ZONE_NAMES[z]]),
      el("span", { class: "area-price" }, [price(forecast), el("small", { text: "SEK/kWh" })]),
      el("span", { class: "area-sub" }, [
        el("span", { text: t ? `Forecast, ${shortDay(t.date)}` : "No forecast yet" }),
        el("span", { text: actual ? `Actual ${shortDay(actual.date)}: ${price(known)}` : "" }),
      ]),
    ]);
    if (t) card.append(sparkline(t.zones[z].map((h) => h.q50), `var(--${z.toLowerCase()})`, hi));
    card.addEventListener("click", () => selectZone(z, true));
    box.append(card);
    if (t) {
      const tab = el("button", { type: "button", class: "tab", "data-zone": z, "aria-pressed": String(z === state.zone) },
        [el("span", { class: `badge ${z}`, text: z }), ZONE_NAMES[z]]);
      tab.addEventListener("click", () => selectZone(z));
      tabs.append(tab);
    }
  }
}

function selectZone(z, scroll = false) {
  state.zone = z;
  for (const button of document.querySelectorAll("button[data-zone]")) {
    button.setAttribute("aria-pressed", String(button.dataset.zone === z));
  }
  if (!state.data.tomorrow) return;
  renderZoneChart(state.data);
  if (scroll) document.getElementById("tomorrow").scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
}

// The latest published delivery day: "today" at the 10:00 issue time, named by date
// because the page can be rebuilt after the next day's prices are out.
const latestLabel = () => shortDay(state.data.latest_actual.date);

function todayByClock(d, z) {
  // Today's actual prices, placed at the same clock time tomorrow so the shapes line up.
  const actual = d.latest_actual;
  if (!actual || !actual.zones[z]) return [];
  const shiftMs = dateFrom(d.tomorrow.date) - dateFrom(actual.date);
  return actual.zones[z].map((p) => ({ t: new Date(p.start).getTime() + shiftMs, price: p.price }));
}

function renderZoneChart(d) {
  const z = state.zone;
  const t = d.tomorrow;
  const hours = t.zones[z];
  const today = todayByClock(d, z);
  const res = (d.latest_actual && d.latest_actual.resolution_minutes) || 60;
  setText("zone-chart-title", `${z} ${ZONE_NAMES[z]}: forecast range by hour, SEK/kWh`);
  setText("today-key", `Day before: actual price ${latestLabel()}, ${res}-minute`);

  const host = document.getElementById("zone-chart");
  host.replaceChildren();
  const W = Math.max(300, host.clientWidth || 900), H = W < 600 ? 240 : 300, M = { t: 14, r: 14, b: 30, l: 44 };
  const every = W < 600 ? 6 : 3;
  const t0 = new Date(hours[0].start).getTime();
  const t1 = t0 + hours.length * 3600e3;
  const values = [...hours.flatMap((h) => [h.q10, h.q90]), ...today.map((p) => p.price)];
  const yLo = Math.min(0, ...values);
  const yHi = niceMax(Math.max(...values));
  const x = linear(t0, t1, M.l, W - M.r);
  const y = linear(yLo, yHi, H - M.b, M.t);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, "aria-hidden": "true" });

  const grid = svg("g", { class: "grid" }, root);
  const axis = svg("g", { class: "axis" }, root);
  for (const v of ticks(yLo, yHi, 4)) {
    svg("line", { x1: M.l, x2: W - M.r, y1: y(v), y2: y(v), class: Math.abs(v) < 1e-9 ? "base" : "" }, grid);
    svg("text", { x: M.l - 8, y: y(v) + 4, "text-anchor": "end" }, axis).textContent = v.toFixed(2);
  }
  hours.forEach((h, i) => {
    if (i % every === 0) {
      svg("text", { x: x(t0 + i * 3600e3), y: H - 8, "text-anchor": i === 0 ? "start" : "middle" }, axis).textContent = `${hourLabel(h.hour)}:00`;
    }
  });

  // The band and the median are drawn per hour, as steps: each value holds for its whole hour.
  const step = (key) => hours.map((h, i) => {
    const a = x(t0 + i * 3600e3), b = x(t0 + (i + 1) * 3600e3), v = y(h[key]);
    return `${i ? "L" : "M"}${a.toFixed(1)},${v.toFixed(1)}L${b.toFixed(1)},${v.toFixed(1)}`;
  }).join("");
  const top = step("q90");
  const bottom = hours.map((h, i) => {
    const j = hours.length - 1 - i;
    const a = x(t0 + (j + 1) * 3600e3), b = x(t0 + j * 3600e3), v = y(hours[j].q10);
    return `L${a.toFixed(1)},${v.toFixed(1)}L${b.toFixed(1)},${v.toFixed(1)}`;
  }).join("");
  svg("path", { d: `${top}${bottom}Z`, fill: "var(--band)" }, root);

  if (today.length) {
    const stepMs = res * 60e3;
    const d2 = today.map((p, i) => {
      const a = x(p.t), b = x(p.t + stepMs), v = y(p.price);
      return `${i ? "L" : "M"}${a.toFixed(1)},${v.toFixed(1)}L${b.toFixed(1)},${v.toFixed(1)}`;
    }).join("");
    svg("path", { d: d2, fill: "none", stroke: "var(--series-day-before)", "stroke-width": 1.5, "stroke-linejoin": "round" }, root);
  }
  svg("path", { d: step("q50"), fill: "none", stroke: "var(--series-elmorgon)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, root);

  const cross = svg("line", { class: "crosshair", y1: M.t, y2: H - M.b, visibility: "hidden" }, root);
  host.append(root);
  const tip = makeTooltip(host);
  const hit = svg("rect", { x: M.l, y: M.t, width: W - M.l - M.r, height: H - M.t - M.b, fill: "transparent" }, root);
  hit.addEventListener("pointermove", (evt) => {
    const box = root.getBoundingClientRect();
    const scale = W / box.width;
    const px = (evt.clientX - box.left) * scale;
    const i = Math.max(0, Math.min(hours.length - 1, Math.floor((px - M.l) / ((W - M.l - M.r) / hours.length))));
    const h = hours[i];
    const mid = x(t0 + (i + 0.5) * 3600e3);
    cross.setAttribute("x1", mid); cross.setAttribute("x2", mid); cross.setAttribute("visibility", "visible");
    const hourStart = t0 + i * 3600e3;
    const inHour = today.filter((p) => p.t >= hourStart && p.t < hourStart + 3600e3);
    const todayAvg = inHour.length ? inHour.reduce((s, p) => s + p.price, 0) / inHour.length : null;
    tip.show(`${hourLabel(h.hour)}:00 to ${hourLabel((h.hour + 1) % 24)}:00`, [
      { key: "elmorgon", label: "Median forecast", value: price(h.q50) },
      { key: "band", label: "80% range", value: `${price(h.q10)} to ${price(h.q90)}` },
      { key: "day-before", label: `Day before (${latestLabel()})`, value: price(todayAvg) },
    ], mid / scale, y(h.q90) / scale);
  });
  hit.addEventListener("pointerleave", () => { tip.hide(); cross.setAttribute("visibility", "hidden"); });

  const table = document.getElementById("zone-table");
  table.replaceChildren(el("thead", {}, el("tr", {}, [
    el("th", { text: "Hour" }), el("th", { class: "num", text: "Low (P10)" }), el("th", { class: "num", text: "Median" }),
    el("th", { class: "num", text: "High (P90)" }), el("th", { class: "num", text: `Day before (${latestLabel()})` }),
  ])));
  const body = el("tbody");
  for (const h of hours) {
    body.append(el("tr", {}, [
      el("td", { text: `${hourLabel(h.hour)}:00` }), el("td", { class: "num", text: price(h.q10) }),
      el("td", { class: "num", text: price(h.q50) }), el("td", { class: "num", text: price(h.q90) }),
      el("td", { class: "num", text: price(h.naive) }),
    ]));
  }
  table.append(body);
}

function renderTomorrow(d) {
  const t = d.tomorrow;
  renderAreas(d);
  if (!t) {
    document.getElementById("tomorrow-body").replaceChildren(el("p", { class: "empty",
      text: "No forecast has been issued yet. The next one is made at 10:00 Stockholm time, before the auction closes at 12:00." }));
    document.getElementById("all-areas").hidden = true;
    return;
  }
  setText("tomorrow-meta", `For ${longDay(t.date)}, issued ${shortDay(t.issued_at)} at ${clock(t.issued_at)}, before the prices were published`);
  renderTimetable(d);
  renderZoneChart(d);
  pocketQuery.addEventListener("change", () => renderTimetable(d));
}

// On narrow screens the side column stacks below everything, so the area cards move up under the intro.
function placeAreas() {
  const block = document.getElementById("areas-block");
  if (stackQuery.matches) document.getElementById("updates").after(block);
  else document.querySelector(".side").prepend(block);
}

/* Value */

function rankedRows(table, rows, format, highlight) {
  const max = Math.max(...rows.map((r) => Math.abs(r.value)));
  table.append(el("thead", {}, el("tr", {}, [
    el("th", { scope: "col", text: "Plan" }),
    el("th", { scope: "col", class: "val", text: rows.unit }),
    el("th", { scope: "col" }, el("span", { class: "visually-hidden", text: "Drawn to scale" })),
  ])));
  const body = el("tbody");
  for (const r of rows) {
    body.append(el("tr", { class: r.key === highlight ? "is-us" : "" }, [
      el("td", { text: r.label }),
      el("td", { class: "val num", text: format(r.value) }),
      el("td", { class: "bar" }, el("div", { class: "meter" }, el("span", { style: `width:${(Math.abs(r.value) / max) * 100}%` }))),
    ]));
  }
  table.append(body);
}

function renderValue(d) {
  const v = d.value;
  const a = v.assumptions;
  const ev = v.overall.ev;
  const bat = v.overall.battery;
  setText("ev-sub", `${a.ev.energy_kwh_per_day} kWh a day on an ${a.ev.charger_kw} kW charger, car at home overnight. Average price paid per kWh.`);
  const evRows = [
    { key: "on_arrival", label: "Plug in at 18:00 and charge", value: ev.sek_per_kwh.on_arrival },
    { key: "naive", label: "Plan by repeating the day before", value: ev.sek_per_kwh.naive },
    { key: "elmorgon", label: "Plan with Elmorgon", value: ev.sek_per_kwh.elmorgon },
    { key: "oracle", label: "Perfect hindsight", value: ev.sek_per_kwh.oracle },
  ];
  evRows.unit = "SEK/kWh";
  rankedRows(document.getElementById("ev-table"), evRows, price, "elmorgon");
  setText("ev-sentence",
    `Planning with Elmorgon captures ${pct(ev.share_of_possible_saving.elmorgon)} of the possible saving, ` +
    `about ${sek(ev.saving_sek_per_year.elmorgon)} a year, against ${pct(ev.share_of_possible_saving.naive)} by repeating the day before.`);

  setText("bat-sub", `${a.battery.capacity_kwh} kWh, ${a.battery.power_kw} kW, ${pct(a.battery.round_trip)} round trip, planned one day at a time. Profit per year from charging cheap and discharging dear.`);
  const batRows = [
    { key: "naive", label: "Plan by repeating the day before", value: bat.profit_sek_per_year.naive },
    { key: "elmorgon", label: "Plan with Elmorgon", value: bat.profit_sek_per_year.elmorgon },
    { key: "oracle", label: "Perfect hindsight", value: bat.profit_sek_per_year.oracle },
  ];
  batRows.unit = "SEK a year";
  rankedRows(document.getElementById("bat-table"), batRows, (x) => count(Math.round(x)), "elmorgon");
  setText("bat-sentence",
    `Elmorgon earns ${pct(bat.share_of_possible_profit.elmorgon)} of what perfect hindsight would, ` +
    `against ${pct(bat.share_of_possible_profit.naive)} by repeating the day before.`);

  const table = document.getElementById("value-zone-table");
  table.append(el("thead", {}, el("tr", {}, [
    el("th", { text: "Zone" }), el("th", { class: "num", text: "EV paid" }),
    el("th", { class: "num", text: "EV saving" }), el("th", { class: "num", text: "Share of saving" }),
    el("th", { class: "num", text: "Battery" }),
  ])));
  const body = el("tbody");
  for (const z of ZONES) {
    const b = v.by_zone[z];
    if (!b) continue;
    body.append(el("tr", {}, [
      el("td", {}, el("span", { class: "zone-label" }, [el("span", { class: `badge ${z}`, text: z }), ZONE_NAMES[z]])),
      el("td", { class: "num", text: price(b.ev.sek_per_kwh.elmorgon) }),
      el("td", { class: "num", text: sek(b.ev.saving_sek_per_year.elmorgon) }),
      el("td", { class: "num", text: pct(b.ev.share_of_possible_saving.elmorgon) }),
      el("td", { class: "num", text: sek(b.battery.profit_sek_per_year.elmorgon) }),
    ]));
  }
  table.append(body);
}

/* Live record */

function issueTiming(r) {
  // The auction for delivery day D closes at 12:00 on D-1 (Stockholm time).
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {
    timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", hour12: false,
  }).formatToParts(new Date(r.issued_at)).map((x) => [x.type, x.value]));
  const issuedDay = `${parts.year}-${parts.month}-${parts.day}`;
  const dayBefore = new Date(dateFrom(r.delivery_date) - 864e5).toISOString().slice(0, 10);
  const hour = +parts.hour % 24;
  const beforeAuction = issuedDay < dayBefore || (issuedDay === dayBefore && hour < 12);
  return beforeAuction ? "before" : "after";
}

function renderRecord(d) {
  const tr = d.track_record;
  const scored = new Map();
  for (const row of tr.days) {
    const key = row.delivery_date.slice(0, 10);
    if (!scored.has(key)) scored.set(key, []);
    scored.get(key).push(row);
  }
  const rows = tr.issued || [];
  const late = rows.filter((r) => issueTiming(r) === "after");
  const s = tr.summary;
  const parts = [];
  if (!s) {
    const first = rows.length ? rows[rows.length - 1] : null;
    parts.push(first
      ? `The record starts with the forecast for ${longDay(first.delivery_date)}. Each day is scored as soon as its prices are published.`
      : "No live forecasts yet. The scheduled run issues one every morning before the auction closes.");
  } else {
    const dayWord = s.days === 1 ? "day" : "days";
    parts.push(
      `Closer than repeating the day before on ${s.days_beating_naive} of ${s.days} scored ${dayWord}. ` +
      `Average error ${err(s.mae_elmorgon)} SEK/kWh against ${err(s.mae_naive)}; the 80% range held ${pct(s.coverage_80)} of actual prices. ` +
      (s.days < 30 ? "Too few days to judge yet: the backtest above is the larger sample." : ""));
  }
  if (late.length) {
    parts.push(
      `${late.length === 1 ? "One forecast was" : `${late.length} forecasts were`} issued after the 12:00 auction close, ` +
      `while the project was being built. ${late.length === 1 ? "It was" : "Each was"} still saved before its prices were published, but too late to bid with. ` +
      "The scheduled run issues at 10:00.");
  }
  const summary = document.getElementById("record-summary");
  summary.replaceChildren(...parts.map((text) => el("span", { class: "record-line", text })));

  const table = document.getElementById("record-table");
  if (!rows.length) { table.remove(); return; }
  table.append(el("thead", {}, el("tr", {}, [
    el("th", { text: "Delivery day" }), el("th", { text: "Issued" }), el("th", { text: "Timing" }),
    el("th", { text: "Status" }), el("th", { class: "num", text: "Elmorgon error" }),
    el("th", { class: "num", text: "Day before" }), el("th", { class: "num", text: "Range held" }),
  ])));
  const body = el("tbody");
  const pending = () => el("td", { class: "num", text: "pending", "aria-label": "not yet scored" });
  for (const r of rows) {
    const key = r.delivery_date.slice(0, 10);
    const days = scored.get(key);
    const mean = (k) => days.reduce((acc, x) => acc + x[k], 0) / days.length;
    body.append(el("tr", {}, [
      el("td", { text: shortDay(key) }),
      el("td", { text: `${shortDay(r.issued_at)} ${clock(r.issued_at)}` }),
      el("td", { text: issueTiming(r) === "before" ? "Before the auction" : "After the auction" }),
      el("td", {}, el("span", { class: `status ${days ? "scored" : "waiting"}` }, [el("i"), days ? "Scored" : "Waiting for prices"])),
      days ? el("td", { class: "num", text: err(mean("mae_elmorgon")) }) : pending(),
      days ? el("td", { class: "num", text: err(mean("mae_naive")) }) : pending(),
      days ? el("td", { class: "num", text: pct(mean("coverage")) }) : pending(),
    ]));
  }
  table.append(body);
}

/* How it's built */

function renderSteps(d) {
  const p = d.pipeline;
  const steps = [
    ["Collect", `Day-ahead prices for SE1 to SE4 from elprisetjustnu.se and hourly weather at eight Swedish locations from Open-Meteo, stored exactly as received. ${count(p.days)} days and ${count(p.intervals)} prices since ${shortDay(p.first_day)} ${p.first_day.slice(0, 4)}.`],
    ["Validate", `Every day is checked for gaps, duplicates, impossible prices and the right number of intervals for a 23, 24 or 25-hour day. Failures are quarantined with a reason instead of loaded. ${p.quarantined_days} days are in quarantine now; the first run caught a daylight-saving error in the source data on 28 days, now repaired.`],
    ["Model the warehouse", `dbt builds a star schema in DuckDB: price facts, zone and calendar dimensions with Swedish holidays, and a feature table that only uses what was known at 10:00 the day before. ${p.data_tests.passed} of ${p.data_tests.total} data tests passed on the last build.`],
    ["Forecast", "LightGBM quantile models predict the 10th, 50th and 90th percentile for every hour, and the range is calibrated on the last 90 days of errors. A walk-forward backtest retrains the model every month."],
    ["Publish", "The forecast is saved once, before the day's prices exist, and never edited. Then this page is rebuilt from a single JSON file."],
  ];
  const list = document.getElementById("steps");
  for (const [title, text] of steps) {
    list.append(el("li", {}, [el("h3", { text: title }), el("p", { text })]));
  }
}

/* Wide tables: fade the edge until scrolled */

function markOverflow() {
  for (const box of document.querySelectorAll(".scroll-x")) {
    const update = () => box.classList.toggle("overflows",
      box.scrollWidth > box.clientWidth + 1 && box.scrollLeft + box.clientWidth < box.scrollWidth - 2);
    if (!box._watched) {
      box.addEventListener("scroll", update, { passive: true });
      box._watched = true;
    }
    update();
  }
}

/* Section highlight in the shell */

function watchSections() {
  const links = new Map([...document.querySelectorAll(".top nav a")].map((a) => [a.getAttribute("href").slice(1), a]));
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      links.forEach((a) => a.removeAttribute("aria-current"));
      const link = links.get(entry.target.id);
      if (link) link.setAttribute("aria-current", "true");
    }
  }, { rootMargin: "-45% 0px -50% 0px" });
  document.querySelectorAll("main section.panel[id]").forEach((s) => observer.observe(s));
}

/* Boot */

async function main() {
  let d;
  try {
    const response = await fetch("data/dashboard.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    d = await response.json();
  } catch (err) {
    document.querySelector("main").replaceChildren(el("p", { class: "wrap load-error",
      text: `The dashboard data could not be loaded (${err.message}). Run "elmorgon site" to regenerate data/dashboard.json, then reload.` }));
    setText("freshness", "Data unavailable");
    return;
  }
  state.data = d;
  renderFreshness(d);
  renderClaim(d);
  renderRanked(d);
  renderMonthly(d);
  renderMonthlyTable(d);
  renderTomorrow(d);
  renderValue(d);
  renderRecord(d);
  renderSteps(d);
  watchSections();
  placeAreas();
  stackQuery.addEventListener("change", placeAreas);
  for (const details of document.querySelectorAll("details")) details.addEventListener("toggle", markOverflow);
  markOverflow();
  let width = window.innerWidth;
  let timer;
  window.addEventListener("resize", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (window.innerWidth === width) return;
      width = window.innerWidth;
      renderMonthly(d);
      if (d.tomorrow) renderZoneChart(d);
      markOverflow();
    }, 150);
  });
}

main();
