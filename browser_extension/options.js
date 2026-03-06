'use strict';

const $url    = document.getElementById('inp-url');
const $key    = document.getElementById('inp-key');
const $status = document.getElementById('status');

function setStatus(cls, msg) {
  $status.className = 'status show ' + cls;
  $status.textContent = msg;
}

// Load saved config
chrome.storage.local.get(['zp_url', 'zp_key'], result => {
  $url.value = result.zp_url || '';
  $key.value = result.zp_key || '';
});

document.getElementById('btn-save').addEventListener('click', () => {
  chrome.storage.local.set(
    { zp_url: $url.value.trim(), zp_key: $key.value.trim() },
    () => setStatus('ok', '✓ Settings saved')
  );
});

document.getElementById('btn-test').addEventListener('click', async () => {
  const url = $url.value.trim().replace(/\/$/, '');
  const key = $key.value.trim();
  if (!url) { setStatus('err', '✗ Enter a server URL first'); return; }
  setStatus('run', 'Testing connection…');
  try {
    const res = await fetch(url + '/api/auth/check', {
      headers: key ? { 'X-API-Key': key } : {},
    });
    if (res.ok) {
      setStatus('ok', '✓ Connected successfully');
    } else if (res.status === 401) {
      setStatus('err', '✗ Invalid API key (401 Unauthorized)');
    } else {
      setStatus('err', `✗ Server returned ${res.status}`);
    }
  } catch (e) {
    setStatus('err', '✗ Could not reach server — check the URL');
  }
});
