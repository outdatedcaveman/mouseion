'use strict';

/**
 * Background service worker (Manifest V3).
 * Currently minimal — handles context menu and right-click-to-save.
 */

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus?.create({
    id: 'mouseion-save',
    title: 'Save to mouseion',
    contexts: ['page', 'link', 'selection'],
  });
});

chrome.contextMenus?.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== 'mouseion-save') return;

  const { zp_url: url, zp_key: key } =
    await chrome.storage.local.get(['zp_url', 'zp_key']);
  if (!url || !key) {
    chrome.runtime.openOptionsPage();
    return;
  }

  let text = '';
  if (info.selectionText) {
    text = info.selectionText.trim();
  } else if (info.linkUrl) {
    text = info.linkUrl;
  } else if (tab?.url) {
    text = tab.url;
  }
  if (!text) return;

  try {
    const res = await fetch(url.replace(/\/$/, '') + '/api/refs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': key },
      body: JSON.stringify({ text }),
    });
    if (res.ok) {
      // Show badge briefly
      chrome.action.setBadgeText({ text: '✓', tabId: tab?.id });
      chrome.action.setBadgeBackgroundColor({ color: '#4caf7d' });
      setTimeout(() => chrome.action.setBadgeText({ text: '' }), 2000);
    }
  } catch (_) {}
});
