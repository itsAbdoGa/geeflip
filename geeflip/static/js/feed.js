(() => {
  const form = document.getElementById('filters-form');
  const terminal = document.getElementById('terminal');
  const feed = document.getElementById('feed');
  const template = document.getElementById('winner-card');

  const startBtn = document.getElementById('start-btn');
  const stopBtn = document.getElementById('stop-btn');
  const autoscroll = document.getElementById('autoscroll');

  const feedSearch = document.getElementById('feed-search');
  const feedReview = document.getElementById('feed-review');
  const feedThisRun = document.getElementById('feed-this-run');
  const feedCount = document.getElementById('feed-count');
  const feedPage = document.getElementById('feed-page');
  const prevBtn = document.getElementById('feed-prev');
  const nextBtn = document.getElementById('feed-next');

  const PAGE_SIZE = 20;
  let lastLogId = 0;
  let offset = 0;
  let total = 0;
  let currentRunId = null;
  let lastWinnerCount = -1;

  // ---------------------------------------------------------------- filters

  const CHECKBOXES = new Set(
    Array.from(form.querySelectorAll('input[type="checkbox"]')).map((input) => input.name)
  );

  function fillForm(filters) {
    Object.entries(filters).forEach(([name, value]) => {
      const field = form.elements[name];
      if (!field) return;
      if (CHECKBOXES.has(name)) field.checked = Boolean(value);
      else if (Array.isArray(value)) field.value = value.join(', ');
      else field.value = value === null ? '' : value;
    });
  }

  function readForm() {
    const filters = {};
    Array.from(form.elements).forEach((field) => {
      if (!field.name) return;
      if (CHECKBOXES.has(field.name)) filters[field.name] = field.checked;
      else if (field.name.endsWith('_brands')) filters[field.name] = field.value;
      else filters[field.name] = field.value === '' ? null : field.value;
    });
    return filters;
  }

  // ---------------------------------------------------------------- console

  function appendLogs(lines) {
    if (!lines.length) return;
    const fragment = document.createDocumentFragment();
    lines.forEach((line) => {
      const row = document.createElement('div');
      row.className = logClass(line.text);
      const time = document.createElement('span');
      time.className = 'log-time';
      time.textContent = line.at;
      row.append(time, document.createTextNode(line.text));
      fragment.appendChild(row);
      lastLogId = line.id;
    });
    terminal.appendChild(fragment);
    while (terminal.childElementCount > 2000) terminal.removeChild(terminal.firstElementChild);
    if (autoscroll.checked) terminal.scrollTop = terminal.scrollHeight;
  }

  function logClass(text) {
    const lower = text.toLowerCase();
    if (lower.includes('error') || lower.includes('failed') || lower.includes('stopped')) return 'log-bad';
    if (lower.includes('winner') || lower.includes('won')) return 'log-win';
    if (lower.startsWith('  ')) return 'log-idle';
    return '';
  }

  // ---------------------------------------------------------------- feed

  function renderFeed(payload) {
    total = payload.total;
    feed.replaceChildren();

    if (!payload.winners.length) {
      const note = document.createElement('p');
      note.className = 'empty-note';
      note.textContent = total
        ? 'No listings match these feed filters.'
        : 'No winning listings yet — start a scrape.';
      feed.appendChild(note);
    } else {
      payload.winners.forEach((winner) => feed.appendChild(buildCard(winner)));
    }

    feedCount.textContent = `${GEEFLIP.count(total)} listing${total === 1 ? '' : 's'}`;
    const from = total ? offset + 1 : 0;
    const to = Math.min(offset + PAGE_SIZE, total);
    feedPage.textContent = total ? `${from}–${to} of ${GEEFLIP.count(total)}` : '—';
    prevBtn.disabled = offset === 0;
    nextBtn.disabled = offset + PAGE_SIZE >= total;
  }

  function buildCard(winner) {
    const card = template.content.firstElementChild.cloneNode(true);
    card.dataset.id = winner.id;

    setPhoto(card.querySelector('.photo-ebay'), winner.ebay_image_url);
    setPhoto(card.querySelector('.photo-amazon'), winner.amazon_image_url);

    card.querySelector('.card-title').textContent =
      winner.ebay_title || winner.title || '(untitled listing)';

    const bits = [winner.asin, winner.brand, winner.ean && `EAN ${winner.ean}`]
      .filter(Boolean)
      .join('  ·  ');
    card.querySelector('.card-sub').textContent = bits || '—';

    card.querySelector('.fig-roi').textContent = GEEFLIP.percent(winner.roi);
    card.querySelector('.fig-buybox').textContent = GEEFLIP.money(winner.buybox);
    card.querySelector('.fig-cost').textContent = GEEFLIP.money(winner.ebay_full_cost);
    card.querySelector('.fig-seller').textContent = winner.seller
      ? `${winner.seller} (${GEEFLIP.count(winner.seller_reviews || 0)})`
      : '—';
    card.querySelector('.fig-listed').textContent = winner.listing_date
      ? GEEFLIP.shortDate(winner.listing_date)
      : '—';
    card.querySelector('.fig-found').textContent = GEEFLIP.shortDate(winner.found_at);

    setLink(card.querySelector('.link-ebay'), winner.ebay_url);
    setLink(card.querySelector('.link-amazon'), winner.amazon_url);

    paintState(card, winner);
    return card;
  }

  function setPhoto(image, url) {
    if (url) {
      image.src = url;
      image.classList.remove('missing');
      image.onerror = () => {
        image.removeAttribute('src');
        image.classList.add('missing');
      };
    } else {
      image.removeAttribute('src');
      image.classList.add('missing');
    }
  }

  function setLink(anchor, url) {
    if (url) {
      anchor.href = url;
      anchor.classList.remove('disabled');
    } else {
      anchor.removeAttribute('href');
      anchor.classList.add('disabled');
    }
  }

  function paintState(card, winner) {
    card.classList.toggle('is-seen', winner.seen);
    card.classList.toggle('is-matched', winner.verdict === 'matched');
    card.classList.toggle('is-mismatched', winner.verdict === 'mismatched');
    card.querySelector('[data-action="seen"]').classList.toggle('on', winner.seen);
    card.querySelector('[data-action="matched"]').classList.toggle('on', winner.verdict === 'matched');
    card.querySelector('[data-action="mismatched"]').classList.toggle('on', winner.verdict === 'mismatched');
  }

  async function loadFeed() {
    const params = {
      limit: PAGE_SIZE,
      offset,
      review: feedReview.value,
      q: feedSearch.value.trim(),
    };
    if (feedThisRun.checked && currentRunId) params.run_id = currentRunId;
    try {
      renderFeed(await GEEFLIP.get(`/api/winners?${GEEFLIP.query(params)}`));
    } catch (error) {
      GEEFLIP.toast(`Could not load listings: ${error.message}`);
    }
  }

  // ---------------------------------------------------------------- marking

  feed.addEventListener('click', async (event) => {
    const button = event.target.closest('.mark');
    if (!button) return;
    const card = button.closest('.card');
    const id = card.dataset.id;
    const action = button.dataset.action;
    const wasOn = button.classList.contains('on');

    button.disabled = true;
    try {
      const winner = action === 'seen'
        ? await GEEFLIP.post(`/api/winners/${id}/seen`, { seen: !wasOn })
        // Clicking the active verdict clears it, so a misclick is easy to undo.
        : await GEEFLIP.post(`/api/winners/${id}/verdict`, { verdict: wasOn ? null : action });
      paintState(card, winner);
      refreshStats();
    } catch (error) {
      GEEFLIP.toast(`Could not save: ${error.message}`);
    } finally {
      button.disabled = false;
    }
  });

  // ---------------------------------------------------------------- status

  function applyStatus(status) {
    const running = status.running;
    GEEFLIP.setLamp(status.status === 'idle' && status.error ? 'error' : status.status, status.status);
    startBtn.disabled = running;
    stopBtn.disabled = !running;
    currentRunId = status.run_id;

    document.getElementById('stat-run').textContent = GEEFLIP.count(status.run_winners);
    paintStats(status.stats);

    // The feed only reloads when the winner tally actually moves.
    if (status.stats.winners !== lastWinnerCount) {
      lastWinnerCount = status.stats.winners;
      loadFeed();
    }
  }

  function paintStats(stats) {
    document.getElementById('stat-products').textContent = GEEFLIP.count(stats.products);
    document.getElementById('stat-winners').textContent = GEEFLIP.count(stats.winners);
    document.getElementById('stat-worked').textContent = GEEFLIP.count(stats.worked);
    document.getElementById('stat-matched').textContent = GEEFLIP.count(stats.matched);
    document.getElementById('stat-mismatched').textContent = GEEFLIP.count(stats.mismatched);
  }

  async function refreshStats() {
    try {
      paintStats(await GEEFLIP.get('/api/stats'));
    } catch (error) {
      /* the next poll will catch up */
    }
  }

  async function poll() {
    try {
      applyStatus(await GEEFLIP.get('/api/status'));
      const { logs } = await GEEFLIP.get(`/api/logs?after=${lastLogId}`);
      appendLogs(logs);
    } catch (error) {
      GEEFLIP.setLamp('error', 'offline');
    }
  }

  // ---------------------------------------------------------------- wiring

  startBtn.addEventListener('click', async () => {
    startBtn.disabled = true;
    try {
      applyStatus(await GEEFLIP.post('/api/scrape/start', readForm()));
    } catch (error) {
      GEEFLIP.toast(error.message);
      startBtn.disabled = false;
    }
  });

  stopBtn.addEventListener('click', async () => {
    stopBtn.disabled = true;
    try {
      applyStatus(await GEEFLIP.post('/api/scrape/stop'));
    } catch (error) {
      GEEFLIP.toast(error.message);
    }
  });

  document.getElementById('clear-logs').addEventListener('click', async () => {
    await GEEFLIP.post('/api/logs/clear');
    terminal.replaceChildren();
  });

  document.getElementById('preview-btn').addEventListener('click', async () => {
    const line = document.getElementById('preview-line');
    line.textContent = 'Counting…';
    try {
      const { searches, details } = await GEEFLIP.post('/api/preview', readForm());
      line.textContent = `${GEEFLIP.count(searches)} searches — ${details}`;
    } catch (error) {
      line.textContent = error.message;
    }
  });

  const saveFilters = GEEFLIP.debounce(async () => {
    try {
      await GEEFLIP.put('/api/filters', readForm());
    } catch (error) {
      GEEFLIP.toast(error.message);
    }
  }, 700);
  form.addEventListener('change', saveFilters);
  form.addEventListener('input', saveFilters);
  form.addEventListener('submit', (event) => event.preventDefault());

  const rerun = () => {
    offset = 0;
    loadFeed();
  };
  feedSearch.addEventListener('input', GEEFLIP.debounce(rerun, 350));
  feedReview.addEventListener('change', rerun);
  feedThisRun.addEventListener('change', rerun);

  prevBtn.addEventListener('click', () => {
    offset = Math.max(0, offset - PAGE_SIZE);
    loadFeed();
  });
  nextBtn.addEventListener('click', () => {
    if (offset + PAGE_SIZE < total) {
      offset += PAGE_SIZE;
      loadFeed();
    }
  });

  fillForm(window.GEEFLIP_FILTERS || {});
  loadFeed();
  poll();
  setInterval(poll, 1200);
})();
