const NUMBER_FIELDS = [
  "start_row",
  "end_row",
  "limit",
  "min_roi_percent",
  "max_roi_percent",
  "max_listing_age_days",
  "min_seller_reviews",
  "min_sales_rank",
  "max_sales_rank",
  "min_drop_count",
  "min_buybox_price",
  "winner_history_retention_days",
  "identifier_no_match_retention_days",
];

const BOOL_FIELDS = [
  "upc_as_well",
  "cleaned_title_as_well",
  "titles_only",
  "image_search",
  "skip_previously_won",
];

const form = document.getElementById("filters-form");
const terminal = document.getElementById("terminal");
const startBtn = document.getElementById("start-btn");
const stopBtn = document.getElementById("stop-btn");
const statusLamp = document.getElementById("status-lamp");
const statusText = document.getElementById("status-text");
const previewLine = document.getElementById("preview-line");
const winnersBody = document.getElementById("winners-body");
const winnersCount = document.getElementById("winners-count");

let lastLogId = 0;
let stickToBottom = true;
let previewTimer = null;
let currentRunId = null;

function emptyToNull(value) {
  const trimmed = String(value ?? "").trim();
  return trimmed === "" ? null : trimmed;
}

function readFilters() {
  const data = {};
  const fields = new FormData(form);
  for (const [name, value] of fields.entries()) {
    data[name] = value;
  }
  for (const name of NUMBER_FIELDS) {
    const raw = emptyToNull(data[name]);
    data[name] = raw === null ? null : Number(raw);
  }
  for (const name of BOOL_FIELDS) {
    data[name] = form.elements[name].checked;
  }
  data.include_brands = String(data.include_brands || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  data.exclude_brands = String(data.exclude_brands || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  return data;
}

function fillFilters(filters) {
  for (const name of NUMBER_FIELDS) {
    form.elements[name].value = filters[name] ?? "";
  }
  form.elements.include_brands.value = (filters.include_brands || []).join(", ");
  form.elements.exclude_brands.value = (filters.exclude_brands || []).join(", ");
  for (const name of BOOL_FIELDS) {
    form.elements[name].checked = Boolean(filters[name]);
  }
}

function appendLog(item) {
  const line = document.createElement("div");
  const text = item.text || "";
  line.innerHTML = `<span class="dim">${item.ts || ""}</span> ${escapeHtml(text)}`;
  if (/error|stopped by|failed/i.test(text)) line.classList.add("err");
  terminal.appendChild(line);
  if (stickToBottom) terminal.scrollTop = terminal.scrollHeight;
}

function setStatus(snapshot) {
  const status = snapshot.status || "idle";
  currentRunId = snapshot.run_id;
  statusLamp.dataset.state = status;
  statusText.textContent = status;
  startBtn.disabled = status === "running" || status === "stopping";
  stopBtn.disabled = status !== "running" && status !== "stopping";
  const products = snapshot.catalog?.products ?? 0;
  document.getElementById("stat-products").textContent = products.toLocaleString();
  document.getElementById("stat-winners").textContent = (
    snapshot.winners_total ?? 0
  ).toLocaleString();
  document.getElementById("stat-run-winners").textContent = (
    snapshot.winners_run ?? 0
  ).toLocaleString();
}

function renderWinners(payload) {
  const rows = payload.winners || [];
  winnersCount.textContent = `${payload.count ?? rows.length} in SQLite`;
  if (!rows.length) {
    winnersBody.innerHTML = `<p class="empty-note">No winners stored yet.</p>`;
    return;
  }
  winnersBody.innerHTML = rows
    .map((row) => winnerCard(row, { showScraped: true }))
    .join("");
}

async function previewFilters() {
  try {
    const payload = await fetchJson("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readFilters()),
    });
    previewLine.textContent = payload.details || `${payload.searches} searches`;
  } catch (error) {
    previewLine.textContent = error.message;
  }
}

let liveSocket = null;
let reconnectTimer = null;

function connectLive() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (liveSocket) {
    liveSocket.onclose = null;
    liveSocket.close();
    liveSocket = null;
  }
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  liveSocket = new WebSocket(`${protocol}//${location.host}/ws/live?after=${lastLogId}`);
  liveSocket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === "logs") {
      for (const item of message.logs || []) {
        lastLogId = Math.max(lastLogId, item.id || 0);
        appendLog(item);
      }
    } else if (message.type === "status") {
      setStatus(message.status);
    } else if (message.type === "winners") {
      renderWinners(message);
    }
  };
  liveSocket.onclose = () => {
    reconnectTimer = setTimeout(connectLive, 1500);
  };
}

terminal.addEventListener("scroll", () => {
  const remaining = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight;
  stickToBottom = remaining < 32;
});

document.getElementById("clear-logs").addEventListener("click", () => {
  terminal.innerHTML = "";
});

startBtn.addEventListener("click", async () => {
  startBtn.disabled = true;
  try {
    const snapshot = await fetchJson("/api/scrape/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(readFilters()),
    });
    setStatus(snapshot);
  } catch (error) {
    appendLog({ ts: "", text: `Could not start: ${error.message}` });
    startBtn.disabled = false;
  }
});

stopBtn.addEventListener("click", async () => {
  try {
    const snapshot = await fetchJson("/api/scrape/stop", { method: "POST" });
    setStatus(snapshot);
  } catch (error) {
    appendLog({ ts: "", text: `Could not stop: ${error.message}` });
  }
});

form.addEventListener("input", () => {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(previewFilters, 450);
});

async function boot() {
  bindSeenClicks(winnersBody);
  const filters = await fetchJson("/api/filters");
  fillFilters(filters);
  await previewFilters();
  connectLive();
}

boot().catch((error) => {
  previewLine.textContent = error.message;
  appendLog({ ts: "", text: `Startup failed: ${error.message}` });
});
