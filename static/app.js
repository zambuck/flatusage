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
      resultsDiv.appendChild(block);
    }
  } catch (err) {
    spinner.style.display = "none";
    errorDiv.innerHTML = `Error: ${err.message}`;
  }
});
