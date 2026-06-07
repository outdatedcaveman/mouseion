'use strict';

// ── Helpers ─────────────────────────────────────────────────────────────────

async function getCfg() {
  return new Promise(resolve => {
    chrome.storage.local.get(['zp_url', 'zp_key'], r =>
      resolve({ url: r.zp_url || '', key: r.zp_key || '' })
    );
  });
}

async function apiFetch(path, opts) {
  const { url, key } = await getCfg();
  const base = (url || '').replace(/\/$/, '');
  const isFormData = opts?.body instanceof FormData;
  const headers = isFormData ? {} : { 'Content-Type': 'application/json' };
  if (key) headers['X-API-Key'] = key;
  return fetch(base + path, { ...opts, headers: { ...headers, ...(opts?.headers || {}) } });
}

function esc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function setStatus(cls, msg) {
  const el = document.getElementById('status');
  el.className = 'status show ' + cls;
  el.innerHTML = msg;
}

// ── Tab switching ────────────────────────────────────────────────────────────

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
    if (tab.dataset.tab === 'recent') loadRecent();
  });
});

// ── Init ─────────────────────────────────────────────────────────────────────

(async () => {
  const cfg = await getCfg();
  if (!cfg.url || !cfg.key) {
    document.getElementById('not-configured').style.display = '';
    return;
  }
  document.getElementById('main-ui').style.display = '';

  // Load collections for selector
  loadCollections();

  // Detect page metadata
  chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
    if (!tabs[0]) return;
    chrome.tabs.sendMessage(tabs[0].id, { type: 'GET_METADATA' }, resp => {
      if (chrome.runtime.lastError || !resp) return;
      const { doi, arxiv, title, url } = resp;
      const box   = document.getElementById('detected-box');
      const label = document.getElementById('detected-label');
      const value = document.getElementById('detected-value');
      const inp   = document.getElementById('inp-ref');
      if (doi) {
        box.style.display = ''; label.textContent = 'DOI detected';
        value.textContent = doi; inp.value = doi;
      } else if (arxiv) {
        box.style.display = ''; label.textContent = 'arXiv ID detected';
        value.textContent = arxiv; inp.value = arxiv;
      } else if (title) {
        box.style.display = ''; label.textContent = 'Title detected';
        value.textContent = title.slice(0, 80) + (title.length > 80 ? '…' : '');
        inp.value = title;
      }
      if (!doi && !arxiv && !title && url) inp.value = url;
    });
  });
})();

// ── Collections ──────────────────────────────────────────────────────────────

async function loadCollections() {
  try {
    const r    = await apiFetch('/api/collections');
    const data = await r.json();
    if (!Array.isArray(data) || !data.length) return;
    const sel = document.getElementById('coll-select');
    sel.innerHTML = '<option value="">None (My Library)</option>' +
      data.map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
    document.getElementById('coll-row').style.display = '';
  } catch(e) {}
}

// ── Recent saves ─────────────────────────────────────────────────────────────

async function loadRecent() {
  const list = document.getElementById('recent-list');
  list.innerHTML = '<div class="recent-empty"><span class="spin"></span> Loading…</div>';
  try {
    const r    = await apiFetch('/api/refs?limit=20&q=');
    const refs = await r.json();
    if (!Array.isArray(refs) || !refs.length) {
      list.innerHTML = '<div class="recent-empty">No references yet</div>';
      return;
    }
    list.innerHTML = refs.slice(0, 15).map(ref => {
      const auth = ref.authors?.[0]?.family || '';
      const year = ref.year || '';
      const st   = ref.status || 'unread';
      return `<div class="recent-item" onclick="openInLibrary('${ref.id}')">
        <div class="ri-title">${esc(ref.title)}</div>
        <div class="ri-meta">${esc(auth)}${year ? ' · ' + year : ''}
          <span class="ri-status ri-${st}">${st}</span>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    list.innerHTML = '<div class="recent-empty">Could not load references</div>';
  }
}

async function openInLibrary(refId) {
  const { url } = await getCfg();
  if (url) chrome.tabs.create({ url: url.replace(/\/$/, '') + `/#ref-${refId}` });
}

// ── Search ───────────────────────────────────────────────────────────────────

document.getElementById('btn-search').addEventListener('click', doSearch);
document.getElementById('inp-search').addEventListener('keydown', e => {
  if (e.key === 'Enter') doSearch();
});

let _searchTimeout = null;
document.getElementById('inp-search').addEventListener('input', () => {
  clearTimeout(_searchTimeout);
  _searchTimeout = setTimeout(doSearch, 400);
});

async function doSearch() {
  const q       = document.getElementById('inp-search').value.trim();
  const results = document.getElementById('search-results');
  if (!q) { results.innerHTML = '<div class="sr-empty">Type to search your library</div>'; return; }
  results.innerHTML = '<div class="sr-empty"><span class="spin"></span> Searching…</div>';
  try {
    const r    = await apiFetch(`/api/refs?q=${encodeURIComponent(q)}&limit=15`);
    const refs = await r.json();
    if (!Array.isArray(refs) || !refs.length) {
      results.innerHTML = '<div class="sr-empty">No results found</div>';
      return;
    }
    results.innerHTML = refs.map(ref => {
      const auth = ref.authors?.[0]?.family || '';
      return `<div class="sr-item" onclick="openInLibrary('${ref.id}')">
        <div class="sr-title">${esc(ref.title)}</div>
        <div class="sr-meta">${esc(auth)}${ref.year ? ' · ' + ref.year : ''}${ref.journal ? ' · ' + esc(ref.journal.slice(0,30)) : ''}</div>
      </div>`;
    }).join('');
  } catch(e) {
    results.innerHTML = '<div class="sr-empty">Search failed — check connection</div>';
  }
}

// ── Save ─────────────────────────────────────────────────────────────────────

document.getElementById('btn-options').addEventListener('click', () => chrome.runtime.openOptionsPage());
document.getElementById('btn-go-options')?.addEventListener('click', () => chrome.runtime.openOptionsPage());
document.getElementById('btn-open-web').addEventListener('click', async () => {
  const { url } = await getCfg();
  if (url) chrome.tabs.create({ url });
});

document.getElementById('btn-save').addEventListener('click', () => saveRef());
document.getElementById('inp-ref').addEventListener('keydown', e => { if (e.key === 'Enter') saveRef(); });

document.getElementById('btn-page-ref').addEventListener('click', () => {
  chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
    if (tabs[0]) { document.getElementById('inp-ref').value = tabs[0].url; saveRef(); }
  });
});

document.getElementById('btn-page-save-read').addEventListener('click', () => {
  chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
    if (tabs[0]) { document.getElementById('inp-ref').value = tabs[0].url; saveRef(true); }
  });
});

async function saveRef(markRead = false) {
  const text   = document.getElementById('inp-ref').value.trim();
  if (!text) return;
  const collId = document.getElementById('coll-select')?.value || '';
  document.getElementById('btn-save').disabled = true;
  setStatus('run', '<span class="spin"></span>Saving…');
  try {
    const res = await apiFetch('/api/refs', {
      method: 'POST',
      body: JSON.stringify({ text }),
    });
    if (res.status === 401) { setStatus('err', '✗ Auth failed — check Settings'); return; }
    if (!res.ok) { setStatus('err', `✗ Server error ${res.status}`); return; }
    const { job_id } = await res.json();
    pollJob(job_id, collId, markRead);
  } catch (e) {
    setStatus('err', '✗ Could not reach server');
    document.getElementById('btn-save').disabled = false;
  }
}

async function pollJob(jobId, collId, markRead) {
  try {
    const res = await apiFetch(`/api/jobs/${jobId}`);
    const job = await res.json();
    if (job.status === 'running') {
      setStatus('run', `<span class="spin"></span>${esc(job.message || 'Processing…')}`);
      setTimeout(() => pollJob(jobId, collId, markRead), 800);
      return;
    }
    if (job.status === 'done') {
      setStatus('ok', '✓ ' + (job.message || 'Saved'));
      // Post-save actions: add to collection and/or mark read
      if (collId || markRead) {
        // Get the just-added ref by re-fetching recent
        try {
          const r2   = await apiFetch('/api/refs?limit=1&q=');
          const refs = await r2.json();
          const refId = refs?.[0]?.id;
          if (refId) {
            if (collId) {
              await apiFetch(`/api/refs/${refId}/collections`, {
                method: 'POST',
                body: JSON.stringify({ collection_id: parseInt(collId) }),
              });
            }
            if (markRead) {
              await apiFetch(`/api/refs/${refId}`, {
                method: 'PATCH',
                body: JSON.stringify({ status: 'read' }),
              });
            }
          }
        } catch(e2) {}
      }
    } else {
      setStatus('err', '✗ ' + (job.message || 'Failed'));
    }
  } catch (e) {
    setStatus('err', '✗ Network error');
  }
  document.getElementById('btn-save').disabled = false;
}
