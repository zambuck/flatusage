const usageFile = document.getElementById("usageFile");
const config1File = document.getElementById("config1File");
const config2File = document.getElementById("config2File");
const dateRangeDiv = document.getElementById("dateRange");
const plan1Div = document.getElementById("plan1");
const plan2Div = document.getElementById("plan2");
const dupWarningDiv = document.getElementById("dupWarning");

let config1Text = null;
let config2Text = null;

function formatDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

async function readFileText(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file);
  });
}

function checkDuplicateConfigs() {
  if (config1Text && config2Text) {
    if (config1Text === config2Text) {
      dupWarningDiv.style.display = "block";
      dupWarningDiv.className = "warning";
      dupWarningDiv.textContent = "Warning: the two config files are identical. The comparison will show no cost difference.";
    } else {
      dupWarningDiv.style.display = "none";
      dupWarningDiv.textContent = "";
    }
  }
}

async function parseCsv(file) {
  if (!file) return;
  const formData = new FormData();
  formData.append("usage_csv", file);
  try {
    const resp = await fetch("/parse-csv", { method: "POST", body: formData });
    const data = await resp.json();
    if (resp.ok) {
      dateRangeDiv.style.display = "block";
      dateRangeDiv.innerHTML = `Date range: <strong>${formatDate(data.start)}</strong> to <strong>${formatDate(data.end)}</strong> (${data.days} days)`;
    } else {
      dateRangeDiv.style.display = "block";
      dateRangeDiv.innerHTML = `<span style="color:#cf222e">${data.error}</span>`;
    }
  } catch (err) {
    dateRangeDiv.style.display = "block";
    dateRangeDiv.innerHTML = `<span style="color:#cf222e">${err.message}</span>`;
  }
}

async function parseConfig(file, div, storeTextRef) {
  if (!file) {
    div.style.display = "none";
    div.innerHTML = "";
    if (storeTextRef === 1) config1Text = null;
    if (storeTextRef === 2) config2Text = null;
    checkDuplicateConfigs();
    return;
  }
  const text = await readFileText(file);
  if (storeTextRef === 1) config1Text = text;
  if (storeTextRef === 2) config2Text = text;

  const formData = new FormData();
  formData.append("tariff_yaml", file);
  try {
    const resp = await fetch("/parse-config", { method: "POST", body: formData });
    const data = await resp.json();
    if (resp.ok) {
      div.style.display = "block";
      let html = `Plan: <span class="plan-name">${data.plan_name}</span>`;
      if (data.registers.length > 0) {
        html += ` <em>(registers: ${data.registers.join(", ")})</em>`;
      }
      div.innerHTML = html;
    } else {
      div.style.display = "block";
      div.innerHTML = `<span style="color:#cf222e">${data.error}</span>`;
    }
  } catch (err) {
    div.style.display = "block";
    div.innerHTML = `<span style="color:#cf222e">${err.message}</span>`;
  }
  checkDuplicateConfigs();
}

usageFile.addEventListener("change", () => parseCsv(usageFile.files[0]));
config1File.addEventListener("change", () => parseConfig(config1File.files[0], plan1Div, 1));
config2File.addEventListener("change", () => parseConfig(config2File.files[0], plan2Div, 2));

const form = document.getElementById("calcForm");
const spinner = document.getElementById("spinner");
const errorDiv = document.getElementById("error");
const resultsDiv = document.getElementById("results");

function fmtNum(n) {
  return n == null ? "-" : n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function renderComparison(comparison, note) {
  const div = document.createElement("div");
  div.className = "comparison";
  let html = "<h2>Cost comparison</h2>";
  if (note) {
    html += `<p style="color:#cf222e">${note}</p>`;
  }
  if (comparison.length === 0) {
    html += "<p>No comparable register totals were found.</p>";
  } else {
    html += `<table>
      <tr>
        <th>Register</th>
        <th>Plan A</th>
        <th>Plan B</th>
        <th>kWh</th>
        <th>Cost A</th>
        <th>Cost B</th>
        <th>Difference</th>
      </tr>`;
    for (const row of comparison) {
      const diffClass = row.cheaper === "a" ? "cheaper" : row.cheaper === "b" ? "cheaper" : "";
      const diffSign = row.diff > 0 ? "+" : "";
      const winner = row.cheaper === "a" ? "Plan A cheaper" : row.cheaper === "b" ? "Plan B cheaper" : "Same cost";
      html += `<tr>
        <td>${row.register}</td>
        <td>${row.plan_a}</td>
        <td>${row.plan_b}</td>
        <td>${fmtNum(row.kwh)}</td>
        <td>$${fmtNum(row.cost_a)}</td>
        <td>$${fmtNum(row.cost_b)}</td>
        <td class="${diffClass}">${diffSign}$${fmtNum(row.diff)}<br><small>${winner}</small></td>
      </tr>`;
    }
    html += "</table>";
  }
  div.innerHTML = html;
  return div;
}

const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const PERIOD_PALETTE = ["#0969da", "#bf3989", "#9a6700", "#1a7f37", "#8250df", "#cf222e", "#0a7ea4"];
const periodColorCache = {};

function colorForPeriod(period) {
  if (!period) return "#d0d7de";
  const lower = period.toLowerCase();

  if (lower === "default") {
    if (!periodColorCache[period]) periodColorCache[period] = "#64748b"; // blue-grey
    return periodColorCache[period];
  }

  if (lower === "solar_soak") {
    if (!periodColorCache[period]) periodColorCache[period] = "#eab308"; // medium-yellow
    return periodColorCache[period];
  }

  if (lower.includes("peak") && !lower.includes("off-peak")) {
    if (!periodColorCache[period]) periodColorCache[period] = "#dc2626"; // red
    return periodColorCache[period];
  }

  if (!periodColorCache[period]) {
    const idx = Object.keys(periodColorCache).length % PERIOD_PALETTE.length;
    periodColorCache[period] = PERIOD_PALETTE[idx];
  }
  return periodColorCache[period];
}

function heatColor(value, min, max) {
  const stops = [
    [230, 241, 251], [181, 212, 244], [133, 183, 235],
    [55, 138, 221], [24, 95, 165], [12, 68, 124], [4, 44, 83],
  ];
  const t = max > min ? (value - min) / (max - min) : 0;
  const idx = t * (stops.length - 1);
  const i0 = Math.floor(idx);
  const i1 = Math.min(i0 + 1, stops.length - 1);
  const f = idx - i0;
  const c = stops[i0].map((s, k) => Math.round(s + (stops[i1][k] - s) * f));
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
}

function renderLoadProfile(register, profile) {
  const wrap = document.createElement("div");
  wrap.className = "load-profile";

  const heading = document.createElement("h4");
  heading.textContent = `Load profile — ${register}`;
  wrap.appendChild(heading);

  const flat = profile.grid.flat();
  const min = Math.min(...flat);
  const max = Math.max(...flat);

  const stripRow = document.createElement("div");
  stripRow.className = "heatmap-row";
  const stripLabel = document.createElement("div");
  stripLabel.className = "heatmap-row-label";
  stripLabel.textContent = "Tariff";
  stripRow.appendChild(stripLabel);
  for (let h = 0; h < 24; h++) {
    const cell = document.createElement("div");
    cell.className = "heatmap-cell period-cell";
    const period = profile.period_by_hour[h];
    cell.style.background = colorForPeriod(period);
    cell.title = `${h}:00 — ${period || "unknown"}`;
    stripRow.appendChild(cell);
  }
  wrap.appendChild(stripRow);

  const hourRow = document.createElement("div");
  hourRow.className = "heatmap-row";
  const hourSpacer = document.createElement("div");
  hourSpacer.className = "heatmap-row-label";
  hourRow.appendChild(hourSpacer);
  for (let h = 0; h < 24; h++) {
    const cell = document.createElement("div");
    cell.className = "heatmap-hour-label";
    cell.textContent = h % 3 === 0 ? h : "";
    hourRow.appendChild(cell);
  }
  wrap.appendChild(hourRow);

  DAY_LABELS.forEach((day, r) => {
    const row = document.createElement("div");
    row.className = "heatmap-row";
    const label = document.createElement("div");
    label.className = "heatmap-row-label";
    label.textContent = day;
    row.appendChild(label);
    for (let h = 0; h < 24; h++) {
      const value = profile.grid[r][h];
      const cell = document.createElement("div");
      cell.className = "heatmap-cell";
      cell.style.background = heatColor(value, min, max);
      cell.title = `${day} ${h}:00 — avg ${value.toFixed(2)} kWh`;
      row.appendChild(cell);
    }
    wrap.appendChild(row);
  });

  const seenPeriods = [...new Set(profile.period_by_hour.filter(Boolean))];
  const legend = document.createElement("div");
  legend.className = "heatmap-legend";
  legend.innerHTML = `
    <span class="legend-scale">
      Low <span class="scale-bar"></span> High
    </span>
    <span class="legend-periods">
      ${seenPeriods
        .map((p) => `<span class="legend-swatch" style="background:${colorForPeriod(p)}"></span>${p}`)
        .join(" ")}
    </span>
  `;
  wrap.appendChild(legend);


  return wrap;
}

/* ----------------- new weekly chart renderer ----------------- */

const REGISTER_PALETTE = ["#fd7e14", "#20c997", "#6f42c1", "#d63384", "#0dcaf0", "#adb5bd", "#ffc107"];
const registerColorCache = {};

function colorForRegister(register) {
  if (!register) return "#6c757d";
  if (register === "B1") return "#f59f00"; // solar yellow
  if (!registerColorCache[register]) {
    const idx = Object.keys(registerColorCache).length % REGISTER_PALETTE.length;
    registerColorCache[register] = REGISTER_PALETTE[idx];
  }
  return registerColorCache[register];
}

function renderWeeklyChart(data, title) {
  const wrap = document.createElement("div");
  wrap.className = "weekly-chart";

  const heading = document.createElement("h4");
  heading.textContent = title || "Weekly usage & cost (last 12 months)";
  wrap.appendChild(heading);

  const labels = data.labels.map((iso) => {
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  });

  const costCategories = data.categories || Object.keys(data.cost_series);
  const costSeries = data.cost_series;
  const kwhCategories = data.kwh_categories || Object.keys(data.kwh_series);
  const kwhSeries = data.kwh_series;
  const exportRegisters = new Set(data.export_registers || []);
  const exportCostCats = new Set(data.export_cost_categories || []);

  const margin = { top: 30, right: 70, bottom: 90, left: 70 };
  const slot = 28;
  const chartHeight = 260;
  const half = chartHeight / 2;
  const width = Math.max(720, margin.left + labels.length * slot + margin.right);
  const height = chartHeight + margin.top + margin.bottom;
  const baseline = margin.top + half;

  function fmt(n) {
    return n.toLocaleString(undefined, { maximumFractionDigits: 1 });
  }
  function fmtMoney(n) {
    return "$" + n.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 });
  }

  const allKwh = Object.values(kwhSeries).flat();
  const maxKwh = Math.max(...(allKwh.length ? allKwh : [0]), 0.1);
  const maxCost = Math.max(data.max_cost || 0, 0.1);

  function kwhY(v) {
    return half * (v / maxKwh);
  }
  function costY(v) {
    return half * (v / maxCost);
  }

  function niceTicks(max, targetCount) {
    if (max <= 0) return [0];
    const raw = max / targetCount;
    const exp = Math.floor(Math.log10(raw));
    const candidates = [];
    for (let e = exp - 1; e <= exp + 1; e++) {
      const base = Math.pow(10, e);
      [1, 2, 5].forEach((m) => candidates.push(m * base));
    }
    let best = null;
    let bestCount = Infinity;
    for (const step of candidates) {
      if (step <= 0) continue;
      const ticks = [];
      for (let i = 0; ; i++) {
        const v = i * step;
        if (v > max * 1.0001) break;
        ticks.push(v);
      }
      const count = ticks.length;
      if (count >= targetCount + 1) {
        if (best === null || count < bestCount) {
          best = ticks;
          bestCount = count;
        }
      }
    }
    if (best === null) {
      const step = raw;
      const ticks = [];
      for (let i = 0; i <= targetCount; i++) {
        const v = i * step;
        if (v > max) break;
        ticks.push(v);
      }
      best = ticks;
    }
    return best;
  }

  const kwhTicks = niceTicks(maxKwh, 4);
  const costTicks = niceTicks(maxCost, 4);

  function costColor(cat) {
    if (cat.toLowerCase().endsWith(" supply")) return "#444c56";
    for (const reg of exportRegisters) {
      if (cat.startsWith(reg + " ")) {
        // Export feed-in credits: distinct from the kWh export colour.
        return "#2ea44f";
      }
    }
    let base = cat;
    for (const reg of kwhCategories) {
      if (base.startsWith(reg + " ")) {
        base = base.slice(reg.length + 1);
        break;
      }
    }
    return colorForPeriod(base);
  }

  let svg = `<svg viewBox="0 0 ${width} ${height}" xmlns="http://www.w3.org/2000/svg">`;

  // central horizontal axis
  svg += `<line class="weekly-axis" x1="${margin.left}" y1="${baseline}" x2="${width - margin.right}" y2="${baseline}" />`;

  // vertical dotted guides at each week
  labels.forEach((_, i) => {
    const x = margin.left + i * slot + slot / 2;
    svg += `<line class="weekly-grid" x1="${x}" y1="${margin.top}" x2="${x}" y2="${margin.top + chartHeight}" />`;
  });

  // horizontal grid lines
  kwhTicks.forEach((t) => {
    if (t === 0) return;
    const y = baseline + kwhY(t);
    svg += `<line class="weekly-grid" x1="${margin.left}" y1="${y}" x2="${width - margin.right}" y2="${y}" />`;
    const yUp = baseline - kwhY(t);
    svg += `<line class="weekly-grid" x1="${margin.left}" y1="${yUp}" x2="${width - margin.right}" y2="${yUp}" />`;
  });
  costTicks.forEach((t) => {
    if (t === 0) return;
    const y = baseline - costY(t);
    svg += `<line class="weekly-grid" x1="${margin.left}" y1="${y}" x2="${width - margin.right}" y2="${y}" />`;
    const yDown = baseline + costY(t);
    svg += `<line class="weekly-grid" x1="${margin.left}" y1="${yDown}" x2="${width - margin.right}" y2="${yDown}" />`;
  });

  // left axis labels: kWh
  kwhTicks.forEach((t) => {
    const yDown = baseline + kwhY(t);
    svg += `<text class="weekly-axis-label" x="${margin.left - 6}" y="${yDown + 3}" text-anchor="end">${fmt(t)}</text>`;
    const yUp = baseline - kwhY(t);
    svg += `<text class="weekly-axis-label" x="${margin.left - 6}" y="${yUp + 3}" text-anchor="end">${fmt(t)}</text>`;
  });
  svg += `<text class="weekly-axis-label" transform="rotate(-90, ${margin.left - 45}, ${baseline + half / 2})" x="${margin.left - 45}" y="${baseline + half / 2}" text-anchor="middle">kWh used</text>`;
  svg += `<text class="weekly-axis-label" transform="rotate(90, ${margin.left - 45}, ${baseline - half / 2})" x="${margin.left - 45}" y="${baseline - half / 2}" text-anchor="middle">kWh exported</text>`;

  // right axis labels: dollars
  costTicks.forEach((t) => {
    const yUp = baseline - costY(t);
    svg += `<text class="weekly-axis-label" x="${width - margin.right + 6}" y="${yUp + 3}" text-anchor="start">${fmtMoney(t)}</text>`;
    const yDown = baseline + costY(t);
    svg += `<text class="weekly-axis-label" x="${width - margin.right + 6}" y="${yDown + 3}" text-anchor="start">${fmtMoney(t)}</text>`;
  });
  svg += `<text class="weekly-axis-label" transform="rotate(90, ${width - margin.right + 45}, ${baseline - half / 2})" x="${width - margin.right + 45}" y="${baseline - half / 2}" text-anchor="middle">Cost ($)</text>`;
  svg += `<text class="weekly-axis-label" transform="rotate(-90, ${width - margin.right + 45}, ${baseline + half / 2})" x="${width - margin.right + 45}" y="${baseline + half / 2}" text-anchor="middle">Credits ($)</text>`;

  const barW = Math.max(6, slot - 8);
  const halfW = barW / 2;

  labels.forEach((label, i) => {
    const cx = margin.left + i * slot + slot / 2;
    // kWh bar starts at the left half of the slot
    const xKwh = cx - halfW / 2;
    // cost bar sits in the right half
    const xCost = cx + halfW / 2;

    // kWh bars: left half
    let kwhDownCum = 0;
    let kwhUpCum = 0;
    kwhCategories.forEach((reg) => {
      const val = kwhSeries[reg][i];
      if (!val) return;
      const h = kwhY(val);
      if (exportRegisters.has(reg)) {
        const y = baseline - kwhUpCum - h;
        kwhUpCum += h;
        svg += `<rect x="${xKwh}" y="${y}" width="${halfW}" height="${h}" fill="${colorForRegister(reg)}" opacity="0.9" rx="1">
          <title>Week starting ${label}\n${reg} exported: ${fmt(val)} kWh</title>
        </rect>`;
      } else {
        const y = baseline + kwhDownCum;
        kwhDownCum += h;
        svg += `<rect x="${xKwh}" y="${y}" width="${halfW}" height="${h}" fill="${colorForRegister(reg)}" opacity="0.9" rx="1">
          <title>Week starting ${label}\n${reg} usage: ${fmt(val)} kWh</title>
        </rect>`;
      }
    });

    // cost bars: right half
    let costUpCum = 0;
    let costDownCum = 0;
    costCategories.forEach((cat) => {
      const val = costSeries[cat][i];
      if (!val) return;
      const h = costY(val);
      if (exportCostCats.has(cat)) {
        const y = baseline + costDownCum;
        costDownCum += h;
        svg += `<rect x="${xCost}" y="${y}" width="${halfW}" height="${h}" fill="${costColor(cat)}" opacity="0.9" rx="1">
          <title>Week starting ${label}\n${cat} credit: ${fmtMoney(val)}</title>
        </rect>`;
      } else {
        const y = baseline - costUpCum - h;
        costUpCum += h;
        svg += `<rect x="${xCost}" y="${y}" width="${halfW}" height="${h}" fill="${costColor(cat)}" opacity="0.9" rx="1">
          <title>Week starting ${label}\n${cat}: ${fmtMoney(val)}</title>
        </rect>`;
      }
    });
  });

  // x-axis week labels
  const labelY = baseline + half + 16;
  labels.forEach((label, i) => {
    const cx = margin.left + i * slot + slot / 2;
    if (labels.length > 30 && i % 2 !== 0) return;
    svg += `<text class="weekly-x-label" transform="rotate(-45, ${cx}, ${labelY})" x="${cx}" y="${labelY}">${label}</text>`;
  });

  svg += `</svg>`;

  const chartDiv = document.createElement("div");
  chartDiv.innerHTML = svg;
  wrap.appendChild(chartDiv);

  const legend = document.createElement("div");
  legend.className = "weekly-legend";
  legend.innerHTML = `
    ${kwhCategories.map((reg) => `<span><span class="legend-swatch" style="background:${colorForRegister(reg)}"></span>${reg} kWh</span>`).join("")}
    ${costCategories.map((cat) => `<span><span class="legend-swatch" style="background:${costColor(cat)}"></span>${cat}</span>`).join("")}
  `;
  wrap.appendChild(legend);

  return wrap;
}

/* ----------------- end weekly chart renderer ----------------- */

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorDiv.innerHTML = "";
  resultsDiv.innerHTML = "";
  spinner.style.display = "block";

  const formData = new FormData(form);

  try {
    const resp = await fetch("/calculate", { method: "POST", body: formData });
    const data = await resp.json();

    spinner.style.display = "none";

    if (!resp.ok || data.error) {
      errorDiv.innerHTML = `Error: ${data.error || "Unknown error"}`;
      if (data.output) {
        resultsDiv.innerHTML = `<div class="console-output">${data.output}</div>`;
      }
      return;
    }

    if (data.comparison || data.comparison_note) {
      resultsDiv.appendChild(renderComparison(data.comparison || [], data.comparison_note));
    }

    for (const cfg of data.configs) {
      const block = document.createElement("div");
      block.className = "config-block";
      const title = cfg.label === "config1" ? "Plan A" : "Plan B";
      let html = `<h3>${title}: ${cfg.plan_name}</h3>`;
      html += `<div class="info">Config file: ${cfg.config_file}</div>`;

      if (cfg.files && cfg.files.length > 0) {
        html += `<div class="downloads">
          <a class="zip" href="${cfg.zip_url}">Download all (${cfg.zip_name})</a>`;
        for (const f of cfg.files) {
          html += `<a href="${f.download_url}">${f.filename}</a>`;
        }
        html += `</div>`;
      }

      html += `<div class="console-output">${cfg.output}</div>`;
      block.innerHTML = html;

      if (cfg.load_profiles) {
        for (const [register, profile] of Object.entries(cfg.load_profiles)) {
          block.appendChild(renderLoadProfile(register, profile));
        }
      }

      if (cfg.weekly_chart) {
        block.appendChild(
          renderWeeklyChart(cfg.weekly_chart, `Weekly usage & cost — ${cfg.plan_name}`)
        );
      }

      resultsDiv.appendChild(block);
    }
  } catch (err) {
    spinner.style.display = "none";
    errorDiv.innerHTML = `Error: ${err.message}`;
  }
});
