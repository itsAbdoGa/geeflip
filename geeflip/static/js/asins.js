const PAGE_SIZE = 50;
let offset = 0;
let query = "";
let total = 0;

const form = document.getElementById("asins-search");
const body = document.getElementById("asins-body");
const count = document.getElementById("asins-count");
const pageLabel = document.getElementById("asins-page");

function money(value) {
  return formatMoney(value);
}

function render(payload) {
  total = payload.total || 0;
  const rows = payload.asins || [];
  count.textContent = `${total.toLocaleString()} ASINs in SQLite`;
  if (!rows.length) {
    body.innerHTML = `<tr class="empty"><td colspan="9">No matching ASINs.</td></tr>`;
  } else {
    body.innerHTML = rows
      .map((row) => {
        const link = row.amazon_url
          ? `<a href="${escapeAttr(row.amazon_url)}" target="_blank" rel="noreferrer">Amazon</a>`
          : "—";
        const photo = row.amazon_image_url
          ? `<a class="thumb-sm" href="${escapeAttr(row.amazon_url || row.amazon_image_url)}" target="_blank" rel="noreferrer"><img src="${escapeAttr(row.amazon_image_url)}" alt="" referrerpolicy="no-referrer"></a>`
          : `<div class="thumb-sm"></div>`;
        const mismatches = Number(row.mismatch_count || 0);
        return `<tr>
          <td>${photo}</td>
          <td>${escapeHtml(row.source_row ?? "")}</td>
          <td class="mono">${escapeHtml(row.asin || "")}</td>
          <td class="title">${escapeHtml(row.title || "")}</td>
          <td>${escapeHtml(row.brand || "")}</td>
          <td>${escapeHtml(money(row.buybox_price))}</td>
          <td>${row.sales_rank == null ? "—" : Number(row.sales_rank).toLocaleString()}</td>
          <td>${mismatches}</td>
          <td>${link}</td>
        </tr>`;
      })
      .join("");
  }
  const start = total === 0 ? 0 : offset + 1;
  const end = offset + rows.length;
  pageLabel.textContent = `${start.toLocaleString()}–${end.toLocaleString()} of ${total.toLocaleString()}`;
  document.getElementById("asins-prev").disabled = offset <= 0;
  document.getElementById("asins-next").disabled = offset + PAGE_SIZE >= total;
}

async function load() {
  const params = new URLSearchParams({
    q: query,
    offset: String(offset),
    limit: String(PAGE_SIZE),
  });
  const payload = await fetchJson(`/api/asins?${params.toString()}`);
  render(payload);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  query = new FormData(form).get("q") || "";
  offset = 0;
  await load();
});

document.getElementById("asins-prev").addEventListener("click", async () => {
  offset = Math.max(0, offset - PAGE_SIZE);
  await load();
});

document.getElementById("asins-next").addEventListener("click", async () => {
  offset += PAGE_SIZE;
  await load();
});

load().catch((error) => {
  count.textContent = error.message;
});
