// TruthScore Content Script v3
let bubble = null, panel = null, hideTimer = null;

document.addEventListener("mouseup", (e) => {
  setTimeout(() => {
    const sel  = window.getSelection();
    const text = sel ? sel.toString().trim() : "";
    if (text && text.length >= 8 && text.length <= 800) {
      showBubble(text, sel);
    } else {
      hideBubble();
    }
  }, 120);
});

document.addEventListener("mousedown", (e) => {
  if (bubble && !bubble.contains(e.target)) hideBubble();
  if (panel && !panel.contains(e.target) && bubble && !bubble.contains(e.target)) removePanel();
});

function showBubble(text, sel) {
  hideBubble();
  if (!sel.rangeCount) return;
  const rect = sel.getRangeAt(0).getBoundingClientRect();
  const b = document.createElement("div");
  b.id = "ts-bubble";
  b.innerHTML = `<span style="font-size:13px">🔍</span><span>TruthScore</span>`;
  document.body.appendChild(b);
  bubble = b;
  const scrollX = window.scrollX, scrollY = window.scrollY;
  b.style.left = Math.max(8, rect.left + scrollX + rect.width/2 - 60) + "px";
  b.style.top  = (rect.top + scrollY - 44) + "px";
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    hideBubble();
    showPanel(text, rect);
  });
  clearTimeout(hideTimer);
  hideTimer = setTimeout(hideBubble, 7000);
}

function hideBubble() {
  if (bubble) { bubble.remove(); bubble = null; }
  clearTimeout(hideTimer);
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "SHOW_INLINE_CHECK") { hideBubble(); showPanel(msg.text, null); }
});

function showPanel(text, anchorRect) {
  removePanel();
  const p = document.createElement("div");
  p.id = "ts-panel";
  p.innerHTML = `
    <div class="tsp-header">
      <span class="tsp-logo">🔍 TruthScore</span>
      <button class="tsp-close">✕</button>
    </div>
    <div class="tsp-claim">"${esc(text.slice(0,100))}${text.length>100?"…":""}"</div>
    <div class="tsp-loading">
      <div class="tsp-spinner"></div>
      <div class="tsp-load-text">Se caută dovezi în 12+ surse academice...</div>
    </div>
    <div class="tsp-result" style="display:none"></div>`;
  document.body.appendChild(p);
  panel = p;

  const scrollY = window.scrollY, scrollX = window.scrollX;
  if (anchorRect) {
    p.style.top  = Math.max(scrollY+8, anchorRect.bottom+scrollY+10) + "px";
    p.style.left = Math.max(8, Math.min(anchorRect.left+scrollX, window.innerWidth+scrollX-360)) + "px";
  } else {
    p.style.top = (scrollY+70) + "px"; p.style.right = "18px";
  }

  p.querySelector(".tsp-close").addEventListener("click", removePanel);

  const msgs = [
    "Se caută în PubMed, arXiv, Semantic Scholar...",
    "Se interogă CORE, EuropePMC, CrossRef...",
    "Se verifică surse oficiale (WHO, EU, CDC)...",
    "Se rulează NLI pe perechile (dovadă, afirmație)...",
    "Se calculează TruthScore final...",
  ];
  let mi = 0;
  const iv = setInterval(() => {
    if (!p.isConnected) { clearInterval(iv); return; }
    const el = p.querySelector(".tsp-load-text");
    if (el) el.textContent = msgs[Math.min(mi++, msgs.length-1)];
  }, 3500);
  p._iv = iv;

  chrome.runtime.sendMessage({ type: "VERIFY_CLAIM", text }, (res) => {
    clearInterval(iv);
    if (chrome.runtime.lastError || !res) {
      showErr(p, "Backend offline. Pornește: uvicorn main:app --reload"); return;
    }
    if (res.error) { showErr(p, res.error); return; }
    showResult(p, res);
  });
}

function showResult(p, d) {
  p.querySelector(".tsp-loading").style.display = "none";
  const el = p.querySelector(".tsp-result");
  el.style.display = "block";
  const color = d.verdict==="TRUE"?"#22c55e":d.verdict==="FALSE"?"#ef4444":"#f59e0b";
  const icon  = d.verdict==="TRUE"?"✅":d.verdict==="FALSE"?"❌":"⚠️";
  const lbl   = d.verdict==="TRUE"?"ADEVĂRAT":d.verdict==="FALSE"?"FALS":"INCERT";
  const sup = d.supporting||[], con = d.contradicting||[], neu = d.neutral_sources||[];

  const srcHtml = (group, color, label) => group.length
    ? `<div class="tsp-grp" style="color:${color}">${label} (${group.length})</div>`
      + group.slice(0,3).map(s => {
          const icons={web:"🌐",wikipedia:"📖",wikidata:"🗄️",academic:"🎓",news:"📰",factcheck:"🔎"};
          return `<a class="tsp-src" href="${esc(s.url||"#")}" target="_blank" rel="noopener"
            style="border-left:3px solid ${color}">
            <span class="tsp-src-icon">${icons[s.type]||"📄"}</span>
            <div class="tsp-src-body">
              <div class="tsp-src-title">${esc(s.title||"")}</div>
              <div class="tsp-src-pub">${esc(s.publisher||"")}</div>
              ${s.snippet?`<div class="tsp-src-snip">${esc(s.snippet.slice(0,110))}…</div>`:""}
            </div></a>`;
        }).join("") : "";

  el.innerHTML = `
    <div class="tsp-score-row">
      <div class="tsp-circle" style="color:${color};border-color:${color}">${d.score}</div>
      <div class="tsp-score-right">
        <div class="tsp-verdict" style="color:${color}">${icon} ${lbl}</div>
        <div class="tsp-conf">${d.confidence==="HIGH"?"🟢 Ridicată":d.confidence==="MEDIUM"?"🟡 Medie":"🔴 Scăzută"}</div>
        <div class="tsp-expl">${esc(d.explanation||"")}</div>
      </div>
    </div>
    <div class="tsp-bar">
      <div style="flex:${sup.length+0.1};background:rgba(34,197,94,.35)"></div>
      <div style="flex:${con.length+0.1};background:rgba(239,68,68,.35)"></div>
      <div style="flex:${Math.max(1,neu.length)};background:rgba(100,100,120,.25)"></div>
    </div>
    <div class="tsp-ev-label">${d.evidence_count||0} dovezi · ${sup.length} susțin · ${con.length} contrazic</div>
    <div class="tsp-sources">
      ${srcHtml(sup,"#22c55e","✅ Susțin")}
      ${srcHtml(con,"#ef4444","❌ Contrazic")}
      ${!sup.length&&!con.length?srcHtml(neu.slice(0,2),"#9090a8","📄 Relevante"):""}
    </div>
    <div class="tsp-model">🤖 ${esc((d.models_used||[]).join(" + ")||"—")}</div>`;
}

function showErr(p, msg) {
  p.querySelector(".tsp-loading").style.display="none";
  const el=p.querySelector(".tsp-result"); el.style.display="block";
  el.innerHTML=`<div class="tsp-error">⚠️ ${esc(msg)}</div>`;
}

function removePanel() {
  if (panel) { clearInterval(panel._iv); panel.remove(); panel=null; }
}

function esc(t){const d=document.createElement("div");d.appendChild(document.createTextNode(String(t||"")));return d.innerHTML;}