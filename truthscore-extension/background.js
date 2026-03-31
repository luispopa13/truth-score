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
    return true; // keep channel open for async response
  }
});

// ── Verify claim via Python backend ──────────────────────────
async function verifyClaim(text) {
  if (!text || text.trim().length < 5) {
    throw new Error("Textul este prea scurt pentru verificare.");
  }

  const settings = await chrome.storage.sync.get("backendUrl");
  const apiBase  = settings.backendUrl || BACKEND_URL;

  let res;
  try {
    res = await fetch(`${apiBase}/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text.trim() }),
    });
  } catch {
    throw new Error("Backend offline. Rulează: uvicorn main:app --reload");
  }

  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`Backend ${res.status}: ${detail.slice(0, 150)}`);
  }

  const d = await res.json();

  // Normalize v3 response format for content.js / popup.js
  return {
    claim:       d.claim,
    score:       d.score,
    verdict:     d.verdict,
    confidence:  d.confidence,
    explanation: d.explanation,
    supporting:     d.supporting     || [],
    contradicting:  d.contradicting  || [],
    neutral_sources: d.neutral_sources || [],
    evidence_count:  d.evidence_count  || 0,
    models_used:     d.models_used     || [],
    cached:      d.cached || false,
    // Legacy fields for popup.js compatibility
    hfResult: null,
    factCheckSources: (d.contradicting || []).concat(d.supporting || [])
      .filter(s => s.type === "factcheck"),
    wikiSources: [...(d.supporting||[]),...(d.contradicting||[]),...(d.neutral_sources||[])]
      .filter(s => s.type === "wikipedia"),
  };
}