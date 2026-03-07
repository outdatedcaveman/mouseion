'use strict';

/**
 * Content script — extracts citation metadata from the current page.
 * Looks for DOI, arXiv ID, citation meta tags, JSON-LD, and Dublin Core.
 */

function extractMetadata() {
  // ── DOI ──────────────────────────────────────────────────────────────────
  // 1. <meta name="citation_doi"> / <meta name="DC.Identifier">
  let doi = (
    document.querySelector('meta[name="citation_doi"]') ||
    document.querySelector('meta[name="DC.Identifier"][scheme="doi"]') ||
    document.querySelector('meta[name="prism.doi"]')
  )?.content?.replace(/^https?:\/\/doi\.org\//i, '').trim();

  // 2. Canonical URL / og:url patterns
  if (!doi) {
    const urlsToCheck = [
      document.querySelector('link[rel="canonical"]')?.href,
      document.querySelector('meta[property="og:url"]')?.content,
      window.location.href,
    ];
    for (const u of urlsToCheck) {
      if (!u) continue;
      const m = u.match(/\bdoi\.org\/(10\.[^?#\s]+)/i);
      if (m) { doi = decodeURIComponent(m[1]).trim(); break; }
      const m2 = u.match(/(10\.\d{4,9}\/[^\s?#]+)/);
      if (m2) { doi = m2[1].trim(); break; }
    }
  }

  // 3. JSON-LD
  if (!doi) {
    document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
      if (doi) return;
      try {
        const d = JSON.parse(s.textContent);
        const candidates = Array.isArray(d) ? d : [d];
        for (const c of candidates) {
          const id = c['@id'] || c.identifier || '';
          const m = String(id).match(/10\.[^\s]+/);
          if (m) { doi = m[0].replace(/[.,;]$/, ''); break; }
        }
      } catch (_) {}
    });
  }

  // ── arXiv ─────────────────────────────────────────────────────────────────
  let arxiv = null;
  {
    const m = window.location.href.match(/arxiv\.org\/(?:abs|pdf|html)\/(\d{4}\.\d{4,5}(?:v\d+)?)/i);
    if (m) arxiv = m[1];
  }
  if (!arxiv) {
    const m2 = (
      document.querySelector('meta[name="arxiv_id"]') ||
      document.querySelector('meta[name="citation_arxiv_id"]')
    )?.content?.match(/(\d{4}\.\d{4,5}(?:v\d+)?)/);
    if (m2) arxiv = m2[1];
  }

  // ── Title ─────────────────────────────────────────────────────────────────
  const title = (
    document.querySelector('meta[name="citation_title"]') ||
    document.querySelector('meta[property="og:title"]') ||
    document.querySelector('meta[name="DC.Title"]')
  )?.content || document.title || null;

  return {
    doi:   doi   || null,
    arxiv: arxiv || null,
    title: title ? title.trim() : null,
    url:   window.location.href,
  };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === 'GET_METADATA') {
    sendResponse(extractMetadata());
  }
  return true; // keep channel open for async
});
