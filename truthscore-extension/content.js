// TruthScore Content Script v5 — clean
if (typeof window.__tsLoaded === 'undefined') {
window.__tsLoaded = true;

// TruthScore Content Script v5 — clean
let bubble = null, panel = null, hideTimer = null;
let scanActive = false, summaryPanel = null;
let _fbCounter = 0;
const HIGHLIGHT_CLASS = "ts-claim";
const stats = { TRUE: 0, FALSE: 0, UNCERTAIN: 0, pending: 0 };
// Free-tier notice shown inside inline panels (self-promo, never third-party
// ads injected into pages — Chrome Web Store policy + user trust).
// Built lazily so it reflects the language synced from chrome.storage.
function sponsorLine() {
  return '<div class="tsp-sponsor">' + t('sponsorLine') + '</div>';
}

// ── Safe chrome.runtime wrapper ──────────────────────────────
function safeMsg(msg) {
  return new Promise((resolve) => {
    try {
      if (!chrome?.runtime?.id) { resolve({ error: t('ctxReload') }); return; }
      chrome.runtime.sendMessage(msg, (res) => {
        if (chrome.runtime.lastError) {
          resolve({ error: chrome.runtime.lastError.message });
        } else {
          resolve(res || {});
        }
      });
    } catch (e) {
      resolve({ error: t('ctxInvalidated') });
    }
  });
}

// ── Selection bubble ──────────────────────────────────────────
document.addEventListener("mouseup", (e) => {
  if (e.target.closest("#ts-panel,#ts-bubble,#ts-summary")) return;
  setTimeout(() => {
    const sel  = window.getSelection();
    const text = sel ? sel.toString().trim() : "";
    if (text && text.length >= 8 && text.length <= 800) showBubble(text, sel);
    else hideBubble();
  }, 120);
});

document.addEventListener("mousedown", (e) => {
  if (!e.target.closest("#ts-panel,#ts-bubble,#ts-summary")) {
    hideBubble();
    if (!e.target.classList.contains(HIGHLIGHT_CLASS)) removePanel();
  }
});

// ── Messages ──────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "SHOW_INLINE_CHECK") { hideBubble(); showPanel(msg.text, null); }
  if (msg.type === "SCAN_PAGE")          { runFullScan(); }
  if (msg.type === "CLEAR_HIGHLIGHTS")   { clearAll(); }
});

// ── Bubble ────────────────────────────────────────────────────
function showBubble(text, sel) {
  hideBubble();
  if (!sel.rangeCount) return;
  const rect = sel.getRangeAt(0).getBoundingClientRect();
  const b = document.createElement("div");
  b.id = "ts-bubble";
  b.innerHTML = `<span style="font-size:12px">🔍</span><span>TruthScore</span>`;
  document.body.appendChild(b);
  bubble = b;
  b.style.left = Math.max(8, rect.left + window.scrollX + rect.width/2 - 58) + "px";
  b.style.top  = (rect.top + window.scrollY - 42) + "px";
  b.addEventListener("click", (e) => { e.stopPropagation(); hideBubble(); showPanel(text, rect); });
  clearTimeout(hideTimer);
  hideTimer = setTimeout(hideBubble, 7000);
}
function hideBubble() {
  if (bubble) { bubble.remove(); bubble = null; }
  clearTimeout(hideTimer);
}

// ── Full page scan ────────────────────────────────────────────
async function runFullScan() {
  if (scanActive) { clearAll(); return; }
  scanActive = true;
  stats.TRUE = stats.FALSE = stats.UNCERTAIN = stats.pending = 0;
  showIndicator(t('scanDetecting'));

  const pageText = extractPageText();
  if (!pageText || pageText.length < 50) {
    showIndicator(t('scanNoText'));
    setTimeout(hideIndicator, 3000);
    scanActive = false; return;
  }

  let claims = [];
  try {
    const res = await safeMsg({ type: "DETECT_CLAIMS", text: pageText.slice(0, 30000) });
    claims = res?.claims || [];
  } catch {
    showIndicator(t('scanBackendOffline')); setTimeout(hideIndicator, 4000);
    scanActive = false; return;
  }

  if (!claims.length) {
    showIndicator(t('scanNoClaims')); setTimeout(hideIndicator, 3000);
    scanActive = false; return;
  }

  const topClaims = claims.slice(0, 8);
  stats.pending = topClaims.length;
  showSummary(); hideIndicator();

  const marks = topClaims.map(c => ({ claim: c, mark: highlightPending(c.text) }));
  for (const { claim, mark } of marks) {
    verifyAndColor(claim.text, mark);
    await sleep(500);
  }
}

async function verifyAndColor(text, mark) {
  try {
    const res = await safeMsg({ type: "VERIFY_CLAIM", text });
    const verdict = res?.verdict || "UNCERTAIN";
    stats.pending = Math.max(0, stats.pending - 1);
    stats[verdict] = (stats[verdict] || 0) + 1;
    updateSummary();
    if (mark?.isConnected) applyVerdict(mark, verdict, res?.score ?? 50);
  } catch {
    stats.pending = Math.max(0, stats.pending - 1);
    updateSummary();
  }
}

// ── Highlighting ──────────────────────────────────────────────
function highlightPending(claimText) {
  const search = claimText.slice(0, 70);
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (node.parentElement?.closest("#ts-panel,#ts-bubble,#ts-summary,#ts-scan-indicator,script,style,nav,footer")) continue;
    if (node.parentElement?.classList?.contains(HIGHLIGHT_CLASS)) continue;
    const pos = node.textContent.indexOf(search);
    if (pos === -1) continue;
    try {
      const range = document.createRange();
      range.setStart(node, pos);
      range.setEnd(node, Math.min(pos + claimText.length, node.textContent.length));
      const mark = document.createElement("mark");
      mark.className = HIGHLIGHT_CLASS;
      mark.dataset.claim = claimText.slice(0, 200);
      mark.title = t('verifyingShort');
      mark.style.cssText = "background:rgba(144,144,168,.15);border-bottom:2px solid #9090a8;cursor:pointer;border-radius:2px;padding:0 1px;transition:background .3s,border-color .3s";
      mark.addEventListener("click", (e) => { e.stopPropagation(); showPanel(mark.dataset.claim, mark.getBoundingClientRect()); });
      range.surroundContents(mark);
      return mark;
    } catch { continue; }
  }
  return null;
}

function applyVerdict(mark, verdict, score) {
  const C = { TRUE:{ bg:"rgba(34,197,94,.18)", border:"#22c55e", icon:"✅" }, FALSE:{ bg:"rgba(239,68,68,.18)", border:"#ef4444", icon:"❌" }, UNCERTAIN:{ bg:"rgba(245,158,11,.15)", border:"#f59e0b", icon:"⚠️" } };
  const c = C[verdict] || C.UNCERTAIN;
  mark.style.background = c.bg; mark.style.borderColor = c.border;
  mark.title = `${c.icon} ${verdict} · Score: ${score}/100 · ${t('clickDetails')}`;
  mark.dataset.verdict = verdict; mark.dataset.score = score;
}

// ── Summary panel ─────────────────────────────────────────────
function showSummary() {
  if (summaryPanel) summaryPanel.remove();
  summaryPanel = document.createElement("div");
  summaryPanel.id = "ts-summary";
  summaryPanel.innerHTML = `
    <div class="ts-sum-header">
      <span style="font-weight:700;font-size:13px">🔍 TruthScore Scan</span>
      <button id="ts-sum-close" style="background:none;border:none;cursor:pointer;color:#9090a8;font-size:14px;padding:2px 4px">✕</button>
    </div>
    <div id="ts-sum-body"></div>
    <div class="ts-sum-footer"><button id="ts-sum-clear">${t('clearHighlights')}</button></div>`;
  document.body.appendChild(summaryPanel);
  summaryPanel.querySelector("#ts-sum-close").addEventListener("click", clearAll);
  summaryPanel.querySelector("#ts-sum-clear").addEventListener("click", clearAll);
  updateSummary();
}

function updateSummary() {
  const body = document.getElementById("ts-sum-body");
  if (!body) return;
  const total = stats.TRUE + stats.FALSE + stats.UNCERTAIN + stats.pending;
  body.innerHTML = `
    <div class="ts-sum-row">
      <span style="color:#22c55e">✅ ${stats.TRUE}</span>
      <span style="color:#ef4444">❌ ${stats.FALSE}</span>
      <span style="color:#f59e0b">⚠️ ${stats.UNCERTAIN}</span>
    </div>
    ${stats.pending > 0
      ? `<div style="font-size:11px;color:#9090a8;margin-top:4px">⏳ ${stats.pending} ${t('scanVerifying')}</div>`
      : `<div style="font-size:11px;color:#9090a8;margin-top:4px">✅ ${t('scanDone')} · ${total} ${t('scanClaims')}</div>`}`;
}

function clearAll() {
  document.querySelectorAll("." + HIGHLIGHT_CLASS).forEach(el => {
    const p = el.parentNode;
    if (p) { p.replaceChild(document.createTextNode(el.textContent), el); p.normalize(); }
  });
  if (summaryPanel) { summaryPanel.remove(); summaryPanel = null; }
  hideIndicator();
  scanActive = false;
  stats.TRUE = stats.FALSE = stats.UNCERTAIN = stats.pending = 0;
}

// ── Scan indicator ────────────────────────────────────────────
function showIndicator(msg) {
  let el = document.getElementById("ts-scan-indicator");
  if (!el) { el = document.createElement("div"); el.id = "ts-scan-indicator"; document.body.appendChild(el); }
  el.textContent = msg;
}
function hideIndicator() { document.getElementById("ts-scan-indicator")?.remove(); }

// ── Panel ─────────────────────────────────────────────────────
function showPanel(text, anchorRect) {
  removePanel();
  const p = document.createElement("div");
  p.id = "ts-panel";
  p.innerHTML = `
    <div class="tsp-header"><span class="tsp-logo">🔍 TruthScore</span><button class="tsp-close">✕</button></div>
    <div class="tsp-claim">"${esc(text.slice(0,100))}${text.length>100?"…":""}"</div>
    <div class="tsp-loading"><div class="tsp-spinner"></div><div class="tsp-load-text">${t('analyzing')}</div></div>
    <div class="tsp-result" style="display:none"></div>`;
  document.body.appendChild(p);
  panel = p;

  const sy = window.scrollY, sx = window.scrollX;
  if (anchorRect) {
    p.style.top  = Math.max(sy+8, anchorRect.bottom+sy+10) + "px";
    p.style.left = Math.max(8, Math.min(anchorRect.left+sx, window.innerWidth+sx-360)) + "px";
  } else {
    p.style.top = (sy+70)+"px"; p.style.right = "18px";
  }
  p.querySelector(".tsp-close").addEventListener("click", removePanel);

  const msgs = [t('loadStep1'),t('loadStep2'),t('loadStep3'),t('loadStep4')];
  let mi = 0;
  const iv = setInterval(() => {
    if (!p.isConnected) { clearInterval(iv); return; }
    const el = p.querySelector(".tsp-load-text");
    if (el) el.textContent = msgs[Math.min(mi++, msgs.length-1)];
  }, 3500);
  p._iv = iv;

  safeMsg({ type: "VERIFY_CLAIM", text }).then((res) => {
    clearInterval(iv);
    if (!res || res.error === t('ctxInvalidated')) { showErr(p, t('backendHint')); return; }
    if (res.error) { showErr(p, res.error); return; }
    showResult(p, text, res);
    document.querySelectorAll("."+HIGHLIGHT_CLASS).forEach(m => {
      if (m.dataset.claim && text.startsWith(m.dataset.claim.slice(0,50)))
        applyVerdict(m, res.verdict, res.score);
    });
  });
}

function showResult(p, text, d) {
  p.querySelector(".tsp-loading").style.display = "none";
  const el = p.querySelector(".tsp-result");
  el.style.display = "block";

  // Paragraph analysis: aggregate header + one card per claim with its own sources.
  if (Array.isArray(d.results) && d.results.length) { showParagraphResult(p, text, d); return; }

  const verdict = d.verdict || "UNCERTAIN";
  const color = verdict==="TRUE"?"#22c55e":verdict==="FALSE"?"#ef4444":verdict==="MIXED"?"#8b5cf6":"#f59e0b";
  const icon  = verdict==="TRUE"?"✅":verdict==="FALSE"?"❌":verdict==="MIXED"?"🔀":"⚠️";
  const lbl   = verdict==="TRUE"?t('TRUE'):verdict==="FALSE"?t('FALSE'):verdict==="MIXED"?t('MIXEDlong'):t('UNCERTAIN');
  const sup=d.supporting||[], con=d.contradicting||[], neu=d.neutral_sources||[];
  const icons={web:"🌐",wikipedia:"📖",wikidata:"🗄️",academic:"🎓",news:"📰",factcheck:"🔎"};

  const srcGroup=(group,clr,label)=>group.length
    ?`<div class="tsp-grp" style="color:${clr}">${label} (${group.length})</div>`
      +group.slice(0,3).map(s=>`<a class="tsp-src" href="${safeUrl(s.url)}" target="_blank" rel="noopener" style="border-left:3px solid ${clr}">
        <span class="tsp-src-icon">${icons[s.type]||"📄"}</span>
        <div class="tsp-src-body">
          <div class="tsp-src-title">${esc(s.title||"")}</div>
          <div class="tsp-src-pub">${esc(s.publisher||"")}</div>
          ${s.snippet?`<div class="tsp-src-snip">${esc(s.snippet.slice(0,100))}…</div>`:""}
        </div></a>`).join(""):"";

  // Unique feedback ID for this panel
  const fbId = ++_fbCounter;
  p._fbClaim = text; p._fbVerdict = d.verdict; p._fbScore = d.score;

  el.innerHTML = `
    <div class="tsp-score-row">
      <div class="tsp-circle" style="color:${color};border-color:${color}">${d.score}</div>
      <div class="tsp-score-right">
        <div class="tsp-verdict" style="color:${color}">${icon} ${lbl}</div>
        <div class="tsp-conf">${d.confidence==="HIGH"?t('confHigh'):d.confidence==="MEDIUM"?t('confMed'):t('confLow')}</div>
        <div class="tsp-expl">${esc(d.explanation||"")}</div>
      </div>
    </div>
    <div class="tsp-bar">
      <div style="flex:${sup.length+.1};background:rgba(34,197,94,.35)"></div>
      <div style="flex:${con.length+.1};background:rgba(239,68,68,.35)"></div>
      <div style="flex:${Math.max(1,neu.length)};background:rgba(100,100,120,.25)"></div>
    </div>
    <div class="tsp-ev-label">${d.evidence_count||0} ${t('evidence')} · ${sup.length} ${t('supportN')} · ${con.length} ${t('contradictN')}</div>
    <div class="tsp-sources">
      ${srcGroup(sup,"#22c55e",t('grpSupport'))}
      ${srcGroup(con,"#ef4444",t('grpContradict'))}
      ${!sup.length&&!con.length?srcGroup(neu.slice(0,2),"#9090a8",t('grpRelevant')):""}
    </div>
    ${d.show_ads ? sponsorLine() : ""}
    <div class="tsp-feedback" id="tsfb${fbId}">
      <span class="tsp-fb-lbl">${t('verdictCorrect')}</span>
      <button class="tsp-fb-yes" data-fbid="${fbId}">${t('yes')}</button>
      <button class="tsp-fb-no"  data-fbid="${fbId}">${t('no')}</button>
    </div>`;

  // Attach feedback listeners (avoid inline onclick + closure issues)
  el.querySelector(".tsp-fb-yes").addEventListener("click", () => sendFeedback(fbId, true, p));
  el.querySelector(".tsp-fb-no").addEventListener("click",  () => sendFeedback(fbId, false, p));
}

function showParagraphResult(p, text, d) {
  const el = p.querySelector(".tsp-result");
  const verdict = d.verdict || "UNCERTAIN";
  const color = verdict==="TRUE"?"#22c55e":verdict==="FALSE"?"#ef4444":verdict==="MIXED"?"#8b5cf6":"#f59e0b";
  const icon  = verdict==="TRUE"?"✅":verdict==="FALSE"?"❌":verdict==="MIXED"?"🔀":"⚠️";
  const lbl   = verdict==="TRUE"?t('TRUE'):verdict==="FALSE"?t('FALSE'):verdict==="MIXED"?t('MIXEDlong'):t('UNCERTAIN');
  const icons={web:"🌐",wikipedia:"📖",wikidata:"🗄️",academic:"🎓",news:"📰",factcheck:"🔎"};

  const mini=(g,clr,gl,ref)=>{g=g||[];if(!g.length)return"";
    const rows=g.slice(0,2).map(s=>`<a class="tsp-src" href="${safeUrl(s.url)}" target="_blank" rel="noopener" style="border-left:3px solid ${clr}">
      <span class="tsp-src-icon">${icons[s.type]||"📄"}</span>
      <div class="tsp-src-body"><div class="tsp-src-title">${esc((s.publisher||s.title||t('sourceFallback')).slice(0,60))}</div></div>
      ${ref?`<span style="flex-shrink:0;font-family:'JetBrains Mono',monospace;font-size:8px;font-weight:700;color:${clr};border:1px solid ${clr};border-radius:3px;padding:0 3px;opacity:.85">${ref}</span>`:""}
      <span style="color:${clr};font-size:10px">${gl}</span></a>`).join("");
    return rows+(g.length>2?`<div style="font-size:9px;color:#9090a8;margin:2px 0 4px">${t('otherSources',{n:g.length-2})}</div>`:"");};

  const cards=(d.results||[]).map((r,idx)=>{
    const c =r.verdict==="TRUE"?"#22c55e":r.verdict==="FALSE"?"#ef4444":"#f59e0b";
    const ci=r.verdict==="TRUE"?"✅":r.verdict==="FALSE"?"❌":"⚠️";
    const cl=r.verdict==="TRUE"?t('TRUE'):r.verdict==="FALSE"?t('FALSE'):t('UNCERTAIN');
    const sup=r.supporting||[], con=r.contradicting||[], neu=r.neutral_sources||[];
    const srcs=(sup.length||con.length)?mini(sup,c,"✓",`#${idx+1}`)+mini(con,"#ef4444","✗",`#${idx+1}`):mini(neu,"#9090a8","•",`#${idx+1}`);
    return `<div style="border:1px solid rgba(128,128,160,.25);border-left:3px solid ${c};border-radius:8px;padding:7px;margin-top:7px;background:rgba(255,255,255,.02)">
      <div style="display:flex;justify-content:space-between;gap:8px;font-size:10px;font-weight:800;color:${c}"><span>${ci} ${cl} <span style="font-size:8px;border:1px solid ${c};border-radius:3px;padding:0 3px;opacity:.85">#${idx+1}</span></span><span>📊 ${r.score}%${r.topic?" · "+esc(TS_TOPIC_LABELS[r.topic]||r.topic):""}</span></div>
      <div style="font-size:11.5px;font-weight:600;margin-top:4px">${esc(r.claim||"")}</div>
      <div style="font-size:10px;color:#9090a8;margin-top:3px">${esc((r.explanation||"").slice(0,140))}</div>
      <div style="display:flex;align-items:center;gap:7px;margin-top:5px">
        <div style="flex:1;height:3px;border-radius:99px;background:rgba(128,128,160,.25);overflow:hidden"><div style="height:100%;width:${Math.max(3,r.score)}%;background:${c};border-radius:99px"></div></div>
        <span style="flex-shrink:0;font-family:'JetBrains Mono',monospace;font-size:9px;font-weight:700;color:${c}">${r.score}%</span>
      </div>
      <div style="font-size:9px;font-weight:700;color:#9090a8;opacity:.85;margin-top:5px">${t('claimSourcesPara',{n:idx+1})}${r.claim?` · „${esc(r.claim.slice(0,42))}${r.claim.length>42?"…":""}"`:""}</div>
      ${srcs||`<div style="font-size:10px;color:#9090a8;opacity:.6;margin-top:3px">${t('noDirectEvidence')}</div>`}
    </div>`;}).join("");

  const fbId = ++_fbCounter;
  p._fbClaim = text; p._fbVerdict = d.verdict; p._fbScore = d.score;

  el.innerHTML = `
    <div class="tsp-score-row">
      <div class="tsp-circle" style="color:${color};border-color:${color}">${d.score}</div>
      <div class="tsp-score-right">
        <div class="tsp-verdict" style="color:${color}">${icon} ${lbl}</div>
        <div class="tsp-conf">${d.confidence==="HIGH"?t('confHigh'):d.confidence==="MEDIUM"?t('confMed'):t('confLow')} · ${d.claim_count||d.results.length} ${t('claimsWord')}</div>
        <div class="tsp-expl">${esc(d.explanation||"")}</div>
      </div>
    </div>
    ${cards}
    ${d.show_ads ? sponsorLine() : ""}
    <div class="tsp-feedback" id="tsfb${fbId}">
      <span class="tsp-fb-lbl">${t('analysisCorrect')}</span>
      <button class="tsp-fb-yes" data-fbid="${fbId}">${t('yes')}</button>
      <button class="tsp-fb-no"  data-fbid="${fbId}">${t('no')}</button>
    </div>`;

  el.querySelector(".tsp-fb-yes").addEventListener("click", () => sendFeedback(fbId, true, p));
  el.querySelector(".tsp-fb-no").addEventListener("click",  () => sendFeedback(fbId, false, p));
}

function sendFeedback(fbId, correct, panelEl) {
  const el = document.getElementById("tsfb" + fbId);
  if (!el) return;
  el.innerHTML = correct
    ? `<span style='color:#22c55e;font-size:11px'>${t('fbThanks')}</span>`
    : `<span style='color:#f59e0b;font-size:11px'>${t('fbNoted')}</span>`;
  safeMsg({
    type: "SUBMIT_FEEDBACK",
    data: {
      claim: (panelEl?._fbClaim || "").slice(0, 300),
      predicted_verdict: panelEl?._fbVerdict || "UNCERTAIN",
      predicted_score: panelEl?._fbScore || 50,
      user_says_correct: correct,
      source_page: window.location.href,
    }
  });
}

function showErr(p, msg) {
  p.querySelector(".tsp-loading").style.display = "none";
  const el = p.querySelector(".tsp-result"); el.style.display = "block";
  el.innerHTML = `<div class="tsp-error">⚠️ ${esc(msg)}</div>`;
}

function removePanel() {
  if (panel) { clearInterval(panel._iv); panel.remove(); panel = null; }
}

// ── Utils ─────────────────────────────────────────────────────
function extractPageText() {
  const skip = new Set(["SCRIPT","STYLE","NAV","FOOTER","HEADER","ASIDE","NOSCRIPT","BUTTON"]);
  const texts = [];
  const walk = (node) => {
    if (skip.has(node.nodeName)) return;
    if (node.nodeType === Node.TEXT_NODE) { const t = node.textContent.trim(); if (t.length > 20) texts.push(t); }
    else { for (const child of node.childNodes) walk(child); }
  };
  walk(document.querySelector("main,article,.content,#content,.post,[role='main']") || document.body);
  return texts.join(" ");
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function esc(t){return String(t==null?"":t).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function safeUrl(u){u=String(u==null?"":u).trim();return /^(https?:|mailto:)/i.test(u)?esc(u):"#";}

} // end guard