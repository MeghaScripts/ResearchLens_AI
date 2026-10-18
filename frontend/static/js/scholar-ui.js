/* scholar-ui.js -- additive UI for the Google Scholar (ScrapingBee) + OpenAlex
 * budget integrations and the "verification did not complete" banner.
 * Touches no existing file's logic: it observes the page and adds elements.
 *
 *  1. "Verification not completed" banner on the results page (it sniffs the
 *     analysis responses; nothing is guessed, it only reports what the server said).
 *  2. A "Check on Scholar" button on every gap card -> /api/scholar/check-gap
 *  3. A "Google Scholar" source checkbox (off by default: it spends credits)
 *  4. A one-line status for OpenAlex (key / daily budget) and Scholar.
 */
(function () {
  'use strict';

  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ── 1. Verification banner ────────────────────────────────────────
  function showVerificationBanner(v) {
    const host = document.getElementById('tab-overview');
    if (!host || !v || typeof v !== 'object') return;
    let el = document.getElementById('rlVerifyBanner');
    if (!el) {
      el = document.createElement('div');
      el.id = 'rlVerifyBanner';
      host.insertBefore(el, host.firstChild);
    }
    if (v.ran === false) {
      const why = v.error || v.reason || 'verification did not run';
      el.className = 'rl-banner rl-banner--warn';
      el.innerHTML = '<b>⚠ Paper verification did not complete.</b> ' +
        'The papers below were <b>not</b> checked for authenticity or retractions (' + esc(why) + '). ' +
        'The research gaps are AI suggestions built on unchecked papers — treat them as candidates to verify, not findings.';
    } else if (v.rejected_removed_from_analysis) {
      el.className = 'rl-banner rl-banner--info';
      el.innerHTML = '<b>ℹ Verification completed.</b> ' + esc(v.rejected_removed_from_analysis) +
        ' paper(s) were rejected and excluded from the analysis. Gaps are still AI judgments — verify each one.';
    } else {
      el.className = 'rl-banner rl-banner--ok';
      el.innerHTML = '<b>✓ Verification completed.</b> Papers had no evidence of a problem. ' +
        'That does not make the AI-generated gaps correct — verify each one against its cited papers.';
    }
  }

  function installFetchSniffer() {
    if (window.__rlScholarFetchWrapped) return;
    window.__rlScholarFetchWrapped = true;
    const orig = window.fetch;
    window.fetch = async function (input, init) {
      const res = await orig.apply(this, arguments);
      try {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        if (/\/api\/(analyze(-multi)?(\/status\/[^/?]+)?|deep-research)(\?|$)/.test(url) && res.ok) {
          res.clone().json().then((d) => {
            const payload = d && d.result ? d.result : d;     // status endpoint nests the result
            if (payload && payload.verification && (d.status === undefined || d.status === 'done')) {
              showVerificationBanner(payload.verification);
            }
          }).catch(() => {});
        }
      } catch (e) { /* never break the page */ }
      return res;
    };
  }

  // ── 2. Gap cards: "Check on Scholar" ──────────────────────────────
  function gapTitleOf(card) {
    const h = card.querySelector('.gap-title');
    if (!h) return '';
    const c = h.cloneNode(true);
    c.querySelectorAll('.cov-badge').forEach((n) => n.remove());
    return c.textContent.trim();
  }

  function renderScholarResult(box, data) {
    const matches = data.matches || [];
    const close = data.verdict === 'close_matches_found';
    let html = '<div class="rl-sch-verdict ' + (close ? 'is-close' : 'is-none') + '">' +
      (close ? '⚠ Close existing work found — this gap may already be addressed. Read these:'
             : 'No close matches in the first page of Google Scholar results. This does <b>not</b> prove the gap is real.') +
      '</div>';
    if (matches.length) {
      html += '<ol class="rl-sch-list">' + matches.map((m) => {
        const pct = Math.round((m.overlap || 0) * 100);
        const t = m.link ? '<a href="' + esc(m.link) + '" target="_blank" rel="noopener">' + esc(m.title) + '</a>' : esc(m.title);
        const bits = [m.year && m.year !== 'N/A' ? esc(m.year) : '', esc(m.venue || ''),
          m.cited_by != null ? 'cited by ' + esc(m.cited_by) : ''].filter(Boolean).join(' · ');
        return '<li><div class="rl-sch-title">' + t + ' <span class="rl-sch-pill" title="Share of the gap\'s key terms found in this result">' + pct + '% term overlap</span></div>' +
          (bits ? '<div class="rl-sch-meta">' + bits + '</div>' : '') +
          (m.snippet ? '<div class="rl-sch-snip">' + esc(m.snippet) + '</div>' : '') + '</li>';
      }).join('') + '</ol>';
    }
    html += '<div class="rl-sch-note">' + esc(data.note || '') + '</div>';
    box.innerHTML = html;
  }

  async function checkGap(card, btn) {
    let box = card.querySelector('.rl-scholar-result');
    if (!box) { box = document.createElement('div'); box.className = 'rl-scholar-result'; card.appendChild(box); }
    const title = gapTitleOf(card);
    const descEl = card.querySelector('.gap-section-text');
    box.innerHTML = '<div class="rl-sch-loading">Searching Google Scholar…</div>';
    btn.disabled = true;
    try {
      const res = await fetch('/api/scholar/check-gap', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ gap_title: title, description: descEl ? descEl.textContent.trim() : '' }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const hint = data.error_type === 'not_configured'
          ? ' Add SCRAPINGBEE_API_KEY to backend/.env and restart the server.' : '';
        box.innerHTML = '<div class="rl-sch-error">' + esc(data.error || ('Request failed (' + res.status + ')')) + esc(hint) + '</div>';
      } else {
        renderScholarResult(box, data);
      }
    } catch (e) {
      box.innerHTML = '<div class="rl-sch-error">Could not reach the server: ' + esc(e.message) + '</div>';
    } finally { btn.disabled = false; }
  }

  function decorateCards() {
    document.querySelectorAll('#gapsGrid .gap-card').forEach((card) => {
      if (card.querySelector('.rl-scholar-btn')) return;
      const top = card.querySelector('.gap-top');
      if (!top) return;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'gap-proof-btn rl-scholar-btn';
      btn.title = 'Search Google Scholar (via ScrapingBee) for existing work on this gap';
      btn.textContent = '🎓 Check on Scholar';
      btn.addEventListener('click', () => checkGap(card, btn));
      top.appendChild(btn);
    });
  }

  function observeGaps() {
    const start = () => {
      const grid = document.getElementById('gapsGrid');
      if (!grid) return false;
      new MutationObserver(decorateCards).observe(grid, { childList: true });
      decorateCards();
      return true;
    };
    if (!start()) document.addEventListener('DOMContentLoaded', start);
  }

  // ── 3 + 4. Source checkbox and status line ────────────────────────
  function addSourceUi(status) {
    const row = document.querySelector('.pe-source-row');
    if (!row || document.getElementById('rlScholarChip')) return;
    const sch = status.scholar || {};
    const chip = document.createElement('label');
    chip.id = 'rlScholarChip';
    chip.className = 'pe-source-chip' + (sch.configured ? '' : ' pe-source-locked');
    chip.title = sch.configured
      ? 'Google Scholar via ScrapingBee. Costs ScrapingBee credits per page (' + sch.requests_left_today + ' requests left today). Results have a snippet, not an abstract, and usually no DOI.'
      : 'Needs SCRAPINGBEE_API_KEY in backend/.env — not configured, so it is disabled rather than silently doing nothing.';
    chip.innerHTML = '<input type="checkbox" class="paperSourceCb" value="Google Scholar" ' +
      (sch.configured ? '' : 'disabled') + '/> Google Scholar' + (sch.configured ? '' : ' 🔒');
    row.appendChild(chip);

    const oa = status.openalex || {};
    const notes = [];
    if (oa.blocked) notes.push('OpenAlex is paused (' + esc(oa.blocked_reason || 'rate-limited') + ', ~' + Math.ceil((oa.blocked_for_seconds || 0) / 60) + ' min). Verification will label affected papers as not checked.');
    else if (!oa.api_key_configured) notes.push('OpenAlex has no API key set — it now allows only a tiny daily budget without one. Free key: openalex.org/settings/api, then OPENALEX_API_KEY in backend/.env.');
    if (notes.length) {
      const n = document.createElement('div');
      n.className = 'rl-source-note';
      n.innerHTML = '⚠ ' + notes.join(' ');
      row.parentNode.insertBefore(n, row.nextSibling);
    }
  }

  async function loadStatus(attempt) {
    try {
      const res = await fetch('/api/integrations/status');
      if (res.ok) { addSourceUi(await res.json()); return; }
    } catch (e) { /* retry below */ }
    if ((attempt || 0) < 4) setTimeout(() => loadStatus((attempt || 0) + 1), 1500);   // wait for login token
  }

  function init() {
    installFetchSniffer();
    observeGaps();
    loadStatus(0);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
