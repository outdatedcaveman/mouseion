'use strict';

// ── Helpers ────────────────────────────────────────────────────────────────

async function getCfg() {
  return new Promise(resolve => {
    chrome.storage.local.get(['zp_url', 'zp_key'], result => {
      resolve({ url: result.zp_url || '', key: result.zp_key || '' });
    });
  });
}

async function apiFetch(path, opts) {
  const { url, key } = await getCfg();
  const base = (url || '').replace(/\/$/, '');
  const headers = { 'Content-Type': 'application/json' };
  if (key) headers['X-API-Key'] = key;
  const res = await fetch(base + path, { ...opts, headers: { ...headers, ...(opts?.headers || {}) } });
  return res;
}

function setStatus(cls, msg) {
  const el = document.getElementById('status');
  el.className = 'status show ' + cls;
  el.textContent = msg;
}

// ── Init ───────────────────────────────────────────────────────────────────

(async () => {
  const cfg = await getCfg();
  if (!cfg.url || !cfg.key) {
    document.getElementById('not-configured').style.display = '';
    document.getElementById('main-ui').style.display = 'none';
  } else {
    // Ask content script for detected metadata
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
        // Pre-fill URL if nothing else
        if (!doi && !arxiv && !title && url) inp.value = url;
      });
    });
  }
})();

// ── Event listeners ────────────────────────────────────────────────────────

document.getElementById('btn-options').addEventListener('click', () => {
  chrome.runtime.openOptionsPage();
});

document.getElementById('btn-go-options')?.addEventListener('click', () => {
  chrome.runtime.openOptionsPage();
});

document.getElementById('btn-open-web').addEventListener('click', async () => {
  const cfg = await getCfg();
  if (cfg.url) chrome.tabs.create({ url: cfg.url });
});

document.getElementById('btn-save').addEventListener('click', saveRef);

document.getElementById('inp-ref').addEventListener('keydown', e => {
  if (e.key === 'Enter') saveRef();
});

document.getElementById('btn-page-ref').addEventListener('click', () => {
  chrome.tabs.query({ active: true, currentWindow: true }, tabs => {
    if (tabs[0]) {
      document.getElementById('inp-ref').value = tabs[0].url;
      saveRef();
    }
  });
});

async function saveRef() {
  const text = document.getElementById('inp-ref').value.trim();
  if (!text) return;
  document.getElementById('btn-save').disabled = true;
  setStatus('run', 'Saving…');
  try {
    const res = await apiFetch('/api/refs', {
      method: 'POST',
      body: JSON.stringify({ text }),
    });
    if (res.status === 401) {
      setStatus('err', '✗ Authentication failed — check Settings');
      return;
    }
    if (!res.ok) {
      setStatus('err', `✗ Server error ${res.status}`);
      return;
    }
    const { job_id } = await res.json();
    pollJob(job_id);
  } catch (e) {
    setStatus('err', '✗ Could not reach server');
    document.getElementById('btn-save').disabled = false;
  }
}

async function pollJob(jobId) {
  try {
    const res = await apiFetch(`/api/jobs/${jobId}`);
    const job = await res.json();
    if (job.status === 'running') {
      setStatus('run', job.message || 'Processing…');
      setTimeout(() => pollJob(jobId), 800);
      return;
    }
    if (job.status === 'done') {
      setStatus('ok', '✓ ' + (job.message || 'Saved'));
    } else {
      setStatus('err', '✗ ' + (job.message || 'Failed'));
    }
  } catch (e) {
    setStatus('err', '✗ Network error while polling');
  }
  document.getElementById('btn-save').disabled = false;
}
