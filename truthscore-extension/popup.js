// ============================================================
// TruthScore – Popup Logic
// ============================================================

const VERDICT_LABELS = {
  TRUE: '✅ ADEVĂRAT',
  FALSE: '❌ FALS',
  UNCERTAIN: '⚠️ INCERT',
  ERROR: '⚠️ EROARE'
};

const VERDICT_COLORS = {
  TRUE: '#22c55e',
  FALSE: '#ef4444',
  UNCERTAIN: '#f59e0b',
  ERROR: '#6b7280'
};

// ── DOM refs ──────────────────────────────────────────────────
const claimInput = document.getElementById('claimInput');
const verifyBtn = document.getElementById('verifyBtn');
const pasteBtn = document.getElementById('pasteBtn');
const loadingEl = document.getElementById('loading');
const loadingText = document.getElementById('loadingText');
const emptyState = document.getElementById('emptyState');
const resultEl = document.getElementById('result');
const errorBox = document.getElementById('errorBox');

const scoreNumber = document.getElementById('scoreNumber');
const scoreRingFill = document.getElementById('scoreRingFill');
const verdictBadge = document.getElementById('verdictBadge');
const explanationText = document.getElementById('explanationText');
const breakdownEl = document.getElementById('breakdown');
const breakdownRows = document.getElementById('breakdownRows');
const sourcesSection = document.getElementById('sourcesSection');
const sourcesList = document.getElementById('sourcesList');

// ── Init ──────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  // Check if there's selected text from context menu
  const stored = await chrome.storage.session.get('pendingClaim');
  if (stored.pendingClaim) {
    claimInput.value = stored.pendingClaim;
    chrome.storage.session.remove('pendingClaim');
    runVerification(stored.pendingClaim);
  }

  // Settings button
  document.getElementById('openSettings').addEventListener('click', () => {
    chrome.runtime.openOptionsPage();
  });
  document.getElementById('openOptions').addEventListener('click', () => {
    chrome.runtime.openOptionsPage();
  });
});

// ── Verify button ─────────────────────────────────────────────
verifyBtn.addEventListener('click', () => {
  const text = claimInput.value.trim();
  if (!text) {
    showError('Introdu o afirmație pentru verificare.');
    return;
  }
  runVerification(text);
});

claimInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.ctrlKey) {
    verifyBtn.click();
  }
});

// ── Paste from page ───────────────────────────────────────────
pasteBtn.addEventListener('click', async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => window.getSelection()?.toString() || ''
    });
    const selected = results?.[0]?.result || '';
    if (selected && selected.length > 3) {
      claimInput.value = selected.slice(0, 500);
    } else {
      showError('Selectează text pe pagină mai întâi.');
    }
  } catch {
    showError('Nu s-a putut accesa pagina curentă.');
  }
});

// ── Main verification flow ────────────────────────────────────
async function runVerification(text) {
  hideError();
  setLoading(true, 'Se extrag claims-urile...');
  setView('loading');

  const loadingSteps = [
    'Se analizează cu modelul NLI...',
    'Se caută în Wikipedia...',
    'Se verifică fact-check sources...',
    'Se calculează scorul final...'
  ];
  let step = 0;
  const stepInterval = setInterval(() => {
    if (step < loadingSteps.length) {
      loadingText.textContent = loadingSteps[step++];
    }
  }, 900);

  try {
    const res = await chrome.runtime.sendMessage({
      type: 'VERIFY_CLAIM',
      text: text
    });

    clearInterval(stepInterval);

    if (res?.error) {
      throw new Error(res.error);
    }

    displayResult(res);
  } catch (err) {
    clearInterval(stepInterval);
    showError(err.message || 'Eroare necunoscută.');
    setView('empty');
  } finally {
    setLoading(false);
  }
}

// ── Display result ────────────────────────────────────────────
function displayResult(data) {
  setView('result');

  const score = data.score ?? 50;
  const verdict = data.verdict || 'UNCERTAIN';
  const color = VERDICT_COLORS[verdict] || VERDICT_COLORS.UNCERTAIN;

  // Animate score ring
  animateScore(score, color);

  // Verdict badge
  verdictBadge.textContent = VERDICT_LABELS[verdict] || verdict;
  verdictBadge.className = `verdict-badge verdict-${verdict}`;

  // Explanation
  explanationText.textContent = data.explanation || 'Nicio explicație disponibilă.';

  // NLI breakdown
  if (data.hfResult?.labelMap) {
    renderBreakdown(data.hfResult.labelMap);
    breakdownEl.style.display = 'block';
  } else {
    breakdownEl.style.display = 'none';
  }

  // Sources
  const allSources = [...(data.factCheckSources || []), ...(data.wikiSources || [])];
  if (allSources.length > 0) {
    renderSources(allSources);
    sourcesSection.style.display = 'block';
  } else {
    sourcesSection.style.display = 'none';
  }
}

function animateScore(score, color) {
  scoreNumber.textContent = score;
  scoreNumber.style.color = color;

  const circumference = 188.5;
  const offset = circumference - (score / 100) * circumference;

  // Slight delay for animation effect
  requestAnimationFrame(() => {
    setTimeout(() => {
      scoreRingFill.style.stroke = color;
      scoreRingFill.style.strokeDashoffset = offset;
    }, 50);
  });
}

function renderBreakdown(labelMap) {
  const labelNames = {
    'factually correct and verified': '✅ Corect & Verificat',
    'misinformation or false claim': '❌ Dezinformare / Fals',
    'partially correct or misleading': '⚠️ Parțial corect',
    'unverifiable or opinion': '🔵 Neverificabil / Opinie'
  };

  const colors = {
    'factually correct and verified': '#22c55e',
    'misinformation or false claim': '#ef4444',
    'partially correct or misleading': '#f59e0b',
    'unverifiable or opinion': '#6c63ff'
  };

  breakdownRows.innerHTML = '';
  Object.entries(labelMap)
    .sort((a, b) => b[1] - a[1])
    .forEach(([label, score]) => {
      const pct = Math.round(score * 100);
      const row = document.createElement('div');
      row.className = 'breakdown-row';
      row.innerHTML = `
        <span class="breakdown-label">${labelNames[label] || label}</span>
        <div class="breakdown-bar-bg">
          <div class="breakdown-bar" style="width:${pct}%; background:${colors[label] || '#6c63ff'}"></div>
        </div>
        <span class="breakdown-pct">${pct}%</span>
      `;
      breakdownRows.appendChild(row);
    });
}

function renderSources(sources) {
  sourcesList.innerHTML = '';

  sources.forEach(src => {
    const item = document.createElement('a');
    item.className = 'source-item';
    item.href = src.url || '#';
    item.target = '_blank';
    item.rel = 'noopener noreferrer';

    if (!src.url) item.style.cursor = 'default';

    const icon = src.type === 'factcheck' ? '🔎' : '📖';
    const ratingClass = getRatingClass(src.credibilityRating);
    const ratingLabel = src.credibilityRating === 'true' ? 'ADEVĂRAT'
      : src.credibilityRating === 'false' ? 'FALS'
      : src.credibilityRating === 'high' ? 'CREDIBIL'
      : 'INCERT';

    item.innerHTML = `
      <span class="source-icon">${icon}</span>
      <div style="flex:1; min-width:0">
        <div class="source-title">${escapeHtml(src.title || 'Sursă necunoscută')}</div>
        <div class="source-meta">
          ${src.publisher ? escapeHtml(src.publisher) + ' · ' : ''}
          ${src.review ? escapeHtml(src.review) : (src.type === 'wikipedia' ? 'Wikipedia' : '')}
        </div>
      </div>
      <span class="source-rating ${ratingClass}">${ratingLabel}</span>
    `;
    sourcesList.appendChild(item);
  });
}

// ── UI Helpers ────────────────────────────────────────────────
function setView(view) {
  emptyState.style.display = view === 'empty' ? 'block' : 'none';
  loadingEl.className = view === 'loading' ? 'loading active' : 'loading';
  resultEl.className = view === 'result' ? 'result active' : 'result';
}

function setLoading(active, msg) {
  verifyBtn.disabled = active;
  verifyBtn.textContent = active ? '⏳ Analiză...' : 'Verifică';
  if (msg) loadingText.textContent = msg;
}

function showError(msg) {
  errorBox.textContent = '⚠️ ' + msg;
  errorBox.classList.add('active');
}

function hideError() {
  errorBox.classList.remove('active');
}

function getRatingClass(rating) {
  if (rating === 'true' || rating === 'high') return 'rating-high';
  if (rating === 'false' || rating === 'low') return 'rating-low';
  return 'rating-medium';
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.appendChild(document.createTextNode(text));
  return div.innerHTML;
}
