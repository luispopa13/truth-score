// TruthScore – Background Service Worker v3
const BACKEND_URL = "http://localhost:8000";

// ── Context Menu ─────────────────────────────────────────────
chrome.runtime.onInstalled.addListener(() => {
  // Remove existing menus first to avoid duplicates on reload
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "truthscore-check",
      title: "🔍 Verifică cu TruthScore",
      contexts: ["selection"],
    });
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== "truthscore-check") return;
  const text = info.selectionText?.trim();
  if (!text || !tab?.id) return;

  try {
    // Always inject content script first — fixes the "nothing happens" bug
    // This ensures content.js is loaded even on pages opened before extension install
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["content.js"],
    });
    await chrome.scripting.insertCSS({
      target: { tabId: tab.id },
      files: ["content.css"],
    });
  } catch (e) {
    // Already injected or restricted page (chrome://, pdf, etc.) — ignore
    console.log("[TruthScore] Script injection:", e.message);
  }

  // Small delay so content script initializes
  setTimeout(() => {
    chrome.tabs.sendMessage(tab.id, {
      type: "SHOW_INLINE_CHECK",
      text: text,
    }).catch(err => {
      console.error("[TruthScore] sendMessage failed:", err.message);
    });
  }, 150);
});

// ── Message handler ───────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "VERIFY_CLAIM") {
    verifyClaim(msg.text)
      .then(sendResponse)
      .catch((err) => sendResponse({ error: err.message }));
    return true;
  }
  if (msg.type === "DETECT_CLAIMS") {
    detectClaims(msg.text)
      .then(sendResponse)
      .catch((err) => sendResponse({ error: err.message }));
    return true;
  }
  if (msg.type === "DETECT_AI_CONTENT") {
    detectAI(msg.text)
      .then(sendResponse)
      .catch((err) => sendResponse({ error: err.message }));
    return true;
  }
  if (msg.type === "SUBMIT_FEEDBACK") {
    submitFeedback(msg.data)
      .then(sendResponse)
      .catch((err) => sendResponse({ error: err.message }));
    return true;
  }
});

async function applyAuthHeaders(headers) {
  // Prefer a stable API key if present, else the login JWT token
  try {
    const { ts_api_key } = await chrome.storage.local.get("ts_api_key");
    if (ts_api_key) {
      headers["Authorization"] = "Bearer " + ts_api_key;
      return;
    }
  } catch (e) {}
  try {
    const { ts_token } = await chrome.storage.local.get("ts_token");
    if (ts_token) headers["Authorization"] = "Bearer " + ts_token;
  } catch (e) {}
}

// ── Verify claim via Python backend ──────────────────────────
async function verifyClaim(text) {
  if (!text || text.trim().length < 5) {
    throw new Error("Textul este prea scurt pentru verificare.");
  }

  const settings = await chrome.storage.sync.get("backendUrl");
  const apiBase  = settings.backendUrl || BACKEND_URL;

  const headers = { "Content-Type": "application/json" };
  await applyAuthHeaders(headers);

  let res;
  try {
    const isParagraph = text.length > 220 || ((text.match(/[.!?]+(?:\s|$)/g) || []).length > 1);
    res = await fetch(`${apiBase}${isParagraph ? "/analyze-text" : "/verify"}`, {
      method: "POST",
      headers: headers,
      body: JSON.stringify({ text: text.trim() }),
    });
  } catch {
    throw new Error("Backend offline. Rulează: uvicorn main:app --reload");
  }

  if (!res.ok) {
    // 429 = rate limit hit
    let detail = "";
    try { const j = await res.json(); detail = j.detail || ""; } catch (_) {}
    const msg = detail || (await res.text().catch(() => "")).slice(0, 150);
    throw new Error(`Backend ${res.status}: ${msg}`);
  }

  const d = await res.json();

  // Free-tier UX signals (headers exposed via CORS expose_headers)
  const showAds   = res.headers.get("X-TruthScore-Show-Ads") === "1";
  const quotaHdr  = res.headers.get("X-TruthScore-Quota-Left");
  const quotaLeft = quotaHdr !== null && quotaHdr !== "" ? parseInt(quotaHdr, 10) : null;

  // Paragraph responses already contain normalized per-claim VerifyResponse items.
  if (Array.isArray(d.results)) {
    return {
      ...d,
      models_used: [],
      results: d.results.map(item => ({ ...item, models_used: [] })),
      show_ads: showAds,
      quota_left: Number.isFinite(quotaLeft) ? quotaLeft : null,
    };
  }

  // Normalize a single-claim response for content.js / popup.js
  return {
    claim:       d.claim,
    score:       d.score,
    verdict:     d.verdict,
    confidence:  d.confidence,
    explanation: d.explanation,
    topic:       d.topic || "general",
    supporting:     d.supporting     || [],
    contradicting:  d.contradicting  || [],
    neutral_sources: d.neutral_sources || [],
    evidence_count:  d.evidence_count  || 0,
    // Never trust/passthrough provider metadata, even if backend leaks it.
    models_used:     [],
    cached:      d.cached || false,
    show_ads:    showAds,
    quota_left:  Number.isFinite(quotaLeft) ? quotaLeft : null,
    // Legacy fields for popup.js compatibility
    hfResult: null,
    factCheckSources: (d.contradicting || []).concat(d.supporting || [])
      .filter(s => s.type === "factcheck"),
    wikiSources: [...(d.supporting||[]),...(d.contradicting||[]),...(d.neutral_sources||[])]
      .filter(s => s.type === "wikipedia"),
  };
}

// ── Claim detection via backend ───────────────────────────────
async function detectClaims(text) {
  const settings = await chrome.storage.sync.get("backendUrl");
  const apiBase  = settings.backendUrl || BACKEND_URL;
  let res;
  try {
    res = await fetch(`${apiBase}/detect-claims`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text.slice(0, 30000), max_claims: 12 }),
    });
  } catch {
    throw new Error("Backend offline. Rulează: uvicorn main:app --reload");
  }
  if (!res.ok) {
    const d = await res.text().catch(() => "");
    throw new Error(`Backend ${res.status}: ${d.slice(0, 150)}`);
  }
  return await res.json();
}

// ── Feedback submission ───────────────────────────────────────
async function submitFeedback(data) {
  const settings = await chrome.storage.sync.get("backendUrl");
  const apiBase  = settings.backendUrl || BACKEND_URL;
  const res = await fetch(`${apiBase}/feedback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  if (!res.ok) throw new Error(`Feedback ${res.status}`);
  return await res.json();
}

// ── AI Content Detection ──────────────────────────────────────
async function detectAI(text) {
  const settings = await chrome.storage.sync.get("backendUrl");
  const apiBase  = settings.backendUrl || BACKEND_URL;
  let res;
  try {
    res = await fetch(`${apiBase}/detect-ai`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text.slice(0, 4000) }),
    });
  } catch {
    throw new Error("Backend offline. Rulează: uvicorn main:app --reload");
  }
  if (!res.ok) {
    const d = await res.text().catch(() => "");
    throw new Error(`Backend ${res.status}: ${d.slice(0, 150)}`);
  }
  return await res.json();
}