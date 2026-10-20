// ==================== REGISTER ZOOM PLUGIN ====================
function registerZoomPlugin() {
  if (typeof Chart === 'undefined') {
    console.error('❌ Chart.js не завантажено');
    return false;
  }
  const plugin = window.ChartZoom || window.chartjsPluginZoom;
  if (!plugin) {
    console.warn('⚠️ Chart.js Zoom plugin не завантажено');
    return false;
  }
  try {
    Chart.register(plugin);
    console.log('✅ Chart.js Zoom plugin зареєстровано');
    return true;
  } catch (e) {
    console.error('❌ Помилка реєстрації zoom plugin:', e);
    return false;
  }
}
const ZOOM_ENABLED = registerZoomPlugin();

// ==================== TIME FORMATTING ====================
const LOCAL_TZ = 'Europe/Kyiv';

function parseUtc(isoStr) {
  if (!isoStr) return null;
  if (!isoStr.endsWith('Z') && !isoStr.includes('+')) isoStr += 'Z';
  return new Date(isoStr);
}

function fmtBoth(isoStr) {
  const d = parseUtc(isoStr);
  if (!d) return '—';
  const local = d.toLocaleTimeString('uk-UA', {
    timeZone: LOCAL_TZ, hour: '2-digit', minute: '2-digit', hour12: false
  });
  const utc = d.toLocaleTimeString('uk-UA', {
    timeZone: 'UTC', hour: '2-digit', minute: '2-digit', hour12: false
  });
  return `${local} <span class="time-utc">(${utc} UTC)</span>`;
}

function fmtBothFull(isoStr) {
  const d = parseUtc(isoStr);
  if (!d) return '—';
  const local = d.toLocaleString('uk-UA', {
    timeZone: LOCAL_TZ, day: '2-digit', month: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false
  }).replace(',', '');
  const utc = d.toLocaleString('uk-UA', {
    timeZone: 'UTC', day: '2-digit', month: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false
  }).replace(',', '');
  return `${local} <span class="time-utc">(${utc} UTC)</span>`;
}

function fmtStatLast(tsStr) {
  if (!tsStr || tsStr === '—') return '—';
  const m = tsStr.match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/);
  if (!m) return tsStr;
  const iso = `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:00Z`;
  const d = new Date(iso);
  const local = d.toLocaleString('uk-UA', {
    timeZone: LOCAL_TZ, day: '2-digit', month: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false
  }).replace(',', '');
  const utc = d.toLocaleTimeString('uk-UA', {
    timeZone: 'UTC', hour: '2-digit', minute: '2-digit', hour12: false
  });
  return `${local} <span class="time-utc">(${utc} UTC)</span>`;
}

// ==================== TABS ====================
document.querySelectorAll('.tab[data-tab]').forEach(t => {
  t.onclick = () => {
    document.querySelectorAll('.tab[data-tab]').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    ['live','dashboard','events','impact','paper','calendar','compare','heatmap','tools','backtest','help'].forEach(name => {
      const el = document.getElementById('tab-' + name);
      if (el) el.style.display = name === t.dataset.tab ? '' : 'none';
    });
    if (t.dataset.tab === 'events') loadEvents();
    if (t.dataset.tab === 'impact') loadImpact();
    if (t.dataset.tab === 'paper') loadPaper();
    if (t.dataset.tab === 'calendar') loadCalendar();
    if (t.dataset.tab === 'compare') loadCompare();
    if (t.dataset.tab === 'heatmap') { loadHeatmap(); loadCrossAsset(); }
    if (t.dataset.tab === 'live') { loadLive(); loadLiveRecent(); }
    if (t.dataset.tab === 'backtest') { loadBacktestTrades(); loadBacktestEquity(); }
  };
});

// ==================== HELPERS ====================
const fmtPct = v => v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(3) + '%';
const fmtUsd = v => v == null ? '—' : '$' + v.toFixed(2);
const fmtUsd4 = v => v == null ? '—' : '$' + v.toFixed(4);
const clsFor = v => v > 0 ? 'up' : v < 0 ? 'down' : 'flat';
const clsForDir = d => d === 'up' ? 'up' : d === 'down' ? 'down' : 'flat';
const badge = imp => `<span class="badge-${imp} px-2 py-0.5 rounded text-xs">${imp}</span>`;

const hitBadge = h => {
  if (!h) return '<span class="badge-na px-2 py-0.5 rounded text-xs">—</span>';
  if (h === 'NO_DATA') return '<span class="badge-na px-2 py-0.5 rounded text-xs" title="Немає forecast/actual">—</span>';
  const cls = {'HIT':'badge-hit','MISS':'badge-miss','NEUTRAL':'badge-neutral','N/A':'badge-na'}[h] || 'badge-na';
  return `<span class="${cls} px-2 py-0.5 rounded text-xs">${h}</span>`;
};

const dirBadge = d => {
  if (d === 'long') return '<span class="badge-long px-2 py-0.5 rounded text-xs">LONG</span>';
  if (d === 'short') return '<span class="badge-short px-2 py-0.5 rounded text-xs">SHORT</span>';
  return '—';
};

const symbolBadge = s => {
  if (s === 'SOLUSDT') return '<span class="badge-sol px-2 py-0.5 rounded text-xs">SOL</span>';
  if (s === 'BTCUSDT') return '<span class="badge-btc px-2 py-0.5 rounded text-xs">BTC</span>';
  if (s === 'ETHUSDT') return '<span class="badge-eth px-2 py-0.5 rounded text-xs">ETH</span>';
  return s || '—';
};

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function metric(label, value, cls, tip) {
  return `<div>
    <div class="text-sm text-slate-400 tip" data-tip="${tip}">${label}</div>
    <div class="text-xl font-mono ${cls}">${value}</div>
  </div>`;
}

// ==================== STATE ====================
let priceChart = null;
let hitChart = null;
let equityChart = null;
let currentChartHours = 6;
let currentPrices = {};
let impactSymbol = 'SOLUSDT';
let tradesByLabel = {};
let eventsByLabelAll = {};
let labelsGlobal = [];
let eventsSearchTimer = null;

// ==================== CHART ZOOM CONTROLS ====================
function zoomChart(factor) {
  if (!priceChart) return;
  if (ZOOM_ENABLED && typeof priceChart.zoomScale === 'function') {
    try { priceChart.zoomScale('x', factor); return; } catch (e) {}
  }
  const xScale = priceChart.scales.x;
  const min = xScale.min ?? 0;
  const max = xScale.max ?? (xScale.labels.length - 1);
  const center = (min + max) / 2;
  const halfRange = (max - min) / 2 / factor;
  const newMin = Math.max(0, center - halfRange);
  const newMax = Math.min(xScale.labels.length - 1, center + halfRange);
  priceChart.options.scales.x.min = newMin;
  priceChart.options.scales.x.max = newMax;
  priceChart.update('none');
}

function resetZoom() {
  if (!priceChart) return;
  if (ZOOM_ENABLED && typeof priceChart.resetZoom === 'function') {
    try { priceChart.resetZoom(); return; } catch (e) {}
  }
  priceChart.options.scales.x.min = undefined;
  priceChart.options.scales.x.max = undefined;
  priceChart.update('none');
}

// ==================== REFRESH ALL ====================
async function refreshAll() {
  const btn = document.getElementById('refresh-all-btn');
  const originalText = btn.innerHTML;
  btn.innerHTML = '⟳ Оновлення...';
  btn.disabled = true;

  try {
    await Promise.allSettled([
      loadDashboard(),
      loadPaper(),
      loadImpact(),
    ]);
  } catch (e) {
    console.error('refreshAll error:', e);
  }

  btn.innerHTML = '✓ Оновлено';
  setTimeout(() => {
    btn.innerHTML = originalText;
    btn.disabled = false;
  }, 1500);
}

// ==================== NEXT HIGH EVENT ====================
async function loadNextHigh() {
  try {
    const data = await api('/events/next_high');
    const badge = document.getElementById('next-high-badge');
    if (!data.next_high) {
      badge.textContent = '⏰ немає high';
      return;
    }
    const ev = data.next_high;
    const d = data.days;
    const h = data.hours;
    const m = data.minutes;

    let timing = '';
    if (d > 0) timing = `${d}д ${h}г`;
    else if (h > 0) timing = `${h}г ${m}хв`;
    else timing = `${m}хв`;

    badge.textContent = `⏰ ${ev.title.slice(0, 20)}${ev.title.length > 20 ? '…' : ''} — ${timing}`;
    badge.title = `${ev.title} (${ev.country})\nЧерез ${timing}`;

    const totalSec = data.seconds_until;
    if (totalSec < 300) badge.style.color = '#ef4444';
    else if (totalSec < 3600) badge.style.color = '#f59e0b';
    else badge.style.color = '';
  } catch (e) {
    console.error('loadNextHigh failed', e);
  }
}

// ==================== LIVE ====================
function fmtSeconds(s) {
  if (s < 60) return `${s}с`;
  if (s < 3600) return `${Math.floor(s/60)}хв ${s%60}с`;
  return `${Math.floor(s/3600)}год ${Math.floor((s%3600)/60)}хв`;
}

function renderLiveCard(item) {
  const ev = item.event;
  const impEmoji = { high: '🔴', medium: '🟡' }[ev.importance] || '⚪';
  const isPast = item.seconds_since > 0;
  const timing = isPast
    ? `T+${fmtSeconds(item.seconds_since)}`
    : `через ${fmtSeconds(item.seconds_to)}`;

  const sym = (label, data) => {
    if (!data) {
      return `<div class="flex-1 min-w-[140px]">
        <div class="text-xs text-slate-500 mb-1">${label}</div>
        <div class="text-lg font-mono text-slate-600">—</div>
      </div>`;
    }
    const cls = data.ret_pct > 0.05 ? 'up' : data.ret_pct < -0.05 ? 'down' : 'flat';
    const arrow = data.ret_pct > 0.05 ? '↑' : data.ret_pct < -0.05 ? '↓' : '→';
    return `<div class="flex-1 min-w-[140px]">
      <div class="text-xs text-slate-500 mb-1">${label}</div>
      <div class="text-lg font-mono">$${data.current.toFixed(4)}</div>
      <div class="text-sm font-mono ${cls}">${arrow} ${data.ret_pct >= 0 ? '+' : ''}${data.ret_pct.toFixed(3)}%</div>
    </div>`;
  };

  return `
    <div class="card p-4 mb-3 border-l-4 ${ev.importance === 'high' ? 'border-red-500' : 'border-amber-500'}" data-event-id="${ev.id}">
      <div class="flex justify-between items-start mb-3 flex-wrap gap-2">
        <div>
          <div class="text-lg font-semibold">${impEmoji} ${ev.title}</div>
          <div class="text-sm text-slate-400">${ev.country} · ${fmtBoth(ev.time_utc)}
            · Forecast: <span class="font-mono">${ev.forecast_value || '—'}</span>
            · Actual: <span class="font-mono">${ev.actual_value || '—'}</span>
          </div>
        </div>
        <div class="text-right">
          <div class="text-2xl font-mono ${isPast ? 'text-cyan-400' : 'text-slate-400'}">${timing}</div>
        </div>
      </div>
      <div class="flex gap-4 flex-wrap">
        ${sym('SOLUSDT', item.symbols.SOLUSDT)}
        ${sym('BTCUSDT', item.symbols.BTCUSDT)}
        ${sym('ETHUSDT', item.symbols.ETHUSDT)}
      </div>
    </div>
  `;
}

function renderLiveList(data) {
  const container = document.getElementById('live-events');
  if (!container) return;
  if (!data.active || data.active.length === 0) {
    container.innerHTML = `<div class="card p-6 text-center text-slate-500">
      Немає активних подій. Найближчі — у розділі «Події».
    </div>`;
  } else {
    container.innerHTML = data.active.map(renderLiveCard).join('');
  }
}

async function loadLive() {
  try {
    const data = await api('/live/current');
    renderLiveList(data);
  } catch (e) {
    console.error('loadLive failed', e);
  }
}

async function loadLiveRecent() {
  const events = await api('/events?limit=30');
  const tbody = document.getElementById('live-recent-tbody');
  const recent = events.filter(e => e.hit && e.hit !== 'N/A' && e.hit !== 'NO_DATA').slice(0, 15);
  tbody.innerHTML = recent.map(e => `
    <tr class="cursor-pointer" onclick="openEvent(${e.id})">
      <td class="font-mono text-xs">${fmtBothFull(e.time_utc)}</td>
      <td class="text-cyan-400">${e.title}</td>
      <td>${badge(e.importance)}</td>
      <td class="text-right font-mono ${clsForDir(e.dir_15m)}">${e.ret_15m != null ? fmtPct(e.ret_15m) : '—'}</td>
      <td class="text-right font-mono ${clsForDir(e.dir_60m)}">${e.ret_60m != null ? fmtPct(e.ret_60m) : '—'}</td>
      <td class="text-center ${clsForDir(e.expected_dir)}">${e.expected_dir || '—'}</td>
      <td class="text-center">${hitBadge(e.hit)}</td>
    </tr>
  `).join('') || '<tr><td colspan="7" class="text-slate-500">Немає даних</td></tr>';
}

// ==================== COMPARE ====================
async function loadCompare() {
  const days = document.getElementById('compare-days').value;
  const rows = await api(`/events/compare?days_back=${days}&limit=100`);

  const tbody = document.getElementById('compare-tbody');
  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" class="text-slate-500">Немає даних за цей період</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map(r => {
    const excess = (r.sol_ret15 != null && r.btc_ret15 != null)
      ? r.sol_ret15 - r.btc_ret15
      : null;
    const excessCls = excess == null ? 'flat' : excess > 0.05 ? 'up' : excess < -0.05 ? 'down' : 'flat';
    const excessText = excess == null ? '—' : (excess >= 0 ? '+' : '') + excess.toFixed(3) + '%';

    const solBadge = r.sol_hit ? hitBadge(r.sol_hit) : '—';
    const btcBadge = r.btc_hit ? hitBadge(r.btc_hit) : '—';
    const ethBadge = r.eth_hit ? hitBadge(r.eth_hit) : '—';

    return `
      <tr class="cursor-pointer" onclick="openEvent(${r.id})">
        <td class="font-mono text-xs">${fmtBothFull(r.time_utc)}</td>
        <td class="text-cyan-400">${r.title}</td>
        <td class="text-center">${badge(r.importance)}</td>
        <td class="text-right font-mono ${clsForDir(r.sol_dir)}">${fmtPct(r.sol_ret15)}</td>
        <td class="text-right font-mono ${clsForDir(r.btc_dir)}">${fmtPct(r.btc_ret15)}</td>
        <td class="text-right font-mono ${clsForDir(r.eth_dir)}">${fmtPct(r.eth_ret15)}</td>
        <td class="text-right font-mono ${excessCls}">${excessText}</td>
        <td class="text-center text-xs">${solBadge} / ${btcBadge} / ${ethBadge}</td>
      </tr>
    `;
  }).join('');
}

// ==================== PRICE CHART ====================
async function loadPriceChart(hours = null) {
  if (hours !== null) currentChartHours = hours;

  document.querySelectorAll('#tab-dashboard button.tab').forEach(b => {
    b.classList.remove('active');
    const txt = b.textContent.trim();
    const match =
      (currentChartHours === 0.25 && txt === '15 хв') ||
      (currentChartHours === 1 && txt === '1 год') ||
      (currentChartHours === 6 && txt === '6 год') ||
      (currentChartHours === 24 && txt === '24 год') ||
      (currentChartHours === 168 && txt === '7 днів');
    if (match) b.classList.add('active');
  });

  const data = await api(`/prices/range?hours=${currentChartHours}&symbol=SOLUSDT`);
  const candles = data.candles;
  const events = data.events;

  let step = 1;
  if (currentChartHours >= 168) step = 30;
  else if (currentChartHours >= 24) step = 5;

  const grouped = [];
  for (let i = 0; i < candles.length; i += step) {
    const chunk = candles.slice(i, i + step);
    if (chunk.length === 0) continue;
    grouped.push({ ts: chunk[0].ts, close: chunk[chunk.length - 1].close });
  }

  const labels = grouped.map(c => {
    const d = new Date(c.ts);
    if (currentChartHours < 1) {
      return d.toLocaleTimeString('uk-UA', {
        timeZone: LOCAL_TZ, hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
      });
    }
    return currentChartHours >= 24
      ? d.toLocaleString('uk-UA', {
          timeZone: LOCAL_TZ, day: '2-digit', month: '2-digit',
          hour: '2-digit', minute: '2-digit', hour12: false
        }).replace(',', '')
      : d.toLocaleTimeString('uk-UA', {
          timeZone: LOCAL_TZ, hour: '2-digit', minute: '2-digit', hour12: false
        });
  });
  const prices = grouped.map(c => c.close);
  const tsArray = grouped.map(c => c.ts);
  labelsGlobal = labels;

  const markersByLevel = { high: [], medium: [] };
  for (const ev of events) {
    const evTs = new Date(ev.time_utc).getTime();
    let bestIdx = -1, bestDist = Infinity;
    for (let i = 0; i < grouped.length; i++) {
      const dist = Math.abs(grouped[i].ts - evTs);
      if (dist < bestDist) { bestDist = dist; bestIdx = i; }
    }
    if (bestIdx < 0 || bestDist > 2 * 60 * 60 * 1000) continue;
    if (ev.importance === 'high') markersByLevel.high.push({ idx: bestIdx, ev });
    else if (ev.importance === 'medium') markersByLevel.medium.push({ idx: bestIdx, ev });
  }

  eventsByLabelAll = {};
  const allEvents = markersByLevel.high.concat(markersByLevel.medium);
  for (const m of allEvents) {
    const lbl = labels[m.idx];
    if (!eventsByLabelAll[lbl]) eventsByLabelAll[lbl] = [];
    eventsByLabelAll[lbl].push(m.ev);
  }

  let trades = [];
  try {
    trades = await api(`/paper/trades/range?hours=${currentChartHours}`);
  } catch (_) {}

  const longEntries = [];
  const shortEntries = [];
  const exits = [];
  tradesByLabel = {};

  for (const t of trades) {
    const entryIdx = findIdxByTs(tsArray, t.entry_ts);
    if (entryIdx >= 0) {
      const point = { x: labels[entryIdx], y: prices[entryIdx] };
      if (t.direction === 'long') longEntries.push(point);
      else shortEntries.push(point);
      tradesByLabel[labels[entryIdx]] = { ...t, kind: 'entry' };
    }
    if (t.exit_ts) {
      const exitIdx = findIdxByTs(tsArray, t.exit_ts);
      if (exitIdx >= 0) {
        exits.push({ x: labels[exitIdx], y: prices[exitIdx], pnl: t.pnl });
        if (!tradesByLabel[labels[exitIdx]]) {
          tradesByLabel[labels[exitIdx]] = { ...t, kind: 'exit' };
        }
      }
    }
  }

  const datasets = [
    {
      label: 'SOLUSDT', data: prices,
      borderColor: '#22d3ee', backgroundColor: 'rgba(34,211,238,0.1)',
      borderWidth: 2, tension: 0.2, pointRadius: 0, fill: true, order: 5,
    },
    {
      label: 'High', data: markersByLevel.high.map(m => ({ x: labels[m.idx], y: prices[m.idx] })),
      backgroundColor: '#dc2626', borderColor: '#fff', borderWidth: 1,
      pointRadius: 6, pointHoverRadius: 10, showLine: false, order: 4,
    },
    {
      label: 'Medium', data: markersByLevel.medium.map(m => ({ x: labels[m.idx], y: prices[m.idx] })),
      backgroundColor: '#f59e0b', borderColor: '#fff', borderWidth: 1,
      pointRadius: 4, pointHoverRadius: 8, showLine: false, order: 4,
    },
    {
      label: 'LONG entry',
      data: longEntries.map(p => ({ x: p.x, y: p.y })),
      backgroundColor: '#22c55e', borderColor: '#ffffff', borderWidth: 1.5,
      pointRadius: 9, pointHoverRadius: 13,
      pointStyle: 'triangle', rotation: 0,
      showLine: false, order: 1,
    },
    {
      label: 'SHORT entry',
      data: shortEntries.map(p => ({ x: p.x, y: p.y })),
      backgroundColor: '#ef4444', borderColor: '#ffffff', borderWidth: 1.5,
      pointRadius: 9, pointHoverRadius: 13,
      pointStyle: 'triangle', rotation: 180,
      showLine: false, order: 1,
    },
    {
      label: 'Exit',
      data: exits.map(p => ({ x: p.x, y: p.y })),
      backgroundColor: exits.map(p => (p.pnl >= 0 ? '#22c55e' : '#ef4444')),
      borderColor: '#ffffff', borderWidth: 1.5,
      pointRadius: 8, pointHoverRadius: 12,
      pointStyle: 'rectRot',
      showLine: false, order: 2,
    },
  ];

  if (priceChart) priceChart.destroy();
  priceChart = new Chart(document.getElementById('price-chart'), {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true,
      interaction: { mode: 'nearest', intersect: true },
      plugins: {
        legend: {
          labels: {
            color: '#e2e8f0',
            filter: (item) => item.text !== 'High' && item.text !== 'Medium',
          }
        },
        tooltip: {
          callbacks: {
            label: (ctx) => {
              if (ctx.dataset.label === 'SOLUSDT') {
                return `Ціна: $${ctx.parsed.y.toFixed(4)}`;
              }
              return null;
            },
            afterBody: (ctx) => {
              const lbl = ctx[0].label;
              const lines = [];

              const trade = tradesByLabel[lbl];
              if (trade) {
                if (trade.kind === 'entry') {
                  lines.push(`💰 ${trade.direction.toUpperCase()} entry ${trade.symbol}`);
                  lines.push(`Подія: ${trade.event_title}`);
                  lines.push(`Ціна входу: $${trade.entry_price.toFixed(4)}`);
                  lines.push(`Розмір: $${trade.size_usd.toFixed(2)}`);
                } else {
                  const sign = trade.pnl >= 0 ? '+' : '';
                  lines.push(`🏁 ${trade.direction.toUpperCase()} exit ${trade.symbol}`);
                  lines.push(`Подія: ${trade.event_title}`);
                  lines.push(`Ціна виходу: $${trade.exit_price.toFixed(4)}`);
                  lines.push(`P&L: ${sign}$${trade.pnl.toFixed(4)} (${sign}${trade.pnl_pct.toFixed(3)}%)`);
                }
              }

              const eventsHere = eventsByLabelAll[lbl] || [];
              if (eventsHere.length > 0) {
                if (eventsHere.length > 1) {
                  lines.push(`📌 Подій на цій точці: ${eventsHere.length}`);
                }
                for (const ev of eventsHere) {
                  lines.push(`📌 ${ev.title} (${ev.importance})`);
                  if (ev.ret_15m != null) lines.push(`   Ret +15хв: ${ev.ret_15m.toFixed(3)}%`);
                  if (ev.hit) lines.push(`   Hit: ${ev.hit}`);
                }
              }
              return lines;
            }
          }
        },
        zoom: ZOOM_ENABLED ? {
          pan: { enabled: true, mode: 'x' },
          zoom: {
            wheel: { enabled: true, speed: 0.1 },
            pinch: { enabled: true },
            mode: 'x',
          },
          limits: { x: { minRange: 10 } },
        } : undefined,
      },
      onClick: (evt, elements) => {
        if (elements.length === 0) return;
        const el = elements[0];
        const lbl = labels[el.index];
        const trade = tradesByLabel[lbl];
        if (trade) {
          openEvent(trade.event_id);
          return;
        }
        const eventsHere = eventsByLabelAll[lbl] || [];
        if (eventsHere.length > 0) {
          openEvent(eventsHere[0].id);
        }
      },
      onHover: (evt, elements) => {
        if (evt.native && evt.native.target) {
          evt.native.target.style.cursor = elements.length > 0 ? 'pointer' : 'default';
        }
      },
      scales: {
        x: {
          ticks: { color: '#94a3b8', maxTicksLimit: 12, autoSkip: true },
          grid: { color: '#334155' },
          title: { display: true, text: 'Час (Київ)', color: '#94a3b8' }
        },
        y: {
          ticks: { color: '#94a3b8' },
          grid: { color: '#334155' },
          title: { display: true, text: 'Ціна, USD', color: '#94a3b8' }
        },
      }
    }
  });
}

function findIdxByTs(tsArray, targetTs) {
  if (!tsArray || tsArray.length === 0 || !targetTs) return -1;
  let bestIdx = -1, bestDist = Infinity;
  for (let i = 0; i < tsArray.length; i++) {
    const dist = Math.abs(tsArray[i] - targetTs);
    if (dist < bestDist) { bestDist = dist; bestIdx = i; }
  }
  if (bestDist > 2 * 60 * 1000) return -1;
  return bestIdx;
}

// ==================== DASHBOARD ====================
async function loadDashboard() {
  const stats = await api('/api/stats');
  document.getElementById('stat-events').textContent = stats.events_count;
  document.getElementById('stat-prices').textContent = stats.prices_count.toLocaleString();
  document.getElementById('stat-impact').textContent = stats.impact_count;
  document.getElementById('stat-last').innerHTML = fmtStatLast(stats.last_ts);

  const events = await api('/events?limit=10');
  const tbody = document.getElementById('recent-events');
  tbody.innerHTML = events.map(e => `
    <tr class="cursor-pointer" onclick="openEvent(${e.id})">
      <td class="font-mono text-xs">${fmtBothFull(e.time_utc)}</td>
      <td>${badge(e.importance)}</td>
      <td class="font-mono">${e.country}</td>
      <td class="text-cyan-400">${e.title}</td>
      <td class="text-center">${hitBadge(e.hit)}</td>
    </tr>
  `).join('') || '<tr><td colspan="5" class="text-slate-500">Немає подій</td></tr>';

  await loadPriceChart();

  const istats = await api('/impact/stats?symbol=SOLUSDT');
  const o = istats.overall || {};
  const denom = (o.hits || 0) + (o.misses || 0);
  if (denom > 0 || (o.n || 0) > 0) {
    document.getElementById('hit-rate-card').style.display = '';
    const rate = denom > 0 ? (o.hits / denom * 100) : null;
    document.getElementById('hit-rate-summary').innerHTML = `
      <div>
        <div class="text-sm text-slate-400">Hit Rate (SOL)</div>
        <div class="text-3xl font-mono ${rate == null ? 'flat' : rate >= 60 ? 'up' : rate <= 40 ? 'down' : 'flat'}">
          ${rate == null ? '—' : rate.toFixed(1) + '%'}
        </div>
        <div class="hint">${denom > 0 ? `HIT/(HIT+MISS), n=${denom}` : 'Немає даних з forecast/actual'}</div>
      </div>
      <div><div class="text-sm text-slate-400">HIT</div><div class="text-3xl font-mono up">${o.hits || 0}</div></div>
      <div><div class="text-sm text-slate-400">MISS</div><div class="text-3xl font-mono down">${o.misses || 0}</div></div>
      <div><div class="text-sm text-slate-400">NEUTRAL / NO_DATA</div><div class="text-3xl font-mono flat">${(o.neutrals||0)} / ${(o.no_data||0)}</div></div>
    `;
  }

  if (istats.overall && istats.overall.n) {
    document.getElementById('overall-impact-card').style.display = '';
    document.getElementById('overall-impact').innerHTML = `
      ${metric('Avg +5хв', fmtPct(o.avg_5m), clsFor(o.avg_5m), 'Середня зміна через 5 хвилин')}
      ${metric('Avg +15хв', fmtPct(o.avg_15m), clsFor(o.avg_15m), 'Середня зміна через 15 хвилин')}
      ${metric('Avg +30хв', fmtPct(o.avg_30m), clsFor(o.avg_30m), 'Середня зміна через 30 хвилин')}
      ${metric('Avg +60хв', fmtPct(o.avg_60m), clsFor(o.avg_60m), 'Середня зміна через 60 хвилин')}
      ${metric('Up/Down/Flat (n=' + o.n + ')',
        `<span class="up">${o.up_15m||0}</span> / <span class="down">${o.down_15m||0}</span> / <span class="flat">${o.flat_15m||0}</span>`, '',
        'Розподіл напрямків через 15 хвилин.')}
    `;
  }

  try {
    const acc = await api('/paper/account');
    if (!acc.error) {
      const el = document.getElementById('nav-equity');
      el.textContent = fmtUsd(acc.equity);
      el.className = 'equity-badge tip ' + clsFor(acc.total_pnl);
    }
  } catch (_) {}

  loadNextHigh();
}

async function toolPaperDiagnostics() {
  try {
    const r = await api('/tools/paper_diagnostics');
    showToolsResult('💰 Діагностика paper', r);
  } catch (e) {
    showToolsResult('❌ Помилка', { error: String(e) });
  }
}

// ==================== EVENTS ====================
function debouncedLoadEvents() {
  if (eventsSearchTimer) clearTimeout(eventsSearchTimer);
  eventsSearchTimer = setTimeout(() => loadEvents(), 300);
}

async function loadEvents() {
  const search = document.getElementById('events-search').value.trim();
  const daysBack = document.getElementById('events-days').value;
  const importance = document.getElementById('events-importance').value;

  const params = new URLSearchParams();
  params.set('limit', '500');
  if (daysBack) params.set('days_back', daysBack);
  if (search) params.set('search', search);
  if (importance) params.set('importance', importance);

  const events = await api(`/events?${params.toString()}`);
  document.getElementById('events-count').textContent = `Знайдено: ${events.length}`;

  const tbody = document.getElementById('events-tbody');
  tbody.innerHTML = events.map(e => `
    <tr class="cursor-pointer" onclick="openEvent(${e.id})">
      <td class="font-mono text-xs text-slate-500">${e.id}</td>
      <td class="font-mono text-xs">${fmtBothFull(e.time_utc)}</td>
      <td>${badge(e.importance)}</td>
      <td class="font-mono">${e.country}</td>
      <td class="text-cyan-400">${e.title}</td>
      <td class="text-right font-mono ${clsForDir(e.dir_15m)}">${e.ret_15m != null ? fmtPct(e.ret_15m) : '—'}</td>
      <td class="text-right font-mono ${clsForDir(e.dir_60m)}">${e.ret_60m != null ? fmtPct(e.ret_60m) : '—'}</td>
      <td class="text-center ${clsForDir(e.expected_dir)}">${e.expected_dir || '—'}</td>
      <td class="text-center">${hitBadge(e.hit)}</td>
    </tr>
  `).join('') || '<tr><td colspan="9" class="text-slate-500">Немає подій за фільтром</td></tr>';
}

function exportEventsCsv() {
  const search = document.getElementById('events-search').value.trim();
  const daysBack = document.getElementById('events-days').value;
  const importance = document.getElementById('events-importance').value;

  const params = new URLSearchParams();
  if (daysBack) params.set('days_back', daysBack);
  if (search) params.set('search', search);
  if (importance) params.set('importance', importance);

  window.location.href = `/events/export.csv?${params.toString()}`;
}

// ==================== IMPACT ====================
function switchImpactSymbol(symbol, el) {
  impactSymbol = symbol;
  document.querySelectorAll('.symbol-tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');

  const label = document.getElementById('impact-symbol-label');
  label.textContent = symbol || 'SOL + BTC + ETH (разом)';

  loadImpact();
}

async function loadImpact() {
  const url = impactSymbol
    ? `/impact/stats?symbol=${impactSymbol}`
    : `/impact/stats`;
  const s = await api(url);
  const o = s.overall || {};
  const denom = (o.hits||0) + (o.misses||0);
  const rateVal = denom > 0 ? (o.hits / denom * 100) : null;
  document.getElementById('impact-overall').innerHTML = o.n ? `
    <div class="grid grid-cols-2 md:grid-cols-7 gap-4">
      ${metric('Всього (n)', o.n, '', 'Загальна кількість подій')}
      ${metric('Hit Rate', rateVal == null ? '—' : rateVal.toFixed(1) + '%',
              rateVal == null ? 'flat' : rateVal >= 50 ? 'up' : 'down',
              'HIT / (HIT + MISS) × 100')}
      ${metric('HIT', o.hits||0, 'up', 'Прогноз справдився')}
      ${metric('MISS', o.misses||0, 'down', 'Прогноз не справдився')}
      ${metric('NEUTRAL', o.neutrals||0, 'flat', 'У межах порогу')}
      ${metric('NO_DATA', o.no_data||0, 'flat', 'Немає forecast/actual')}
      ${metric('Avg +15хв', fmtPct(o.avg_15m), clsFor(o.avg_15m), 'Середня зміна через 15 хвилин')}
    </div>
  ` : '<p class="text-slate-500">Немає даних</p>';

  const tbody = document.getElementById('impact-tbody');
  tbody.innerHTML = (s.by_title || []).map(r => {
    const denom = (r.hits||0) + (r.misses||0);
    const rate = denom > 0 ? ((r.hits / denom) * 100).toFixed(0) + '%' : '—';
    const rateCls = denom > 0 ? ((r.hits/denom) >= 0.6 ? 'up' : (r.hits/denom) <= 0.4 ? 'down' : 'flat') : 'flat';

    const rateTip = [
      `HIT: ${r.hits||0}`, `MISS: ${r.misses||0}`,
      `NEUTRAL: ${r.neutrals||0}`, `NO_DATA: ${r.no_data||0}`, `N/A: ${r.na||0}`,
    ].join(' · ');

    const dirTip = [
      `Up: ${r.up_15m||0}`, `Down: ${r.down_15m||0}`, `Flat: ${r.flat_15m||0}`,
    ].join(' · ');

    return `
      <tr>
        <td>${r.title}</td>
        <td>${badge(r.importance)}</td>
        <td class="text-right font-mono">${r.n}</td>
        <td class="text-right font-mono ${clsFor(r.avg_5m)}">${fmtPct(r.avg_5m)}</td>
        <td class="text-right font-mono ${clsFor(r.avg_15m)}">${fmtPct(r.avg_15m)}</td>
        <td class="text-right font-mono ${clsFor(r.avg_30m)}">${fmtPct(r.avg_30m)}</td>
        <td class="text-right font-mono ${clsFor(r.avg_60m)}">${fmtPct(r.avg_60m)}</td>
        <td class="text-right font-mono text-xs" title="${dirTip}">
          <span class="up">${r.up_15m||0}</span>/<span class="down">${r.down_15m||0}</span>/<span class="flat">${r.flat_15m||0}</span>
        </td>
        <td class="text-right font-mono ${rateCls}" title="${rateTip}">${rate}</td>
      </tr>
    `;
  }).join('') || '<tr><td colspan="9" class="text-slate-500">Немає даних</td></tr>';

  const rows = (s.by_title || []).filter(r => (r.hits||0)+(r.misses||0)+(r.neutrals||0) > 0);
  if (hitChart) hitChart.destroy();
  if (rows.length > 0) {
    hitChart = new Chart(document.getElementById('hit-chart'), {
      type: 'bar',
      data: {
        labels: rows.map(r => r.title.length > 35 ? r.title.slice(0, 35) + '…' : r.title),
        datasets: [
          { label: 'HIT',     data: rows.map(r => r.hits || 0),     backgroundColor: '#22c55e' },
          { label: 'MISS',    data: rows.map(r => r.misses || 0),   backgroundColor: '#ef4444' },
          { label: 'NEUTRAL', data: rows.map(r => r.neutrals || 0), backgroundColor: '#64748b' },
        ]
      },
      options: {
        indexAxis: 'y',
        responsive: true,
        plugins: {
          legend: { labels: { color: '#e2e8f0' } },
          tooltip: { mode: 'index', intersect: false }
        },
        scales: {
          x: { stacked: true, ticks: { color: '#94a3b8' }, grid: { color: '#334155' } },
          y: { stacked: true, ticks: { color: '#94a3b8' }, grid: { color: '#334155' } },
        }
      }
    });
  }
}

// ==================== PAPER ====================
async function loadPaper() {
  const acc = await api('/paper/account');
  if (!acc.error) {
    document.getElementById('paper-initial').textContent = fmtUsd(acc.initial_balance);
    document.getElementById('paper-equity').textContent = fmtUsd(acc.equity);
    document.getElementById('paper-equity').className = 'text-3xl font-bold font-mono ' + clsFor(acc.total_pnl);
    document.getElementById('paper-equity-hint').textContent =
      `Balance ${fmtUsd(acc.balance)} + frozen ${fmtUsd(acc.frozen)}`;

    const pnlSign = acc.total_pnl >= 0 ? '+' : '';
    document.getElementById('paper-pnl').textContent = `${pnlSign}${fmtUsd(acc.total_pnl)}`;
    document.getElementById('paper-pnl').className = 'text-3xl font-bold font-mono ' + clsFor(acc.total_pnl);
    document.getElementById('paper-pnl-pct').textContent =
      `${pnlSign}${acc.total_pnl_pct.toFixed(3)}%`;

    document.getElementById('paper-winrate').textContent =
      acc.win_rate == null ? '—' : acc.win_rate.toFixed(1) + '%';
    document.getElementById('paper-winrate').className =
      'text-3xl font-bold font-mono ' + (acc.win_rate == null ? 'flat' : acc.win_rate >= 50 ? 'up' : 'down');
    document.getElementById('paper-trades-count').textContent =
      `${acc.total_trades} угод (${acc.closed_trades} закр.)`;

    document.getElementById('paper-open').textContent = acc.open_trades;
    document.getElementById('paper-closed').textContent = acc.closed_trades;
    document.getElementById('paper-wins').textContent = acc.wins;
    document.getElementById('paper-losses').textContent = acc.losses;
    document.getElementById('paper-avg-win').textContent =
      acc.avg_win == null ? '—' : '+' + fmtUsd(acc.avg_win);
    document.getElementById('paper-avg-loss').textContent =
      acc.avg_loss == null ? '—' : fmtUsd(acc.avg_loss);

    const bySymbolCard = document.getElementById('paper-by-symbol-card');
    const bySymbolTbody = document.getElementById('paper-by-symbol-tbody');
    if (acc.by_symbol && acc.by_symbol.length > 0) {
      bySymbolCard.style.display = '';
      bySymbolTbody.innerHTML = acc.by_symbol.map(s => `
        <tr>
          <td>${symbolBadge(s.symbol)}</td>
          <td class="text-right font-mono">${s.total}</td>
          <td class="text-right font-mono text-cyan-400">${s.open}</td>
          <td class="text-right font-mono">${s.closed}</td>
          <td class="text-right font-mono">
            <span class="up">${s.wins}</span> / <span class="down">${s.losses}</span>
          </td>
          <td class="text-right font-mono ${s.win_rate == null ? 'flat' : s.win_rate >= 50 ? 'up' : 'down'}">
            ${s.win_rate == null ? '—' : s.win_rate.toFixed(1) + '%'}
          </td>
          <td class="text-right font-mono ${clsFor(s.total_pnl)}">
            ${s.total_pnl >= 0 ? '+' : ''}${fmtUsd(s.total_pnl)}
          </td>
          <td class="text-right font-mono up">
            ${s.avg_win == null ? '—' : '+' + fmtUsd(s.avg_win)}
          </td>
          <td class="text-right font-mono down">
            ${s.avg_loss == null ? '—' : fmtUsd(s.avg_loss)}
          </td>
        </tr>
      `).join('');
    } else {
      bySymbolCard.style.display = 'none';
    }
  }

  const curve = await api('/paper/equity_curve');
  if (equityChart) equityChart.destroy();
  if (curve.length === 0) {
    document.getElementById('equity-hint').textContent = 'Очікування першої закритої угоди…';
  } else {
    document.getElementById('equity-hint').textContent =
      `${curve.length} закритих угод, фінальний cumulative: ${fmtUsd(curve[curve.length-1].cum_pnl)}`;

    const labels = curve.map(p => {
      const d = new Date(p.ts.replace(' ', 'T') + 'Z');
      return d.toLocaleString('uk-UA', {
        timeZone: LOCAL_TZ, day: '2-digit', month: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false
      }).replace(',', '');
    });
    const data = curve.map(p => p.cum_pnl);

    equityChart = new Chart(document.getElementById('equity-chart'), {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'Cumulative P&L, $',
          data,
          borderColor: '#22d3ee',
          backgroundColor: 'rgba(34,211,238,0.1)',
          borderWidth: 2, tension: 0.2, pointRadius: 3, fill: true,
        }]
      },
      options: {
        responsive: true,
        plugins: { legend: { labels: { color: '#e2e8f0' } } },
        scales: {
          x: { ticks: { color: '#94a3b8', maxTicksLimit: 12 }, grid: { color: '#334155' } },
          y: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' },
               title: { display: true, text: 'P&L, $', color: '#94a3b8' } },
        }
      }
    });
  }

  const trades = await api('/paper/trades?limit=100');
  const openTrades = trades.filter(t => t.status === 'OPEN');
  const closedTrades = trades.filter(t => t.status === 'CLOSED');

  const openTbody = document.getElementById('paper-open-tbody');
  if (openTrades.length === 0) {
    openTbody.innerHTML = '<tr><td colspan="9" class="text-slate-500">Немає відкритих угод</td></tr>';
  } else {
    openTbody.innerHTML = openTrades.map(t => {
      const current = currentPrices[t.symbol];
      let livePnl = '—';
      let liveCls = 'flat';
      if (current && t.entry_price) {
        const qty = t.size_usd / t.entry_price;
        let pnl;
        if (t.direction === 'long') {
          pnl = qty * (current - t.entry_price) - t.entry_fee;
        } else {
          pnl = qty * (t.entry_price - current) - t.entry_fee;
        }
        livePnl = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(4);
        liveCls = clsFor(pnl);
      }
      return `
        <tr>
          <td class="font-mono text-xs text-slate-500">${t.id}</td>
          <td>${symbolBadge(t.symbol)}</td>
          <td class="text-cyan-400">${t.event_title}</td>
          <td class="text-center">${dirBadge(t.direction)}</td>
          <td class="text-right font-mono">${fmtUsd4(t.entry_price)}</td>
          <td class="text-right font-mono">${current ? fmtUsd4(current) : '—'}</td>
          <td class="text-right font-mono ${liveCls}">${livePnl}</td>
          <td class="text-right font-mono">${fmtUsd(t.size_usd)}</td>
          <td class="text-right font-mono text-xs">${fmtBothFull(t.event_time)}</td>
        </tr>
      `;
    }).join('');
  }

  const closedTbody = document.getElementById('paper-closed-tbody');
  if (closedTrades.length === 0) {
    closedTbody.innerHTML = '<tr><td colspan="9" class="text-slate-500">Ще немає закритих угод</td></tr>';
  } else {
    closedTbody.innerHTML = closedTrades.map(t => {
      const pnlSign = (t.pnl || 0) >= 0 ? '+' : '';
      const closedTs = t.closed_at ? t.closed_at.replace(' ', 'T') + 'Z' : null;
      return `
        <tr>
          <td class="font-mono text-xs text-slate-500">${t.id}</td>
          <td>${symbolBadge(t.symbol)}</td>
          <td class="text-cyan-400">${t.event_title}</td>
          <td class="text-center">${dirBadge(t.direction)}</td>
          <td class="text-right font-mono">${fmtUsd4(t.entry_price)}</td>
          <td class="text-right font-mono">${fmtUsd4(t.exit_price)}</td>
          <td class="text-right font-mono ${clsFor(t.pnl)}">${pnlSign}${fmtUsd(t.pnl)}</td>
          <td class="text-right font-mono ${clsFor(t.pnl)}">${pnlSign}${(t.pnl_pct || 0).toFixed(3)}%</td>
          <td class="text-right font-mono text-xs">${closedTs ? fmtBothFull(closedTs) : '—'}</td>
        </tr>
      `;
    }).join('');
  }

  if (!acc.error) {
    const el = document.getElementById('nav-equity');
    el.textContent = fmtUsd(acc.equity);
    el.className = 'equity-badge tip ' + clsFor(acc.total_pnl);
  }
}

async function runPaperEngine() {
  if (!confirm('Запустити paper engine вручну?')) return;
  const r = await api('/paper/run', { method: 'POST' });
  alert(`Готово: ${JSON.stringify(r)}`);
  loadPaper();
}

async function resetPaper() {
  if (!confirm('Скинути акаунт до $100 і видалити всі угоди?')) return;
  await api('/paper/reset', { method: 'POST' });
  alert('Акаунт скинуто');
  loadPaper();
}

function exportPaperCsv() {
  window.location.href = '/paper/trades/export.csv';
}

// ==================== CALENDAR ====================
async function loadCalendar() {
  const days = document.getElementById('calendar-days').value;
  const events = await api(`/events/calendar?days=${days}`);

  const container = document.getElementById('calendar-container');
  if (events.length === 0) {
    container.innerHTML = '<p class="text-slate-500">Немає подій на цей період.</p>';
    return;
  }

  const byDay = {};
  for (const ev of events) {
    const d = new Date(ev.time_utc);
    const dayKey = d.toLocaleDateString('uk-UA', {
      timeZone: LOCAL_TZ, year: 'numeric', month: '2-digit', day: '2-digit'
    });
    if (!byDay[dayKey]) byDay[dayKey] = [];
    byDay[dayKey].push(ev);
  }

  const days_sorted = Object.keys(byDay).sort();
  const todayKey = new Date().toLocaleDateString('uk-UA', {
    timeZone: LOCAL_TZ, year: 'numeric', month: '2-digit', day: '2-digit'
  });

  let html = '';
  for (const day of days_sorted) {
    const dayEvents = byDay[day];
    const d = new Date(dayEvents[0].time_utc);
    const dayLabel = d.toLocaleDateString('uk-UA', {
      timeZone: LOCAL_TZ, weekday: 'long', day: 'numeric', month: 'long'
    });
    const isToday = day === todayKey;

    html += `<div class="mb-6">
      <div class="flex items-center gap-3 mb-3">
        <h2 class="text-lg font-semibold ${isToday ? 'text-cyan-400' : 'text-slate-200'}">
          ${dayLabel}${isToday ? ' · сьогодні' : ''}
        </h2>
        <span class="text-xs text-slate-500">${dayEvents.length} подій</span>
      </div>
      <div class="space-y-1">
        ${dayEvents.map(ev => {
          const localTime = new Date(ev.time_utc).toLocaleTimeString('uk-UA', {
            timeZone: LOCAL_TZ, hour: '2-digit', minute: '2-digit', hour12: false
          });
          const impColor = {
            high: 'border-l-red-500',
            medium: 'border-l-amber-500',
            low: 'border-l-slate-500',
          }[ev.importance] || 'border-l-slate-500';
          return `
            <div class="flex items-center gap-4 px-3 py-2 bg-slate-800/40 rounded border-l-4 ${impColor} hover:bg-slate-800 cursor-pointer"
                 onclick="openEvent(${ev.id})">
              <span class="font-mono text-sm text-cyan-400 w-16">${localTime}</span>
              ${badge(ev.importance)}
              <span class="font-mono text-xs text-slate-500 w-10">${ev.country}</span>
              <span class="flex-1 text-slate-200">${ev.title}</span>
              ${ev.forecast_value ? `<span class="text-xs text-slate-500">fc: ${ev.forecast_value}</span>` : ''}
            </div>
          `;
        }).join('')}
      </div>
    </div>`;
  }

  container.innerHTML = html;
}

// ==================== HEATMAP ====================
async function loadHeatmap() {
  const data = await api('/impact/heatmap');
  const cells = data.cells || [];

  const days = ['Нд', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'];
  const hours = Array.from({length: 24}, (_, i) => i);

  const idx = {};
  let maxAbs = 0;
  for (const c of cells) {
    idx[`${c.dow}_${c.hour}`] = c;
    if (c.avg_15m != null) maxAbs = Math.max(maxAbs, Math.abs(c.avg_15m));
  }

  function colorFor(avg) {
    if (avg == null || maxAbs === 0) return '#334155';
    const norm = Math.max(-1, Math.min(1, avg / maxAbs));
    if (norm > 0) return `rgba(34,197,94,${0.15 + 0.85 * norm})`;
    return `rgba(239,68,68,${0.15 + 0.85 * Math.abs(norm)})`;
  }

  function kyivHourLabel(utcHour) {
    const d = new Date(Date.UTC(2026, 0, 1, utcHour, 0, 0));
    const kh = parseInt(d.toLocaleTimeString('uk-UA', {
      timeZone: LOCAL_TZ, hour: '2-digit', hour12: false
    }), 10);
    return `${kh}`;
  }

  let html = `<table style="border-collapse: separate; border-spacing: 2px;">`;
  html += `<tr><th style="padding:4px;font-size:0.7rem;color:#94a3b8;text-align:right">Київ →<br><span style="font-size:0.6rem">UTC ↓</span></th>`;
  for (const h of hours) {
    html += `<th style="padding:4px;font-size:0.65rem;color:#94a3b8;text-align:center">${kyivHourLabel(h)}</th>`;
  }
  html += `</tr>`;

  for (const d of [1,2,3,4,5,6,0]) {
    html += `<tr><td style="padding:6px;font-size:0.75rem;color:#94a3b8;text-align:right;padding-right:8px">${days[d]}</td>`;
    for (const h of hours) {
      const cell = idx[`${d}_${h}`];
      const avg = cell?.avg_15m ?? null;
      const n = cell?.n ?? 0;
      const bg = colorFor(avg);
      const utcLabel = `${days[d]} ${String(h).padStart(2,'0')}:00 UTC`;
      const title = cell
        ? `${utcLabel}\nКиїв: ${kyivHourLabel(h)}:00\nn=${n}\nAvg +15хв: ${avg?.toFixed(3)}%\nAvg +60хв: ${cell.avg_60m?.toFixed(3)}%`
        : `${utcLabel} — немає даних`;
      const text = avg != null ? avg.toFixed(1) : '';
      html += `<td title="${title}" style="background:${bg};width:32px;height:28px;text-align:center;font-size:0.65rem;color:#fff;border-radius:3px;font-family:monospace">${text}</td>`;
    }
    html += `</tr>`;
  }
  html += `</table>`;
  document.getElementById('heatmap-container').innerHTML = html;
}

async function loadCrossAsset() {
  const s = await api('/impact/stats');
  const sum = s.cross_asset_summary || {};
  const rows = s.cross_asset || [];

  document.getElementById('cross-asset-summary').innerHTML = `
    <div class="grid grid-cols-2 md:grid-cols-5 gap-4">
      ${metric('Подій з BTC', sum.n_events || 0, '', 'Скільки подій мають impact і для BTC')}
      ${metric('Збіг напрямку', sum.same_direction_as_btc || 0, 'up', 'Скільки разів SOL рухався в той самий бік, що BTC')}
      ${metric('Кореляція', (sum.correlation_pct ?? 0).toFixed(1) + '%',
              (sum.correlation_pct ?? 0) >= 70 ? 'up' : (sum.correlation_pct ?? 0) <= 40 ? 'down' : 'flat',
              'Скільки % подій SOL рухався в той самий бік, що BTC')}
      ${metric('Avg SOL +15хв', fmtPct(sum.avg_sol_ret_15m), clsFor(sum.avg_sol_ret_15m),
              'Середня зміна SOL через 15 хв після подій')}
      ${metric('Excess SOL vs BTC', fmtPct(sum.avg_excess_sol_vs_btc), clsFor(sum.avg_excess_sol_vs_btc),
              'Різниця: SOL − BTC.')}
    </div>
  `;

  const tbody = document.getElementById('cross-asset-tbody');
  tbody.innerHTML = rows.map(r => {
    const sameDir = r.sol_dir && r.btc_dir && r.sol_dir !== 'flat' && r.btc_dir !== 'flat';
    const match = sameDir && r.sol_dir === r.btc_dir;
    const badgeHtml = !sameDir ? '<span class="badge-na px-2 py-0.5 rounded text-xs">—</span>'
                          : (match ? '<span class="badge-hit px-2 py-0.5 rounded text-xs">ТАК</span>'
                                   : '<span class="badge-miss px-2 py-0.5 rounded text-xs">НІ</span>');
    return `
      <tr>
        <td class="font-mono text-xs">${fmtBothFull(r.time_utc)}</td>
        <td class="text-cyan-400">${r.title}</td>
        <td class="text-right font-mono ${clsFor(r.sol_15m)}">${fmtPct(r.sol_15m)}</td>
        <td class="text-right font-mono ${clsFor(r.btc_15m)}">${fmtPct(r.btc_15m)}</td>
        <td class="text-right font-mono ${clsFor(r.eth_15m)}">${fmtPct(r.eth_15m)}</td>
        <td class="text-center">${badgeHtml}</td>
      </tr>
    `;
  }).join('') || '<tr><td colspan="6" class="text-slate-500">Немає даних</td></tr>';
}

// ==================== MODAL ====================
async function openEvent(id) {
  const e = await api(`/event/${id}`);
  const i = await api(`/impact/event/${id}`).catch(() => null);

  document.getElementById('modal-title').textContent = e.title;
  let body = `
    <div class="grid grid-cols-2 gap-3 mb-4 text-sm">
      <div><span class="text-slate-400">Час (Київ / UTC):</span> <span class="font-mono">${fmtBothFull(e.time_utc)}</span></div>
      <div><span class="text-slate-400">Валюта:</span> <span class="font-mono">${e.country}</span></div>
      <div><span class="text-slate-400">Важливість:</span> ${badge(e.importance)}</div>
      <div><span class="text-slate-400">Forecast / Prev / Actual:</span>
        <span class="font-mono">${e.forecast_value||'—'} / ${e.previous_value||'—'} / ${e.actual_value||'—'}</span>
      </div>
    </div>
  `;

  if (i && !i.error) {
    const hit = i.hit || 'NO_DATA';
    body += `<div class="mb-3 p-3 rounded bg-slate-800 flex items-center gap-4 flex-wrap">
      <div><span class="text-slate-400 text-sm">Очікуваний:</span> <span class="${clsForDir(i.expected_dir)} font-mono">${i.expected_dir || '—'}</span></div>
      <div><span class="text-slate-400 text-sm">Фактичний (15хв):</span> <span class="${clsForDir(i.dir_15m)} font-mono">${i.dir_15m || '—'}</span></div>
      <div>${hitBadge(hit)}</div>
    </div>`;

    body += `<h3 class="font-semibold mb-2">Вплив на ${i.symbol || 'SOLUSDT'}</h3>
      <div class="text-xs text-slate-400 mb-2">
        Baseline (T-1): <span class="font-mono text-white">${fmtUsd4(i.price_baseline)}</span>
        · Поріг: <span class="font-mono text-white">${i.threshold_pct ? i.threshold_pct.toFixed(3) : '—'}%</span>
      </div>
      <table><thead><tr>
        <th>Вікно</th><th class="text-right">Ціна</th>
        <th class="text-right">Зміна</th><th class="text-right">Напрямок</th>
      </tr></thead><tbody>`;
    for (const w of [1,5,15,30,60]) {
      body += `<tr>
        <td>T+${w}хв</td>
        <td class="text-right font-mono">${i['price_t'+w] ? fmtUsd4(i['price_t'+w]) : '—'}</td>
        <td class="text-right font-mono ${clsFor(i['ret_'+w+'m'])}">${fmtPct(i['ret_'+w+'m'])}</td>
        <td class="text-right ${clsForDir(i['dir_'+w+'m'])}">${i['dir_'+w+'m'] || '—'}</td>
      </tr>`;
    }
    body += `</tbody></table>`;
  } else {
    body += `<p class="text-slate-400">Вплив ще не розраховано.</p>`;
  }

  document.getElementById('modal-body').innerHTML = body;
  document.getElementById('modal').style.display = 'flex';
}

function closeModal() { document.getElementById('modal').style.display = 'none'; }
document.getElementById('modal').onclick = e => { if (e.target.id === 'modal') closeModal(); };

// ==================== ACTIONS ====================
async function runRecompute() {
  if (!confirm('Перерахувати вплив для всіх минулих подій?')) return;
  const r = await api('/impact/recompute', { method: 'POST' });
  alert(`Готово: ${JSON.stringify(r)}`);
  loadDashboard();
}

// ==================== DOUBLE-CLICK RESET ZOOM ====================
document.addEventListener('DOMContentLoaded', () => {
  const canvas = document.getElementById('price-chart');
  if (canvas) {
    canvas.addEventListener('dblclick', () => resetZoom());
  }
});

// ==================== WEBSOCKET ====================
(function connectWS() {
  const status = document.getElementById('ws-status');
  let ws, delay = 1000;
  function connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { status.textContent = '● online'; status.className = 'text-green-400'; delay = 1000; };
    ws.onclose = () => {
      status.textContent = '● offline'; status.className = 'text-red-400';
      setTimeout(connect, delay); delay = Math.min(delay * 2, 30000);
    };
    ws.onerror = () => ws.close();
    ws.onmessage = e => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'calendar_refreshed') loadDashboard();
        if (msg.type === 'impact_recomputed') loadDashboard();
        if (msg.type === 'paper_updated') {
          const tab = document.querySelector('.tab.active');
          if (tab && tab.dataset.tab === 'paper') loadPaper();
          if (tab && tab.dataset.tab === 'dashboard') loadPriceChart();
        }
        if (msg.type === 'live_update') {
          const liveTab = document.getElementById('tab-live');
          if (liveTab && liveTab.style.display !== 'none') renderLiveList(msg.data);
          for (const item of (msg.data.active || [])) {
            for (const sym of ['SOLUSDT','BTCUSDT','ETHUSDT']) {
              if (item.symbols[sym]) currentPrices[sym] = item.symbols[sym].current;
            }
          }
        }
      } catch (_) {}
    };
  }
  connect();
})();

// ==================== BACKTEST ====================
let btEquityChart = null;

// ==================== BACKTEST PRESETS ====================
const BT_PRESETS = {
  default: {
    label: 'Default',
    hit_rate: 60, min_move: 0.30, min_events: 1,
    symbols: 'SOLUSDT,BTCUSDT', spot_long_only: true,
    exit_offset: 60, size_usd: 10, direction: '',
    hint: 'Базовий: high-only, HIT/MISS, hit_rate≥60%, |ret|≥0.3%, T+60, LONG+SHORT (але SPOT-long-only блокує SHORT).',
  },
  all: {
    label: 'All signals',
    hit_rate: 0, min_move: 0, min_events: 1,
    symbols: 'SOLUSDT,BTCUSDT', spot_long_only: false,
    exit_offset: 60, size_usd: 10, direction: '',
    hint: 'Без фільтрів: усі сигнали, LONG+SHORT. Показує максимум можливих угод.',
  },
  long_only: {
    label: 'LONG-only',
    hit_rate: 60, min_move: 0.30, min_events: 1,
    symbols: 'SOLUSDT,BTCUSDT', spot_long_only: false,
    exit_offset: 60, size_usd: 10, direction: 'long',
    hint: 'Тільки LONG. Чистий прогін long-сценаріїв.',
  },
  short_only: {
    label: 'SHORT-only',
    hit_rate: 60, min_move: 0.30, min_events: 1,
    symbols: 'SOLUSDT,BTCUSDT', spot_long_only: false,
    exit_offset: 60, size_usd: 10, direction: 'short',
    hint: 'Тільки SHORT. Чистий прогін short-сценаріїв.',
  },
  fast_exit: {
    label: 'Fast exit T+15',
    hit_rate: 60, min_move: 0.30, min_events: 1,
    symbols: 'SOLUSDT,BTCUSDT', spot_long_only: false,
    exit_offset: 15, size_usd: 10, direction: '',
    hint: 'Вихід через 15 хв. LONG+SHORT.',
  },
  mid_exit: {
    label: 'Mid exit T+30',
    hit_rate: 60, min_move: 0.30, min_events: 1,
    symbols: 'SOLUSDT,BTCUSDT', spot_long_only: false,
    exit_offset: 30, size_usd: 10, direction: '',
    hint: 'Вихід через 30 хв. LONG+SHORT.',
  },
  slow_exit: {
    label: 'Slow exit T+120',
    hit_rate: 60, min_move: 0.30, min_events: 1,
    symbols: 'SOLUSDT,BTCUSDT', spot_long_only: false,
    exit_offset: 120, size_usd: 10, direction: '',
    hint: 'Вихід через 2 год. LONG+SHORT.',
  },
  strict: {
    label: 'Strict',
    hit_rate: 80, min_move: 0.50, min_events: 2,
    symbols: 'SOLUSDT,BTCUSDT', spot_long_only: false,
    exit_offset: 60, size_usd: 10, direction: '',
    hint: 'Строгі фільтри: hit_rate≥80%, |ret|≥0.5%, min_events≥2.',
  },
  custom: {
    label: 'Custom',
    hint: 'Ручні параметри. Змінюй поля нижче.',
  },
};

function applyBacktestPreset(name) {
  const p = BT_PRESETS[name];
  if (!p) return;

  const hintEl = document.getElementById('bt-preset-hint');
  if (hintEl) hintEl.textContent = p.hint || '';

  if (name === 'custom') return;

  const setVal = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.value = v;
  };
  const setChk = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.checked = v;
  };

  setVal('bt-hit-rate', p.hit_rate);
  setVal('bt-min-move', p.min_move);
  setVal('bt-min-events', p.min_events);
  setVal('bt-symbols', p.symbols);
  setChk('bt-long-only', p.spot_long_only);
  setVal('bt-exit-offset', p.exit_offset);
  setVal('bt-size-usd', p.size_usd);
  setVal('bt-direction', p.direction ?? '');
}

// Автоперемикання на Custom при ручній зміні будь-якого поля
document.addEventListener('DOMContentLoaded', () => {
  ['bt-hit-rate', 'bt-min-move', 'bt-min-events', 'bt-symbols',
   'bt-long-only', 'bt-exit-offset', 'bt-size-usd', 'bt-direction'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const evt = el.type === 'checkbox' ? 'change' : 'input';
    el.addEventListener(evt, () => {
      const sel = document.getElementById('bt-preset');
      if (sel && sel.value !== 'custom') {
        sel.value = 'custom';
        const hintEl = document.getElementById('bt-preset-hint');
        if (hintEl) {
          const p = BT_PRESETS.custom;
          hintEl.textContent = p ? (p.hint || '') : '';
        }
      }
    });
  });
});

// Ініціалізація: застосувати default при завантаженні
document.addEventListener('DOMContentLoaded', () => {
  const sel = document.getElementById('bt-preset');
  if (sel) applyBacktestPreset(sel.value);
});

async function runBacktest() {
  const btn = document.getElementById('bt-run-btn');
  const originalText = btn.innerHTML;
  btn.innerHTML = '⏳ Виконується...';
  btn.disabled = true;

  const params = new URLSearchParams();
  const days = document.getElementById('bt-days').value.trim();
  if (days) params.set('days', days);
  params.set('reset', 'true');
  params.set('hit_rate_threshold', document.getElementById('bt-hit-rate').value);
  params.set('min_move_pct', document.getElementById('bt-min-move').value);
  params.set('min_events', document.getElementById('bt-min-events').value);
  params.set('symbols', document.getElementById('bt-symbols').value);
  params.set('spot_long_only', document.getElementById('bt-long-only').checked);
  params.set('exit_offset_min', document.getElementById('bt-exit-offset').value);
  params.set('trade_size_usd', document.getElementById('bt-size-usd').value);

  const dir = document.getElementById('bt-direction').value;
  if (dir) params.set('direction_filter', dir);

  try {
    const r = await api(`/backtest/run?${params.toString()}`, { method: 'POST' });
    renderBacktestResult(r);
    await loadBacktestTrades();
    await loadBacktestEquity();
  } catch (e) {
    alert('Помилка: ' + e);
  } finally {
    btn.innerHTML = originalText;
    btn.disabled = false;
  }
}

function renderBacktestResult(r) {
  const s = r.summary || {};
  const p = r.params || {};

  // Підсумок
  document.getElementById('bt-summary-card').style.display = '';
  document.getElementById('bt-summary').innerHTML = `
    ${metric('Угод', s.n || 0, '', 'Скільки угод згенеровано')}
    ${metric('Wins', s.wins || 0, 'up', 'Прибуткових')}
    ${metric('Losses', s.losses || 0, 'down', 'Збиткових')}
    ${metric('Win Rate', (s.win_rate ?? 0).toFixed(1) + '%',
            (s.win_rate ?? 0) >= 50 ? 'up' : 'down', 'Wins / n')}
  `;
  document.getElementById('bt-summary-2').innerHTML = `
    ${metric('Total P&L', fmtUsd(s.total_pnl), clsFor(s.total_pnl), 'Сума P&L')}
    ${metric('Avg P&L', fmtUsd(s.avg_pnl), clsFor(s.avg_pnl), 'Середній P&L на угоду')}
    ${metric('Avg Win', fmtUsd(s.avg_win), 'up', 'Середній прибуток')}
    ${metric('Avg Loss', fmtUsd(s.avg_loss), 'down', 'Середній збиток')}
  `;

  // Параметри + skip reasons
  const skips = r.skipped_reasons || {};
  const skipsEntries = Object.entries(skips).sort((a, b) => b[1] - a[1]);

  document.getElementById('bt-skips-card').style.display = '';
  const paramsHtml = Object.entries(p).map(([k, v]) =>
    `<div class="text-xs"><span class="text-slate-400">${k}:</span> <span class="font-mono text-cyan-400">${JSON.stringify(v)}</span></div>`
  ).join('');

  let skipsHtml = '';
  if (skipsEntries.length > 0) {
    skipsHtml = skipsEntries.map(([reason, count]) =>
      `<div class="text-xs"><span class="text-amber-400 font-mono">${reason}</span>: <span class="text-white">${count}</span></div>`
    ).join('');
  } else {
    skipsHtml = '<div class="text-xs text-slate-500">Немає</div>';
  }

  document.getElementById('bt-skips').innerHTML = `
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div>
        <div class="text-sm font-semibold mb-2">Кандидатів / Відкрито</div>
        <div class="text-xs"><span class="text-slate-400">candidates:</span> <span class="font-mono text-cyan-400">${r.candidates}</span></div>
        <div class="text-xs"><span class="text-slate-400">opened:</span> <span class="font-mono text-green-400">${r.opened}</span></div>
        <div class="mt-3 text-sm font-semibold mb-2">Параметри</div>
        ${paramsHtml}
      </div>
      <div>
        <div class="text-sm font-semibold mb-2">Причини skip</div>
        ${skipsHtml}
      </div>
    </div>
  `;

  // По незалежних подіях
  if (r.by_events && r.by_events.n > 0) {
    document.getElementById('bt-by-events-card').style.display = '';
    const e = r.by_events;
    document.getElementById('bt-by-events').innerHTML = `
      ${metric('Подій', e.n, '', 'Кількість незалежних подій (event_time_utc + direction)')}
      ${metric('Wins', e.wins, 'up', 'Прибуткових подій')}
      ${metric('Losses', e.losses, 'down', 'Збиткових подій')}
      ${metric('Win Rate', (e.win_rate ?? 0).toFixed(1) + '%',
              (e.win_rate ?? 0) >= 50 ? 'up' : 'down', 'Wins / n')}
      ${metric('Total P&L', fmtUsd(e.total_pnl), clsFor(e.total_pnl),
              'Сума P&L по подіях (з урахуванням обох символів)')}
    `;
  } else {
    document.getElementById('bt-by-events-card').style.display = 'none';
  }

  // По символах
  if (r.by_symbol && r.by_symbol.length > 0) {
    document.getElementById('bt-by-symbol-card').style.display = '';
    document.getElementById('bt-by-symbol-tbody').innerHTML = r.by_symbol.map(x => `
      <tr>
        <td>${symbolBadge(x.symbol)}</td>
        <td class="text-right font-mono">${x.n}</td>
        <td class="text-right font-mono up">${x.wins}</td>
        <td class="text-right font-mono ${(x.win_rate ?? 0) >= 50 ? 'up' : 'down'}">${(x.win_rate ?? 0).toFixed(1)}%</td>
        <td class="text-right font-mono ${clsFor(x.total_pnl)}">${fmtUsd(x.total_pnl)}</td>
      </tr>
    `).join('');
  } else {
    document.getElementById('bt-by-symbol-card').style.display = 'none';
  }

  // По напрямках
  if (r.by_direction && r.by_direction.length > 0) {
    document.getElementById('bt-by-direction-card').style.display = '';
    document.getElementById('bt-by-direction-tbody').innerHTML = r.by_direction.map(x => `
      <tr>
        <td>${dirBadge(x.direction)}</td>
        <td class="text-right font-mono">${x.n}</td>
        <td class="text-right font-mono up">${x.wins}</td>
        <td class="text-right font-mono ${(x.win_rate ?? 0) >= 50 ? 'up' : 'down'}">${(x.win_rate ?? 0).toFixed(1)}%</td>
        <td class="text-right font-mono ${clsFor(x.total_pnl)}">${fmtUsd(x.total_pnl)}</td>
      </tr>
    `).join('');
  } else {
    document.getElementById('bt-by-direction-card').style.display = 'none';
  }
}

async function loadBacktestTrades() {
  try {
    const trades = await api('/backtest/trades?limit=500');
    if (trades.length === 0) {
      document.getElementById('bt-trades-card').style.display = 'none';
      return;
    }
    document.getElementById('bt-trades-card').style.display = '';
    document.getElementById('bt-trades-tbody').innerHTML = trades.map(t => {
      const pnlSign = (t.pnl || 0) >= 0 ? '+' : '';
      return `
        <tr class="cursor-pointer" onclick="openEvent(${t.event_id})">
          <td class="font-mono text-xs">${fmtBothFull(t.event_time_utc)}</td>
          <td class="text-cyan-400">${t.event_title}</td>
          <td>${symbolBadge(t.symbol)}</td>
          <td class="text-center">${dirBadge(t.direction)}</td>
          <td class="text-right font-mono">${fmtUsd4(t.entry_price)}</td>
          <td class="text-right font-mono">${fmtUsd4(t.exit_price)}</td>
          <td class="text-right font-mono ${clsFor(t.pnl)}">${pnlSign}${fmtUsd(t.pnl)}</td>
          <td class="text-right font-mono ${clsFor(t.pnl)}">${pnlSign}${(t.pnl_pct || 0).toFixed(3)}%</td>
        </tr>
      `;
    }).join('');
  } catch (e) {
    console.error('loadBacktestTrades failed', e);
  }
}

async function loadBacktestEquity() {
  try {
    const curve = await api('/backtest/equity_curve');
    const card = document.getElementById('bt-equity-card');
    if (curve.length === 0) {
      card.style.display = 'none';
      return;
    }
    card.style.display = '';

    document.getElementById('bt-equity-hint').textContent =
      `${curve.length} угод, фінальний cumulative: ${fmtUsd(curve[curve.length-1].cum_pnl)}`;

    const labels = curve.map(p => {
      const d = new Date(p.ts);
      return d.toLocaleString('uk-UA', {
        timeZone: LOCAL_TZ, day: '2-digit', month: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false
      }).replace(',', '');
    });
    const data = curve.map(p => p.cum_pnl);

    if (btEquityChart) btEquityChart.destroy();
    btEquityChart = new Chart(document.getElementById('bt-equity-chart'), {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'Cumulative P&L, $',
          data,
          borderColor: '#22d3ee',
          backgroundColor: 'rgba(34,211,238,0.1)',
          borderWidth: 2, tension: 0.2, pointRadius: 3, fill: true,
        }]
      },
      options: {
        responsive: true,
        plugins: { legend: { labels: { color: '#e2e8f0' } } },
        scales: {
          x: { ticks: { color: '#94a3b8', maxTicksLimit: 12 }, grid: { color: '#334155' } },
          y: { ticks: { color: '#94a3b8' }, grid: { color: '#334155' },
               title: { display: true, text: 'P&L, $', color: '#94a3b8' } },
        }
      }
    });
  } catch (e) {
    console.error('loadBacktestEquity failed', e);
  }
}

async function clearBacktest() {
  if (!confirm('Видалити всі backtest-угоди?')) return;
  await api('/backtest/clear', { method: 'POST' });
  document.getElementById('bt-summary-card').style.display = 'none';
  document.getElementById('bt-skips-card').style.display = 'none';
  document.getElementById('bt-by-events-card').style.display = 'none';
  document.getElementById('bt-by-symbol-card').style.display = 'none';
  document.getElementById('bt-by-direction-card').style.display = 'none';
  document.getElementById('bt-equity-card').style.display = 'none';
  document.getElementById('bt-trades-card').style.display = 'none';
  if (btEquityChart) { btEquityChart.destroy(); btEquityChart = null; }
}

// ==================== TOOLS ====================
function showToolsResult(label, data) {
  const card = document.getElementById('tools-result-card');
  const pre = document.getElementById('tools-result');
  card.style.display = '';
  const timestamp = new Date().toLocaleString('uk-UA', { timeZone: LOCAL_TZ });
  pre.textContent = `[${timestamp}] ${label}\n\n${JSON.stringify(data, null, 2)}`;
}

function hideToolsResult() {
  document.getElementById('tools-result-card').style.display = 'none';
}

async function toolRecomputeImpact() {
  const limit = parseInt(document.getElementById('tool-impact-limit').value) || 1000;
  showToolsResult(`⟳ Перерахувати impact (limit=${limit})`, { loading: true });
  try {
    const r = await api(`/tools/recompute_impact?limit=${limit}`, { method: 'POST' });
    showToolsResult(`⟳ Перерахувати impact (limit=${limit})`, r);
    setTimeout(() => loadDashboard(), 500);
  } catch (e) {
    showToolsResult('❌ Помилка', { error: String(e) });
  }
}

async function toolRunPaper() {
  showToolsResult('▶ Запустити paper engine', { loading: true });
  try {
    const r = await api('/tools/run_paper_engine', { method: 'POST' });
    showToolsResult('▶ Запустити paper engine', r);
    setTimeout(() => loadPaper(), 500);
  } catch (e) {
    showToolsResult('❌ Помилка', { error: String(e) });
  }
}

async function toolLoadHistory() {
  const days = parseInt(document.getElementById('tool-history-days').value) || 2;
  showToolsResult(`📥 Довантажити свічки (days=${days})`, { loading: true });
  try {
    const r = await api(`/tools/load_history?days=${days}&interval=1`, { method: 'POST' });
    showToolsResult(`📥 Довантажити свічки (days=${days})`, r);
    setTimeout(() => loadDashboard(), 500);
  } catch (e) {
    showToolsResult('❌ Помилка', { error: String(e) });
  }
}

async function toolRefreshCalendar() {
  showToolsResult('📅 Оновити календар (today)', { loading: true });
  try {
    const r = await api('/tools/refresh_calendar?day=today', { method: 'POST' });
    showToolsResult('📅 Оновити календар (today)', r);
    setTimeout(() => loadDashboard(), 500);
  } catch (e) {
    showToolsResult('❌ Помилка', { error: String(e) });
  }
}

async function toolLoadCalendarWeek() {
  const week = document.getElementById('tool-calendar-week').value;
  showToolsResult(`📅 Завантажити тиждень (week=${week})`, { loading: true });
  try {
    const r = await api(`/tools/load_calendar_week?week=${week}`, { method: 'POST' });
    showToolsResult(`📅 Завантажити тиждень (week=${week})`, r);
    setTimeout(() => loadDashboard(), 500);
  } catch (e) {
    showToolsResult('❌ Помилка', { error: String(e) });
  }
}

async function toolShowUpcoming() {
  try {
    const events = await api('/events/upcoming_high?days=7&limit=20');
    const card = document.getElementById('tools-upcoming-card');
    const tbody = document.getElementById('tools-upcoming-tbody');
    if (events.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-slate-500">Немає high подій у наступні 7 днів</td></tr>';
    } else {
      tbody.innerHTML = events.map(e => `
        <tr class="cursor-pointer" onclick="openEvent(${e.id})">
          <td class="font-mono text-xs">${fmtBothFull(e.time_utc)}</td>
          <td class="font-mono">${e.country}</td>
          <td class="text-amber-400">${e.title}</td>
          <td class="text-right font-mono text-xs">${e.forecast_value || '—'}</td>
          <td class="text-right font-mono text-xs">${e.previous_value || '—'}</td>
        </tr>
      `).join('');
    }
    card.style.display = '';
  } catch (e) {
    showToolsResult('❌ Помилка', { error: String(e) });
  }
}

async function toolDbStats() {
  try {
    const r = await api('/tools/db_stats');
    showToolsResult('📊 Статистика БД', r);
  } catch (e) {
    showToolsResult('❌ Помилка', { error: String(e) });
  }
}

async function toolDbTables() {
  const container = document.getElementById('db-tables-result');
  container.innerHTML = '<span class="loading"></span>';
  try {
    const data = await api('/tools/db_tables');
    let html = `<div class="text-xs text-slate-400 mb-2">
      📁 ${data.db_path}<br>
      Загальний розмір: <b>${data.db_total_mb} МБ</b>
    </div>`;
    html += '<table class="text-xs"><thead><tr>';
    html += '<th>Таблиця</th><th class="text-right">Рядків</th><th class="text-right">Розмір</th>';
    html += '</tr></thead><tbody>';
    for (const t of data.tables) {
      html += `<tr>
        <td class="font-mono">${t.table}</td>
        <td class="text-right font-mono">${t.rows ?? '—'}</td>
        <td class="text-right font-mono">${t.size_mb != null ? t.size_mb + ' МБ' : '—'}</td>
      </tr>`;
    }
    html += '</tbody></table>';
    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = `<span class="text-red-400 text-xs">Помилка: ${e}</span>`;
  }
}

async function toolLogs() {
  const lines = parseInt(document.getElementById('tool-logs-lines').value) || 100;
  const grep = document.getElementById('tool-logs-grep').value.trim();
  try {
    const params = new URLSearchParams({ lines: lines });
    if (grep) params.set('grep', grep);
    const r = await api(`/tools/logs?${params.toString()}`);
    if (r.error) {
      showToolsResult('📋 Логи (помилка)', r);
    } else {
      const card = document.getElementById('tools-result-card');
      const pre = document.getElementById('tools-result');
      card.style.display = '';
      const timestamp = new Date().toLocaleString('uk-UA', { timeZone: LOCAL_TZ });
      pre.textContent = `[${timestamp}] Логи (${r.count} рядків${grep ? `, grep="${grep}"` : ''})\n\n`
                      + r.lines.join('\n');
    }
  } catch (e) {
    showToolsResult('❌ Помилка', { error: String(e) });
  }
}

async function toolDiagnostics() {
  try {
    const r = await api('/tools/diagnostics');
    showToolsResult('🔍 Діагностика impact', r);
  } catch (e) {
    showToolsResult('❌ Помилка', { error: String(e) });
  }
}

async function toolClearImpact() {
  if (!confirm('⚠️ Видалити всі записи з event_impact?')) return;
  if (!confirm('Точно впевнений? Друге підтвердження.')) return;
  try {
    const r = await api('/tools/clear_impact', { method: 'POST' });
    showToolsResult('🗑 Очистити impact', r);
    setTimeout(() => loadDashboard(), 500);
  } catch (e) {
    showToolsResult('❌ Помилка', { error: String(e) });
  }
}

async function toolResetPaper() {
  if (!confirm('⚠️ Скинути paper акаунт? Баланс повернеться до $100.')) return;
  if (!confirm('Точно впевнений? Друге підтвердження.')) return;
  try {
    const r = await api('/paper/reset', { method: 'POST' });
    showToolsResult('↺ Скинути paper акаунт', r);
    setTimeout(() => loadPaper(), 500);
  } catch (e) {
    showToolsResult('❌ Помилка', { error: String(e) });
  }
}

// ==================== INIT ====================
loadDashboard();
loadNextHigh();

setInterval(() => {
  const active = document.querySelector('.tab.active');
  if (!active) return;
  if (active.dataset.tab === 'dashboard') loadDashboard();
  if (active.dataset.tab === 'live') loadLive();
  if (active.dataset.tab === 'paper') loadPaper();
  if (active.dataset.tab === 'events') loadEvents();
  if (active.dataset.tab === 'calendar') loadCalendar();
  if (active.dataset.tab === 'compare') loadCompare();
  if (active.dataset.tab === 'backtest') { loadBacktestTrades(); loadBacktestEquity(); }

}, 30000);

setInterval(() => {
  const active = document.querySelector('.tab.active');
  if (active && active.dataset.tab === 'live') loadLive();
}, 3000);

setInterval(async () => {
  try {
    const data = await api('/prices/latest?limit=3');
    for (const p of data) currentPrices[p.symbol] = p.close;
  } catch (_) {}
}, 10000);

// Next high — кожні 60 сек
setInterval(loadNextHigh, 60000);