(() => {
  const REFRESH_INTERVAL_MS = 10000;
  const state = {
    carparks: null,
    carparksStale: false,
    carparksLastUpdated: null,
    users: null,
    requests: { carparks: false, users: false }
  };
  const $ = (id) => document.getElementById(id);

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, (character) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
    }[character]));
  }

  function addActivity(message, kind = 'ok') {
    const list = $('activity-list');
    const empty = list.querySelector('.activity-muted');
    if (empty) empty.remove();
    const item = document.createElement('div');
    item.className = 'activity-item';
    item.innerHTML = `<span class="activity-icon">${kind === 'error' ? '!' : 'OK'}</span><p>${escapeHtml(message)}<time>${new Date().toLocaleTimeString()}</time></p>`;
    list.prepend(item);
    while (list.children.length > 4) list.lastElementChild.remove();
  }

  function setSystemState() {
    const status = $('system-status');
    const label = $('system-status-label');
    if (state.carparksStale) {
      status.dataset.state = 'stale'; label.textContent = 'Showing stale car park data';
    } else if (state.carparks?.status === 'error' || state.users?.status === 'error') {
      status.dataset.state = 'error'; label.textContent = 'Operational data unavailable';
    } else if (state.carparks?.status === 'partial' || state.users?.error) {
      status.dataset.state = 'degraded'; label.textContent = 'Operating with degraded data';
    } else if (state.carparks && state.users) {
      status.dataset.state = 'healthy'; label.textContent = 'All services reporting';
    }
  }

  function markCarparksStale() {
    state.carparksStale = true;
    const updated = state.carparksLastUpdated
      ? `Last successful update ${state.carparksLastUpdated}`
      : 'No successful car park update yet';
    $('last-updated').textContent = `${updated} / Refresh failed`;
    $('chart-meta').textContent = 'Stale data';
    $('table-refreshed-at').textContent = state.carparksLastUpdated
      ? `Refreshed at ${state.carparksLastUpdated} / Stale`
      : 'Not refreshed yet';
    setSystemState();
  }

  function updateMetrics(data) {
    const rows = data.carparks || [];
    const online = rows.filter((row) => row.status === 'available');
    const spaces = online.reduce((sum, row) => sum + (Number(row.available_spaces) || 0), 0);
    $('total-spaces').textContent = spaces.toLocaleString();
    $('online-carparks').textContent = online.length;
    $('offline-carparks').textContent = rows.length - online.length;
    $('carpark-count-foot').textContent = `${rows.length} configured car parks`;
    $('table-meta').textContent = `${rows.length} locations`;
    $('chart-meta').textContent = `${online.length} reporting`;
  }

  function updateTable(data) {
    const rows = data.carparks || [];
    $('carpark-rows').innerHTML = rows.map((row) => {
      const available = row.status === 'available';
      const spaces = available ? Number(row.available_spaces).toLocaleString() : '--';
      const confidence = available && row.confidence_score != null ? `${(Number(row.confidence_score) * 100).toFixed(1)}%` : '--';
      const recordedAt = row.created_at ? new Date(row.created_at).toLocaleTimeString() : '--';
      const rowAttributes = available
        ? `class="carpark-row" data-carpark-id="${escapeHtml(row.carpark_id)}" tabindex="0" role="button" aria-label="View analysis image for ${escapeHtml(row.name || row.carpark_id)}"`
        : 'class="carpark-row carpark-row-disabled"';
      return `<tr ${rowAttributes}><td>${escapeHtml(row.name || row.carpark_id)}</td><td><span class="status-pill ${available ? 'status-available' : 'status-unavailable'}">${available ? 'Available' : 'Unavailable'}</span></td><td>${spaces}</td><td class="confidence">${confidence}</td><td class="recorded-at">${escapeHtml(recordedAt)}</td></tr>`;
    }).join('') || '<tr><td colspan="5" class="table-placeholder">No car parks configured.</td></tr>';
  }

  async function openCarparkImage(carparkId) {
    const dialog = $('carpark-dialog');
    const image = $('analysis-image');
    $('dialog-title').textContent = carparkId;
    $('image-loading').hidden = false;
    $('image-error').hidden = true;
    $('image-metrics').hidden = true;
    image.hidden = true;
    image.removeAttribute('src');
    dialog.showModal();

    try {
      const response = await fetch(`/api/ops/carparks/${encodeURIComponent(carparkId)}/image`, { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.msg || 'Analysis image is unavailable');
      $('dialog-title').textContent = data.name || data.carpark_id;
      $('image-spaces').textContent = Number(data.available_spaces).toLocaleString();
      $('image-confidence').textContent = `${(Number(data.confidence_score) * 100).toFixed(1)}%`;
      image.alt = `Annotated parking analysis for ${data.name || data.carpark_id}`;
      image.src = `data:image/png;base64,${data.image_base64}`;
      image.hidden = false;
      $('image-metrics').hidden = false;
    } catch (error) {
      $('image-error').textContent = error.message;
      $('image-error').hidden = false;
    } finally {
      $('image-loading').hidden = true;
    }
  }

  function updateChart(data) {
    const rows = (data.carparks || []).filter((row) => row.status === 'available');
    $('chart-empty').hidden = rows.length > 0;
    $('availability-chart').style.display = rows.length > 0 ? 'block' : 'none';
    if (!rows.length || typeof Plotly === 'undefined') return;
    const ordered = [...rows].sort((a, b) => Number(a.available_spaces) - Number(b.available_spaces));
    Plotly.react('availability-chart', [{
      x: ordered.map((row) => Number(row.available_spaces)),
      y: ordered.map((row) => row.carpark_id),
      type: 'bar', orientation: 'h', marker: { color: '#14b8a6' },
      hovertemplate: '%{y}<br><b>%{x}</b> available spaces<extra></extra>'
    }], {
      margin: { l: 75, r: 20, t: 8, b: 42 }, paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
      font: { family: 'DM Sans, sans-serif', color: '#486581', size: 11 },
      xaxis: { title: 'Available spaces', gridcolor: '#e6edf3', zeroline: false },
      yaxis: { automargin: true, gridcolor: 'transparent' }, hoverlabel: { bgcolor: '#102a43' }
    }, { responsive: true, displayModeBar: false });
  }

  async function loadCarparks() {
    if (state.requests.carparks) return;
    state.requests.carparks = true;
    try {
      const response = await fetch('/api/ops/carparks', { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.msg || 'Car park service failed');
      state.carparks = data;
      state.carparksStale = false;
      state.carparksLastUpdated = new Date().toLocaleTimeString();
      updateMetrics(data); updateTable(data); updateChart(data); setSystemState(); $('last-updated').textContent = `Updated ${state.carparksLastUpdated}`; $('table-refreshed-at').textContent = `Refreshed at ${state.carparksLastUpdated}`;
      addActivity(data.status === 'partial' || data.status === 'error' ? 'Some car park analysis is unavailable.' : 'Car park telemetry refreshed.', data.status === 'success' ? 'ok' : 'error');
    } catch (error) {
      addActivity(`Car park telemetry failed: ${error.message}`, 'error');
      if (state.carparks) {
        markCarparksStale();
      } else {
        $('system-status').dataset.state = 'error'; $('system-status-label').textContent = 'Car park service unavailable';
      }
    } finally { state.requests.carparks = false; }
  }

  async function loadUsers() {
    if (state.requests.users) return;
    state.requests.users = true;
    try {
      const response = await fetch('/api/ops/users', { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.msg || 'User activity service failed');
      state.users = data; $('active-users').textContent = Number(data.users_last_30s).toLocaleString(); setSystemState();
    } catch (error) {
      $('active-users').textContent = '--'; state.users = { error: true }; addActivity(`User activity unavailable: ${error.message}`, 'error'); setSystemState();
    } finally { state.requests.users = false; }
  }

  function refreshAll() { loadCarparks(); loadUsers(); }
  $('carpark-rows').addEventListener('click', (event) => {
    const row = event.target.closest('[data-carpark-id]');
    if (row) openCarparkImage(row.dataset.carparkId);
  });
  $('carpark-rows').addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    const row = event.target.closest('[data-carpark-id]');
    if (row) {
      event.preventDefault();
      openCarparkImage(row.dataset.carparkId);
    }
  });
  $('dialog-close').addEventListener('click', () => $('carpark-dialog').close());
  $('carpark-dialog').addEventListener('close', () => $('analysis-image').removeAttribute('src'));
  $('carpark-dialog').addEventListener('click', (event) => {
    if (event.target === $('carpark-dialog')) $('carpark-dialog').close();
  });
  $('refresh-all').addEventListener('click', refreshAll);
  refreshAll();
  window.setInterval(loadUsers, 5000);
  window.setInterval(loadCarparks, REFRESH_INTERVAL_MS);
})();
