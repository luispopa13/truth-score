// TruthScore Popup v8 — Auth + routing-aware free-tier UX
const CIRC_V = 163.4, CIRC_A = 163.4, MAX_HIST = 30;
let currentResult = null;

// ── Free-tier / anti-abuse config ─────────────────────────────
// Paste your Cloudflare Turnstile SITE key here (dash.cloudflare.com).
// Leave empty for dev — backend skips verification when its secret is unset.
const TS_TURNSTILE_SITEKEY = "";
let _tsTurnstileToken = "";
let _tsTurnstileLoaded = false;

const TOPIC_ICONS = {
  medical:"🏥",biology:"🧬",chemistry:"⚗️",physics:"⚛️",astronomy:"🌌",
  mathematics:"📐",logic:"🔣",cs_tech:"💻",engineering:"⚙️",geography:"🌍",
  history:"📜",literature:"📚",art:"🎨",sports:"⚽",economics:"💰",business:"💼",
  climate:"🌡️",politics:"🏛️",sociology:"👥",psychology:"🧠",philosophy:"💭",
  ethics:"⚖️",religion:"✝️",nutrition:"🥗",news:"📰",general:"🔍"
};
// Topic labels come from the shared i18n table (TS_TOPIC_LABELS, language-aware).
const TOPIC_LABELS = TS_TOPIC_LABELS;

// ── Auth state ─────────────────────────────────────────────────
// Token is mirrored to chrome.storage.local so the background service
// worker (a separate context) can read it for the Authorization header.
function getToken() { return localStorage.getItem('ts_token'); }
function setToken(t) {
  localStorage.setItem('ts_token', t);
  try { chrome.storage.local.set({ ts_token: t }); } catch (e) {}
}
function clearToken() { localStorage.removeItem('ts_token');
  try { chrome.storage.local.remove('ts_token'); } catch (e) {}
}

document.addEventListener("DOMContentLoaded", async () => {
  // Tab switching
  document.querySelectorAll(".tab").forEach(t =>
    t.addEventListener("click", () => switchTab(t.dataset.tab)));
  document.getElementById("openSettings").addEventListener("click", () => chrome.runtime.openOptionsPage());
  document.getElementById("openOptions").addEventListener("click", () => chrome.runtime.openOptionsPage());

  // Verify tab
  document.getElementById("verifyBtn").addEventListener("click", onVerify);
  document.getElementById("pasteBtn").addEventListener("click", onPaste);
  document.getElementById("scanBtn").addEventListener("click", onScan);
  document.getElementById("claimInput").addEventListener("keydown", e => { if (e.key==="Enter"&&e.ctrlKey) onVerify(); });

  // AI Detect tab
  document.getElementById("aiDetectBtn").addEventListener("click", onAIDetect);
  document.getElementById("aiPasteBtn").addEventListener("click", onAIPaste);

  // Feedback
  document.getElementById("fbYes").addEventListener("click", () => submitFeedback(true));
  document.getElementById("fbNo").addEventListener("click", () => submitFeedback(false));

  // History
  document.getElementById("clearHistBtn").addEventListener("click", clearHistory);

  // Auth buttons
  document.getElementById("loginBtn")?.addEventListener("click", () => switchTab("auth"));
  document.getElementById("logoutBtn")?.addEventListener("click", doLogout);
  document.getElementById("doLoginBtn")?.addEventListener("click", doLogin);
  document.getElementById("doRegisterBtn")?.addEventListener("click", doRegister);
  document.getElementById("switchToRegister")?.addEventListener("click", () => showAuthForm("register"));
  document.getElementById("switchToLogin")?.addEventListener("click", () => showAuthForm("login"));
  document.getElementById("googleLoginBtn")?.addEventListener("click", doGoogleAuth);
  document.getElementById("googleRegisterBtn")?.addEventListener("click", doGoogleAuth);

  // Upgrade and dashboard buttons
  document.getElementById("upgradeBtn")?.addEventListener("click", openUpgrade);
  document.getElementById("dashboardBtn")?.addEventListener("click", openDashboard);
  // Check pending claim from context menu
  const stored = await chrome.storage.session.get("pendingClaim").catch(() => ({}));
  if (stored.pendingClaim) {
    chrome.storage.session.remove("pendingClaim");
    document.getElementById("claimInput").value = stored.pendingClaim;
    runVerify(stored.pendingClaim);
  }

  await initAuth();
  await refreshHistBadge();
});

// ── Auth ───────────────────────────────────────────────────────
async function initAuth() {
  const t = getToken();
  if (!t) { showLoggedOut(); return; }
  try {
    const settings = await chrome.storage.sync.get("backendUrl");
    const base = settings.backendUrl || "http://localhost:8000";
    const r = await fetch(`${base}/auth/me`, {
      headers: { Authorization: `Bearer ${t}` },
      signal: AbortSignal.timeout(3000)
    });
    if (!r.ok) { clearToken(); showLoggedOut(); return; }
    const user = await r.json();
    showLoggedIn(user);
  } catch { showLoggedOut(); }
}

function showLoggedIn(user) {
  const planColor = user.plan === "pro" ? "#818cf8" : user.plan === "enterprise" ? "#f59e0b" : "#8080a8";
  const usageEl = document.getElementById("usageInfo");
  if (usageEl) {
    const bonus = user.bonus_today || 0;
    const limit = (user.daily_limit || 10) + bonus;
    usageEl.textContent = t('usageToday', {used: user.used_today||0, limit})
      + (bonus > 0 ? ` (+${bonus}🎁)` : "");
    usageEl.style.display = "block";
  }
  const userInfoEl = document.getElementById("userInfo");
  if (userInfoEl) {
    userInfoEl.innerHTML = `<span style="color:var(--text2);font-size:11px">${esc(user.email)}</span> <span style="color:${planColor};font-size:10px;font-weight:700;margin-left:4px">${esc((user.plan||"free").toUpperCase())}</span>`;
    userInfoEl.style.display = "flex";
  }
  // Show upgrade button for free users
  const upgradeEl = document.getElementById("upgradeBtn");
  if (upgradeEl) {
    upgradeEl.style.display = user.plan === "free" ? "block" : "none";
  }
  document.getElementById("loginBtn") && (document.getElementById("loginBtn").style.display = "none");
  document.getElementById("logoutBtn") && (document.getElementById("logoutBtn").style.display = "block");
}

function showLoggedOut() {
  const usageEl = document.getElementById("usageInfo");
  if (usageEl) usageEl.style.display = "none";
  const userInfoEl = document.getElementById("userInfo");
  if (userInfoEl) userInfoEl.style.display = "none";
  document.getElementById("loginBtn") && (document.getElementById("loginBtn").style.display = "block");
  document.getElementById("logoutBtn") && (document.getElementById("logoutBtn").style.display = "none");
}

function showAuthForm(type) {
  const loginForm = document.getElementById("loginForm");
  const registerForm = document.getElementById("registerForm");
  if (type === "login") {
    loginForm && (loginForm.style.display = "block");
    registerForm && (registerForm.style.display = "none");
  } else {
    loginForm && (loginForm.style.display = "none");
    registerForm && (registerForm.style.display = "block");
    ensureTurnstile();
  }
}

// ── Cloudflare Turnstile (loads lazily only on the register form) ──
function ensureTurnstile() {
  const wrap = document.getElementById("tsTurnstileWrap");
  if (!wrap) return;
  if (!TS_TURNSTILE_SITEKEY) { wrap.style.display = "none"; return; }
  wrap.style.display = "block";
  if (_tsTurnstileLoaded) return;
  _tsTurnstileLoaded = true;
  const s = document.createElement("script");
  s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
  s.async = true;
  s.onload = () => {
    try {
      window.turnstile.render("#tsTurnstileBox", {
        sitekey: TS_TURNSTILE_SITEKEY,
        callback: (tok) => { _tsTurnstileToken = tok; },
        "expired-callback": () => { _tsTurnstileToken = ""; },
      });
    } catch (e) { console.warn("Turnstile render:", e); }
  };
  document.head.appendChild(s);
}

// ── Free-tier UX: sponsor slot + quota indicator ──────────────
function applyFreeTierUX(d) {
  const slot = document.getElementById("sponsorSlot");
  if (!slot) return;
  if (d?.show_ads) {
    slot.style.display = "block";
    // Self-promo by default. When you sign direct-sold sponsorship deals,
    // replace this innerHTML with the paid creative (keep it truthful —
    // this is a fact-checking product; scammy ads kill trust).
    slot.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px">' +
      '<span>' + t('unlimitedNoBanner') + '</span>' +
      '<a href="#" id="sponsorUpgrade" style="margin-left:auto;color:#a0a0ff;font-weight:600;text-decoration:none">' + t('proArrow') + '</a>' +
      '</div>';
    document.getElementById("sponsorUpgrade")?.addEventListener("click", (e) => {
      e.preventDefault(); openUpgrade();
    });
  } else {
    slot.style.display = "none";
  }
  if (typeof d?.quota_left === "number") {
    const fi = document.getElementById("footerInfo");
    if (fi) fi.textContent = t('quotaLeft', {n: d.quota_left});
  }
}

async function doLogin() {
  const email = document.getElementById("loginEmail")?.value.trim();
  const pass = document.getElementById("loginPass")?.value;
  const errEl = document.getElementById("authErr");
  if (!email || !pass) { if(errEl) errEl.textContent = t('fillAll'); return; }
  try {
    const settings = await chrome.storage.sync.get("backendUrl");
    const base = settings.backendUrl || "http://localhost:8000";
    const r = await fetch(`${base}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password: pass })
    });
    const d = await r.json();
    if (!r.ok) { if(errEl) errEl.textContent = d.detail || t('genericError'); return; }
    setToken(d.token);
    if(errEl) errEl.textContent = "";
    await initAuth();
    switchTab("verify");
  } catch { if(errEl) errEl.textContent = t('backendOffline'); }
}

async function doRegister() {
  const name = document.getElementById("regName")?.value.trim();
  const email = document.getElementById("regEmail")?.value.trim();
  const pass = document.getElementById("regPass")?.value;
  const errEl = document.getElementById("authErr");
  if (!email || !pass) { if(errEl) errEl.textContent = t('fillAll'); return; }
  try {
    const settings = await chrome.storage.sync.get("backendUrl");
    const base = settings.backendUrl || "http://localhost:8000";
    const r = await fetch(`${base}/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password: pass, name,
                              turnstile_token: _tsTurnstileToken })
    });
    const d = await r.json();
    if (!r.ok) { if(errEl) errEl.textContent = d.detail || t('genericError'); return; }
    setToken(d.token);
    if(errEl) errEl.textContent = "";
    await initAuth();
    switchTab("verify");
  } catch { if(errEl) errEl.textContent = t('backendOffline'); }
}

async function openUpgrade() {
  const { backendUrl } = await chrome.storage.sync.get("backendUrl");
  const base = backendUrl || "http://localhost:8000";
  chrome.tabs.create({ url: `${base}/?pricing=1` });
}

async function openDashboard() {
  const { backendUrl } = await chrome.storage.sync.get("backendUrl");
  const base = backendUrl || "http://localhost:8000";
  chrome.tabs.create({ url: `${base}/` });
}

async function doGoogleAuth() {
  const errEl = document.getElementById("authErr");
  if (errEl) { errEl.textContent = t('connectingGoogle'); errEl.classList.add("show"); }
  try {
    // Get Google OAuth token via Chrome identity API
    const token = await new Promise((resolve, reject) => {
      chrome.identity.getAuthToken({ interactive: true }, (token) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
        } else if (!token) {
          reject(new Error(t('googleTokenMissing')));
        } else {
          resolve(token);
        }
      });
    });

    const settings = await chrome.storage.sync.get("backendUrl");
    const base = settings.backendUrl || "http://localhost:8000";

    const r = await fetch(`${base}/auth/google`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token })
    });
    const d = await r.json();
    if (!r.ok) {
      if (errEl) { errEl.textContent = "⚠️ " + (d.detail || t('googleAuthError')); errEl.classList.add("show"); }
      return;
    }
    setToken(d.token);
    if (errEl) { errEl.textContent = ""; errEl.classList.remove("show"); }
    await initAuth();
    switchTab("verify");
  } catch(e) {
    const msg = e.message || t('googleAuthError');
    if (errEl) { errEl.textContent = "⚠️ " + msg; errEl.classList.add("show"); }
    console.error("[Google Auth]", e);
  }
}

function doLogout() {
  // Also revoke Google token if it exists
  chrome.identity.getAuthToken({ interactive: false }, (token) => {
    if (token) chrome.identity.removeCachedAuthToken({ token });
  });
  clearToken();
  showLoggedOut();
  switchTab("verify");
}

// ── Tab switching ──────────────────────────────────────────────
function switchTab(name) {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab===name));
  document.querySelectorAll(".tab-content").forEach(c => c.classList.toggle("active", c.id===`tab-${name}`));
  if (name==="history") loadHistory();
  if (name==="auth") showAuthForm("login");
}

// ── VERIFY ────────────────────────────────────────────────────
function onVerify() {
  const text = document.getElementById("claimInput").value.trim();
  if (!text) { showErr("errBox",t('enterClaim')); return; }
  runVerify(text);
}

async function onPaste() {
  try {
    const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
    const res = await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>window.getSelection()?.toString()||""});
    const sel = res?.[0]?.result||"";
    if (sel?.length>3) document.getElementById("claimInput").value = sel.slice(0,4000);
    else showErr("errBox",t('selectFirst'));
  } catch { showErr("errBox",t('noPageAccess')); }
}

async function onScan() {
  const btn = document.getElementById("scanBtn");
  btn.textContent="⏳"; btn.disabled=true;
  try {
    const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
    await chrome.scripting.executeScript({target:{tabId:tab.id},files:["i18n.js","content.js"]}).catch(()=>{});
    await chrome.scripting.insertCSS({target:{tabId:tab.id},files:["content.css"]}).catch(()=>{});
    await chrome.tabs.sendMessage(tab.id,{type:"SCAN_PAGE"});
    window.close();
  } catch(e) {
    showErr("errBox",e.message);
    btn.textContent=t('scanPage'); btn.disabled=false;
  }
}

async function runVerify(text) {
  hideErr("errBox");
  setVerifyView("loading");
  currentResult = null;
  const steps = [
    t('stepClassify'),
    t('stepSearch'),
    t('stepCompare'),
    t('stepScore'),
  ];
  let si=0;
  const iv = setInterval(()=>{
    document.getElementById("vLoadText").textContent = steps[Math.min(si++,steps.length-1)];
  },4000);
  try {
    const res = await chrome.runtime.sendMessage({type:"VERIFY_CLAIM",text});
    clearInterval(iv);
    if (res?.error) throw new Error(res.error);
    currentResult = {claim:text,...res};
    renderVerify(res);
    applyFreeTierUX(res);
    await saveHistory(text,res);
    await refreshHistBadge();
  } catch(err) {
    clearInterval(iv);
    showErr("errBox",err.message||t('unknownError'));
    setVerifyView("empty");
  }
}


function renderParagraph(d) {
  // Reuse the proven single-result renderer for the aggregate header.
  const summary = {
    ...d,
    results: undefined,
    topic: "general",
    supporting: [],
    contradicting: [],
    neutral_sources: [],
    evidence_count: (d.results||[]).reduce((n,r)=>n+(r.evidence_count||0),0),
    explanation: d.explanation || t('paraSplit'),
    models_used: [],
  };
  renderVerify(summary);

  const palette = {
    TRUE:["#22c55e","✅ "+t('TRUE')], FALSE:["#ef4444","❌ "+t('FALSE')],
    UNCERTAIN:["#f59e0b","⚠️ "+t('UNCERTAIN')], MIXED:["#8b5cf6","🔀 "+t('MIXED')]
  };
  const DICS={web:"🌐",wikipedia:"📖",wikidata:"🗄️",academic:"🎓",news:"📰",factcheck:"🔎"};
  const msrc=(g,clr,gl,lb,ref)=>{g=g||[];if(!g.length)return"";
    const rows=g.slice(0,2).map(s=>`<a href="${safeUrl(s.url)}" target="_blank" rel="noopener" style="display:flex;gap:5px;align-items:center;font-size:10px;color:var(--text2);text-decoration:none;margin-top:3px;min-width:0"><span style="color:${clr};flex-shrink:0">${gl}</span>${ref?`<span style="flex-shrink:0;font-family:'JetBrains Mono',monospace;font-size:8px;font-weight:700;color:${clr};border:1px solid ${clr};border-radius:3px;padding:0 3px;opacity:.85">${ref}</span>`:""}<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${DICS[s.type]||"📄"} ${esc((s.publisher||s.title||t('sourceFallback')).slice(0,70))}</span></a>`).join("");
    return `<div style="margin-top:6px;padding-top:5px;border-top:1px dashed var(--border)"><span style="font-size:9px;font-weight:800;color:${clr};text-transform:uppercase;letter-spacing:.4px">${lb} (${g.length})</span>${rows}${g.length>2?`<div style="font-size:9px;color:var(--text2);opacity:.7;margin-top:2px">${t('otherSources',{n:g.length-2})}</div>`:""}</div>`;};
  document.getElementById("sourcesWrap").innerHTML = `
    <div class="sec-label">${t('perClaimResult',{n:d.claim_count||d.results.length})}</div>
    ${(d.results||[]).map((r,i)=>{
      const p=palette[r.verdict]||palette.UNCERTAIN;
      const sup=r.supporting||[], con=r.contradicting||[], neu=r.neutral_sources||[];
      const srcBlock=(sup.length||con.length)
        ? msrc(sup,"#22c55e","✓",t('supportN'),`#${i+1}`)+msrc(con,"#ef4444","✗",t('contradictN'),`#${i+1}`)
        : msrc(neu,"#9090a8","•",t('relevantN'),`#${i+1}`);
      return `<button class="paragraph-claim" data-claim-index="${i}" style="display:block;width:100%;text-align:left;background:var(--bg3);border:1px solid var(--border);border-left:3px solid ${p[0]};border-radius:8px;padding:9px;margin:6px 0;color:var(--text);cursor:pointer">
        <div style="display:flex;justify-content:space-between;gap:8px;font-size:10px;color:${p[0]};font-weight:700"><span>${p[1]}</span><span>${esc(TOPIC_LABELS[r.topic]||r.topic||"General")} · ${r.score}%</span></div>
        <div style="font-size:12px;font-weight:600;margin-top:5px">${esc(r.claim||"")}</div>
        <div style="font-size:10px;color:var(--text2);margin-top:4px">${esc((r.explanation||"").slice(0,160))}</div>
        <div style="display:flex;align-items:center;gap:8px;margin-top:6px">
          <div style="flex:1;height:4px;border-radius:99px;background:rgba(128,128,160,.22);overflow:hidden"><div style="height:100%;width:${Math.max(3,r.score)}%;background:${p[0]};border-radius:99px"></div></div>
          <span style="font-family:'JetBrains Mono',monospace;font-size:9px;font-weight:700;color:${p[0]}">${r.score}%</span>
        </div>
        <div style="margin-top:6px;font-size:9px;font-weight:700;color:var(--text2);opacity:.8">${t('claimSources',{n:i+1})} <span style="font-weight:500;opacity:.7">· „${esc((r.claim||"").slice(0,40))}${(r.claim||"").length>40?"…":""}"</span></div>
        ${srcBlock||`<div style="font-size:10px;color:var(--text2);opacity:.65;margin-top:4px">${t('noDirectEvidence')}</div>`}
      </button>`;
    }).join("")}
    <div style="font-size:10px;color:var(--text2);opacity:.6;margin:8px 0 2px">${t('tapClaim')}</div>`;
  document.querySelectorAll(".paragraph-claim").forEach(btn=>btn.addEventListener("click",()=>{
    const r=d.results[Number(btn.dataset.claimIndex)];
    currentResult=r;
    renderVerify(r);
  }));
}

function renderVerify(d) {
  setVerifyView("result");
  const score=d.score??50, verdict=d.verdict||"UNCERTAIN";
  if (Array.isArray(d.results)) {
    renderParagraph(d);
    return;
  }
  const color = verdict==="MIXED"?"#8b5cf6":
                verdict==="TRUE"?"#22c55e":verdict==="FALSE"?"#ef4444":"#f59e0b";
  const icon = verdict==="TRUE"?"✅":verdict==="FALSE"?"❌":verdict==="MIXED"?"🔀":"⚠️";
  const lbl  = verdict==="TRUE"?t('TRUE'):verdict==="FALSE"?t('FALSE'):verdict==="MIXED"?t('MIXEDlong'):t('UNCERTAIN');
  const topic=d.topic||"general";

  setTimeout(()=>{
    const fill=document.getElementById("ringFill");
    fill.style.stroke=color;
    fill.style.strokeDashoffset=CIRC_V-(score/100)*CIRC_V;
  },50);
  const sn=document.getElementById("scoreNum");
  sn.textContent=score; sn.style.color=color;

  const vb=document.getElementById("vbadge");
  vb.textContent=`${icon} ${lbl}`; vb.className=`vbadge v-${verdict}`;

  document.getElementById("confText").innerHTML =
    `<span style="opacity:.8">${TOPIC_ICONS[topic]||"🔍"} ${TOPIC_LABELS[topic]||topic}</span>&nbsp;·&nbsp;` +
    (d.confidence==="HIGH"?t('confHigh'):d.confidence==="MEDIUM"?t('confMed'):t('confLow')) +
    (d.calibrated_confidence?` · ${d.calibrated_confidence}`:"");
  document.getElementById("explText").textContent = d.explanation||"";

  const sup=(d.supporting||[]).length, con=(d.contradicting||[]).length;
  document.getElementById("evBar").innerHTML=`
    <div style="flex:${sup+.1};background:rgba(34,197,94,.4)"></div>
    <div style="flex:${con+.1};background:rgba(239,68,68,.4)"></div>
    <div style="flex:${Math.max(1,(d.neutral_sources||[]).length)};background:rgba(100,100,120,.25)"></div>`;
  document.getElementById("evLabel").textContent=
    `${d.evidence_count||0} ${t('evidence')} · ${sup} ${t('supportN')} · ${con} ${t('contradictN')}`;

  const SRC_ICONS={web:"🌐",wikipedia:"📖",wikidata:"🗄️",academic:"🎓",news:"📰",factcheck:"🔎"};
  const srcHtml=(group,cls,label)=>group.length
    ?`<div class="src-grp-label" style="color:${cls==="tag-s"?"var(--green)":cls==="tag-c"?"var(--red)":"var(--text2)"}">${label} (${group.length})</div>`
      +group.slice(0,6).map(s=>`<a class="src" href="${safeUrl(s.url)}" target="_blank" rel="noopener">
        <span>${SRC_ICONS[s.type]||"📄"}</span>
        <div class="src-body">
          <div class="src-title">${esc(s.title||"")}</div>
          <div class="src-pub">${esc(s.publisher||"")}</div>
          ${s.snippet?`<div class="src-snip">${esc(s.snippet.slice(0,100))}</div>`:""}
        </div>
        <span class="src-tag ${cls}">${cls==="tag-s"?"✓":cls==="tag-c"?"✗":"—"}</span>
      </a>`).join(""):"";

  const neutralFallback = (!(d.supporting||[]).length && !(d.contradicting||[]).length)
    ? (d.neutral_sources||[]).slice(0,4) : [];
  document.getElementById("sourcesWrap").innerHTML=`
    <div class="sec-label">${t('sourcesTitle')}</div>
    <div class="src-list">
      ${srcHtml(d.supporting||[],"tag-s",t('grpSupport'))}
      ${srcHtml(d.contradicting||[],"tag-c",t('grpContradict'))}
      ${neutralFallback.length?srcHtml(neutralFallback,"tag-n",t('grpRelevant')):""}
    </div>
    ${(d.sub_claims||[]).length>1?`
    <div style="margin-top:10px;font-size:10px;font-weight:700;color:var(--text2);text-transform:uppercase;letter-spacing:.5px">${t('subClaims')}</div>
    ${(window.tsSubClaims=(d.sub_claims||[])).map((c,i)=>`<div style="font-size:11px;padding:5px 8px;background:var(--bg3);border-radius:6px;margin-top:4px;cursor:pointer;border:1px solid var(--border)"
      onclick="tsSetClaim(${i})"
    >▸ ${esc(c)}</div>`).join("")}`:""}`;

  document.getElementById("fbYes").textContent=t('yes');
  document.getElementById("fbNo").textContent=t('no');
  document.getElementById("fbYes").disabled=false;
  document.getElementById("fbNo").disabled=false;
}

function setVerifyView(view) {
  document.getElementById("vEmpty").style.display=view==="empty"?"block":"none";
  document.getElementById("vResult").className=view==="result"?"result show":"result";
  document.getElementById("vLoading").className=view==="loading"?"loading show":"loading";
}

function submitFeedback(correct) {
  if (!currentResult) return;
  document.getElementById("fbYes").textContent=correct?"✅":t('yes');
  document.getElementById("fbNo").textContent=correct?t('no'):"❌";
  document.getElementById("fbYes").disabled=true;
  document.getElementById("fbNo").disabled=true;
  chrome.runtime.sendMessage({type:"SUBMIT_FEEDBACK",data:{
    claim:(currentResult.claim||"").slice(0,300),
    predicted_verdict:currentResult.verdict||"UNCERTAIN",
    predicted_score:currentResult.score||50,
    user_says_correct:correct,source_page:"",
  }}, (res)=>{
    // Surface the gamified bonus and refresh the usage counter so the extra
    // check shows up immediately (parity with the dashboard).
    if (res && res.bonus && res.bonus.granted) {
      const fi = document.getElementById("footerInfo");
      if (fi) fi.textContent = t('bonusEarned') || "+1 bonus check! 🎁";
      initAuth();
    }
  });
}

// ── AI DETECT ─────────────────────────────────────────────────
function onAIDetect() {
  const text=document.getElementById("aiInput").value.trim();
  if (!text||text.length<20) { showErr("aiErrBox",t('min20')); return; }
  runAIDetect(text);
}

async function onAIPaste() {
  try {
    const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
    const res=await chrome.scripting.executeScript({target:{tabId:tab.id},
      func:()=>window.getSelection()?.toString()||document.body.innerText.slice(0,2000)||""});
    const text=res?.[0]?.result||"";
    if (text.length>10) document.getElementById("aiInput").value=text.slice(0,2000);
    else showErr("aiErrBox",t('noText'));
  } catch { showErr("aiErrBox",t('noPageAccess')); }
}

async function runAIDetect(text) {
  hideErr("aiErrBox"); setAIView("loading");
  try {
    const res=await chrome.runtime.sendMessage({type:"DETECT_AI_CONTENT",text});
    if (res?.error) throw new Error(res.error);
    renderAIResult(res);
  } catch(err) {
    showErr("aiErrBox",err.message||t('genericError')); setAIView("empty");
  }
}

function renderAIResult(d) {
  setAIView("result");
  const aiPct=Math.round((d.ai_probability||0)*100);
  const color=aiPct>=70?"#ef4444":aiPct>=50?"#f59e0b":"#22c55e";
  setTimeout(()=>{
    const fill=document.getElementById("aiRingFill");
    fill.style.stroke=color;
    fill.style.strokeDashoffset=CIRC_A-(aiPct/100)*CIRC_A;
  },50);
  document.getElementById("aiNum").textContent=aiPct+"%";
  document.getElementById("aiNum").style.color=color;
  const rb=document.getElementById("aiRisk");
  rb.textContent={HIGH:t('riskHigh'),MEDIUM:t('riskMed'),LOW:t('riskLow')}[d.risk_level]||d.risk_level;
  rb.className=`risk-badge risk-${d.risk_level||"LOW"}`;
  document.getElementById("aiVerdict").textContent=
    d.verdict==="LIKELY_AI"?t('likelyAI'):d.verdict==="LIKELY_HUMAN"?t('likelyHuman'):t('aiUncertain');
  document.getElementById("aiVerdict").style.color=color;
  document.getElementById("aiInterp").textContent=d.interpretation||"";
  document.getElementById("aiBar").style.width=aiPct+"%";
  document.getElementById("humanBar").style.width=(100-aiPct)+"%";
  document.getElementById("aiPct").textContent=aiPct+"%";
  document.getElementById("humanPct").textContent=(100-aiPct)+"%";
  document.getElementById("aiFeatures").innerHTML=`
    <div class="ai-feat"><div class="ai-feat-val">${d.avg_sentence_length||"—"}</div><div class="ai-feat-lbl">${t('wordsPerSent')}</div></div>
    <div class="ai-feat"><div class="ai-feat-val">${d.vocabulary_richness?Math.round(d.vocabulary_richness*100)+"%":"—"}</div><div class="ai-feat-lbl">${t('vocab')}</div></div>
    <div class="ai-feat"><div class="ai-feat-val">${d.num_sentences||"—"}</div><div class="ai-feat-lbl">${t('sentences')}</div></div>`;
  document.getElementById("aiModel").textContent=d.model?`🤖 ${d.model}`:"";
}

function setAIView(view) {
  document.getElementById("aiEmpty").style.display=view==="empty"?"block":"none";
  document.getElementById("aiResult").className=view==="result"?"ai-result show":"ai-result";
  document.getElementById("aiLoading").className=view==="loading"?"loading show":"loading";
}

// ── HISTORY ───────────────────────────────────────────────────
async function saveHistory(claim,result) {
  try {
    const {ts_history=[]}=await chrome.storage.local.get("ts_history");
    const entry={id:Date.now(),claim:claim.slice(0,200),
      verdict:result.verdict||"UNCERTAIN",score:result.score??50,
      topic:result.topic||"general",
      confidence:result.confidence||"LOW",evidence_count:result.evidence_count||0,
      timestamp:new Date().toISOString()};
    await chrome.storage.local.set({ts_history:[entry,...ts_history].slice(0,MAX_HIST)});
  } catch(e){console.warn("History:",e);}
}

async function loadHistory() {
  const {ts_history=[]}=await chrome.storage.local.get("ts_history").catch(()=>({}));
  document.getElementById("histTitle").textContent=t('histTitleN',{n:ts_history.length});
  if (!ts_history.length) {
    document.getElementById("histEmpty").style.display="block";
    document.getElementById("histList").innerHTML=""; return;
  }
  document.getElementById("histEmpty").style.display="none";
  document.getElementById("histList").innerHTML=ts_history.map(item=>{
    const color=item.verdict==="TRUE"?"#22c55e":item.verdict==="FALSE"?"#ef4444":item.verdict==="MIXED"?"#8b5cf6":"#f59e0b";
    const icon=item.verdict==="TRUE"?"✅":item.verdict==="FALSE"?"❌":item.verdict==="MIXED"?"🔀":"⚠️";
    const lbl=item.verdict==="TRUE"?t('TRUE'):item.verdict==="FALSE"?t('FALSE'):item.verdict==="MIXED"?t('MIXED'):t('UNCERTAIN');
    const tIcon=TOPIC_ICONS[item.topic||"general"]||"🔍";
    return `<div class="hist-item" data-claim="${esc(item.claim)}">
      <div class="hist-score" style="color:${color};border-color:${color}">${item.score}</div>
      <div class="hist-body">
        <div class="hist-claim">${esc(item.claim)}</div>
        <div class="hist-meta">
          <span class="vbadge v-${item.verdict}" style="font-size:9px;padding:1px 6px">${icon} ${lbl}</span>
          <span>${tIcon}</span><span>${formatTime(item.timestamp)}</span>
        </div>
      </div>
      <button class="hist-rerun" title="${t('reverify')}">↻</button>
    </div>`;
  }).join("");
  document.querySelectorAll(".hist-item").forEach(el=>{
    el.querySelector(".hist-rerun").addEventListener("click",e=>{
      e.stopPropagation();
      document.getElementById("claimInput").value=el.dataset.claim;
      switchTab("verify"); runVerify(el.dataset.claim);
    });
    el.addEventListener("click",()=>{
      document.getElementById("claimInput").value=el.dataset.claim;
      switchTab("verify");
    });
  });
}

async function clearHistory() {
  if (!confirm(t('confirmClear'))) return;
  await chrome.storage.local.set({ts_history:[]});
  await refreshHistBadge(); loadHistory();
}

async function refreshHistBadge() {
  try {
    const {ts_history=[]}=await chrome.storage.local.get("ts_history");
    const badge=document.getElementById("histBadge");
    badge.textContent=ts_history.length;
    badge.style.display=ts_history.length>0?"inline-block":"none";
  } catch {}
}

function formatTime(iso) {
  try {
    const diff=Math.floor((Date.now()-new Date(iso))/1000);
    if (diff<60) return t('now');
    if (diff<3600) return `${Math.floor(diff/60)}min`;
    if (diff<86400) return `${Math.floor(diff/3600)}h`;
    return new Date(iso).toLocaleDateString(TS_LANG==='ro'?"ro-RO":"en-US",{day:"numeric",month:"short"});
  } catch { return ""; }
}

function showErr(id,msg){const el=document.getElementById(id);el.textContent="⚠️ "+msg;el.classList.add("show");}
function hideErr(id){document.getElementById(id).classList.remove("show");}
function esc(t){return String(t==null?"":t).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function safeUrl(u){u=String(u==null?"":u).trim();return /^(https?:|mailto:)/i.test(u)?esc(u):"#";}
// Index-based dispatch for the sub-claim shortcuts — never interpolate claim
// text into an inline handler (HTML→JS double-decode makes that injectable).
window.tsSubClaims=window.tsSubClaims||[];
window.tsSetClaim=function(i){const c=(window.tsSubClaims||[])[i];if(c!=null){const el=document.getElementById('claimInput');if(el)el.value=c;}};