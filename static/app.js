const state = { records: [], context: [], meals: [], transfers: [], status: null, demoBpRevealed: false };
const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), "X-Local-Vitals": "1" };
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) throw new Error(payload.error || "Request failed");
  return payload;
}

function formatTime(value, short = false) {
  if (!value) return "Time unavailable";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return short
    ? date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : date.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[character]);
}

function busy(button, value, label) {
  if (value) {
    button.dataset.original = button.textContent;
    button.textContent = label;
  } else {
    button.textContent = button.dataset.original || button.textContent;
  }
  button.disabled = value;
}

function metric(day, name) { return day?.health?.[name]; }
function metricNumber(day, name, digits = 0) {
  const sample = metric(day, name);
  if (!sample) return "—";
  return Number(sample.value).toLocaleString(undefined, { maximumFractionDigits: digits });
}

function recordClass(record) {
  return record.source_model?.startsWith("Synthetic") ? "synthetic" : "real";
}

function renderBloodPressure() {
  const isDemo = Boolean(state.status?.demo_mode);
  const records = isDemo
    ? state.records.filter((record) => record.source_model === "Synthetic button · demo only").slice(0, 1)
    : state.records;
  $("recordCount").textContent = isDemo
    ? (state.demoBpRevealed ? "1 demo reading" : "Ready for demo")
    : `${records.length} records`;
  if (records[0] && (!isDemo || state.demoBpRevealed)) {
    const record = records[0];
    $("latestTime").textContent = formatTime(record.timestamp);
    $("latestSys").textContent = record.systolic;
    $("latestDia").textContent = record.diastolic;
    $("latestPulse").textContent = record.pulse ?? "—";
    $("latestSource").className = `badge ${recordClass(record)}`;
    $("latestSource").textContent = recordClass(record) === "synthetic" ? "Synthetic demo reading" : "Local device reading";
  } else if (isDemo) {
    $("latestTime").textContent = "Click Syn BP data";
    $("latestSys").textContent = "—";
    $("latestDia").textContent = "—";
    $("latestPulse").textContent = "—";
    $("latestSource").className = "badge";
    $("latestSource").textContent = "Waiting for sample";
  }
  const visibleRecords = isDemo && !state.demoBpRevealed ? [] : records;
  $("recentReadings").innerHTML = visibleRecords.length
    ? visibleRecords.slice(0, 6).map((record) => `
      <div class="reading-chip">
        <strong>${escapeHtml(record.systolic)}/${escapeHtml(record.diastolic)} · ${escapeHtml(record.pulse ?? "—")}</strong>
        <span>${escapeHtml(formatTime(record.timestamp))}</span>
      </div>`).join("")
    : '<p class="empty">No readings.</p>';
}

function renderMeals() {
  $("mealHistory").innerHTML = state.meals.length
    ? state.meals.slice(0, 6).map((meal) => {
      const range = meal.energy_low != null ? `${Math.round(meal.energy_low)}–${Math.round(meal.energy_high)} kcal` : "Nutrition pending";
      return `<div class="meal-item">
        <span class="meal-time">${escapeHtml(formatTime(meal.eaten_at, true))}</span>
        <div><strong>${escapeHtml(meal.description)}</strong><small>${escapeHtml(range)}</small></div>
        <span class="badge ${escapeHtml(meal.data_class)}">${meal.data_class === "synthetic" ? "Synthetic" : "Manual"}</span>
      </div>`;
    }).join("")
    : '<p class="empty">No meals logged.</p>';
}

function renderApple() {
  const latest = state.context[0];
  $("appleSteps").textContent = metricNumber(latest, "step_count");
  $("appleExercise").textContent = metricNumber(latest, "exercise_minutes");
  $("appleResting").textContent = metricNumber(latest, "resting_heart_rate");
  $("appleHrv").textContent = metricNumber(latest, "hrv_sdnn", 1);
  $("appleSleep").textContent = metric(latest, "sleep_duration") ? `${metricNumber(latest, "sleep_duration", 1)}h` : "—";
  const source = $("appleSource");
  if (latest?.classes?.includes("synthetic")) {
    source.className = "badge synthetic";
    source.textContent = "Synthetic demo context";
  } else if (latest) {
    source.className = "badge real";
    source.textContent = "Local health data";
  } else {
    source.className = "badge";
    source.textContent = "Awaiting Apple import";
  }
}

function renderTransfers() {
  const rows = state.transfers.flatMap((batch) => batch.records.map((record) => ({ batch, record })));
  $("transfersBody").innerHTML = rows.length ? rows.map(({ batch, record }) => `
    <tr><td>${escapeHtml(formatTime(batch.received_at))}</td><td>${escapeHtml(formatTime(record.timestamp))}</td>
    <td><strong>${escapeHtml(record.systolic)}/${escapeHtml(record.diastolic)}</strong></td><td>${escapeHtml(record.pulse ?? "—")}</td>
    <td>${record.was_inserted ? "New" : "Duplicate"}</td><td class="raw-data">${escapeHtml(record.raw_hex || "Unavailable")}</td></tr>
  `).join("") : '<tr><td colspan="6" class="empty">No transfer batches in this database.</td></tr>';
}

function renderStatus() {
  const status = state.status;
  $("dataPath").textContent = `Database: ${status.data_path}`;
  if (status.demo_mode) {
    $("demoBanner").hidden = false;
    $("demoSummary").textContent = `${status.demo_manifest?.synthetic_bp || 0} synthetic BP records and ${status.demo_manifest?.synthetic_meals || 0} synthetic meals. No real BP values are shown.`;
    $("syncButton").hidden = true;
    $("syntheticButton").hidden = false;
    $("syntheticButton").textContent = "Syn BP data";
    $("deviceState").textContent = "Ready";
    $("deviceDetails").textContent = "Click Syn BP data to load a sample reading";
  } else {
    $("syncButton").hidden = false;
    $("syntheticButton").hidden = true;
  }
}

function render() {
  renderStatus();
  renderBloodPressure();
  renderMeals();
  renderApple();
  renderTransfers();
}

async function refresh() {
  const [records, context, meals, transfers, status] = await Promise.all([
    api("/api/records"), api("/api/context"), api("/api/meals"), api("/api/transfers"), api("/api/status")
  ]);
  state.records = records.records;
  state.context = context.days;
  state.meals = meals.meals;
  state.transfers = transfers.batches;
  state.status = status;
  render();
}

function addAgentMessage(role, text) {
  const message = document.createElement("div");
  message.className = `message ${role}`;
  const author = document.createElement("span");
  author.textContent = role === "user" ? "You" : "LocalVitals Agent";
  const body = document.createElement("p");
  body.textContent = text;
  message.append(author, body);
  $("agentMessages").appendChild(message);
  $("agentMessages").scrollTop = $("agentMessages").scrollHeight;
}

$("agentForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const prompt = $("agentInput").value.trim();
  if (!prompt) return;
  addAgentMessage("user", prompt);
  $("agentInput").value = "";
  window.setTimeout(() => addAgentMessage("assistant", `LLM endpoint reserved. This request would receive a compact local summary of ${state.records.length} BP records, ${state.meals.length} meals, and ${state.context.length} health-context days. Nothing was sent.`), 150);
});

document.querySelectorAll("[data-agent-prompt]").forEach((button) => {
  button.addEventListener("click", () => {
    $("agentInput").value = button.dataset.agentPrompt;
    $("agentInput").focus();
  });
});

$("syntheticButton").addEventListener("click", async () => {
  const button = $("syntheticButton");
  busy(button, true, "Generating…");
  try {
    await api("/api/demo/synthetic-reading", { method: "POST", body: "{}" });
    state.demoBpRevealed = true;
    $("syncMessage").textContent = "BP synced: 119/80 mmHg · pulse 83.";
    await refresh();
  } catch (error) {
    $("syncMessage").textContent = error.message;
  } finally {
    busy(button, false);
  }
});

$("syncButton").addEventListener("click", async () => {
  const button = $("syncButton");
  $("deviceState").textContent = "Listening";
  $("deviceDetails").textContent = "Take a measurement now; LocalVitals is waiting for the BP7255";
  busy(button, true, "Listening…");
  try {
    const address = localStorage.getItem("localVitalsDeviceAddress");
    const result = await api("/api/device/sync", { method: "POST", body: JSON.stringify({ address }) });
    localStorage.setItem("localVitalsDeviceAddress", result.device.address);
    $("deviceState").textContent = "Synced";
    $("syncMessage").textContent = `Saved ${result.inserted} new reading locally; ignored ${result.duplicates} duplicate transmissions.`;
    await refresh();
  } catch (error) {
    $("deviceState").textContent = "Ready";
    $("syncMessage").textContent = error.message;
  } finally {
    busy(button, false);
  }
});

let mealPreviewUrl = null;
$("mealPhoto").addEventListener("change", (event) => {
  const file = event.target.files[0];
  if (!file) return;
  if (mealPreviewUrl) URL.revokeObjectURL(mealPreviewUrl);
  mealPreviewUrl = URL.createObjectURL(file);
  $("mealPreview").src = mealPreviewUrl;
  $("mealPreview").hidden = false;
  $("mealEmpty").hidden = true;
  $("mealStatus").textContent = `${file.name} · previewed locally, not uploaded`;
});

$("addMealButton").addEventListener("click", async () => {
  const description = $("mealDescription").value.trim();
  if (!description) {
    $("mealStatus").textContent = "Add a short description before saving the meal note.";
    return;
  }
  const button = $("addMealButton");
  busy(button, true, "Saving…");
  try {
    await api("/api/meals", {
      method: "POST",
      body: JSON.stringify({ description, photo_name: $("mealPhoto").files[0]?.name || null })
    });
    $("mealDescription").value = "";
    $("mealStatus").textContent = "Meal note saved locally. Nutrition remains blank until a model estimate is confirmed.";
    await refresh();
  } catch (error) {
    $("mealStatus").textContent = error.message;
  } finally {
    busy(button, false);
  }
});

refresh().catch((error) => {
  $("syncMessage").textContent = error.message;
});
