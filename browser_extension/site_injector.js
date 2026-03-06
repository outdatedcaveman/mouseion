'use strict';

/**
 * Site injector content script.
 * Injects "＋ Save" buttons next to search results on:
 *   - Google Scholar
 *   - arXiv
 *   - PubMed
 *   - Semantic Scholar
 */

const INJECTOR_ATTR = 'data-zp-injected';

// ── Site definitions ─────────────────────────────────────────────────────────

const SITES = [
  {
    name: 'scholar',
    match: () => location.hostname.includes('scholar.google'),
    // Each result row
    resultSelector: '.gs_r.gs_or',
    // Where to append the button within a row
    buttonTarget: row => row.querySelector('.gs_fl') || row.querySelector('.gs_rs') || row,
    // Extract text to save (title + URL)
    extractText: row => {
      const titleEl = row.querySelector('.gs_rt a, .gs_rt');
      return titleEl?.href || titleEl?.textContent || row.querySelector('h3')?.textContent || '';
    },
  },
  {
    name: 'arxiv',
    match: () => location.hostname.includes('arxiv.org'),
    resultSelector: 'li.arxiv-result, .arxiv-result',
    buttonTarget: row => row.querySelector('.tags, .is-marginless') || row,
    extractText: row => {
      // Prefer the abstract page URL which has the arxiv ID
      const link = row.querySelector('p.list-title a, a[href*="/abs/"]');
      return link?.href || location.href;
    },
  },
  {
    name: 'pubmed',
    match: () => location.hostname.includes('pubmed.ncbi'),
    resultSelector: 'article.full-docsum',
    buttonTarget: row => row.querySelector('.docsum-wrap') || row,
    extractText: row => {
      const link = row.querySelector('a.docsum-title');
      return link ? 'https://pubmed.ncbi.nlm.nih.gov' + link.getAttribute('href') : location.href;
    },
  },
  {
    name: 'semantic-scholar',
    match: () => location.hostname.includes('semanticscholar.org'),
    resultSelector: '[data-test-id="search-result-item"], .cl-paper-row',
    buttonTarget: row => row.querySelector('.cl-paper-actions, .paper-meta-data') || row,
    extractText: row => {
      const link = row.querySelector('a[href*="/paper/"]');
      return link ? 'https://www.semanticscholar.org' + link.getAttribute('href') : location.href;
    },
  },
];

// ── Core injection logic ─────────────────────────────────────────────────────

function getSite() {
  return SITES.find(s => s.match());
}

async function getConfig() {
  return chrome.storage.local.get(['zp_url', 'zp_key']);
}

async function saveText(text) {
  const { zp_url: url, zp_key: key } = await getConfig();
  if (!url || !key) {
    chrome.runtime.sendMessage({ type: 'OPEN_OPTIONS' });
    return false;
  }
  const res = await fetch(url.replace(/\/$/, '') + '/api/refs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-API-Key': key },
    body: JSON.stringify({ text }),
  });
  return res.ok;
}

function makeButton(text, onClick) {
  const btn = document.createElement('button');
  btn.className = 'zp-inject-btn';
  btn.textContent = text;
  btn.addEventListener('click', onClick);
  return btn;
}

async function injectRow(site, row) {
  if (row.hasAttribute(INJECTOR_ATTR)) return;
  row.setAttribute(INJECTOR_ATTR, '1');

  const target = site.buttonTarget(row);
  if (!target) return;

  const btn = makeButton('＋ Save', async e => {
    e.preventDefault();
    e.stopPropagation();
    btn.disabled = true;
    btn.textContent = '…';
    try {
      const text = site.extractText(row);
      const ok = await saveText(text);
      btn.textContent = ok ? '✓ Saved' : '✗ Error';
      btn.classList.toggle('zp-saved', ok);
      btn.classList.toggle('zp-error', !ok);
    } catch {
      btn.textContent = '✗ Error';
      btn.classList.add('zp-error');
    }
  });

  target.appendChild(btn);
}

function injectAll(site) {
  document.querySelectorAll(site.resultSelector).forEach(row => injectRow(site, row));
}

// ── Entry point ──────────────────────────────────────────────────────────────

function init() {
  const site = getSite();
  if (!site) return;

  // Inject existing rows immediately
  injectAll(site);

  // Watch for dynamically added rows (AJAX navigation)
  const observer = new MutationObserver(() => injectAll(site));
  observer.observe(document.body, { childList: true, subtree: true });
}

// Run after DOM is available
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
