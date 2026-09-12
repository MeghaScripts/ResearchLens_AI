/*
 * Publication Scope card.
 *
 * Flow: Primary Domain -> Secondary Domain (cascading, from the curated
 * PRIMARY_TO_SECONDARY table) -> Research Topic (the existing free-text
 * field above, untouched) -> Research Papers, which has THREE layers,
 * all genuinely functional, no reference-only decoration:
 *
 *   1. The base 3 checkboxes (arXiv / Semantic Scholar / OpenAlex).
 *   2. "Recommended for <domain>" — real additional sources
 *      (academic_sources.py) recommended for the selected Primary
 *      Domain, from PRIMARY_DOMAIN_SOURCES.
 *   3. "Specific venues for your domain" — real, specific, filterable
 *      venue names (NeurIPS, CVPR, NEJM, ...) for the selected Primary
 *      or Secondary Domain, from SPECIFIC_VENUES_BY_FIELD. Checking one
 *      sends it as an actual `venue` filter to Semantic Scholar. Only
 *      genuine venue names appear here — umbrella publisher/society
 *      names (IEEE, ACM, ACS, ...) were deliberately left out because
 *      Semantic Scholar can't filter by those (see publication_data.py).
 *
 * Everything here is read directly by app.js's submit handler via
 * getElementById at submit time, so script load order between this
 * file and app.js never matters.
 */

function esc_(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function initPublicationScope() {
  initPaperSources();
  await initPrimarySecondaryCascade();
}

// ─── Base 3 source checkboxes ──────────────────────────────────────

function syncPaperSources() {
  const hidden = document.getElementById('paperSourcesInput');
  if (!hidden) return;
  const checked = Array.from(document.querySelectorAll('.paperSourceCb:checked')).map(cb => cb.value);
  hidden.value = checked.join(',');
}

function initPaperSources() {
  const hidden = document.getElementById('paperSourcesInput');
  if (!hidden) return;
  // Event delegation on the document so checkboxes added later (the
  // per-domain "Recommended for X" ones) are picked up automatically
  // without re-binding listeners.
  document.addEventListener('change', (e) => {
    if (e.target.classList && e.target.classList.contains('paperSourceCb')) {
      // Remember a manual uncheck (while not in strict venue mode) so
      // applyStrictVenueMode's restore step doesn't silently re-check
      // something the user deliberately turned off.
      if (!e.target.disabled) {
        if (e.target.checked) delete e.target.dataset.userUnchecked;
        else e.target.dataset.userUnchecked = '1';
      }
      syncPaperSources();
    } else if (e.target.classList && e.target.classList.contains('venueFilterCb')) {
      // Specific-venue chips (AAAI/NeurIPS/ICML/ICLR/...) are added
      // dynamically by loadSpecificVenues, so this has to be delegated
      // the same way paperSourceCb is above. Without this, checking a
      // venue box never updates the hidden venueFilterInput, so
      // venue_filter is silently never sent to the backend and strict
      // venue mode (Semantic Scholar only) never engages — the app
      // just falls back to whatever base/recommended source checkboxes
      // happen to be checked instead.
      syncVenueFilter();
    }
  });
  syncPaperSources(); // base 3 checked by default
}

// ─── Layer 2: recommended additional sources per Primary Domain ───

async function loadRecommendedSources(primary) {
  const block = document.getElementById('recommendedSourcesBlock');
  const label = document.getElementById('recommendedSourcesLabel');
  const row = document.getElementById('recommendedSourcesRow');
  const note = document.getElementById('recommendedSourcesNote');
  if (!block) return;
  if (!primary) { block.style.display = 'none'; row.innerHTML = ''; return; }

  try {
    const res = await fetch(`/api/primary-domains/sources?primary=${encodeURIComponent(primary)}`);
    const data = await res.json();
    row.innerHTML = '';
    if (!data.curated || !data.sources.length) {
      block.style.display = 'none';
      return;
    }
    block.style.display = 'block';
    label.textContent = `Recommended for ${primary}`;
    const baseThree = new Set(['ArXiv', 'Semantic Scholar', 'OpenAlex']);
    for (const src of data.sources) {
      if (baseThree.has(src)) continue; // already shown above, don't duplicate
      const isKeyed = src in data.keyed_source_status;
      const configured = isKeyed ? data.keyed_source_status[src] : true;
      const chip = document.createElement('label');
      chip.className = 'pe-source-chip' + (configured ? '' : ' pe-source-locked');
      chip.title = configured ? '' : `Needs a free API key (see backend/academic_sources.py) — not configured on this server, so it's disabled rather than silently doing nothing.`;
      chip.innerHTML = `<input type="checkbox" class="paperSourceCb" value="${esc_(src)}" ${configured ? 'checked' : 'disabled'}/> ${esc_(src)}${configured ? '' : ' 🔒'}`;
      row.appendChild(chip);
    }
    const notes = [];
    if (data.not_available && data.not_available.length) {
      notes.push(`No public API exists for: ${data.not_available.join(', ')} — subscription/institutional-only, can't be queried by this app.`);
    }
    if (data.not_yet_wired && data.not_yet_wired.length) {
      notes.push(`Has a real API but not wired in yet: ${data.not_yet_wired.join(', ')}.`);
    }
    note.textContent = notes.join(' ');
    syncPaperSources(); // reflect newly-added, default-checked recommended sources
  } catch (e) {
    console.warn('Publication Scope: could not load recommended sources', e);
    block.style.display = 'none';
  }
}

// ─── Layer 3: specific, real, filterable venue names ───────────────

function syncVenueFilter() {
  const hidden = document.getElementById('venueFilterInput');
  if (!hidden) return;
  const checked = Array.from(document.querySelectorAll('.venueFilterCb:checked')).map(cb => cb.value);
  hidden.value = checked.join(',');
  applyStrictVenueMode(checked);
}

// When any specific venue is checked, results come directly from that
// venue's own dedicated source (DBLP's per-venue `streamid:` search for
// AAAI/NeurIPS/ICML/ICLR; Semantic Scholar's `venue=` filter as a
// fallback for venues without a DBLP mapping) — never mixed with ArXiv
// or OpenAlex's general search, which would dilute results with
// unrelated papers. So this locks the base-3 and "Recommended"
// checkboxes off while a venue filter is active, and restores them the
// moment every venue is unchecked. The backend enforces the same rule
// independently (see fetch_papers_multi_source's "STRICT VENUE MODE" in
// app.py) — this UI state is just making that real behavior visible,
// not the only thing enforcing it.
function applyStrictVenueMode(checkedVenues) {
  const notice = document.getElementById('strictVenueNotice');
  const allSourceCbs = Array.from(document.querySelectorAll('.paperSourceCb'));
  const active = checkedVenues.length > 0;
  for (const cb of allSourceCbs) {
    cb.disabled = active;
    if (active) cb.checked = false;
    else if (!cb.dataset.userUnchecked) cb.checked = true; // restore default state
  }
  if (notice) {
    notice.style.display = active ? 'block' : 'none';
    if (active) {
      notice.textContent = `Venue filter active (${checkedVenues.join(', ')}) — only papers actually published in exactly these venues are fetched, directly from each venue's own source. Other sources are locked off so results aren't diluted with unrelated papers. Uncheck all venues above to search normally again.`;
    }
  }
  syncPaperSources();
}

async function loadSpecificVenues(primary, secondary) {
  const block = document.getElementById('specificVenuesBlock');
  const row = document.getElementById('specificVenuesRow');
  if (!block) return;
  const fields = [primary, secondary].filter(Boolean);
  if (!fields.length) { block.style.display = 'none'; row.innerHTML = ''; syncVenueFilter(); return; }

  try {
    const params = fields.map(f => `field=${encodeURIComponent(f)}`).join('&');
    const res = await fetch(`/api/specific-venues?${params}`);
    const data = await res.json();
    row.innerHTML = '';
    if (!data.venues.length) {
      block.style.display = 'none';
      syncVenueFilter();
      return;
    }
    block.style.display = 'block';
    for (const v of data.venues) {
      const chip = document.createElement('label');
      chip.className = 'pe-source-chip';
      chip.innerHTML = `<input type="checkbox" class="venueFilterCb" value="${esc_(v)}"/> ${esc_(v)}`;
      row.appendChild(chip);
    }
    syncVenueFilter();
  } catch (e) {
    console.warn('Publication Scope: could not load specific venues', e);
    block.style.display = 'none';
  }
}

// ─── Primary Domain -> Secondary Domain cascade ────────────────────

async function initPrimarySecondaryCascade() {
  const primarySelect = document.getElementById('primaryDomainSelect');
  const secondarySelect = document.getElementById('secondaryDomainSelect');
  const secondaryHint = document.getElementById('secondaryDomainHint');
  if (!primarySelect) return;

  try {
    const res = await fetch('/api/primary-domains');
    const data = await res.json();
    for (const name of data.primary_domains) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      primarySelect.appendChild(opt);
    }
  } catch (e) {
    console.warn('Publication Scope: could not load primary domains', e);
    return;
  }

  if (!secondarySelect) return;

  document.addEventListener('change', (e) => {
    if (e.target === secondarySelect) {
      loadSpecificVenues(primarySelect.value, secondarySelect.value);
    }
  });

  primarySelect.addEventListener('change', async () => {
    loadRecommendedSources(primarySelect.value);
    loadSpecificVenues(primarySelect.value, '');
    secondarySelect.innerHTML = '';
    secondaryHint.textContent = '';
    if (!primarySelect.value) {
      secondarySelect.disabled = true;
      const opt = document.createElement('option');
      opt.textContent = 'Select a Primary Domain first';
      secondarySelect.appendChild(opt);
      return;
    }
    secondarySelect.disabled = false;
    const loadingOpt = document.createElement('option');
    loadingOpt.textContent = 'Loading…';
    secondarySelect.appendChild(loadingOpt);

    try {
      const res = await fetch(`/api/primary-domains/secondary?primary=${encodeURIComponent(primarySelect.value)}`);
      const data = await res.json();
      secondarySelect.innerHTML = '';
      const noneOpt = document.createElement('option');
      noneOpt.value = '';
      noneOpt.textContent = data.curated ? 'None / not sure' : 'No curated breakdown — use Research Topic above';
      secondarySelect.appendChild(noneOpt);
      for (const name of data.secondary_domains) {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        secondarySelect.appendChild(opt);
      }
      secondaryHint.textContent = data.curated
        ? ''
        : `No curated secondary-domain list for "${primarySelect.value}" yet — describe your specific angle in the Research Topic field above instead.`;
    } catch (e) {
      secondarySelect.innerHTML = '<option value="">Could not load — skip this field</option>';
      console.warn('Publication Scope: could not load secondary domains', e);
    }
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initPublicationScope);
} else {
  initPublicationScope();
}
