(() => {
  const form = document.getElementById('browse-form');
  const body = document.getElementById('asins-body');
  const countLabel = document.getElementById('asins-count');
  const pageLabel = document.getElementById('asins-page');
  const prevBtn = document.getElementById('asins-prev');
  const nextBtn = document.getElementById('asins-next');

  let offset = 0;
  let total = 0;

  const pageSize = () => Number(form.elements.limit.value) || 50;

  function readFilters() {
    return {
      q: form.elements.q.value.trim(),
      brand: form.elements.brand.value,
      activity: form.elements.activity.value,
      sort: form.elements.sort.value,
      min_rank: form.elements.min_rank.value,
      max_rank: form.elements.max_rank.value,
      min_buybox: form.elements.min_buybox.value,
      max_buybox: form.elements.max_buybox.value,
      limit: pageSize(),
      offset,
    };
  }

  function cell(text, className) {
    const td = document.createElement('td');
    if (className) td.className = className;
    td.textContent = text;
    return td;
  }

  function tally(value, tone) {
    const number = Number(value) || 0;
    const td = document.createElement('td');
    td.className = `num ${number === 0 ? 'tally-none' : tone || ''}`.trim();
    td.textContent = number ? number.toLocaleString() : '0';
    return td;
  }

  function linkCell(asin) {
    const td = document.createElement('td');
    if (asin.amazon_url) td.appendChild(anchor(asin.amazon_url, 'Amazon'));
    if (asin.ebay_search_url) {
      if (td.childElementCount) td.append(' · ');
      td.appendChild(anchor(asin.ebay_search_url, 'eBay'));
    }
    if (!td.childElementCount) td.textContent = '—';
    return td;
  }

  function anchor(href, text) {
    const link = document.createElement('a');
    link.href = href;
    link.target = '_blank';
    link.rel = 'noreferrer noopener';
    link.textContent = text;
    return link;
  }

  function photoCell(url, title) {
    const td = document.createElement('td');
    if (!url) {
      td.textContent = '—';
      return td;
    }
    const image = document.createElement('img');
    image.className = 'thumb';
    image.src = url;
    image.alt = title || '';
    image.loading = 'lazy';
    image.onerror = () => image.replaceWith(document.createTextNode('—'));
    td.appendChild(image);
    return td;
  }

  function render(payload) {
    total = payload.total;
    body.replaceChildren();

    if (!payload.asins.length) {
      const row = document.createElement('tr');
      row.className = 'empty';
      const td = document.createElement('td');
      td.colSpan = 15;
      td.textContent = 'No ASINs match these filters.';
      row.appendChild(td);
      body.appendChild(row);
    }

    payload.asins.forEach((asin) => {
      const row = document.createElement('tr');
      row.append(
        photoCell(asin.amazon_image_url, asin.title),
        cell(asin.source_row, 'num'),
        // How many catalog rows collapsed into this ASIN.
        cell(asin.catalog_rows > 1 ? asin.catalog_rows : '—', 'num'),
        cell(asin.asin || '—', 'mono'),
        cell(asin.title || '—', 'title'),
        cell(asin.brand || '—'),
        cell(asin.buybox_price === null ? '—' : GEEFLIP.money(asin.buybox_price), 'num'),
        cell(asin.sales_rank === null ? '—' : GEEFLIP.count(asin.sales_rank), 'num'),
        cell(asin.drops_count === null ? '—' : GEEFLIP.count(asin.drops_count), 'num'),
        tally(asin.winners_count),
        tally(asin.worked_count),
        tally(asin.matched_count, 'tally-good'),
        tally(asin.mismatched_count, 'tally-bad'),
        cell(asin.last_won_at ? GEEFLIP.shortDate(asin.last_won_at) : '—'),
        linkCell(asin)
      );
      body.appendChild(row);
    });

    const size = pageSize();
    countLabel.textContent = `${GEEFLIP.count(total)} ASIN${total === 1 ? '' : 's'}`;
    const from = total ? offset + 1 : 0;
    pageLabel.textContent = total
      ? `${from}–${Math.min(offset + size, total)} of ${GEEFLIP.count(total)}`
      : '—';
    prevBtn.disabled = offset === 0;
    nextBtn.disabled = offset + size >= total;
  }

  async function load() {
    try {
      render(await GEEFLIP.get(`/api/asins?${GEEFLIP.query(readFilters())}`));
    } catch (error) {
      GEEFLIP.toast(`Could not load ASINs: ${error.message}`);
    }
  }

  const reload = () => {
    offset = 0;
    load();
  };

  form.addEventListener('submit', (event) => event.preventDefault());
  form.addEventListener('change', reload);
  form.elements.q.addEventListener('input', GEEFLIP.debounce(reload, 350));
  form.elements.min_rank.addEventListener('input', GEEFLIP.debounce(reload, 450));
  form.elements.max_rank.addEventListener('input', GEEFLIP.debounce(reload, 450));
  form.elements.min_buybox.addEventListener('input', GEEFLIP.debounce(reload, 450));
  form.elements.max_buybox.addEventListener('input', GEEFLIP.debounce(reload, 450));

  document.getElementById('reset-filters').addEventListener('click', () => {
    form.reset();
    reload();
  });

  prevBtn.addEventListener('click', () => {
    offset = Math.max(0, offset - pageSize());
    load();
  });
  nextBtn.addEventListener('click', () => {
    if (offset + pageSize() < total) {
      offset += pageSize();
      load();
    }
  });

  load();
})();
