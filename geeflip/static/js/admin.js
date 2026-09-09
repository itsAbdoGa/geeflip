const statusLine = document.getElementById("cookie-status");
const input = document.getElementById("cookie-input");
const saveBtn = document.getElementById("save-cookie");
const saveMsg = document.getElementById("cookie-save-msg");

function describeCookie(payload) {
  if (!payload.present) return "No cookie saved yet.";
  const names = (payload.cookie_names || []).join(", ");
  return `Saved (${payload.characters.toLocaleString()} characters)${names ? ` · ${names}` : ""}`;
}

async function loadCookie() {
  const payload = await fetchJson("/api/admin/cookie");
  statusLine.textContent = describeCookie(payload);
  input.value = payload.cookie || "";
}

saveBtn.addEventListener("click", async () => {
  saveBtn.disabled = true;
  saveMsg.textContent = "Saving…";
  try {
    const payload = await fetchJson("/api/admin/cookie", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookie: input.value }),
    });
    statusLine.textContent = describeCookie(payload);
    saveMsg.textContent = payload.present ? "Cookie updated." : "Cookie cleared.";
  } catch (error) {
    saveMsg.textContent = error.message;
  } finally {
    saveBtn.disabled = false;
  }
});

loadCookie().catch((error) => {
  statusLine.textContent = error.message;
});
