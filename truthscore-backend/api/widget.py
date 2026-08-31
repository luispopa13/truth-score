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
        return FResponse(content=stub, media_type="application/javascript; charset=utf-8",
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
      ".ts-widget {{ box-sizing: border-box; font-family: system-ui, -apple-system, sans-serif; max-width: 480px; border: 1px solid #2a2a4a;",
      "border-radius: 12px; background: #0d0d1a; color: #e2e2f0; padding: 16px; margin: 8px 0; }}",
      ".ts-widget *, .ts-widget *::before, .ts-widget *::after {{ box-sizing: border-box; }}",
      ".ts-widget-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }}",
      ".ts-widget-logo {{ font-size: 16px; }}",
      ".ts-widget-title {{ font-weight: 700; font-size: 14px; }}",
      ".ts-widget-input {{ display: block; width: 100%; padding: 10px; background: #12121f; border: 1px solid #2a2a4a;",
      "border-radius: 8px; color: #e2e2f0; font-size: 13px; font-family: inherit; resize: vertical; outline: none; }}",
      ".ts-widget-input:focus {{ border-color: #5b5bff; }}",
      ".ts-widget-btn {{ display: block; margin-top: 8px; padding: 10px 16px; background: #5b5bff; color: white;",
      "border: none; border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 600; width: 100%; }}",
      ".ts-widget-btn:hover {{ background: #7c74ff; }}",
      ".ts-widget-btn:disabled {{ opacity: 0.6; cursor: default; }}",
      ".ts-widget-result {{ margin-top: 12px; padding: 12px; background: #12121f;",
      "border-radius: 8px; font-size: 13px; display: none; }}",
      ".ts-widget-result.show {{ display: block; }}",
      ".ts-widget-score {{ font-size: 20px; font-weight: 800; }}",
      ".ts-widget-verdict {{ font-weight: 700; margin-left: 6px; }}",
      ".ts-widget-expl {{ font-size: 12px; color: #8080a8; margin-top: 6px; line-height: 1.5; }}",
      ".ts-widget-correct {{ font-size: 12px; color: #e2e2f0; margin-top: 10px; padding: 9px 11px;",
      "background: #14142a; border-left: 3px solid #22d47a; border-radius: 6px; line-height: 1.5; }}",
      ".ts-widget-srcs {{ margin-top: 10px; }}",
      ".ts-widget-srcs-title {{ font-size: 10px; font-weight: 700; color: #8080a8; text-transform: uppercase;",
      "letter-spacing: 0.04em; margin: 10px 0 4px; }}",
      ".ts-widget-src {{ display: block; font-size: 12px; color: #9898ff; text-decoration: none; padding: 5px 0;",
      "border-bottom: 1px solid #1e1e33; line-height: 1.4; }}",
      ".ts-widget-src:hover {{ color: #b7b0ff; }}",
      ".ts-widget-src-badge {{ font-size: 9px; font-weight: 800; padding: 1px 5px; border-radius: 3px;",
      "margin-right: 6px; vertical-align: middle; }}",
      ".ts-widget-src-pub {{ color: #6b6b95; }}",
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
      '<textarea class="ts-widget-input" rows="3" ' +
      'placeholder="Enter a claim to fact-check..."></textarea>' +
      '<button class="ts-widget-btn">Verify</button>' +
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
      btn.textContent = '[loading] Verifying...';
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
        function esc(s) {{
          return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }}
        // API-level error (quota, plan, bad key) — surface it plainly.
        if (d && (d.detail || d.error) && d.verdict == null) {{
          result.innerHTML = '<span style="color:#ff4d6d">' + esc(d.detail || d.error) + '</span>';
          result.className = 'ts-widget-result show';
          btn.textContent = 'Verify';
          btn.disabled = false;
          return;
        }}
        function srcList(arr, label, bc, bg) {{
          if (!arr || !arr.length) return '';
          var h = '<div class="ts-widget-srcs-title">' + label + ' (' + arr.length + ')</div>';
          arr.slice(0, 5).forEach(function(s) {{
            var pub   = esc(s.publisher || s.source || '');
            var title = esc(s.title || s.url || 'source');
            var url   = s.url || '';
            var badge = '<span class="ts-widget-src-badge" style="color:' + bc +
                        ';background:' + bg + '">' + label.charAt(0) + '</span>';
            var inner = badge + (pub ? '<span class="ts-widget-src-pub">' + pub + '</span> ' : '') + title;
            h += url
              ? '<a class="ts-widget-src" href="' + esc(url) + '" target="_blank" rel="noopener">' + inner + '</a>'
              : '<div class="ts-widget-src">' + inner + '</div>';
          }});
          return h;
        }}

        var color = d.verdict === 'TRUE' ? '#22d47a' : d.verdict === 'FALSE' ? '#ff4d6d' : '#f0b429';
        var lbl   = d.verdict === 'TRUE' ? 'TRUE' : d.verdict === 'FALSE' ? 'FALSE' : 'UNCERTAIN';

        var html =
          '<span class="ts-widget-score" style="color:' + color + '">' + (d.score != null ? d.score : 50) + '</span>' +
          '<span class="ts-widget-verdict" style="color:' + color + '">' + lbl + '</span>' +
          '<div class="ts-widget-expl">' + esc(d.explanation || '') + '</div>';

        // Correct answer / context when the claim is false or misleading.
        var correct = d.correct_answer || d.corrected_context || '';
        if (correct) {{
          html += '<div class="ts-widget-correct"><b>Correct answer:</b> ' + esc(correct) + '</div>';
        }}

        // Sources with clickable links, grouped by stance.
        var srcs = srcList(d.supporting, 'Supporting sources', '#22d47a', 'rgba(34,212,122,0.15)') +
                   srcList(d.contradicting, 'Contradicting sources', '#ff4d6d', 'rgba(255,77,109,0.15)');
        if (!srcs) srcs = srcList(d.neutral_sources, 'Related sources', '#8080a8', 'rgba(128,128,168,0.15)');
        if (srcs) html += '<div class="ts-widget-srcs">' + srcs + '</div>';

        result.innerHTML = html;
        result.className = 'ts-widget-result show';
        btn.textContent = 'Verify';
        btn.disabled = false;
      }})
      .catch(function() {{
        result.innerHTML = '<span style="color:#ff4d6d">Connection error</span>';
        result.className = 'ts-widget-result show';
        btn.textContent = 'Verify';
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
    return FResponse(content=js, media_type="application/javascript; charset=utf-8",
                     headers={"Access-Control-Allow-Origin": "*",
                               # Revalidate every load so UI/copy updates propagate
                               # immediately instead of serving a stale cached copy.
                               "Cache-Control": "no-cache, must-revalidate"})


