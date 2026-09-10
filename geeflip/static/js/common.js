const GEEFLIP = (() => {
  async function request(url, options = {}) {
    const response = await fetch(url, {
      headers: options.body ? { 'Content-Type': 'application/json' } : {},
      ...options,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.error || `${response.status} ${response.statusText}`);
    }
    return payload;
  }

  const get = (url) => request(url);
  const send = (method) => (url, body) =>
    request(url, { method, body: body === undefined ? undefined : JSON.stringify(body) });

  function money(value) {
    if (value === null || value === undefined || value === '') return '—';
    const number = typeof value === 'number' ? value : parseFloat(String(value).replace(/[^0-9.-]/g, ''));
    return Number.isFinite(number) ? `$${number.toFixed(2)}` : String(value);
  }

  function count(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number.toLocaleString() : '—';
  }

  function percent(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `${number.toFixed(0)}%` : '—';
  }

  function shortDate(value) {
    if (!value) return '—';
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value).slice(0, 16).replace('T', ' ');
    return parsed.toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }

  let toastTimer = null;
  function toast(message) {
    let node = document.querySelector('.toast');
    if (!node) {
      node = document.createElement('div');
      node.className = 'toast';
      document.body.appendChild(node);
    }
    node.textContent = message;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.remove(), 5000);
  }

  function debounce(fn, wait = 300) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  function setLamp(state, text) {
    const lamp = document.getElementById('status-lamp');
    const label = document.getElementById('status-text');
    if (lamp) lamp.dataset.state = state;
    if (label) label.textContent = text || state;
  }

  function query(params) {
    const search = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== null && value !== undefined && value !== '') search.set(key, value);
    });
    return search.toString();
  }

  return {
    get,
    post: send('POST'),
    put: send('PUT'),
    money, count, percent, shortDate, toast, debounce, setLamp, query,
  };
})();
