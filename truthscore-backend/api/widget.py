"""
TruthScore — Embeddable widget JavaScript endpoint.
"""
from config import *

async def widget_script(user_key: str = ""):
    """Serve embeddable widget JavaScript."""
    from fastapi.responses import Response as FResponse
    api_url = os.getenv("WIDGET_API_URL", os.getenv("PUBLIC_BASE_URL", "")).rstrip("/")
    if not api_url:
        # Widget can't function without knowing the API URL. Return a stub that
        # logs the error in the browser console so operators notice immediately.
        stub = "console.error('[TruthScore widget] WIDGET_API_URL not configured on server — widget disabled.');"
        return FResponse(content=stub, media_type="application/javascript",
                         headers={"Access-Control-Allow-Origin": "*"})
    js = f"""
(function() {{
  'use strict';
  var API = '{api_url}';
  var KEY = '{user_key}';

  function injectStyles() {{
    if (document.getElementById('ts-widget-css')) return;
    var s = document.createElement('style');
    s.id = 'ts-widget-css';
    s.textContent = [
      ".ts-widget {{ font-family: system-ui, sans-serif; max-width: 480px; border: 1px solid #2a2a4a;",
      "border-radius: 12px; background: #0d0d1a; color: #e2e2f0; padding: 16px; margin: 8px 0; }}",
      ".ts-widget-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }}",
      ".ts-widget-logo {{ font-size: 16px; }}",
      ".ts-widget-title {{ font-weight: 700; font-size: 14px; }}",
      ".ts-widget-input {{ width: 100%; padding: 10px; background: #12121f; border: 1px solid #2a2a4a;",
      "border-radius: 8px; color: #e2e2f0; font-size: 13px; font-family: inherit; resize: none; outline: none; }}",
      ".ts-widget-btn {{ margin-top: 8px; padding: 8px 16px; background: #5b5bff; color: white;",
      "border: none; border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 600; width: 100%; }}",
      ".ts-widget-btn:hover {{ background: #7c74ff; }}",
      ".ts-widget-result {{ margin-top: 12px; padding: 12px; background: #12121f;",
      "border-radius: 8px; font-size: 13px; display: none; }}",
      ".ts-widget-result.show {{ display: block; }}",
      ".ts-widget-score {{ font-size: 20px; font-weight: 800; }}",
      ".ts-widget-verdict {{ font-weight: 700; margin-left: 6px; }}",
      ".ts-widget-expl {{ font-size: 12px; color: #8080a8; margin-top: 6px; line-height: 1.4; }}",
      ".ts-widget-powered {{ font-size: 10px; color: #5050a0; margin-top: 8px; text-align: right; }}"
    ].join("");
    document.head.appendChild(s);
  }}

  function createWidget(container, options) {{
    options = options || {{}};
    injectStyles();
    container.innerHTML = (
      '<div class="ts-widget">' +
      '<div class="ts-widget-header">' +
      '<span class="ts-widget-logo">[TS]</span>' +
      '<span class="ts-widget-title">TruthScore Fact Checker</span>' +
      '</div>' +
      '<textarea class="ts-widget-input" rows="3"' +
      'placeholder="Introdu o afirmație pentru verificare..."></textarea>' +
      '<button class="ts-widget-btn">Verifică</button>' +
      '<div class="ts-widget-result"></div>' +
      '<div class="ts-widget-powered">' +
      '<a href="{api_url}" target="_blank" style="color:#5050a0;text-decoration:none">' +
      'Powered by TruthScore' +
      '</a>' +
      '</div>' +
      '</div>'
    );

    var textarea = container.querySelector('.ts-widget-input');
    var btn      = container.querySelector('.ts-widget-btn');
    var result   = container.querySelector('.ts-widget-result');

    btn.addEventListener('click', function() {{
      var text = textarea.value.trim();
      if (!text) return;
      btn.textContent = '[loading] Se verifică...';
      btn.disabled = true;
      result.className = 'ts-widget-result';

      fetch(API + '/verify', {{
        method: 'POST',
        headers: {{
          'Content-Type': 'application/json',
          'Authorization': 'Bearer ' + KEY
        }},
        body: JSON.stringify({{text: text}})
      }})
      .then(function(r) {{ return r.json(); }})
      .then(function(d) {{
        var color = d.verdict === 'TRUE' ? '#22d47a' : d.verdict === 'FALSE' ? '#ff4d6d' : '#f0b429';
        var lbl   = d.verdict === 'TRUE' ? 'ADEVĂRAT' : d.verdict === 'FALSE' ? 'FALS' : 'INCERT';
        result.innerHTML =
          '<span class="ts-widget-score" style="color:' + color + '">' + (d.score||50) + '</span>' +
          '<span class="ts-widget-verdict" style="color:' + color + '">' + lbl + '</span>' +
          '<div class="ts-widget-expl">' + (d.explanation||'').slice(0,200) + '</div>';
        result.className = 'ts-widget-result show';
        btn.textContent = 'Verifică';
        btn.disabled = false;
      }})
      .catch(function() {{
        result.innerHTML = '<span style="color:#ff4d6d">Eroare de conexiune</span>';
        result.className = 'ts-widget-result show';
        btn.textContent = 'Verifică';
        btn.disabled = false;
      }});
    }});
  }}

  // Auto-init on data-truthscore elements
  document.addEventListener('DOMContentLoaded', function() {{
    document.querySelectorAll('[data-truthscore]').forEach(function(el) {{
      createWidget(el, {{}});
    }});
  }});

  // Expose global API
  window.TruthScore = {{ init: createWidget }};
}})();
""".strip()
    return FResponse(content=js, media_type="application/javascript",
                     headers={"Access-Control-Allow-Origin": "*",
                               "Cache-Control": "public, max-age=3600"})


