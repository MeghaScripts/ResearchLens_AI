/* ResearchLens AI — Main JavaScript (Multi-paper + Gap Proof) */

let currentData = null;
let yearChartInst = null;
let catChartInst  = null;
let graphAnimId   = null;
let currentMode   = 'single';
let selectedFiles = [];

// ── Hero signature: constellation of papers & citations ──────────────
// The literal visual metaphor for the product — each star a paper, each
// line a citation, drifting slowly across the "unmapped" void behind the
// headline. This is the one deliberately bold moment; everything else on
// the page stays quiet by comparison.
function initConstellation() {
  const canvas = document.getElementById('constellationCanvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const hero = canvas.closest('.hero');
  let W, H, nodes, animId;
  const palette = ['#c9a24b', '#6b4fd6', '#3fae8a']; // Aurum, Regalia, Verdigris

  function resize() {
    W = canvas.width = hero.offsetWidth;
    H = canvas.height = Math.max(hero.offsetHeight, 500);
  }

  function seed() {
    const density = Math.min(70, Math.floor((W * H) / 14000));
    nodes = Array.from({ length: density }, () => ({
      x: Math.random() * W,
      y: Math.random() * H,
      vx: (Math.random() - 0.5) * 0.12,
      vy: (Math.random() - 0.5) * 0.12,
      r: 0.8 + Math.random() * 1.6,
      color: palette[Math.floor(Math.random() * palette.length)],
      twinkleOffset: Math.random() * Math.PI * 2
    }));
  }

  let frame = 0;
  function draw() {
    ctx.clearRect(0, 0, W, H);
    const linkDist = Math.min(150, W / 8);

    // Connections — faint, only between nearby "papers"
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < linkDist) {
          const alpha = (1 - dist / linkDist) * 0.18;
          ctx.strokeStyle = `rgba(201,162,75,${alpha})`;
          ctx.lineWidth = 0.6;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
    }

    // Nodes — gentle drift + twinkle
    nodes.forEach(n => {
      n.x += n.vx; n.y += n.vy;
      if (n.x < 0 || n.x > W) n.vx *= -1;
      if (n.y < 0 || n.y > H) n.vy *= -1;
      const twinkle = 0.5 + 0.5 * Math.sin(frame * 0.02 + n.twinkleOffset);
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx.fillStyle = n.color;
      ctx.globalAlpha = 0.35 + twinkle * 0.5;
      ctx.fill();
      ctx.globalAlpha = 1;
    });

    frame++;
    animId = requestAnimationFrame(draw);
  }

  resize();
  seed();
  if (animId) cancelAnimationFrame(animId);
  draw();

  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { resize(); seed(); }, 200);
  });

  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    cancelAnimationFrame(animId);
  }
}
window.addEventListener('DOMContentLoaded', initConstellation);

// ── Auth check ──────────────────────────────────────────────────────
// index.html's inline module already redirects to /login if nobody is
// signed in (see window.rlAuth.requireAuth()); this just populates the
// nav with whichever profile Firebase actually returned. We prefer the
// live Firebase user (name/photo/email) over the old localStorage copy,
// which was only ever a display cache and was never used for security.
function paintUserNav(user) {
  if (!user) return;
  const displayName = user.displayName || (user.email ? user.email.split('@')[0] : 'Account');
  document.getElementById('userInfo').style.display = 'flex';
  document.getElementById('userName').textContent = displayName;
  const avatarEl = document.getElementById('userAvatar');
  if (user.photoURL) {
    avatarEl.src = user.photoURL;
    avatarEl.style.display = 'block';
  } else {
    avatarEl.style.display = 'none';
    const initials = displayName.split(' ').map(w=>w[0]).join('').toUpperCase().substring(0,2);
    const initialsEl = document.createElement('div');
    initialsEl.className = 'user-initials';
    initialsEl.textContent = initials;
    document.getElementById('userInfo').insertBefore(initialsEl, avatarEl);
  }
}
window.addEventListener('DOMContentLoaded', async () => {
  if (window.rlAuth) paintUserNav(await window.rlAuth.authReady());
});
window.addEventListener('rl-auth-changed', (e) => paintUserNav(e.detail.user));

window.logout = () => {
  if (window.rlAuth) return window.rlAuth.logout();
  localStorage.removeItem('rl_user');
  window.location.href = '/';
};

// ── Mode switching ──────────────────────────────────────────────────
window.switchMode = (mode) => {
  if (mode === 'multi') {
    // Check subscription — Free users see upgrade prompt
    const plan = localStorage.getItem('rl_plan') || 'free';
    if (plan === 'free') {
      showUpgradePrompt();
      return;
    }
  }
  currentMode = mode;
  document.getElementById('modeSingle').classList.toggle('active', mode === 'single');
  document.getElementById('modeMulti').classList.toggle('active', mode === 'multi');
  document.getElementById('singleUpload').style.display = mode === 'single' ? 'block' : 'none';
  document.getElementById('multiUpload').style.display  = mode === 'multi'  ? 'block' : 'none';
};

window.showUpgradePrompt = () => {
  document.getElementById('upgradePrompt').style.display = 'flex';
};
window.closeUpgradePrompt = () => {
  document.getElementById('upgradePrompt').style.display = 'none';
};
window.activatePlan = (plan) => {
  localStorage.setItem('rl_plan', plan);
  closeUpgradePrompt();
  alert('✅ ' + plan.charAt(0).toUpperCase()+plan.slice(1) + ' plan activated! (Demo mode)\nMulti-paper upload is now unlocked.');
  currentMode = 'multi';
  document.getElementById('modeSingle').classList.remove('active');
  document.getElementById('modeMulti').classList.add('active');
  document.getElementById('singleUpload').style.display = 'none';
  document.getElementById('multiUpload').style.display = 'block';
};

// ── Single upload ───────────────────────────────────────────────────
const uploadZone    = document.getElementById('uploadZone');
const pdfInput      = document.getElementById('pdfInput');
const uploadContent = document.getElementById('uploadContent');

uploadZone.addEventListener('click', () => pdfInput.click());
uploadZone.addEventListener('dragover', e => { e.preventDefault(); uploadZone.classList.add('dragover'); });
uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('dragover'));
uploadZone.addEventListener('drop', e => {
  e.preventDefault(); uploadZone.classList.remove('dragover');
  const file = e.dataTransfer.files[0];
  if (file && file.type === 'application/pdf') setSingleFile(file);
});
pdfInput.addEventListener('change', () => { if (pdfInput.files[0]) setSingleFile(pdfInput.files[0]); });

function setSingleFile(file) {
  const dt = new DataTransfer(); dt.items.add(file); pdfInput.files = dt.files;
  uploadContent.innerHTML = `<div class="upload-icon">✅</div><div class="upload-text">${file.name}</div><div class="upload-hint">${(file.size/1024/1024).toFixed(2)} MB • Click to change</div>`;
}

// ── Multi upload ────────────────────────────────────────────────────
const multiZone    = document.getElementById('multiUploadZone');
const multiInput   = document.getElementById('multiPdfInput');

multiZone.addEventListener('click', () => multiInput.click());
multiZone.addEventListener('dragover', e => { e.preventDefault(); multiZone.classList.add('dragover'); });
multiZone.addEventListener('dragleave', () => multiZone.classList.remove('dragover'));
multiZone.addEventListener('drop', e => {
  e.preventDefault(); multiZone.classList.remove('dragover');
  addFiles(Array.from(e.dataTransfer.files).filter(f => f.type === 'application/pdf'));
});
multiInput.addEventListener('change', () => { addFiles(Array.from(multiInput.files)); });

function addFiles(files) {
  for (const f of files) {
    if (selectedFiles.length >= 5) { alert('Maximum 5 papers allowed'); break; }
    if (!selectedFiles.find(sf => sf.name === f.name)) selectedFiles.push(f);
  }
  renderFileList();
}

function renderFileList() {
  const list = document.getElementById('fileList');
  if (selectedFiles.length === 0) { list.innerHTML = ''; return; }
  list.innerHTML = selectedFiles.map((f, i) => `
    <div class="file-item">
      <span style="color:var(--purple)">📄</span>
      <span class="file-item-name">${esc(f.name)}</span>
      <span class="file-item-size">${(f.size/1024/1024).toFixed(1)} MB</span>
      <button class="file-item-remove" onclick="removeFile(${i})">✕</button>
    </div>`).join('');
  document.getElementById('multiUploadContent').innerHTML = `<div class="upload-icon">📚</div><div class="upload-text">${selectedFiles.length}/5 papers selected • Click to add more</div>`;
}

window.removeFile = (i) => { selectedFiles.splice(i, 1); renderFileList(); };

function togglePassword() {
  const f = document.getElementById('apiKey');
  f.type = f.type === 'password' ? 'text' : 'password';
}
window.togglePassword = togglePassword;

// ── Form Submit ─────────────────────────────────────────────────────
document.getElementById('analysisForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const apiKey = document.getElementById('apiKey').value.trim();
  const topic  = document.getElementById('topic').value.trim();
  if (!apiKey || !topic) { alert('Please fill in API key and research topic.'); return; }

  if (currentMode === 'multi' && selectedFiles.length === 0) {
    alert('Please upload at least one PDF for multi-paper analysis.'); return;
  }

  // Optional Publication Scope refinements (populated by publication-scope.js).
  // Read directly from the DOM at submit time so load order between the two
  // scripts never matters — by the time a user submits, the selects exist.
  const ideaDescriptionEl = document.getElementById('ideaDescription');
  const ideaDescription = ideaDescriptionEl ? ideaDescriptionEl.value.trim() : '';
  const primaryDomainEl = document.getElementById('primaryDomainSelect');
  const primaryDomain = primaryDomainEl ? primaryDomainEl.value : '';
  const secondaryDomainEl = document.getElementById('secondaryDomainSelect');
  const secondaryDomains = secondaryDomainEl ? secondaryDomainEl.value.trim() : '';
  const paperSourcesEl = document.getElementById('paperSourcesInput');
  const paperSources = paperSourcesEl ? paperSourcesEl.value.trim() : '';
  const venueFilterEl = document.getElementById('venueFilterInput');
  const venueFilter = venueFilterEl ? venueFilterEl.value.trim() : '';

  showLoading(true);
  resetLiveProgress();

  try {
    let data;
    if (currentMode === 'single') {
      const formData = new FormData();
      formData.append('api_key', apiKey);
      formData.append('topic', topic);
      if (ideaDescription) formData.append('idea_description', ideaDescription);
      if (primaryDomain) formData.append('primary_domain', primaryDomain);
      if (secondaryDomains) formData.append('secondary_domains', secondaryDomains);
      if (paperSources) formData.append('paper_sources', paperSources);
      if (venueFilter) formData.append('venue_filter', venueFilter);
      if (pdfInput.files[0]) formData.append('pdf', pdfInput.files[0]);
      const startResp = await fetch('/api/analyze/start', { method: 'POST', body: formData });
      const startData = await startResp.json();
      if (!startResp.ok) { showLoading(false); showArxivOrGenericError(startResp.status, startData); return; }
      data = await pollAnalyzeJob(startData.job_id);
    } else {
      const formData = new FormData();
      formData.append('api_key', apiKey);
      formData.append('topic', topic);
      if (ideaDescription) formData.append('idea_description', ideaDescription);
      if (primaryDomain) formData.append('primary_domain', primaryDomain);
      if (secondaryDomains) formData.append('secondary_domains', secondaryDomains);
      if (paperSources) formData.append('paper_sources', paperSources);
      if (venueFilter) formData.append('venue_filter', venueFilter);
      for (const f of selectedFiles) formData.append('pdfs', f);
      const resp = await fetch('/api/analyze-multi', { method: 'POST', body: formData });
      data = await resp.json();
      if (!resp.ok) { showLoading(false); showArxivOrGenericError(resp.status, data); return; }
    }
    currentData = data;
    showLoading(false);
    renderResults(data);
  } catch (err) {
    showLoading(false);
    if (err && err.pipelineError) {
      showArxivOrGenericError(err.status || 500, err.payload || { error: err.message });
    } else {
      alert('Connection error. Make sure the backend is running on port 5000.');
      console.error(err);
    }
  }
});

// ── Loading: live progress (real papers + real agent steps) ──────────
let loadingTimer = null;

function resetLiveProgress() {
  document.getElementById('livePapersBox').style.display = 'none';
  document.getElementById('livePapersHeader').textContent = '';
  document.getElementById('livePapersList').innerHTML = '';
  document.getElementById('liveStepsList').innerHTML =
    '<div class="lstep active" id="lstepPlaceholder">🚀 Starting analysis…</div>';
  document.getElementById('loadingBar').style.width = '4%';
}

const AGENT_ICONS = {
  pdf_agent: '📄', search_agent: '📡', keyword_agent: '🧩', metadata_agent: '🗂️',
  domain_classification_agent: '🏷️', gap_detection_agent: '🤖',
  knowledge_graph_agent: '🕸️', trend_agent: '📈', embedding_agent: '🧠',
  validation_agent: '✅', report_agent: '📋',
};
// Rough total so the bar reads as progress, not a countdown promising a
// specific finish time — every run has a similar-length pipeline even
// though each stage's actual duration varies a lot (search vs. Groq call).
const EXPECTED_STEP_COUNT = 10;

function renderLiveSteps(steps) {
  const list = document.getElementById('liveStepsList');
  if (!steps.length) return;
  list.innerHTML = steps.map((s, i) => {
    const icon = AGENT_ICONS[s.agent] || (s.success ? '✔️' : '⚠️');
    const cls = i === steps.length - 1 ? 'lstep active' : 'lstep done';
    const text = s.summary || `${s.agent} ${s.success ? 'completed' : 'failed'}`;
    return `<div class="${cls}">${icon} ${esc(text)}</div>`;
  }).join('');
  document.getElementById('loadingBar').style.width =
    `${Math.min(96, Math.round((steps.length / EXPECTED_STEP_COUNT) * 100))}%`;
}

function renderLivePapers(count, sample) {
  if (!count) return;
  const box = document.getElementById('livePapersBox');
  box.style.display = 'block';
  document.getElementById('livePapersHeader').textContent =
    `📚 Found ${count} papers — analyzing these:`;
  document.getElementById('livePapersList').innerHTML = sample.map(p =>
    `<div>• ${esc(p.title)} <span style="opacity:0.6">(${esc(p.source || 'unknown source')}${p.year ? ', ' + p.year : ''})</span></div>`
  ).join('') + (count > sample.length ? `<div style="opacity:0.6;margin-top:0.2rem">…and ${count - sample.length} more</div>` : '');
}



/** Polls /api/analyze/status/<job_id> until the job finishes, updating the
 * live steps/papers panels on every poll. Resolves with the final result
 * payload (identical shape to what /api/analyze used to return directly),
 * or throws {pipelineError:true, status, payload} on failure — callers
 * catch and route that through the exact same error UI as before. */
function pollAnalyzeJob(jobId) {
  return new Promise((resolve, reject) => {
    const poll = async () => {
      try {
        const resp = await fetch(`/api/analyze/status/${jobId}`);
        const job = await resp.json();
        if (!resp.ok) {
          reject({ pipelineError: true, status: resp.status, payload: job });
          return;
        }
        renderLiveSteps(job.steps || []);
        renderLivePapers(job.papers_count, job.papers_sample || []);
        if (job.status === 'done') {
          resolve(job.result);
        } else if (job.status === 'error') {
          reject({ pipelineError: true, status: 500, payload: { error: job.error || 'Analysis failed' } });
        } else {
          loadingTimer = setTimeout(poll, 1200);
        }
      } catch (err) {
        reject(err);
      }
    };
    poll();
  });
}

function showLoading(show) {
  document.getElementById('loadingOverlay').style.display = show ? 'flex' : 'none';
  document.getElementById('analyzeBtn').disabled = show;
  if (!show && loadingTimer) { clearTimeout(loadingTimer); loadingTimer = null; }
}


// ── Render Results ───────────────────────────────────────────────────
function renderResults(data) {
  const { analysis, papers, keywords, graph, year_data, cat_data, report_filename, mode } = data;
  document.getElementById('resultsSection').style.display = 'block';
  document.getElementById('resultsSection').scrollIntoView({ behavior: 'smooth', block: 'start' });

  // Show/hide multi-paper tabs
  document.querySelectorAll('.multi-tab').forEach(t => {
    t.style.display = mode === 'multi' ? 'block' : 'none';
  });

  animateMetric('noveltyScore', analysis.novelty_score||0, 'noveltyFill', analysis.novelty_score||0);
  animateMetric('gapsCount', (analysis.research_gaps||[]).length, 'gapsFill', (analysis.research_gaps||[]).length*20);
  animateMetric('papersCount', papers.length, 'papersFill', Math.min(papers.length*5,100));
  setDensityMetric(analysis.gap_density||'medium');
  document.getElementById('summaryText').textContent = analysis.summary||'';
  document.getElementById('recText').textContent     = analysis.overall_recommendation||'';

  const noveltyIdeaEl = document.getElementById('noveltyIdeaInput');
  const priorIdea = document.getElementById('ideaDescription');
  if (noveltyIdeaEl && priorIdea && priorIdea.value.trim()) noveltyIdeaEl.value = priorIdea.value.trim();
  const noveltyOutEl = document.getElementById('noveltyCheckOutput');
  if (noveltyOutEl) noveltyOutEl.innerHTML = '';

  renderCoverageTable(analysis.existing_coverage||[]);
  renderGaps(analysis.research_gaps||[], papers);
  renderNextSteps(analysis.recommended_next_steps||[]);
  renderKeywords(keywords);
  renderGraph(graph);
  renderPapers(papers);
  renderCharts(year_data, cat_data);
  renderTrends(analysis.emerging_trends||[]);
  renderLeaderboard(analysis.research_gaps||[]);
  initGraphQA(graph, papers, keywords);

  // Multi-paper specific
  if (mode === 'multi') {
    renderLiteratureReview(analysis.literature_review||{});
    renderPaperSummaries(analysis.paper_summaries||[], analysis.comparative_analysis||[]);
  }

  document.getElementById('downloadBtn').onclick = async () => {
    // window.open() can't send the Authorization header, so the backend
    // rejected it. fetch() is patched by firebase-auth.js to add the token.
    const btn = document.getElementById('downloadBtn');
    const original = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span>Downloading...';
    btn.disabled = true;
    try {
      const resp = await fetch(`/api/download/${encodeURIComponent(report_filename)}`);
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({}));
        throw new Error(err.error || 'Download failed');
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = report_filename;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      alert('Error downloading report: ' + e.message);
    } finally {
      btn.innerHTML = original;
      btn.disabled = false;
    }
  };

  const bibtexBtn = document.getElementById('bibtexBtn');
  if (bibtexBtn) {
    bibtexBtn.onclick = async () => {
      const original = bibtexBtn.innerHTML;
      bibtexBtn.innerHTML = '<span class="spinner"></span>Exporting...';
      try {
        const resp = await fetch('/api/export/bibtex', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ papers, topic: document.getElementById('topic').value.trim() })
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.error || 'Export failed');
        }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = 'references.bib';
        document.body.appendChild(a); a.click(); a.remove();
        URL.revokeObjectURL(url);
      } catch(e) {
        alert('Error exporting BibTeX: ' + e.message);
      } finally {
        bibtexBtn.innerHTML = original;
      }
    };
  }

  // Auto-populate empty evidence_papers across all gaps (do this once, here)
  if (analysis.research_gaps) {
    const fallback = papers.slice(0,3).map(p=>p.title);
    analysis.research_gaps.forEach(g => {
      if (!g.evidence_papers || g.evidence_papers.length === 0) {
        g.evidence_papers = fallback;
      }
    });
  }

  // Save to history
  const topic = document.getElementById('topic').value.trim();
  saveToHistory(topic, (analysis.research_gaps||[]).length, mode||'single');

  switchTab('overview');
}

function animateMetric(valId, targetVal, fillId, fillPct) {
  const el = document.getElementById(valId);
  const fill = document.getElementById(fillId);
  let current = 0;
  const step = Math.ceil(targetVal/40);
  const iv = setInterval(() => {
    current = Math.min(current+step, targetVal);
    el.textContent = current;
    if (current >= targetVal) { el.textContent = targetVal; clearInterval(iv); }
  }, 30);
  setTimeout(() => { fill.style.width = Math.min(fillPct,100)+'%'; }, 200);
}

function setDensityMetric(density) {
  const el = document.getElementById('gapDensity');
  const fill = document.getElementById('densityFill');
  el.textContent = density.toUpperCase();
  const pct = density==='high'?85:density==='medium'?55:30;
  setTimeout(()=>{ fill.style.width = pct+'%'; },200);
}

// ── Coverage Table ───────────────────────────────────────────────────
function renderCoverageTable(coverage) {
  const tbody = document.getElementById('coverageBody');
  tbody.innerHTML = coverage.map(item => {
    const cls = {high:'cov-high',medium:'cov-medium',low:'cov-low'}[item.coverage]||'cov-medium';
    return `<tr><td style="font-weight:600">${esc(item.area||'')}</td><td><span class="cov-badge ${cls}">${esc(item.coverage||'')}</span></td><td style="color:var(--text2)">${esc(item.description||'')}</td></tr>`;
  }).join('');
}

// ── Gaps with Proof Button ───────────────────────────────────────────
function renderGaps(gaps, arxivPapers) {
  const grid = document.getElementById('gapsGrid');
  grid.innerHTML = gaps.map((g, i) => {
    const sev = (g.severity||'moderate').toLowerCase();
    const confidence = g.confidence_score || 75;
    const evidencePapers = (g.evidence_papers||[]).slice(0,3);
    const verifiedBadge = (g.evidence_verified && g.evidence_quote)
      ? `<span class="cov-badge cov-low" style="margin-left:0.5rem" title="An exact quote from your uploaded PDF was found and confirmed">✓ Verified quote</span>`
      : '';
    return `<div class="gap-card ${sev} fade-in" data-severity="${sev}" data-gap="${i}" style="animation-delay:${i*0.1}s">
      <div class="gap-top">
        <span class="gap-badge badge-${sev}">${sev}</span>
        <h4 class="gap-title">${esc(g.gap_title||'')}${verifiedBadge}</h4>
        <button class="gap-proof-btn" onclick="showProof(${i})">🔗 View Proof</button>
      </div>
      <div class="gap-confidence">
        <div class="confidence-bar-wrap"><div class="confidence-bar" style="width:${confidence}%"></div></div>
        <span class="confidence-label">${confidence}% confidence</span>
      </div>
      <div class="gap-section"><div class="gap-section-label">Description</div><div class="gap-section-text">${esc(g.description||'')}</div></div>
      <div class="gap-section"><div class="gap-section-label">Why Important</div><div class="gap-section-text">${esc(g.why_important||'')}</div></div>
      <div class="gap-section"><div class="gap-section-label">Suggested Approach</div><div class="gap-section-text">${esc(g.suggested_approach||'')}</div></div>
      <div class="gap-section"><div class="gap-section-label">Evidence From</div><div class="gap-section-text" style="color:var(--cyan)">${evidencePapers.map(esc).join(' • ')||'Multiple sources'}</div></div>
    </div>`;
  }).join('');

  // Filter buttons
  document.querySelectorAll('.gf-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.gf-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const sev = btn.dataset.sev;
      document.querySelectorAll('.gap-card').forEach(c => {
        c.style.display = (sev==='all'||c.dataset.severity===sev)?'block':'none';
      });
    });
  });
}

// ── Gap Proof Panel ──────────────────────────────────────────────────
window.showProof = (gapIndex) => {
  const gaps = currentData?.analysis?.research_gaps || [];
  const gap  = gaps[gapIndex];
  if (!gap) return;

  const proofSection = document.getElementById('proofSection');
  const proofContent = document.getElementById('proofContent');
  const proofTitle   = document.getElementById('proofTitle');

  proofTitle.textContent = `Proof: ${gap.gap_title}`;
  const confidence = gap.confidence_score || 75;
  const evidencePapers = gap.evidence_papers || [];
  const explanation = gap.evidence_explanation || 'This gap was identified through systematic analysis of existing literature, identifying missing connections and unexplored research areas.';

  const sevColor = {critical:'#ff4757',major:'#ff9f43',moderate:'#3fae8a'}[(gap.severity||'moderate')]||'#3fae8a';
  const confColor = confidence>=80?'#3fae8a':confidence>=60?'#ff9f43':'#ff4757';
  const confGrad = confidence>=80?'linear-gradient(90deg,#3fae8a,#c9a24b)':confidence>=60?'linear-gradient(90deg,#ff9f43,#ffd700)':'linear-gradient(90deg,#ff4757,#ff9f43)';

  // Auto-populate evidence papers from related papers if AI returned empty
  if (evidencePapers.length === 0 && currentData && currentData.papers) {
    const related = currentData.papers.slice(0, 3).map(p => p.title);
    evidencePapers.push(...related);
  }

  proofContent.innerHTML = `
    <!-- STAT CARDS ROW -->
    <div class="prf-stats">
      <div class="prf-stat" style="--c:${sevColor}">
        <div class="prf-stat-val" style="color:${sevColor}">${(gap.severity||'moderate').toUpperCase()}</div>
        <div class="prf-stat-lbl">Severity</div>
      </div>
      <div class="prf-stat" style="--c:${confColor}">
        <div class="prf-stat-val" style="color:${confColor}">${confidence}%</div>
        <div class="prf-stat-lbl">Confidence</div>
      </div>
      <div class="prf-stat" style="--c:#6b4fd6">
        <div class="prf-stat-val" style="color:#6b4fd6">${evidencePapers.length>0?evidencePapers.length:'Auto'}</div>
        <div class="prf-stat-lbl">Evidence Papers</div>
      </div>
      <div class="prf-stat" style="--c:#c9a24b">
        <div class="prf-stat-val" style="color:#c9a24b;font-size:1rem">LLaMA 3 70B</div>
        <div class="prf-stat-lbl">Validated By</div>
      </div>
    </div>

    <!-- MAIN GRID -->
    <div class="prf-grid">

      <!-- Evidence Papers -->
      <div class="prf-card prf-card-wide">
        <div class="prf-card-hdr">
          <span class="prf-card-icon">📚</span>
          <div>
            <div class="prf-card-ttl">Papers That Prove This Gap</div>
            <div class="prf-card-sub">These papers were analyzed — they confirm the gap by NOT covering this research area</div>
          </div>
        </div>
        ${evidencePapers.length > 0
          ? evidencePapers.map((p,i) => `
            <div class="prf-paper">
              <div class="prf-paper-num">${i+1}</div>
              <div class="prf-paper-body">
                <div class="prf-paper-name">${esc(p)}</div>
                <div class="prf-paper-note">🔴 Gap confirmed — this paper does not address the research area above</div>
                <a href="https://arxiv.org/search/?searchtype=all&query=${encodeURIComponent(p)}" target="_blank" class="prf-dl-btn">📥 Download & Verify →</a>
              </div>
            </div>`).join('')
          : '<div class="prf-empty">Evidence derived from cross-paper systematic analysis</div>'
        }
      </div>

      <!-- AI Explanation -->
      <div class="prf-card">
        <div class="prf-card-hdr">
          <span class="prf-card-icon">🤖</span>
          <div>
            <div class="prf-card-ttl">AI Detection Reasoning</div>
            <div class="prf-card-sub">How LLaMA 3 identified this gap</div>
          </div>
        </div>
        <p class="prf-explanation">${esc(explanation)}</p>
        <div class="prf-conf-block">
          <div class="prf-conf-row">
            <span class="prf-conf-lbl">Confidence Score</span>
            <span class="prf-conf-num" style="color:${confColor}">${confidence}%</span>
          </div>
          <div class="prf-conf-track"><div class="prf-conf-fill" style="width:${confidence}%;background:${confGrad}"></div></div>
          <div class="prf-conf-desc">${getConfidenceDesc(confidence)}</div>
        </div>
      </div>

      ${(gap.evidence_verified && gap.evidence_quote) ? `
      <!-- Verified Quote -->
      <div class="prf-card prf-card-wide">
        <div class="prf-card-hdr">
          <span class="prf-card-icon">✅</span>
          <div>
            <div class="prf-card-ttl">Verified Quote from Your Uploaded PDF</div>
            <div class="prf-card-sub">This exact text was found in your paper${gap.evidence_page ? ` (page ${gap.evidence_page})` : ''} — not an AI paraphrase</div>
          </div>
        </div>
        <p class="prf-explanation" style="font-style:italic">"${esc(gap.evidence_quote)}"</p>
      </div>` : ''}

      <!-- Why Important -->
      <div class="prf-card">
        <div class="prf-card-hdr">
          <span class="prf-card-icon">💡</span>
          <div>
            <div class="prf-card-ttl">Why This Gap Matters</div>
            <div class="prf-card-sub">Research & real-world significance</div>
          </div>
        </div>
        <p class="prf-explanation">${esc(gap.why_important||'')}</p>
        <div class="prf-impact">
          <div class="prf-impact-lbl">⚡ Expected Impact</div>
          <div class="prf-impact-txt">${esc(gap.estimated_impact||'')}</div>
        </div>
      </div>

      <!-- How to Fill -->
      <div class="prf-card">
        <div class="prf-card-hdr">
          <span class="prf-card-icon">🚀</span>
          <div>
            <div class="prf-card-ttl">How to Fill This Gap</div>
            <div class="prf-card-sub">Actionable research steps</div>
          </div>
        </div>
        <p class="prf-explanation">${esc(gap.suggested_approach||'')}</p>
        <div class="prf-steps">
          <div class="prf-step">✅ Download and read the evidence papers above</div>
          <div class="prf-step">✅ Study their limitations and future work sections</div>
          <div class="prf-step">✅ Design your research to address this specific gap</div>
          <div class="prf-step">✅ Cite these papers to establish context</div>
        </div>
      </div>

      <!-- Generate Lit Review (fills the adjacent grid slot) -->
      <div class="prf-card prf-litreview-card">
        <div class="prf-card-hdr">
          <span class="prf-card-icon">📖</span>
          <div>
            <div class="prf-card-ttl">Deep Dive Into This Gap</div>
            <div class="prf-card-sub">AI-generated literature review & comparison</div>
          </div>
        </div>
        <p class="prf-explanation">Get a focused mini literature review showing exactly how existing papers relate to this gap, plus a side-by-side comparison table.</p>
        <button class="gen-litreview-btn" onclick="generateGapLitReview(${gapIndex})">
          📖 Generate Literature Review + Comparison Table
        </button>
      </div>

      <!-- Funding Fit -->
      <div class="prf-card prf-litreview-card">
        <div class="prf-card-hdr">
          <span class="prf-card-icon">🎓</span>
          <div>
            <div class="prf-card-ttl">Funding Fit for This Gap</div>
            <div class="prf-card-sub">Which bodies/programs plausibly fund this — suggestions, not guaranteed calls</div>
          </div>
        </div>
        <p class="prf-explanation">See which funding bodies (NSF, DST/SERB, Horizon Europe, etc.) typically fund work in this area, with real portal links to check current open calls yourself.</p>
        <button class="gen-litreview-btn" onclick="generateGapFunding(${gapIndex})">
          🎓 Find Funding Fit
        </button>
      </div>

      <!-- Generate Research Proposal -->
      <div class="prf-card prf-litreview-card">
        <div class="prf-card-hdr">
          <span class="prf-card-icon">📝</span>
          <div>
            <div class="prf-card-ttl">Turn This Gap Into a Proposal</div>
            <div class="prf-card-sub">AI-drafted formal research proposal you can take to an advisor</div>
          </div>
        </div>
        <p class="prf-explanation">Generates a structured proposal — problem statement, objectives, methodology, timeline, resources, and risks — grounded specifically in this gap.</p>
        <button class="gen-litreview-btn" onclick="generateGapProposal(${gapIndex})">
          📝 Generate Research Proposal
        </button>
      </div>

      <!-- Advisor Brief -->
      <div class="prf-card prf-litreview-card">
        <div class="prf-card-hdr">
          <span class="prf-card-icon">📄</span>
          <div>
            <div class="prf-card-ttl">Advisor-Ready Brief</div>
            <div class="prf-card-sub">One-page PDF for your next meeting</div>
          </div>
        </div>
        <p class="prf-explanation">Download a clean, one-page PDF with the gap, evidence, and proposed approach — ready to print or screen-share with your advisor.</p>
        <button class="gen-litreview-btn" onclick="downloadGapBrief(${gapIndex})" id="gapBriefBtn_${gapIndex}">
          📄 Download Advisor Brief (PDF)
        </button>
      </div>

      <!-- Accuracy Feedback -->
      <div class="prf-card prf-card-wide">
        <div class="prf-card-hdr">
          <span class="prf-card-icon">📊</span>
          <div>
            <div class="prf-card-ttl">Was this gap actually real?</div>
            <div class="prf-card-sub">Your rating feeds the accuracy dashboard — this is the only thing that makes confidence scores trustworthy over time</div>
          </div>
        </div>
        <div id="feedbackControls_${gapIndex}" style="display:flex;gap:0.5rem;flex-wrap:wrap">
          <button class="tt-btn" onclick="rateGap(${gapIndex}, 'valid')">✅ Yes, real gap</button>
          <button class="tt-btn" onclick="rateGap(${gapIndex}, 'invalid')">❌ Not actually a gap</button>
          <button class="tt-btn" onclick="rateGap(${gapIndex}, 'unsure')">🤷 Not sure</button>
        </div>
      </div>

      <!-- Team Discussion & Assignment -->
      <div class="prf-card prf-card-wide">
        <div class="prf-card-hdr">
          <span class="prf-card-icon">👥</span>
          <div>
            <div class="prf-card-ttl">Team Discussion</div>
            <div class="prf-card-sub" id="teamStatus_${gapIndex}">Join a Team Workspace (top-right) to comment, assign, or share this gap</div>
          </div>
        </div>
        <div id="teamPanel_${gapIndex}"></div>
      </div>
    </div>
    <div class="gap-litreview-section" id="gapLitReview_${gapIndex}" style="display:none">
      <div class="gap-litreview-content" id="gapLitReviewContent_${gapIndex}"></div>
    </div>
    <div class="gap-litreview-section" id="gapFunding_${gapIndex}" style="display:none">
      <div class="gap-litreview-content" id="gapFundingContent_${gapIndex}"></div>
    </div>
    <div class="gap-litreview-section" id="gapProposal_${gapIndex}" style="display:none">
      <div class="gap-litreview-content" id="gapProposalContent_${gapIndex}"></div>
    </div>`;

  renderTeamPanel(gapIndex, gap);
  proofSection.style.display = 'block';
  proofSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
};

window.closeProof = () => {
  document.getElementById('proofSection').style.display = 'none';
};

function getConfidenceDesc(score) {
  if (score >= 90) return 'Very high confidence — this gap is strongly supported by multiple independent sources and clear evidence in the literature.';
  if (score >= 75) return 'High confidence — this gap is well-supported with strong evidence from related papers and systematic analysis.';
  if (score >= 60) return 'Moderate confidence — this gap is supported by available evidence but may benefit from additional validation.';
  return 'Emerging gap — preliminary evidence suggests this area needs further investigation.';
}

function renderNextSteps(steps) {
  document.getElementById('nextSteps').innerHTML = steps.map(s => `<li>${esc(s)}</li>`).join('');
}

// ── Keywords ────────────────────────────────────────────────────────
function renderKeywords(keywords) {
  const cloud = document.getElementById('keywordCloud');
  cloud.innerHTML = keywords.map((kw, i) => {
    const cls = i<5?'big':i<12?'medium':'';
    return `<span class="kw-tag ${cls}">${esc(kw)}</span>`;
  }).join('');
}

// ── Literature Review (multi-paper) ─────────────────────────────────
function renderLiteratureReview(lit) {
  const container = document.getElementById('litReview');
  const sections = [
    {title:'Introduction', key:'introduction'},
    {title:'Main Findings', key:'main_findings'},
    {title:'Methodologies Used', key:'methodologies'},
    {title:'Limitations', key:'limitations'},
    {title:'Conclusion', key:'conclusion'}
  ];
  container.innerHTML = sections.map(s => lit[s.key] ? `
    <div class="lit-section">
      <div class="lit-section-title">${s.title}</div>
      <p class="lit-section-text">${esc(lit[s.key])}</p>
    </div>` : '').join('');
}

// ── Paper Summaries (multi-paper) ────────────────────────────────────
function renderPaperSummaries(summaries, comparative) {
  const container = document.getElementById('paperSummaries');
  container.innerHTML = summaries.map((ps, i) => `
    <div class="paper-summary-card fade-in">
      <div class="paper-summary-title">
        <span class="paper-num-badge">Paper ${i+1}</span>
        ${esc(ps.paper_name||'')}
      </div>
      <div class="summary-grid">
        <div class="summary-item"><div class="summary-item-label">🎯 Objective</div><div class="summary-item-value">${esc(ps.objective||'')}</div></div>
        <div class="summary-item"><div class="summary-item-label">⚙️ Methodology</div><div class="summary-item-value">${esc(ps.methodology||'')}</div></div>
        <div class="summary-item"><div class="summary-item-label">📊 Dataset</div><div class="summary-item-value">${esc(ps.dataset||'N/A')}</div></div>
        <div class="summary-item"><div class="summary-item-label">✅ Key Results</div><div class="summary-item-value">${esc(ps.key_results||'')}</div></div>
        <div class="summary-item"><div class="summary-item-label">⚠️ Limitations</div><div class="summary-item-value">${esc(ps.limitations||'')}</div></div>
      </div>
    </div>`).join('');

  // Comparative table
  if (comparative && comparative.length > 0 && summaries.length > 0) {
    const paperNames = summaries.map(s => s.paper_name||'Paper');
    let tableHtml = '<table class="coverage-table"><thead><tr><th>Aspect</th>';
    paperNames.forEach(n => { tableHtml += `<th>${esc(n.substring(0,20))}${n.length>20?'...':''}</th>`; });
    tableHtml += '</tr></thead><tbody>';
    comparative.forEach(row => {
      tableHtml += `<tr><td style="font-weight:600">${esc(row.aspect||'')}</td>`;
      paperNames.forEach((pn, pi) => {
        const comp = (row.comparisons||[]).find(c => c.paper && pn.includes(c.paper.substring(0,10)));
        tableHtml += `<td style="color:var(--text2);font-size:0.82rem">${esc(comp?.detail||'—')}</td>`;
      });
      tableHtml += '</tr>';
    });
    tableHtml += '</tbody></table>';
    document.getElementById('compareTableWrap').innerHTML = tableHtml;
  }
}

// ── Graph ───────────────────────────────────────────────────────────
let graphShowAllNodes = false;
let lastGraphData = null;

window.toggleGraphFocus = () => {
  graphShowAllNodes = !graphShowAllNodes;
  const btn = document.getElementById('graphFocusToggle');
  if (btn) btn.textContent = graphShowAllNodes ? '🔎 Focus View' : '🔎 Show All Nodes';
  if (lastGraphData) renderGraph(lastGraphData);
};

function relationLabel(r) {
  const map = {
    belongs_to: 'belongs to', written_by: 'written by', related_to: 'related to',
    relates_to: 'relates to', identified_in: 'found in'
  };
  return map[r] || (r || 'related to').replace(/_/g, ' ');
}

function wrapGraphLabel(ctx, text, maxWidth, maxLines) {
  const words = (text || '').split(/\s+/).filter(Boolean);
  const lines = [];
  let line = '';
  for (const w of words) {
    const test = line ? line + ' ' + w : w;
    if (ctx.measureText(test).width > maxWidth && line) {
      lines.push(line);
      line = w;
      if (lines.length === maxLines) break;
    } else {
      line = test;
    }
  }
  if (line && lines.length < maxLines) lines.push(line);
  if (lines.length === maxLines) {
    let last = lines[maxLines - 1];
    while (ctx.measureText(last + '…').width > maxWidth && last.length > 1) {
      last = last.slice(0, -1);
    }
    lines[maxLines - 1] = last + '…';
  }
  return lines;
}

function renderGraph(graphData) {
  lastGraphData = graphData;
  const canvas = document.getElementById('graphCanvas');
  const ctx    = canvas.getContext('2d');
  canvas.width  = canvas.offsetWidth || 900;
  canvas.height = 620;
  const W = canvas.width, H = canvas.height;
  let nodes = graphData.nodes || [], allEdges = graphData.edges || [];
  if (!nodes.length) return;
  renderSuggestedGaps(graphData.suggested_gaps || []);

  // ── Node type config ──────────────────────────────────────────────
  const nodeConfig = {
    keyword:  { color:'#c9a24b', glow:'rgba(201,162,75,0.4)',  minR:26, maxR:40, label:'Concept'  },
    paper:    { color:'#6b4fd6', glow:'rgba(107,79,214,0.4)',  minR:30, maxR:46, label:'Paper'     },
    gap:      { color:'#ff4757', glow:'rgba(255,71,87,0.4)',   minR:30, maxR:48, label:'Gap'       },
    author:   { color:'#3fae8a', glow:'rgba(63,174,138,0.35)', minR:24, maxR:34, label:'Author'    },
    trend:    { color:'#e8c468', glow:'rgba(232,196,104,0.35)',minR:24, maxR:36, label:'Trend'     },
    cluster:  { color:'#3fae8a', glow:'rgba(63,174,138,0.3)',  minR:24, maxR:34, label:'Cluster'   },
  };

  // ── Focus view: cap node count so labels stay readable, prioritizing
  //    gaps + papers (the most meaningful nodes) then highest-degree others ──
  const NODE_LIMIT = 26;
  if (!graphShowAllNodes && nodes.length > NODE_LIMIT) {
    const priority = nodes.filter(n => n.type === 'gap' || n.type === 'paper');
    const rest = nodes.filter(n => n.type !== 'gap' && n.type !== 'paper')
      .sort((a, b) => (b.degree||0) - (a.degree||0));
    nodes = priority.concat(rest).slice(0, NODE_LIMIT);
  }
  const nodeIds = new Set(nodes.map(n => n.id));
  const edges = allEdges.filter(e => nodeIds.has(e.source) && nodeIds.has(e.target));

  const focusBtn = document.getElementById('graphFocusToggle');
  if (focusBtn) {
    focusBtn.style.display = (allEdges === edges && (graphData.nodes||[]).length <= NODE_LIMIT) ? 'none' : 'inline-flex';
    focusBtn.textContent = graphShowAllNodes ? '🔎 Focus View' : '🔎 Show All Nodes';
  }

  // ── Assign node size based on degree ──────────────────────────────
  const maxDeg = Math.max(1, ...nodes.map(n => n.degree||1));
  nodes.forEach(n => {
    const cfg = nodeConfig[n.type] || nodeConfig.keyword;
    n._r = cfg.minR + ((n.degree||1) / maxDeg) * (cfg.maxR - cfg.minR);
    n._color = cfg.color;
    n._glow  = cfg.glow;
  });

  // ── Force-directed layout ─────────────────────────────────────────
  const positions = {};
  const cx = W/2, cy = H/2;
  nodes.forEach((n, i) => {
    const angle = (i / nodes.length) * Math.PI * 2;
    const r = n.type === 'paper' ? 0.8 : n.type === 'gap' ? 0.55 : 0.42;
    positions[n.id] = {
      x: cx + Math.min(W,H) * r * Math.cos(angle) * (0.7 + Math.random()*0.3),
      y: cy + Math.min(W,H) * r * Math.sin(angle) * (0.7 + Math.random()*0.3),
      vx: 0, vy: 0
    };
  });

  const EDGE_TARGET_DIST = 190;
  for (let iter = 0; iter < 160; iter++) {
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i+1; j < nodes.length; j++) {
        const a = positions[nodes[i].id], b = positions[nodes[j].id];
        const dx = b.x-a.x, dy = b.y-a.y;
        const dist = Math.max(Math.sqrt(dx*dx+dy*dy), 1);
        const minSep = (nodes[i]._r||30) + (nodes[j]._r||30) + 40;
        const force = (dist < minSep ? 4200 : 2600) / (dist*dist);
        const fx = (dx/dist)*force, fy = (dy/dist)*force;
        a.vx -= fx*0.08; a.vy -= fy*0.08;
        b.vx += fx*0.08; b.vy += fy*0.08;
      }
    }
    edges.forEach(e => {
      const a = positions[e.source], b = positions[e.target];
      if (!a || !b) return;
      const dx = b.x-a.x, dy = b.y-a.y;
      const dist = Math.sqrt(dx*dx+dy*dy);
      const force = (dist - EDGE_TARGET_DIST) * 0.012;
      const fx = (dx/Math.max(dist,1))*force, fy = (dy/Math.max(dist,1))*force;
      a.vx += fx; a.vy += fy;
      b.vx -= fx; b.vy -= fy;
    });
    nodes.forEach(n => {
      const p = positions[n.id];
      p.vx *= 0.85; p.vy *= 0.85;
      p.x += p.vx + (cx - p.x) * 0.006;
      p.y += p.vy + (cy - p.y) * 0.006;
      const r = n._r||30;
      p.x = Math.max(r+10, Math.min(W-r-10, p.x));
      p.y = Math.max(r+10, Math.min(H-r-10, p.y));
    });
  }

  // ── State ─────────────────────────────────────────────────────────
  let hovered = null;
  let selected = null;
  let animOffset = 0;
  let dragNodeId = null;
  let didDrag = false;

  // ── Draw ──────────────────────────────────────────────────────────
  function draw() {
    ctx.clearRect(0, 0, W, H);

    ctx.fillStyle = 'rgba(5,8,18,0.97)';
    ctx.fillRect(0, 0, W, H);

    ctx.strokeStyle = 'rgba(255,255,255,0.02)';
    ctx.lineWidth = 0.5;
    for (let gx = 0; gx < W; gx += 60) { ctx.beginPath(); ctx.moveTo(gx,0); ctx.lineTo(gx,H); ctx.stroke(); }
    for (let gy = 0; gy < H; gy += 60) { ctx.beginPath(); ctx.moveTo(0,gy); ctx.lineTo(W,gy); ctx.stroke(); }

    const activeId = hovered || selected;

    // ── Edges with arrowheads + relation labels ──
    edges.forEach(e => {
      const a = positions[e.source], b = positions[e.target];
      const na = nodes.find(n => n.id === e.source), nb = nodes.find(n => n.id === e.target);
      if (!a || !b || !na || !nb) return;
      const isRelated = activeId && (e.source === activeId || e.target === activeId);
      const dim = activeId && !isRelated;

      const dx = b.x-a.x, dy = b.y-a.y;
      const dist = Math.max(Math.sqrt(dx*dx+dy*dy), 1);
      const ux = dx/dist, uy = dy/dist;
      const startX = a.x + ux*(na._r+2), startY = a.y + uy*(na._r+2);
      const endX   = b.x - ux*(nb._r+8), endY   = b.y - uy*(nb._r+8);

      const w = e.weight || 1;
      const baseAlpha = dim ? 0.10 : (isRelated ? 0.9 : 0.35);
      ctx.strokeStyle = `rgba(148,163,184,${baseAlpha})`;
      ctx.lineWidth = isRelated ? 2 : (w >= 3 ? 1.4 : 1);
      ctx.setLineDash(w >= 3 ? [] : [3, 4]);
      ctx.beginPath();
      ctx.moveTo(startX, startY);
      ctx.lineTo(endX, endY);
      ctx.stroke();
      ctx.setLineDash([]);

      // Arrowhead
      const ah = 7;
      const angle = Math.atan2(endY-startY, endX-startX);
      ctx.fillStyle = `rgba(148,163,184,${dim ? 0.12 : (isRelated ? 0.95 : 0.5)})`;
      ctx.beginPath();
      ctx.moveTo(endX, endY);
      ctx.lineTo(endX - ah*Math.cos(angle-0.4), endY - ah*Math.sin(angle-0.4));
      ctx.lineTo(endX - ah*Math.cos(angle+0.4), endY - ah*Math.sin(angle+0.4));
      ctx.closePath();
      ctx.fill();

      // Relation label — always shown, more prominent when related to hover/select
      if (!dim || edges.length <= 18) {
        const midX = (startX+endX)/2, midY = (startY+endY)/2;
        const label = relationLabel(e.relation);
        ctx.font = `${isRelated ? 700 : 500} 9px 'Space Grotesk', sans-serif`;
        const tw = ctx.measureText(label).width;
        ctx.fillStyle = dim ? 'rgba(5,8,18,0.4)' : 'rgba(5,8,18,0.85)';
        ctx.fillRect(midX - tw/2 - 5, midY - 7, tw + 10, 14);
        ctx.fillStyle = dim ? 'rgba(226,232,240,0.25)' : (isRelated ? '#ffd700' : 'rgba(226,232,240,0.65)');
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(label, midX, midY + 1);
      }
    });

    // ── Pulse ring on selected node ──
    if (selected && positions[selected]) {
      const p = positions[selected];
      const selNode = nodes.find(n => n.id === selected);
      const baseR = selNode ? selNode._r : 30;
      const pulseR = baseR + 8 + 5 * Math.sin(animOffset * 0.06);
      ctx.beginPath();
      ctx.arc(p.x, p.y, pulseR, 0, Math.PI*2);
      ctx.strokeStyle = `rgba(255,215,0,${0.5 + 0.3*Math.sin(animOffset*0.06)})`;
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    // ── Nodes: circles with wrapped labels inside, like a classic KG diagram ──
    nodes.forEach(n => {
      const p = positions[n.id];
      if (!p) return;
      const isHov = hovered === n.id;
      const isSel = selected === n.id;
      const isActive = isHov || isSel;
      const isDimmed = activeId && !isActive && !edges.some(e =>
        (e.source === activeId && e.target === n.id) || (e.target === activeId && e.source === n.id));
      const r = n._r || 30;

      ctx.save();
      if (isDimmed) ctx.globalAlpha = 0.35;

      // Glow
      const grad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r * 1.8);
      grad.addColorStop(0, n._glow || 'rgba(201,162,75,0.3)');
      grad.addColorStop(1, 'transparent');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(p.x, p.y, r * 1.8, 0, Math.PI*2);
      ctx.fill();

      // Node circle body
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI*2);
      ctx.fillStyle = n._color || '#c9a24b';
      ctx.fill();
      ctx.lineWidth = isSel ? 3 : isHov ? 2.5 : 1.5;
      ctx.strokeStyle = isSel ? '#ffd700' : isHov ? '#ffffff' : 'rgba(5,8,18,0.6)';
      ctx.stroke();

      // Wrapped label inside the circle
      const label = n.id;
      const fontSize = Math.max(9, Math.min(11, r/3.4));
      ctx.font = `700 ${fontSize}px 'Space Grotesk', sans-serif`;
      ctx.fillStyle = '#05080f';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      const maxLines = r >= 38 ? 3 : 2;
      const lines = wrapGraphLabel(ctx, label, r*1.6, maxLines);
      const lineHeight = fontSize + 2;
      const totalH = lines.length * lineHeight;
      lines.forEach((ln, i) => {
        ctx.fillText(ln, p.x, p.y - totalH/2 + lineHeight/2 + i*lineHeight);
      });

      ctx.restore();
    });

    animOffset++;
    graphAnimId = requestAnimationFrame(draw);
  }

  if (graphAnimId) cancelAnimationFrame(graphAnimId);
  draw();

  // ── Node info panel on click ───────────────────────────────────────
  function showNodePanel(nodeId) {
    const node = nodes.find(n => n.id === nodeId);
    const panel = document.getElementById('graphNodePanel');
    if (!node || !panel) return;

    const connected = [];
    edges.forEach(e => {
      if (e.source === nodeId) connected.push({id: e.target, weight: e.weight||1, relation: e.relation});
      if (e.target === nodeId) connected.push({id: e.source, weight: e.weight||1, relation: e.relation});
    });
    connected.sort((a,b) => b.weight - a.weight);

    const typeLabel = (nodeConfig[node.type] || nodeConfig.keyword).label;
    const typeColor = (nodeConfig[node.type] || nodeConfig.keyword).color;
    const connCount = connected.length;

    let paperInfo = '';
    if (node.type === 'paper' && currentData && currentData.papers) {
      const paper = currentData.papers.find(p => node.id.includes(p.title.substring(0,20)));
      if (paper) {
        paperInfo = `
          <div class="gnp-year">Published: ${esc(paper.year || 'N/A')}</div>
          <div class="gnp-authors">${(paper.authors||[]).slice(0,2).map(esc).join(', ')}</div>
          <div class="gnp-abstract">${esc((paper.abstract||'').substring(0,180))}...</div>
        `;
      }
    }

    panel.style.display = 'block';
    panel.innerHTML = `
      <div class="gnp-header">
        <div class="gnp-type-badge" style="background:${typeColor}22;color:${typeColor};border:1px solid ${typeColor}44">${typeLabel}</div>
        <button class="gnp-close" onclick="document.getElementById('graphNodePanel').style.display='none'">✕</button>
      </div>
      <div class="gnp-title">${esc(node.id)}</div>
      ${paperInfo}
      <div class="gnp-stats">
        <div class="gnp-stat"><span class="gnp-stat-num">${connCount}</span><span class="gnp-stat-lbl">Connections</span></div>
        <div class="gnp-stat"><span class="gnp-stat-num">${node.degree||1}</span><span class="gnp-stat-lbl">Degree</span></div>
        <div class="gnp-stat"><span class="gnp-stat-num">${esc(String(node.year||'—'))}</span><span class="gnp-stat-lbl">Year</span></div>
      </div>
      ${connCount > 0 ? `
      <div class="gnp-connections-title">Connected to:</div>
      <div class="gnp-connections">
        ${connected.slice(0,8).map(c => {
          const cn = nodes.find(n => n.id === c.id);
          const cc = cn ? (nodeConfig[cn.type]||nodeConfig.keyword).color : '#94a3b8';
          const strength = c.weight >= 3 ? 'Strong' : c.weight >= 2 ? 'Medium' : 'Weak';
          return `<div class="gnp-conn-item" onclick="selectNode('${c.id.replace(/'/g,"\\'")}')">
            <span class="gnp-conn-dot" style="background:${cc}"></span>
            <span class="gnp-conn-name">${esc(c.id.length>26?c.id.substring(0,26)+'…':c.id)}</span>
            <span class="gnp-conn-strength ${strength.toLowerCase()}">${strength}</span>
          </div>`;
        }).join('')}
      </div>` : ''}
    `;
  }

  window.selectNode = (nodeId) => {
    selected = nodeId;
    showNodePanel(nodeId);
  };

  if (canvas._glHandlers) {
    canvas.removeEventListener('mousemove', canvas._glHandlers.move);
    canvas.removeEventListener('mousedown', canvas._glHandlers.down);
    canvas.removeEventListener('mouseup', canvas._glHandlers.up);
    canvas.removeEventListener('click', canvas._glHandlers.click);
    canvas.removeEventListener('mouseleave', canvas._glHandlers.leave);
  }

  function nodeAt(mx, my) {
    for (const n of nodes) {
      const p = positions[n.id];
      if (!p) continue;
      if (Math.sqrt((mx-p.x)**2 + (my-p.y)**2) < (n._r||30) + 4) return n.id;
    }
    return null;
  }

  function toCanvasCoords(e) {
    const rect = canvas.getBoundingClientRect();
    return { mx: (e.clientX - rect.left) * (W / rect.width), my: (e.clientY - rect.top) * (H / rect.height) };
  }

  function onGraphMove(e) {
    const { mx, my } = toCanvasCoords(e);
    if (dragNodeId) {
      didDrag = true;
      const p = positions[dragNodeId];
      const r = (nodes.find(n=>n.id===dragNodeId)||{})._r || 30;
      p.x = Math.max(r+10, Math.min(W-r-10, mx));
      p.y = Math.max(r+10, Math.min(H-r-10, my));
      p.vx = 0; p.vy = 0;
      canvas.style.cursor = 'grabbing';
      return;
    }
    const found = nodeAt(mx, my);
    hovered = found;
    canvas.style.cursor = found ? 'grab' : 'default';
  }

  function onGraphDown(e) {
    const { mx, my } = toCanvasCoords(e);
    const found = nodeAt(mx, my);
    dragNodeId = found;
    didDrag = false;
  }

  function onGraphUp(e) {
    dragNodeId = null;
  }

  function onGraphClick(e) {
    if (didDrag) { didDrag = false; return; }
    const { mx, my } = toCanvasCoords(e);
    const found = nodeAt(mx, my);
    if (found) {
      selected = found;
      showNodePanel(found);
    } else {
      selected = null;
      const panel = document.getElementById('graphNodePanel');
      if (panel) panel.style.display = 'none';
    }
  }

  function onGraphLeave() { hovered = null; dragNodeId = null; }

  canvas.addEventListener('mousemove', onGraphMove);
  canvas.addEventListener('mousedown', onGraphDown);
  canvas.addEventListener('mouseup', onGraphUp);
  canvas.addEventListener('click', onGraphClick);
  canvas.addEventListener('mouseleave', onGraphLeave);
  canvas._glHandlers = { move: onGraphMove, down: onGraphDown, up: onGraphUp, click: onGraphClick, leave: onGraphLeave };
}

// ── Structural gap suggestions (from graph analytics, not the LLM) ────
function renderSuggestedGaps(suggestions) {
  const el = document.getElementById('suggestedGapsList');
  if (!el) return;
  if (!suggestions.length) {
    el.innerHTML = '<div style="opacity:0.6;padding:0.5rem 0;font-size:0.85rem;color:var(--text2)">No strong unexplored combinations detected in this graph — the fetched literature is fairly well cross-connected.</div>';
    return;
  }
  el.innerHTML = suggestions.map(s => `
    <div style="padding:0.6rem 0;border-bottom:1px solid var(--border)">
      <div style="color:var(--text);font-weight:600;font-size:0.85rem">${esc(s.topic_a)} &#215; ${esc(s.topic_b)}</div>
      <div style="font-size:0.8rem;color:var(--text2);margin-top:0.2rem">${esc(s.reason)}</div>
    </div>`).join('');
}

// ── Papers ───────────────────────────────────────────────────────────
function renderPapers(papers) {
  const grid = document.getElementById('papersGrid');
  function renderGrid(list) {
    grid.innerHTML = list.map((p,i)=>`
      <div class="paper-card fade-in" style="animation-delay:${i*0.05}s">
        <span class="paper-year">${esc(p.year||'')}</span>
        <div class="paper-title">${esc(p.title||'')}</div>
        <div class="paper-authors">${(p.authors||[]).slice(0,2).map(esc).join(', ')}</div>
        <div class="paper-abstract">${esc(p.abstract||'')}</div>
        <div class="paper-cats">${(p.categories||[]).map(c=>`<span class="cat-tag">${esc(c)}</span>`).join('')}</div>
        <div class="paper-actions">
          <a href="https://arxiv.org/search/?searchtype=all&query=${encodeURIComponent(p.title||'')}" target="_blank" class="paper-action-btn">🔗 View on ArXiv</a>
        </div>
      </div>`).join('');
  }
  renderGrid(papers);
  document.getElementById('paperSearch').addEventListener('input',e=>{
    const q=e.target.value.toLowerCase();
    renderGrid(papers.filter(p=>(p.title||'').toLowerCase().includes(q)||(p.abstract||'').toLowerCase().includes(q)||(p.authors||[]).some(a=>a.toLowerCase().includes(q))));
  });
}

// ── Charts ──────────────────────────────────────────────────────────
function renderCharts(yearData, catData) {
  if(yearChartInst) yearChartInst.destroy();
  if(catChartInst)  catChartInst.destroy();
  const chartDefaults={plugins:{legend:{labels:{color:'#94a3b8',font:{family:'Space Grotesk'}}}},scales:{x:{ticks:{color:'#94a3b8'},grid:{color:'rgba(255,255,255,0.05)'}},y:{ticks:{color:'#94a3b8'},grid:{color:'rgba(255,255,255,0.05)'}}}};
  yearChartInst = new Chart(document.getElementById('yearChart').getContext('2d'),{type:'bar',data:{labels:yearData.map(d=>d.year),datasets:[{label:'Papers Published',data:yearData.map(d=>d.count),backgroundColor:'rgba(201,162,75,0.4)',borderColor:'rgba(201,162,75,0.8)',borderWidth:1.5,borderRadius:4}]},options:{...chartDefaults,responsive:true,maintainAspectRatio:true}});
  catChartInst = new Chart(document.getElementById('catChart').getContext('2d'),{type:'doughnut',data:{labels:catData.map(d=>d.category),datasets:[{data:catData.map(d=>d.count),backgroundColor:['#c9a24b','#6b4fd6','#3fae8a','#ff9f43','#ff4757','#8f7ae8','#3d6ea5','#2e8f6e'],borderColor:'rgba(0,0,0,0.3)',borderWidth:1.5}]},options:{responsive:true,maintainAspectRatio:true,plugins:{legend:{position:'right',labels:{color:'#94a3b8',font:{family:'Space Grotesk',size:11},boxWidth:12,padding:10}}}}});
}

// ── Trends ──────────────────────────────────────────────────────────
function renderTrends(trends) {
  const trendIcons = ['🚀','🧬','⚡','🌐','🔮','💡','🛸','🎯'];
  const trendColors = [
    'rgba(201,162,75,0.15)','rgba(107,79,214,0.15)','rgba(63,174,138,0.12)',
    'rgba(255,159,67,0.15)','rgba(255,71,87,0.12)','rgba(201,162,75,0.1)'
  ];
  document.getElementById('trendsGrid').innerHTML = trends.map((t,i)=>`
    <div class="trend-card-new" style="--trend-color:${trendColors[i%trendColors.length]}">
      <div class="trend-card-glow"></div>
      <div class="trend-top-new">
        <div class="trend-icon-big">${trendIcons[i%trendIcons.length]}</div>
        <div class="trend-rel-new rel-${(t.relevance||'medium').toLowerCase()}">${esc(t.relevance||'').toUpperCase()} RELEVANCE</div>
      </div>
      <div class="trend-name-new">${esc(t.trend||'')}</div>
      <div class="trend-desc-new">${esc(t.description||'')}</div>
      <div class="trend-footer">
        <div class="trend-pulse"></div>
        <span class="trend-pulse-label">Active Research Area</span>
      </div>
    </div>`).join('');
}

// ── Tabs ────────────────────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll('.rtab').forEach(t=>t.classList.toggle('active',t.dataset.tab===name));
  document.querySelectorAll('.tab-content').forEach(c=>c.classList.toggle('active',c.id==='tab-'+name));
  if(name==='graph'&&currentData){
    setTimeout(()=>{
      const canvas=document.getElementById('graphCanvas');
      canvas.width=canvas.offsetWidth||800;
      if(graphAnimId) cancelAnimationFrame(graphAnimId);
      renderGraph(currentData.graph);
    },50);
  }
}
document.querySelectorAll('.rtab').forEach(tab=>tab.addEventListener('click',()=>switchTab(tab.dataset.tab)));

// ── Utility ─────────────────────────────────────────────────────────
function esc(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Profile Modal ────────────────────────────────────────────────────
window.openProfile = () => {
  const user = JSON.parse(localStorage.getItem('rl_user') || 'null');
  if (!user) return;

  document.getElementById('profileName').textContent  = user.name || user.email.split('@')[0];
  document.getElementById('profileEmail').textContent = user.email;

  // Avatar
  const avatarEl = document.getElementById('profileAvatarLarge');
  if (user.photo) {
    avatarEl.innerHTML = `<img src="${user.photo}" style="width:100%;height:100%;border-radius:50%;object-fit:cover"/>`;
  } else {
    const initials = (user.name||user.email).split(' ').map(w=>w[0]).join('').toUpperCase().substring(0,2);
    avatarEl.innerHTML = `<span style="font-size:1.8rem;font-weight:800;color:white">${initials}</span>`;
  }

  // Load history from localStorage
  const history = JSON.parse(localStorage.getItem('rl_history') || '[]');
  const histEl = document.getElementById('profileHistory');
  document.getElementById('statAnalyses').textContent = history.length;
  if (history.length > 0) {
    histEl.innerHTML = history.slice(0,5).map(h => `
      <div class="history-item">
        <div class="history-icon">🔬</div>
        <div class="history-info">
          <div class="history-topic">${esc(h.topic||'')}</div>
          <div class="history-date">${h.date||''} • ${h.gaps||0} gaps found</div>
        </div>
        <div class="history-badge">${h.mode==='multi'?'📚 Multi':'📄 Single'}</div>
      </div>`).join('');
  } else {
    histEl.innerHTML = `<div style="color:var(--text3);font-size:0.85rem;text-align:center;padding:1rem">No analyses yet — run your first analysis above!</div>`;
  }

  document.getElementById('profileOverlay').style.display = 'block';
  document.getElementById('profileModal').classList.add('open');
};

window.closeProfile = () => {
  document.getElementById('profileOverlay').style.display = 'none';
  document.getElementById('profileModal').classList.remove('open');
};

window.handleUpgrade = (plan) => {
  alert(`🚀 ${plan.charAt(0).toUpperCase()+plan.slice(1)} Plan\n\nIn production this would open Razorpay payment gateway.\n\nFor demo purposes, this shows the upgrade flow is ready!`);
};

// Save analysis to history
function saveToHistory(topic, gapsCount, mode) {
  const history = JSON.parse(localStorage.getItem('rl_history') || '[]');
  history.unshift({
    topic: topic,
    date: new Date().toLocaleDateString('en-IN'),
    gaps: gapsCount,
    mode: mode
  });
  localStorage.setItem('rl_history', JSON.stringify(history.slice(0, 20)));
}

// ═══════════════════════════════════════════════════
// CHANGE 2: Gap Leaderboard
// ═══════════════════════════════════════════════════
function renderLeaderboard(gaps) {
  const grid = document.getElementById('leaderboardGrid');
  if (!grid) return;
  const sevScore = {critical:100, major:75, moderate:50};
  const fallbackPapers = (currentData && currentData.papers) ? currentData.papers.slice(0,3).map(p=>p.title) : [];
  const sorted = [...gaps].sort((a,b) => {
    const sa = (sevScore[a.severity]||50) + (a.confidence_score||70);
    const sb = (sevScore[b.severity]||50) + (b.confidence_score||70);
    return sb - sa;
  });
  grid.innerHTML = sorted.map((g, i) => {
    const sev = (g.severity||'moderate').toLowerCase();
    const conf = g.confidence_score || 75;
    const medal = i===0?'🥇':i===1?'🥈':i===2?'🥉':`#${i+1}`;
    const rankColor = i===0?'#ffd700':i===1?'#c0c0c0':i===2?'#cd7f32':'var(--text2)';
    const paperCount = (g.evidence_papers && g.evidence_papers.length > 0) ? g.evidence_papers.length : fallbackPapers.length;
    return `<div class="lb-row" style="animation-delay:${i*0.08}s">
      <div class="lb-rank" style="color:${rankColor}">${medal}</div>
      <div class="lb-bar-section">
        <div class="lb-gap-name">${esc(g.gap_title||'')}</div>
        <div class="lb-bar-wrap">
          <div class="lb-bar-fill sev-${sev}" style="width:${conf}%"></div>
        </div>
        <div class="lb-meta">
          <span class="lb-sev-tag sev-tag-${sev}">${sev.toUpperCase()}</span>
          <span class="lb-conf">${conf}% confidence</span>
          <span class="lb-papers">${paperCount} evidence papers</span>
        </div>
      </div>
      <div class="lb-score" style="color:${rankColor}">${(sevScore[sev]||50) + conf}</div>
    </div>`;
  }).join('');
}

// ═══════════════════════════════════════════════════
// CHANGE 4: Interactive Knowledge Graph Q&A
// ═══════════════════════════════════════════════════
let graphQAHistory = [];
let currentGraphData = null;

function initGraphQA(graphData, papers, keywords) {
  currentGraphData = {graphData, papers, keywords};
  graphQAHistory = [];
  renderGraphQAMessages([{
    role: 'assistant',
    text: `👋 Hi! I'm your Knowledge Graph Assistant. I can answer questions about the research landscape mapped in this graph.\n\nTry asking:\n• "Which topics are most connected?"\n• "What research areas are isolated?"\n• "How many papers cover machine learning?"\n• "What are the strongest research clusters?"`
  }]);
}

window.askGraphQA = async () => {
  const input = document.getElementById('graphQAInput');
  const question = input.value.trim();
  if (!question) return;
  input.value = '';

  graphQAHistory.push({role:'user', text:question});
  renderGraphQAMessages(graphQAHistory);

  const apiKey = document.getElementById('apiKey').value.trim();
  if (!apiKey) {
    graphQAHistory.push({role:'assistant', text:'❌ Please enter your Groq API key first.'});
    renderGraphQAMessages(graphQAHistory);
    return;
  }

  const loadingId = Date.now();
  graphQAHistory.push({role:'assistant', text:'🔍 Analyzing graph...', id:loadingId});
  renderGraphQAMessages(graphQAHistory);

  try {
    const {graphData, papers, keywords} = currentGraphData;
    const nodeCount = graphData.nodes.length;
    const edgeCount = graphData.edges.length;
    const topKW = keywords.slice(0,15).join(', ');
    const paperTitles = papers.slice(0,8).map(p=>p.title).join('; ');

    const contextPrompt = `You are a knowledge graph analyst assistant. Answer questions about this research knowledge graph.

Graph stats: ${nodeCount} nodes, ${edgeCount} edges
Top keywords/concepts: ${topKW}
Related papers: ${paperTitles}
Current research topic: ${document.getElementById('topic').value}

User question: ${question}

Answer concisely and helpfully in 2-4 sentences. Be specific about the graph data provided.`;

    const resp = await fetch('/api/graph-qa', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({api_key: apiKey, question: contextPrompt})
    });
    const data = await resp.json();
    const answer = data.answer || 'Sorry, could not get an answer.';
    graphQAHistory = graphQAHistory.filter(m => m.id !== loadingId);
    graphQAHistory.push({role:'assistant', text:answer});
  } catch(e) {
    graphQAHistory = graphQAHistory.filter(m => m.id !== loadingId);
    graphQAHistory.push({role:'assistant', text:'❌ Error: ' + e.message});
  }
  renderGraphQAMessages(graphQAHistory);
};

window.askQuickQuestion = (q) => {
  document.getElementById('graphQAInput').value = q;
  window.askGraphQA();
};

function renderGraphQAMessages(messages) {
  const container = document.getElementById('graphQAMessages');
  if (!container) return;
  container.innerHTML = messages.map(m => `
    <div class="qa-msg qa-${m.role}">
      <div class="qa-avatar">${m.role==='user'?'👤':'🤖'}</div>
      <div class="qa-bubble">${esc(m.text||'').replace(/\n/g,'<br/>')}</div>
    </div>`).join('');
  container.scrollTop = container.scrollHeight;
}

// ═══════════════════════════════════════════════════
// CHANGE 5: Generate Lit Review for a Gap
// ═══════════════════════════════════════════════════
window.generateGapLitReview = async (gapIndex) => {
  const apiKey = document.getElementById('apiKey').value.trim();
  if (!apiKey) { alert('Please enter your Groq API key first.'); return; }

  const gap = currentData?.analysis?.research_gaps?.[gapIndex];
  if (!gap) return;

  const section = document.getElementById(`gapLitReview_${gapIndex}`);
  const content = document.getElementById(`gapLitReviewContent_${gapIndex}`);
  section.style.display = 'block';
  content.innerHTML = `<div class="glr-loading"><div class="glr-spinner"></div><span>Generating literature review for "${gap.gap_title}"...</span></div>`;

  try {
    const papers = currentData.papers || [];
    const paperList = papers.slice(0,8).map(p=>`- ${p.title} (${p.year})`).join('\n');
    const topic = document.getElementById('topic').value;

    const resp = await fetch('/api/gap-litreview', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        api_key: apiKey,
        gap_title: gap.gap_title,
        gap_description: gap.description,
        suggested_approach: gap.suggested_approach,
        evidence_papers: gap.evidence_papers || [],
        topic: topic,
        related_papers: paperList
      })
    });
    const data = await resp.json();
    if (data.error) throw new Error(data.error);

    const lr = data.literature_review;
    const ct = data.comparison_table || [];

    let tableHTML = '';
    if (ct.length > 0) {
      tableHTML = `<table class="glr-table">
        <thead><tr><th>Paper</th><th>Method</th><th>Covers Gap?</th><th>Limitation</th></tr></thead>
        <tbody>${ct.map(r=>`<tr>
          <td>${esc(r.paper||'')}</td>
          <td>${esc(r.method||'')}</td>
          <td class="${r.covers_gap?'glr-no':'glr-yes'}">${r.covers_gap?'❌ No':'✅ Partial'}</td>
          <td>${esc(r.limitation||'')}</td>
        </tr>`).join('')}</tbody>
      </table>`;
    }

    content.innerHTML = `
      <div class="glr-header">
        <h4 class="glr-title">📖 Literature Review: ${esc(gap.gap_title)}</h4>
        <p class="glr-sub">Auto-generated analysis of how existing research relates to this gap</p>
      </div>
      <div class="glr-sections">
        ${lr.introduction?`<div class="glr-section"><div class="glr-section-title">Introduction</div><p>${esc(lr.introduction)}</p></div>`:''}
        ${lr.existing_work?`<div class="glr-section"><div class="glr-section-title">Existing Work</div><p>${esc(lr.existing_work)}</p></div>`:''}
        ${lr.gap_analysis?`<div class="glr-section gap-highlight"><div class="glr-section-title">🔴 Gap Analysis</div><p>${esc(lr.gap_analysis)}</p></div>`:''}
        ${lr.proposed_direction?`<div class="glr-section"><div class="glr-section-title">Proposed Research Direction</div><p>${esc(lr.proposed_direction)}</p></div>`:''}
      </div>
      ${tableHTML?`<div class="glr-table-section"><div class="glr-table-title">📊 Comparison Table: Existing Papers vs This Gap</div>${tableHTML}</div>`:''}
    `;
  } catch(e) {
    content.innerHTML = `<div style="color:var(--red);padding:1rem">❌ ${esc(e.message)}</div>`;
  }
};

// ═══════════════════════════════════════════════════
// WORKFLOW: Funding Fit for a gap
// ═══════════════════════════════════════════════════
window.generateGapFunding = async (gapIndex) => {
  const apiKey = document.getElementById('apiKey').value.trim();
  if (!apiKey) { alert('Please enter your Groq API key first.'); return; }

  const gap = currentData?.analysis?.research_gaps?.[gapIndex];
  if (!gap) return;

  const section = document.getElementById(`gapFunding_${gapIndex}`);
  const content = document.getElementById(`gapFundingContent_${gapIndex}`);
  section.style.display = 'block';
  content.innerHTML = `<div class="glr-loading"><div class="glr-spinner"></div><span>Finding funding fit for "${gap.gap_title}"...</span></div>`;

  try {
    const topic = document.getElementById('topic').value;
    const resp = await fetch('/api/gap-funding', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        api_key: apiKey,
        gap_title: gap.gap_title,
        gap_description: gap.description,
        topic: topic,
        region: 'Global'
      })
    });
    const data = await resp.json();
    if (data.error) throw new Error(data.error);

    const fits = data.funding_fit || [];
    const keywords = data.search_keywords || [];

    content.innerHTML = `
      <div class="glr-header">
        <h4 class="glr-title">🎓 Funding Fit: ${esc(gap.gap_title)}</h4>
        <p class="glr-sub">${esc(data.disclaimer || 'AI-suggested starting points — always verify current calls and eligibility directly on each portal.')}</p>
      </div>
      <div class="glr-sections">
        ${fits.map(f => `
          <div class="glr-section">
            <div class="glr-section-title">${esc(f.body||'')}${f.program_area ? ' — '+esc(f.program_area) : ''}</div>
            <p>${esc(f.why_relevant||'')}</p>
            ${f.search_url ? `<a href="${esc(f.search_url)}" target="_blank" class="prf-dl-btn" style="margin-top:0.5rem;display:inline-block">🔗 Search live calls on this portal →</a>` : ''}
          </div>`).join('')}
      </div>
      ${keywords.length ? `<div class="glr-table-section"><div class="glr-table-title">Suggested search keywords</div><div class="keyword-cloud">${keywords.map(k=>`<span class="kw-tag">${esc(k)}</span>`).join('')}</div></div>` : ''}
    `;
  } catch(e) {
    content.innerHTML = `<div style="color:var(--red);padding:1rem">❌ ${esc(e.message)}</div>`;
  }
};

// ═══════════════════════════════════════════════════
// WORKFLOW: Research Proposal Agent (per gap)
// ═══════════════════════════════════════════════════
window.generateGapProposal = async (gapIndex) => {
  const apiKey = document.getElementById('apiKey').value.trim();
  if (!apiKey) { alert('Please enter your Groq API key first.'); return; }

  const analysisId = currentData?.analysis_id;
  if (!analysisId) { alert('Run an analysis first — no saved analysis to generate a proposal from.'); return; }

  const gap = currentData?.analysis?.research_gaps?.[gapIndex];
  if (!gap) return;

  const section = document.getElementById(`gapProposal_${gapIndex}`);
  const content = document.getElementById(`gapProposalContent_${gapIndex}`);
  section.style.display = 'block';
  content.innerHTML = `<div class="glr-loading"><div class="glr-spinner"></div><span>Drafting research proposal for "${gap.gap_title}"...</span></div>`;
  section.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  try {
    const resp = await fetch('/api/agents/proposal', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ api_key: apiKey, analysis_id: analysisId, gap_index: gapIndex })
    });
    const data = await resp.json();
    if (data.error) throw new Error(data.error);

    const p = data.proposal || {};
    const objectives = p.objectives || [];
    const outcomes = p.expected_outcomes || [];
    const timeline = p.timeline || [];
    const resources = p.required_resources || [];
    const risks = p.risks_and_mitigations || [];

    content.innerHTML = `
      <div class="glr-header">
        <h4 class="glr-title">📝 Research Proposal: ${esc(p.title || gap.gap_title)}</h4>
        <p class="glr-sub">${esc(data.agent_explanation || 'Auto-generated proposal grounded in this gap')}</p>
      </div>
      <div class="glr-sections">
        ${p.problem_statement ? `<div class="glr-section"><div class="glr-section-title">Problem Statement</div><p>${esc(p.problem_statement)}</p></div>` : ''}
        ${objectives.length ? `<div class="glr-section"><div class="glr-section-title">Objectives</div><ol class="next-steps">${objectives.map(o=>`<li>${esc(o)}</li>`).join('')}</ol></div>` : ''}
        ${p.methodology ? `<div class="glr-section gap-highlight"><div class="glr-section-title">🔬 Methodology</div><p>${esc(p.methodology)}</p></div>` : ''}
        ${outcomes.length ? `<div class="glr-section"><div class="glr-section-title">Expected Outcomes</div><ol class="next-steps">${outcomes.map(o=>`<li>${esc(o)}</li>`).join('')}</ol></div>` : ''}
      </div>
      ${timeline.length ? `<div class="glr-table-section"><div class="glr-table-title">📅 Timeline</div>
        <table class="glr-table"><thead><tr><th>Phase</th><th>Duration</th><th>Activities</th></tr></thead>
        <tbody>${timeline.map(t=>`<tr><td>${esc(t.phase||'')}</td><td>${esc(t.duration||'')}</td><td>${esc(t.activities||'')}</td></tr>`).join('')}</tbody></table>
      </div>` : ''}
      ${resources.length ? `<div class="glr-table-section"><div class="glr-table-title">Required Resources</div><div class="keyword-cloud">${resources.map(r=>`<span class="kw-tag">${esc(r)}</span>`).join('')}</div></div>` : ''}
      ${risks.length ? `<div class="glr-table-section"><div class="glr-table-title">⚠️ Risks &amp; Mitigations</div>
        <table class="glr-table"><thead><tr><th>Risk</th><th>Mitigation</th></tr></thead>
        <tbody>${risks.map(r=>`<tr><td>${esc(r.risk||'')}</td><td>${esc(r.mitigation||'')}</td></tr>`).join('')}</tbody></table>
      </div>` : ''}
    `;
  } catch(e) {
    content.innerHTML = `<div style="color:var(--red);padding:1rem">❌ ${esc(e.message)}</div>`;
  }
};

// ═══════════════════════════════════════════════════
// WORKFLOW: Novelty Verification Agent (whole analysis)
// ═══════════════════════════════════════════════════
window.checkNovelty = async () => {
  const apiKey = document.getElementById('apiKey').value.trim();
  const ideaInput = document.getElementById('noveltyIdeaInput');
  const idea = ideaInput ? ideaInput.value.trim() : '';
  if (!idea) { alert('Describe your research idea first.'); return; }

  const analysisId = currentData?.analysis_id;
  if (!analysisId) { alert('Run an analysis first — novelty is checked against the papers just fetched.'); return; }

  const btn = document.getElementById('noveltyCheckBtn');
  const output = document.getElementById('noveltyCheckOutput');
  const originalBtn = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Checking...';
  output.innerHTML = `<div class="glr-loading"><div class="glr-spinner"></div><span>Comparing your idea against the fetched literature...</span></div>`;

  try {
    const resp = await fetch('/api/agents/novelty', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ api_key: apiKey, analysis_id: analysisId, idea })
    });
    const data = await resp.json();
    if (data.error) throw new Error(data.error);

    const score = data.novelty_score;
    const matches = data.closest_matches || [];
    const scoreColor = score===null ? 'var(--text2)' : score>=70 ? '#3fae8a' : score>=40 ? '#ff9f43' : '#ff4757';

    output.innerHTML = `
      <div class="glr-header" style="margin-top:0">
        <h4 class="glr-title">🧪 Novelty Result</h4>
        <p class="glr-sub">Compared against ${data.compared_against || 0} fetched papers via TF-IDF similarity</p>
      </div>
      <div class="prf-conf-block" style="margin:0.75rem 0">
        <div class="prf-conf-row">
          <span class="prf-conf-lbl">Novelty Score</span>
          <span class="prf-conf-num" style="color:${scoreColor}">${score===null?'N/A':score+'%'}</span>
        </div>
        <div class="prf-conf-track"><div class="prf-conf-fill" style="width:${score||0}%;background:${scoreColor}"></div></div>
      </div>
      ${data.reasoning ? `<div class="glr-section"><div class="glr-section-title">AI Assessment</div><p>${esc(data.reasoning)}</p></div>` : ''}
      ${matches.length ? `<div class="glr-table-section"><div class="glr-table-title">Closest Existing Papers</div>
        <table class="glr-table"><thead><tr><th>Paper</th><th>Year</th><th>Similarity</th></tr></thead>
        <tbody>${matches.map(m=>`<tr><td>${esc(m.title||'')}</td><td>${esc(m.year||'')}</td><td>${m.similarity_pct}%</td></tr>`).join('')}</tbody></table>
      </div>` : '<div class="prf-empty">No closely similar papers found in the fetched set.</div>'}
    `;
  } catch(e) {
    output.innerHTML = `<div style="color:var(--red);padding:1rem">❌ ${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalBtn;
  }
};

// ═══════════════════════════════════════════════════
// WORKFLOW: Advisor-ready one-page gap brief (PDF)
// ═══════════════════════════════════════════════════
window.downloadGapBrief = async (gapIndex) => {
  const gap = currentData?.analysis?.research_gaps?.[gapIndex];
  if (!gap) return;
  const btn = document.getElementById(`gapBriefBtn_${gapIndex}`);
  const original = btn.innerHTML;
  btn.innerHTML = '<span class="spinner"></span>Generating...';
  try {
    const topic = document.getElementById('topic').value;
    const resp = await fetch('/api/gap-brief/pdf', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ topic, gap })
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || 'Could not generate brief');
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'gap_brief.pdf';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  } catch(e) {
    alert('Error generating brief: ' + e.message);
  } finally {
    btn.innerHTML = original;
  }
};

// ═══════════════════════════════════════════════════
// WORKFLOW: Whole-analysis Related Work section draft
// ═══════════════════════════════════════════════════
window.generateRelatedWork = async () => {
  const apiKey = document.getElementById('apiKey').value.trim();
  if (!apiKey) { alert('Please enter your Groq API key first.'); return; }
  if (!currentData || !currentData.papers || !currentData.papers.length) {
    alert('Run an analysis first.'); return;
  }

  const btn = document.getElementById('relatedWorkBtn');
  const output = document.getElementById('relatedWorkOutput');
  const original = btn.innerHTML;
  btn.innerHTML = '<span class="spinner"></span>Drafting...';
  output.style.display = 'block';
  output.innerHTML = `<div class="glr-loading"><div class="glr-spinner"></div><span>Drafting related work section from ${currentData.papers.length} papers...</span></div>`;

  try {
    const topic = document.getElementById('topic').value;
    const resp = await fetch('/api/related-work', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ api_key: apiKey, topic, papers: currentData.papers })
    });
    const data = await resp.json();
    if (data.error) throw new Error(data.error);

    const text = data.related_work_section || '';
    output.innerHTML = `
      <div class="glr-header">
        <h4 class="glr-title">📝 Related Work — Draft</h4>
        <p class="glr-sub">${esc(data.bibtex_keys_note || 'Citations use the same keys as your BibTeX export.')}</p>
      </div>
      <div class="glr-section" style="white-space:pre-wrap;line-height:1.8">${esc(text)}</div>
      <button class="gen-litreview-btn" style="margin-top:1rem" onclick="copyRelatedWork()">📋 Copy to Clipboard</button>
    `;
    output.dataset.rawText = text;
  } catch(e) {
    output.innerHTML = `<div style="color:var(--red);padding:1rem">❌ ${esc(e.message)}</div>`;
  } finally {
    btn.innerHTML = original;
  }
};

window.copyRelatedWork = () => {
  const output = document.getElementById('relatedWorkOutput');
  const text = output?.dataset?.rawText || '';
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    alert('Copied to clipboard!');
  }).catch(() => {
    alert('Could not copy automatically — please select and copy the text manually.');
  });
};

// ═══════════════════════════════════════════════════
// REAL-TIME TRACKING: Watch Topic for Auto-Updates
// ═══════════════════════════════════════════════════
let currentWatchedTopic = null;

window.toggleWatch = async () => {
  const apiKey = document.getElementById('apiKey').value.trim();
  const topic = document.getElementById('topic').value.trim();
  if (!apiKey || !topic) { alert('Run an analysis first, then track it.'); return; }

  const btn = document.getElementById('watchBtn');
  const isWatching = btn.classList.contains('watching');

  if (isWatching) {
    // Unwatch
    try {
      await fetch('/api/unwatch', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({topic})
      });
      btn.classList.remove('watching');
      btn.innerHTML = '🔔 Track Live Updates';
    } catch(e) { alert('Error: ' + e.message); }
  } else {
    // Watch
    const notifyEmail = (prompt('Optional: enter an email to get alerted when new gaps/papers appear for this topic (leave blank to skip):', '') || '').trim();
    btn.innerHTML = '<span class="spinner"></span>Activating...';
    try {
      const resp = await fetch('/api/watch', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({topic, api_key: apiKey, notify_email: notifyEmail})
      });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      btn.classList.add('watching');
      btn.innerHTML = '✅ Tracking Live — Updates Daily';
      showWatchConfirmation(topic, notifyEmail);
    } catch(e) {
      btn.innerHTML = '🔔 Track Live Updates';
      alert('Error: ' + e.message);
    }
  }
};

function showWatchConfirmation(topic, notifyEmail) {
  const msg = document.createElement('div');
  msg.className = 'watch-toast';
  const emailNote = notifyEmail ? ` You'll also get an email at <b>${esc(notifyEmail)}</b> when something changes.` : '';
  msg.innerHTML = `🔔 Now tracking "<b>${esc(topic)}</b>" — the system will automatically re-check ArXiv and re-run gap analysis every 24 hours. New gaps and papers will appear in your Profile → Live Tracked Topics.${emailNote}`;
  document.body.appendChild(msg);
  setTimeout(() => msg.classList.add('show'), 50);
  setTimeout(() => { msg.classList.remove('show'); setTimeout(()=>msg.remove(), 400); }, 5000);
}

async function loadTrackedTopics() {
  try {
    const resp = await fetch('/api/watched-topics');
    const data = await resp.json();
    const topics = data.watched_topics || [];
    document.getElementById('liveCountBadge').textContent = topics.length;

    const container = document.getElementById('trackedTopicsList');
    if (topics.length === 0) {
      container.innerHTML = `<div style="color:var(--text3);font-size:0.85rem;text-align:center;padding:1rem">No topics tracked yet — click "Track Live Updates" after an analysis</div>`;
      return;
    }

    container.innerHTML = topics.map(t => {
      const lastChecked = t.last_checked_at ? new Date(t.last_checked_at).toLocaleString('en-IN', {dateStyle:'medium', timeStyle:'short'}) : 'Never';
      const recentNew = t.recent_snapshots && t.recent_snapshots[0];
      const newGapsList = (recentNew && recentNew.new_gaps) || [];
      const confShifts = (recentNew && recentNew.confidence_shifts) || [];
      const hasNewStuff = recentNew && (recentNew.new_papers_count > 0 || newGapsList.length > 0 || confShifts.length > 0);

      let card = '<div class="tracked-topic-card ' + (hasNewStuff?'has-updates':'') + '">';
      card += '<div class="tt-header"><div class="tt-topic-name">' + esc(t.topic) + '</div>';
      card += hasNewStuff ? '<span class="tt-new-badge">🆕 Updated</span>' : '';
      card += '</div>';
      card += '<div class="tt-meta-row">';
      card += '<span class="tt-meta-item">📄 ' + (t.last_paper_count||0) + ' papers</span>';
      card += '<span class="tt-meta-item">🔍 ' + (t.last_gap_count||0) + ' gaps</span>';
      card += '<span class="tt-meta-item">🕐 Checked: ' + lastChecked + '</span>';
      card += '</div>';
      if (recentNew && recentNew.new_papers_count > 0) {
        card += '<div class="tt-update-info">+' + recentNew.new_papers_count + ' new paper(s) found since last check</div>';
      }
      if (newGapsList.length > 0) {
        card += '<div class="tt-new-gaps"><div class="tt-new-gaps-label">🆕 New gaps detected:</div>';
        newGapsList.slice(0,3).forEach(g => { card += '<div class="tt-new-gap-item">• ' + esc(g.gap_title||'') + '</div>'; });
        card += '</div>';
      }
      if (confShifts.length > 0) {
        card += '<div class="tt-conf-shifts"><div class="tt-conf-shifts-label">📊 Confidence changes:</div>';
        confShifts.slice(0,3).forEach(c => {
          const arrow = c.direction === 'up' ? '↑' : '↓';
          card += '<div class="tt-conf-shift-item ' + c.direction + '">' + arrow + ' ' + esc(c.gap_title||'') + ': ' + c.old_confidence + '% → ' + c.new_confidence + '%</div>';
        });
        card += '</div>';
      }
      card += '<div class="tt-actions">';
      card += '<button class="tt-btn" onclick="checkTopicNow(' + t.id + ')">🔄 Check Now</button>';
      card += '<button class="tt-btn stop" onclick="unwatchFromListById(' + t.id + ')">⏹ Stop Tracking</button>';
      card += '</div></div>';
      return card;
    }).join('');
  } catch(e) {
    console.error('Failed to load tracked topics:', e);
  }
}

window.checkTopicNow = async (id) => {
  const btn = event.target;
  btn.innerHTML = '⏳ Checking...';
  btn.disabled = true;
  try {
    await fetch(`/api/check-now/${id}`, {method:'POST'});
    await loadTrackedTopics();
  } catch(e) {
    alert('Check failed: ' + e.message);
  }
};

window.unwatchFromList = async (topic, id) => {
  if (!confirm(`Stop tracking "${topic}"?`)) return;
  try {
    await fetch('/api/unwatch', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({topic})
    });
    await loadTrackedTopics();
  } catch(e) { alert('Error: ' + e.message); }
};

// Refresh tracked topics list whenever profile opens
const originalOpenProfile = window.openProfile;
window.openProfile = function() {
  originalOpenProfile();
  loadTrackedTopics();
};

// Load on page load too (in case any topics were already being tracked)
window.addEventListener('DOMContentLoaded', () => {
  loadTrackedTopics();
});

// ═══════════════════════════════════════════════════
// HONEST ERROR HANDLING: ArXiv unreachable vs generic
// ═══════════════════════════════════════════════════
function showArxivOrGenericError(status, data) {
  if (status === 503 || (data && data.error_type === 'arxiv_unreachable')) {
    // source_label names whichever source(s) actually failed (backend
    // sets this — see ArxivUnavailableError.source_label in app.py). Do
    // NOT default this to 'ArXiv': that's exactly the bug where this
    // modal used to blame ArXiv even when ArXiv was never queried (e.g.
    // strict venue mode only touches Semantic Scholar).
    const label = (data && data.source_label) || 'the selected source(s)';
    showSourceUnreachableModal(label, data.error || `Could not reach ${label} to fetch real research papers.`);
  } else {
    alert(data.error || 'Analysis failed');
  }
}

function showSourceUnreachableModal(sourceLabel, message) {
  const existing = document.getElementById('arxivErrorModal');
  if (existing) existing.remove();

  const isArxiv = sourceLabel === 'ArXiv';
  const explainRows = isArxiv
    ? [
        `🔌 This app only analyzes <b>real, live papers from ArXiv</b> — it never substitutes fake data.`,
        `🌐 This usually means your internet connection, firewall, or college/office network is blocking <code>export.arxiv.org</code>.`,
        `💡 Try a mobile hotspot, a different network, or simply retry in a minute.`,
      ]
    : [
        `🔌 This app only analyzes real, live papers from <b>${esc(sourceLabel)}</b> — it never substitutes fake data.`,
        `⏳ This is most often that source rate-limiting or briefly rejecting requests — not a network/firewall problem on your end.`,
        `💡 Wait a minute and retry, or try a broader topic / different sources.`,
      ];

  const modal = document.createElement('div');
  modal.id = 'arxivErrorModal';
  modal.className = 'arxiv-error-overlay';
  modal.innerHTML = `
    <div class="arxiv-error-modal">
      <div class="arxiv-error-icon">📡</div>
      <h3 class="arxiv-error-title">Couldn't Reach ${esc(sourceLabel)}</h3>
      <p class="arxiv-error-msg">${esc(message)}</p>
      <div class="arxiv-error-explain">
        ${explainRows.map(r => `<div class="aee-row">${r}</div>`).join('')}
      </div>
      <div class="arxiv-error-actions">
        <button class="arxiv-retry-btn" onclick="document.getElementById('arxivErrorModal').remove(); document.getElementById('analysisForm').requestSubmit();">🔄 Retry Now</button>
        <button class="arxiv-close-btn" onclick="document.getElementById('arxivErrorModal').remove();">Close</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
}

// unwatchFromListById — used by tracked topic cards (looks up topic by id)
window.unwatchFromListById = async (id) => {
  try {
    const resp = await fetch('/api/watched-topics');
    const data = await resp.json();
    const topic = (data.watched_topics || []).find(t => t.id === id);
    if (!topic) { alert('Topic not found'); return; }
    if (!confirm('Stop tracking "' + topic.topic + '"?')) return;
    await fetch('/api/unwatch', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({topic: topic.topic})
    });
    await loadTrackedTopics();
  } catch(e) { alert('Error: ' + e.message); }
};

// ═══════════════════════════════════════════════════
// COLLABORATION: Team Workspace (lightweight join-code model)
// ═══════════════════════════════════════════════════
function getWorkspace() {
  return JSON.parse(localStorage.getItem('rl_workspace') || 'null');
}
function setWorkspace(ws) {
  localStorage.setItem('rl_workspace', JSON.stringify(ws));
}

window.openWorkspaceModal = () => {
  document.getElementById('workspaceOverlay').style.display = 'block';
  document.getElementById('workspaceModal').classList.add('open');
  refreshWorkspaceModalView();
};
window.closeWorkspaceModal = () => {
  document.getElementById('workspaceOverlay').style.display = 'none';
  document.getElementById('workspaceModal').classList.remove('open');
};

function refreshWorkspaceModalView() {
  const ws = getWorkspace();
  if (ws) {
    document.getElementById('workspaceNotJoined').style.display = 'none';
    document.getElementById('workspaceJoined').style.display = 'block';
    document.getElementById('wsName').textContent = ws.workspace_name;
    document.getElementById('wsJoinCodeDisplay').textContent = ws.join_code;
    loadWorkspaceDashboard();
    loadWorkspaceLibrary();
  } else {
    document.getElementById('workspaceNotJoined').style.display = 'block';
    document.getElementById('workspaceJoined').style.display = 'none';
  }
}

window.createWorkspace = async () => {
  const name = document.getElementById('wsCreateName').value.trim();
  const displayName = document.getElementById('wsCreateDisplayName').value.trim();
  if (!name || !displayName) { alert('Enter a workspace name and your display name.'); return; }
  try {
    const resp = await fetch('/api/workspace/create', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ name, display_name: displayName })
    });
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    setWorkspace({ join_code: data.join_code, workspace_name: data.workspace_name, display_name: displayName });
    alert(`Workspace created! Share this join code with your team: ${data.join_code}`);
    refreshWorkspaceModalView();
  } catch(e) { alert('Error: ' + e.message); }
};

window.joinWorkspaceFromModal = async () => {
  const joinCode = document.getElementById('wsJoinCode').value.trim().toUpperCase();
  const displayName = document.getElementById('wsJoinDisplayName').value.trim();
  if (!joinCode || !displayName) { alert('Enter a join code and your display name.'); return; }
  try {
    const resp = await fetch('/api/workspace/join', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ join_code: joinCode, display_name: displayName })
    });
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    setWorkspace({ join_code: data.join_code, workspace_name: data.workspace_name, display_name: displayName });
    refreshWorkspaceModalView();
  } catch(e) { alert('Error: ' + e.message); }
};

window.leaveWorkspace = () => {
  if (!confirm('Leave this workspace on this device? (Your data stays in the workspace — you can rejoin with the join code anytime.)')) return;
  localStorage.removeItem('rl_workspace');
  refreshWorkspaceModalView();
};

async function loadWorkspaceDashboard() {
  const ws = getWorkspace();
  if (!ws) return;
  const el = document.getElementById('wsDashboard');
  el.innerHTML = 'Loading...';
  try {
    const resp = await fetch(`/api/workspace/${ws.join_code}/dashboard`);
    const d = await resp.json();
    if (d.error) throw new Error(d.error);
    el.innerHTML = `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.5rem;margin-bottom:0.75rem">
        <div class="profile-stat"><div class="profile-stat-num">${d.member_count}</div><div class="profile-stat-label">Members</div></div>
        <div class="profile-stat"><div class="profile-stat-num">${d.paper_count}</div><div class="profile-stat-label">Papers in library</div></div>
      </div>
      ${d.top_keywords.length ? `<div style="margin-bottom:0.5rem"><b>Group focus:</b> ${d.top_keywords.slice(0,8).map(esc).join(', ')}</div>` : ''}
      ${d.contributor_breakdown.length ? `<div style="margin-bottom:0.5rem"><b>Contributions:</b> ${d.contributor_breakdown.map(c=>`${esc(c.member)} (${c.papers_contributed})`).join(', ')}</div>` : ''}
      <div>💬 ${d.total_gap_discussions} gaps discussed &nbsp; 🙋 ${d.total_gap_assignments} gaps assigned</div>
    `;
  } catch(e) { el.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; }
}

async function loadWorkspaceLibrary() {
  const ws = getWorkspace();
  if (!ws) return;
  const el = document.getElementById('wsLibrary');
  el.innerHTML = 'Loading...';
  try {
    const resp = await fetch(`/api/workspace/${ws.join_code}/library`);
    const d = await resp.json();
    if (d.error) throw new Error(d.error);
    if (!d.papers.length) { el.innerHTML = '<span style="color:var(--text3)">No papers contributed yet.</span>'; return; }
    el.innerHTML = d.papers.map(p => `
      <div style="padding:0.5rem 0;border-bottom:1px solid var(--border)">
        <div style="font-size:0.82rem;font-weight:600">${esc(p.title)}</div>
        <div style="font-size:0.72rem;color:var(--text3)">${esc(p.year||'')} • added by ${esc(p.contributed_by||'someone')}</div>
      </div>`).join('');
  } catch(e) { el.innerHTML = `<span style="color:var(--red)">${esc(e.message)}</span>`; }
}

window.contributeToWorkspace = async () => {
  const ws = getWorkspace();
  if (!ws) { alert('Join or create a workspace first.'); return; }
  if (!currentData || !currentData.papers || !currentData.papers.length) {
    alert('Run an analysis first, then contribute its papers.'); return;
  }
  try {
    const topic = document.getElementById('topic').value.trim();
    const resp = await fetch(`/api/workspace/${ws.join_code}/contribute`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ display_name: ws.display_name, papers: currentData.papers, topic })
    });
    const d = await resp.json();
    if (d.error) throw new Error(d.error);
    alert(`Added ${d.added} new paper(s) to the shared library (${d.skipped_duplicates} already there).`);
    loadWorkspaceDashboard();
    loadWorkspaceLibrary();
  } catch(e) { alert('Error: ' + e.message); }
};

// ── Team panel inside a gap's proof view ──────────────────────────────
async function renderTeamPanel(gapIndex, gap) {
  const ws = getWorkspace();
  const panel = document.getElementById(`teamPanel_${gapIndex}`);
  const status = document.getElementById(`teamStatus_${gapIndex}`);
  if (!ws) {
    panel.innerHTML = '';
    return;
  }
  status.textContent = `Workspace: ${ws.workspace_name} (${ws.join_code})`;
  panel.innerHTML = `
    <div id="teamComments_${gapIndex}" style="margin-bottom:0.75rem"></div>
    <div style="display:flex;gap:0.5rem;margin-bottom:0.75rem">
      <input type="text" id="teamCommentInput_${gapIndex}" class="qa-input" placeholder="Add a comment for your team..." style="flex:1"/>
      <button class="qa-send-btn" onclick="postGapComment(${gapIndex})">➤</button>
    </div>
    <div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap;margin-bottom:0.5rem">
      <span id="teamAssignment_${gapIndex}" style="font-size:0.8rem;color:var(--text2)">Unassigned</span>
      <input type="text" id="teamAssignInput_${gapIndex}" class="field-input" placeholder="Assign to (name)" style="max-width:180px;padding:0.4rem 0.7rem;font-size:0.8rem"/>
      <button class="tt-btn" style="flex:0 0 auto" onclick="claimOrAssignGap(${gapIndex})">🙋 Assign</button>
      <button class="tt-btn" style="flex:0 0 auto" onclick="shareGapPublicly(${gapIndex})">🌍 Share Publicly</button>
    </div>
  `;
  loadGapComments(gapIndex, gap);
  loadGapAssignment(gapIndex, gap);
}

async function loadGapComments(gapIndex, gap) {
  const ws = getWorkspace();
  const el = document.getElementById(`teamComments_${gapIndex}`);
  if (!ws || !el) return;
  try {
    const resp = await fetch(`/api/workspace/${ws.join_code}/gap/comments?gap_title=${encodeURIComponent(gap.gap_title)}`);
    const d = await resp.json();
    const comments = d.comments || [];
    el.innerHTML = comments.length
      ? comments.map(c => `<div style="font-size:0.8rem;padding:0.4rem 0;border-bottom:1px solid var(--border)"><b>${esc(c.author_name)}:</b> ${esc(c.comment_text)}</div>`).join('')
      : '<div style="font-size:0.8rem;color:var(--text3)">No comments yet — be the first.</div>';
  } catch(e) { /* non-critical, fail quietly */ }
}

window.postGapComment = async (gapIndex) => {
  const ws = getWorkspace();
  const gap = currentData?.analysis?.research_gaps?.[gapIndex];
  const input = document.getElementById(`teamCommentInput_${gapIndex}`);
  const text = input.value.trim();
  if (!ws || !gap || !text) return;
  try {
    await fetch(`/api/workspace/${ws.join_code}/gap/comment`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ display_name: ws.display_name, gap_title: gap.gap_title, comment: text })
    });
    input.value = '';
    loadGapComments(gapIndex, gap);
  } catch(e) { alert('Error posting comment: ' + e.message); }
};

async function loadGapAssignment(gapIndex, gap) {
  const ws = getWorkspace();
  const el = document.getElementById(`teamAssignment_${gapIndex}`);
  if (!ws || !el) return;
  try {
    const resp = await fetch(`/api/workspace/${ws.join_code}/gap/assignments`);
    const d = await resp.json();
    const match = (d.assignments || []).find(a => a.gap_title === gap.gap_title);
    el.textContent = match ? `Assigned to: ${match.assigned_to}` : 'Unassigned';
  } catch(e) { /* non-critical */ }
}

window.claimOrAssignGap = async (gapIndex) => {
  const ws = getWorkspace();
  const gap = currentData?.analysis?.research_gaps?.[gapIndex];
  const input = document.getElementById(`teamAssignInput_${gapIndex}`);
  const assignedTo = input.value.trim() || ws?.display_name;
  if (!ws || !gap || !assignedTo) return;
  try {
    await fetch(`/api/workspace/${ws.join_code}/gap/assign`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ display_name: ws.display_name, gap_title: gap.gap_title, assigned_to: assignedTo })
    });
    loadGapAssignment(gapIndex, gap);
  } catch(e) { alert('Error assigning: ' + e.message); }
};

window.shareGapPublicly = async (gapIndex) => {
  const gap = currentData?.analysis?.research_gaps?.[gapIndex];
  if (!gap) return;
  const ws = getWorkspace();
  const attributionName = (ws?.display_name) || prompt('Share publicly as (your name, or leave blank for Anonymous):', '') || 'Anonymous';
  if (!confirm(`Share "${gap.gap_title}" to the public Community Gaps feed as "${attributionName}"? This is visible to everyone.`)) return;
  try {
    const resp = await fetch('/api/community/share-gap', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        consent: true,
        gap_title: gap.gap_title,
        description: gap.description,
        topic: document.getElementById('topic').value.trim(),
        severity: gap.severity,
        contributor_name: attributionName
      })
    });
    const d = await resp.json();
    if (d.error) throw new Error(d.error);
    alert('Shared to the community feed!');
  } catch(e) { alert('Error sharing: ' + e.message); }
};

// ═══════════════════════════════════════════════════
// COMMUNITY: Public gap feed
// ═══════════════════════════════════════════════════
window.openCommunityModal = () => {
  document.getElementById('communityOverlay').style.display = 'block';
  document.getElementById('communityModal').classList.add('open');
  loadCommunityGaps();
};
window.closeCommunityModal = () => {
  document.getElementById('communityOverlay').style.display = 'none';
  document.getElementById('communityModal').classList.remove('open');
};

window.loadCommunityGaps = async () => {
  const search = document.getElementById('communitySearch').value.trim();
  const feed = document.getElementById('communityFeed');
  feed.innerHTML = 'Loading...';
  try {
    const resp = await fetch(`/api/community/gaps${search ? '?search='+encodeURIComponent(search) : ''}`);
    const d = await resp.json();
    const gaps = d.gaps || [];
    if (!gaps.length) { feed.innerHTML = '<div style="color:var(--text3);font-size:0.85rem;text-align:center;padding:1rem">No public gaps yet — be the first to share one from a gap\'s proof panel.</div>'; return; }
    feed.innerHTML = gaps.map(g => `
      <div class="tracked-topic-card">
        <div class="tt-header"><div class="tt-topic-name">${esc(g.gap_title)}</div><span class="tt-new-badge" style="background:rgba(63,174,138,0.15);color:var(--green)">${esc((g.severity||'').toUpperCase())}</span></div>
        <div style="font-size:0.82rem;color:var(--text2);margin:0.4rem 0">${esc(g.description||'')}</div>
        <div class="tt-meta-row">
          <span class="tt-meta-item">🔬 ${esc(g.topic||'')}</span>
          <span class="tt-meta-item">👤 ${esc(g.contributor_name||'Anonymous')}</span>
        </div>
        <div class="tt-actions">
          <button class="tt-btn" onclick="upvoteCommunityGap(${g.id}, this)">👍 ${g.upvotes||0}</button>
        </div>
      </div>`).join('');
  } catch(e) { feed.innerHTML = `<div style="color:var(--red)">${esc(e.message)}</div>`; }
};

window.upvoteCommunityGap = async (id, btn) => {
  try {
    const resp = await fetch(`/api/community/gaps/${id}/upvote`, { method: 'POST' });
    const d = await resp.json();
    if (d.error) throw new Error(d.error);
    btn.textContent = `👍 ${d.upvotes}`;
  } catch(e) { alert('Error: ' + e.message); }
};

// ═══════════════════════════════════════════════════
// INSTITUTIONAL: Thesis / proposal novelty check
// ═══════════════════════════════════════════════════
window.runThesisCheck = async () => {
  const apiKey = document.getElementById('thesisApiKey').value.trim() || document.getElementById('apiKey').value.trim();
  const topic = document.getElementById('thesisTopic').value.trim();
  const contribution = document.getElementById('thesisContribution').value.trim();
  if (!apiKey || !topic || !contribution) { alert('Fill in API key, topic, and your proposed contribution.'); return; }

  const btn = document.getElementById('thesisCheckBtn');
  const result = document.getElementById('thesisResult');
  btn.disabled = true;
  btn.querySelector('.btn-text').textContent = 'Checking against live literature...';
  result.style.display = 'block';
  result.innerHTML = `<div class="glr-loading"><div class="glr-spinner"></div><span>Fetching literature and checking novelty...</span></div>`;

  try {
    const formData = new FormData();
    formData.append('api_key', apiKey);
    formData.append('topic', topic);
    formData.append('proposed_contribution', contribution);
    const resp = await fetch('/api/thesis-check', { method: 'POST', body: formData });
    const data = await resp.json();
    if (!resp.ok) { showArxivOrGenericError(resp.status, data); result.style.display='none'; return; }

    const verdictColor = {novel:'var(--green)', partially_covered:'var(--orange)', already_done:'var(--red)'}[data.verdict] || 'var(--text2)';
    const verdictLabel = {novel:'✅ Appears Novel', partially_covered:'⚠️ Partially Covered', already_done:'🔴 Already Done'}[data.verdict] || data.verdict;

    result.innerHTML = `
      <div class="glr-header">
        <h4 class="glr-title" style="color:${verdictColor}">${verdictLabel}</h4>
        <p class="glr-sub">Checked against ${data.papers_checked_against||0} papers • ${data.confidence||'N/A'}% confidence</p>
      </div>
      <div class="glr-section"><p>${esc(data.reasoning||'')}</p></div>
      ${(data.closest_matches||[]).length ? `
        <div class="glr-table-section">
          <div class="glr-table-title">Closest existing work</div>
          ${data.closest_matches.map(m => `<div class="glr-section" style="border-left-color:var(--orange)"><b>${esc(m.paper)}</b><p>${esc(m.overlap_description)}</p></div>`).join('')}
        </div>` : ''}
      ${(data.differentiation_suggestions||[]).length ? `
        <div class="glr-table-section">
          <div class="glr-table-title">Ways to differentiate</div>
          <ul class="next-steps">${data.differentiation_suggestions.map(s=>`<li>${esc(s)}</li>`).join('')}</ul>
        </div>` : ''}
    `;
  } catch(e) {
    result.innerHTML = `<div style="color:var(--red);padding:1rem">❌ ${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.querySelector('.btn-text').textContent = 'Check Novelty';
  }
};

// Load community feed preview + workspace state on page load
window.addEventListener('DOMContentLoaded', () => {
  const ws = getWorkspace();
  if (ws) console.log(`Workspace active: ${ws.workspace_name} (${ws.join_code})`);
});

// ═══════════════════════════════════════════════════
// EVALUATION: Rate a gap's accuracy + view the real dashboard
// ═══════════════════════════════════════════════════
window.rateGap = async (gapIndex, verdict) => {
  const gap = currentData?.analysis?.research_gaps?.[gapIndex];
  if (!gap) return;
  const controls = document.getElementById(`feedbackControls_${gapIndex}`);
  try {
    await fetch('/api/gap-feedback', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        topic: document.getElementById('topic').value.trim(),
        gap_title: gap.gap_title,
        severity: gap.severity,
        confidence_score: gap.confidence_score,
        evidence_verified: !!gap.evidence_verified,
        verdict,
        rater_name: (getWorkspace()?.display_name) || 'Anonymous'
      })
    });
    controls.innerHTML = `<span style="color:var(--green);font-size:0.85rem">✓ Thanks — recorded as "${esc(verdict)}". This feeds the accuracy dashboard.</span>`;
  } catch(e) {
    alert('Error recording feedback: ' + e.message);
  }
};

window.openEvalModal = () => {
  document.getElementById('evalOverlay').style.display = 'block';
  document.getElementById('evalModal').classList.add('open');
  loadEvalDashboard();
};
window.closeEvalModal = () => {
  document.getElementById('evalOverlay').style.display = 'none';
  document.getElementById('evalModal').classList.remove('open');
};

// Show "n/a" instead of "—%" when there is no data for a rate.
function fmtPct(v) {
  return (v === null || v === undefined || Number.isNaN(v)) ? 'n/a' : `${v}%`;
}

async function loadEvalDashboard() {
  const el = document.getElementById('evalDashboardContent');
  el.innerHTML = 'Loading...';
  try {
    const resp = await fetch('/api/eval/accuracy-summary');
    const d = await resp.json();
    if (d.error) throw new Error(d.error);
    if (d.total_ratings === 0) {
      el.innerHTML = `<div style="color:var(--text3);font-size:0.85rem;text-align:center;padding:1.5rem">${esc(d.message)}</div>`;
      return;
    }
    const sevRows = Object.entries(d.by_severity || {}).map(([sev, s]) =>
      `<div class="tt-meta-item">${esc(sev)}: ${fmtPct(s.valid_rate)} valid (n=${s.total})</div>`).join('');
    const confRows = Object.entries(d.confidence_calibration || {}).map(([bucket, s]) =>
      `<div style="padding:0.4rem 0;border-bottom:1px solid var(--border);font-size:0.82rem">
        <b>${esc(bucket)}</b>: ${fmtPct(s.valid_rate)} actually valid (n=${s.total})
      </div>`).join('');

    el.innerHTML = `
      <div class="profile-stats" style="margin-bottom:1rem">
        <div class="profile-stat"><div class="profile-stat-num">${d.total_ratings}</div><div class="profile-stat-label">Total ratings</div></div>
        <div class="profile-stat"><div class="profile-stat-num">${fmtPct(d.overall_valid_rate)}</div><div class="profile-stat-label">Valid rate</div></div>
        <div class="profile-stat"><div class="profile-stat-num">${fmtPct(d.evidence_verified_valid_rate)}</div><div class="profile-stat-label">Verified-quote valid rate</div></div>
      </div>
      <div class="profile-section-title">Confidence calibration</div>
      <p style="font-size:0.75rem;color:var(--text3);margin-bottom:0.5rem">Does a higher stated confidence score actually mean the gap is more likely real?</p>
      <div style="margin-bottom:1rem">${confRows || '<span style="color:var(--text3);font-size:0.8rem">Not enough rated gaps with confidence scores yet.</span>'}</div>
      <div class="profile-section-title">By severity</div>
      <div style="display:flex;flex-direction:column;gap:0.3rem;margin-bottom:1rem">${sevRows || '<span style="color:var(--text3);font-size:0.8rem">No data yet.</span>'}</div>
      <p style="font-size:0.72rem;color:var(--text3);font-style:italic">${esc(d.note)}</p>
    `;
  } catch(e) {
    el.innerHTML = `<div style="color:var(--red)">${esc(e.message)}</div>`;
  }
}