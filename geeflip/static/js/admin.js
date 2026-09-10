(() => {
  const form = document.getElementById('browser-form');
  const summary = document.getElementById('browser-summary');
  const cookieBox = document.getElementById('cookie-box');
  const cookieState = document.getElementById('cookie-state');
  const cookieNames = document.getElementById('cookie-names');
  const catalogState = document.getElementById('catalog-state');

  const CHECKBOXES = [
    'headless', 'use_installed_chrome', 'disable_gpu', 'low_memory',
    'block_images', 'block_fonts',
  ];
  const NUMBERS = [
    'window_width', 'window_height', 'restart_every',
    'restart_pause_seconds', 'page_timeout_ms', 'results_timeout_ms',
  ];

  function fill(settings) {
    CHECKBOXES.forEach((name) => {
      form.elements[name].checked = Boolean(settings[name]);
    });
    NUMBERS.forEach((name) => {
      form.elements[name].value = settings[name] ?? '';
    });
    form.elements.extra_args.value = (settings.extra_args || []).join('\n');
  }

  function read() {
    const settings = { extra_args: form.elements.extra_args.value };
    CHECKBOXES.forEach((name) => {
      settings[name] = form.elements[name].checked;
    });
    NUMBERS.forEach((name) => {
      settings[name] = form.elements[name].value;
    });
    return settings;
  }

  document.querySelectorAll('.preset').forEach((button) => {
    button.addEventListener('click', () => {
      const preset = window.GEEFLIP_PRESETS[button.dataset.preset];
      if (!preset) return;
      fill(preset.values);
      GEEFLIP.toast(`${preset.label} loaded — press Save settings to keep it`);
    });
  });

  document.getElementById('browser-save').addEventListener('click', async () => {
    try {
      const payload = await GEEFLIP.put('/api/browser', read());
      fill(payload.browser);
      summary.textContent = payload.summary;
      GEEFLIP.toast(
        payload.restart_needed
          ? 'Saved — the running scrape keeps its old browser until it finishes'
          : 'Browser settings saved',
      );
    } catch (error) {
      GEEFLIP.toast(error.message);
    }
  });

  document.getElementById('browser-reset').addEventListener('click', async () => {
    try {
      const payload = await GEEFLIP.post('/api/browser/reset');
      fill(payload.browser);
      summary.textContent = payload.summary;
      GEEFLIP.toast('Defaults restored');
    } catch (error) {
      GEEFLIP.toast(error.message);
    }
  });

  function showCookie(payload) {
    cookieBox.value = payload.cookie || '';
    cookieState.textContent = payload.present
      ? `${GEEFLIP.count(payload.characters)} characters, ${payload.names.length} values`
      : 'No cookie saved';
    cookieNames.textContent = (payload.names || []).join(', ');
  }

  async function saveCookie(value) {
    try {
      showCookie(await GEEFLIP.post('/api/cookie', { cookie: value }));
      GEEFLIP.toast(value ? 'Cookie saved' : 'Cookie cleared');
    } catch (error) {
      GEEFLIP.toast(error.message);
    }
  }

  document.getElementById('cookie-save').addEventListener('click', () => {
    saveCookie(cookieBox.value.trim());
  });

  document.getElementById('cookie-clear').addEventListener('click', () => {
    saveCookie('');
  });

  document.getElementById('reimport').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    catalogState.textContent = 'Reading the Excel files…';
    try {
      const payload = await GEEFLIP.post('/api/catalog/reimport');
      catalogState.textContent =
        `${GEEFLIP.count(payload.products)} rows, ${GEEFLIP.count(payload.images)} Amazon photos`;
      GEEFLIP.toast('Catalog reimported');
    } catch (error) {
      catalogState.textContent = '';
      GEEFLIP.toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  async function pollStatus() {
    try {
      const status = await GEEFLIP.get('/api/status');
      GEEFLIP.setLamp(status.running ? 'running' : 'idle', status.running ? 'scraping' : 'idle');
    } catch (error) {
      GEEFLIP.setLamp('error', 'offline');
    }
  }

  fill(window.GEEFLIP_BROWSER || {});
  pollStatus();
  setInterval(pollStatus, 4000);
})();
