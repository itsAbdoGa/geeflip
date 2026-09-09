const PAGE_SIZE = 40;
let offset = 0;
let total = 0;

const body = document.getElementById("history-body");
const count = document.getElementById("history-count");
const pageLabel = document.getElementById("history-page");

bindSeenClicks(body);

function render(payload) {
  total = payload.count || 0;
  const rows = payload.winners || [];
  count.textContent = `${total.toLocaleString()} stored · newest first`;
  if (!rows.length) {
    body.innerHTML = `<p class="empty-note">No winning listings yet.</p>`;
  } else {
    body.innerHTML = rows.map((row) => winnerCard(row, { showScraped: true })).join("");
  }
  const start = total === 0 ? 0 : offset + 1;
  const end = offset + rows.length;
  pageLabel.textContent = `${start}–${end} of ${total.toLocaleString()}`;
  document.getElementById("history-prev").disabled = offset <= 0;
  document.getElementById("history-next").disabled = offset + PAGE_SIZE >= total;
}

async function load() {
  const payload = await fetchJson(`/api/winners?limit=${PAGE_SIZE}&offset=${offset}`);
  render(payload);
}

document.getElementById("history-prev").addEventListener("click", async () => {
  offset = Math.max(0, offset - PAGE_SIZE);
  await load();
});

document.getElementById("history-next").addEventListener("click", async () => {
  offset += PAGE_SIZE;
  await load();
});

load().catch((error) => {
  count.textContent = error.message;
});
