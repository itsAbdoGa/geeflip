function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll('"', "&quot;");
}

function formatMoney(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (Number.isNaN(number)) return String(value);
  return `$${number.toFixed(2)}`;
}

function formatRoi(value) {
  if (value === null || value === undefined || value === "") return "—";
  return `${Number(value).toFixed(1)}%`;
}

function formatTime(value) {
  if (!value) return "";
  return String(value).replace("T", " ");
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail || response.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

function photoFigure(url, href, label) {
  const img = url
    ? `<img src="${escapeAttr(url)}" alt="${escapeAttr(label)}" referrerpolicy="no-referrer">`
    : `<div class="thumb-empty">no photo</div>`;
  const frame = href
    ? `<a class="thumb" href="${escapeAttr(href)}" target="_blank" rel="noreferrer">${img}</a>`
    : `<div class="thumb">${img}</div>`;
  return `<figure>${frame}<figcaption>${escapeHtml(label)}</figcaption></figure>`;
}

function winnerCard(row, { showScraped = true } = {}) {
  const seen = Boolean(row.seen);
  const mismatched = Boolean(row.mismatched);
  const ebayUrl = row["EBAY listing URL"] || "";
  const amazonUrl = row["AMAZON URL"] || "";
  const ebayTitle = row["EBAY listing title"] || row.title || "Untitled listing";
  const scraped = row.found_at || row.scrape_started_at || row.first_won_at || "";
  const classes = ["winner-card"];
  if (seen) classes.push("seen");
  if (mismatched) classes.push("mismatched");
  const titleWrap = ebayUrl
    ? `<a href="${escapeAttr(ebayUrl)}" target="_blank" rel="noreferrer">${escapeHtml(ebayTitle)}</a>`
    : escapeHtml(ebayTitle);
  const amazon = amazonUrl
    ? `<a href="${escapeAttr(amazonUrl)}" target="_blank" rel="noreferrer">${escapeHtml(row.ASIN || "Amazon")}</a>`
    : escapeHtml(row.ASIN || "");
  const mismatchCount = Number(row.asin_mismatch_count || 0);
  return `<article class="${classes.join(" ")}" data-id="${row.id}">
    <div class="compare">
      ${photoFigure(row["AMAZON image URL"] || "", amazonUrl, "Amazon")}
      ${photoFigure(row["EBAY image URL"] || "", ebayUrl, "eBay")}
    </div>
    <div class="winner-copy">
      <h3>${titleWrap}</h3>
      <p class="meta">${escapeHtml(row.title || "")}</p>
      <p class="nums">
        <strong>${escapeHtml(formatRoi(row.ROI))}</strong>
        · eBay ${escapeHtml(formatMoney(row["EBAY full cost"]))}
        · buy box ${escapeHtml(formatMoney(row.BUYBOX))}
        · ${escapeHtml(row.Brand || "—")}
        · ${escapeHtml(row.SELLER || "—")}${row["SELLER REVIEWS"] ? ` (${row["SELLER REVIEWS"]})` : ""}
        ${amazon ? ` · ${amazon}` : ""}
        ${showScraped && scraped ? ` · scraped ${escapeHtml(formatTime(scraped))}` : ""}
        ${mismatchCount ? ` · ASIN mismatches ${mismatchCount}` : ""}
      </p>
    </div>
    <div class="winner-actions">
      <button type="button" class="seen-btn" data-id="${row.id}" data-seen="${seen ? "1" : "0"}">
        ${seen ? "Seen" : "Mark seen"}
      </button>
      <button type="button" class="mismatch-btn" data-id="${row.id}" data-mismatched="${mismatched ? "1" : "0"}">
        ${mismatched ? "Mismatched" : "Mismatch"}
      </button>
    </div>
  </article>`;
}

function replaceWinnerCard(button, row) {
  const card = button.closest(".winner-card");
  if (!card) return row;
  card.outerHTML = winnerCard(row, { showScraped: true });
  return row;
}

async function toggleSeen(button) {
  const id = Number(button.dataset.id);
  const nextSeen = button.dataset.seen !== "1";
  const row = await fetchJson(`/api/winners/${id}/seen`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ seen: nextSeen }),
  });
  return replaceWinnerCard(button, row);
}

async function toggleMismatch(button) {
  const id = Number(button.dataset.id);
  const next = button.dataset.mismatched !== "1";
  const row = await fetchJson(`/api/winners/${id}/mismatch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mismatched: next }),
  });
  return replaceWinnerCard(button, row);
}

function bindSeenClicks(root) {
  root.addEventListener("click", async (event) => {
    const mismatchBtn = event.target.closest(".mismatch-btn");
    const seenBtn = event.target.closest(".seen-btn");
    const button = mismatchBtn || seenBtn;
    if (!button) return;
    button.disabled = true;
    try {
      if (mismatchBtn) await toggleMismatch(mismatchBtn);
      else await toggleSeen(seenBtn);
    } catch (error) {
      button.disabled = false;
      window.alert(error.message);
    }
  });
}
