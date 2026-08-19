// ════════════════════════════════════════════════════════════
//  AGENT · DEEP DIVE  (tool-using LLM panel)
//  Loaded after app.js. Reuses _esc / _n if present, otherwise
//  defines local equivalents.
// ════════════════════════════════════════════════════════════
"use strict";

(function () {
  const esc = window._esc || function (s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };

  function init() {
    const overlay = document.getElementById("deep-dive-overlay");
    if (!overlay) return;

    const elTitle      = document.getElementById("deep-dive-title");
    const elQuestion   = document.getElementById("deep-dive-question");
    const elModel      = document.getElementById("deep-dive-model");
    const elTraceWrap  = document.getElementById("deep-dive-trace-wrap");
    const elTrace      = document.getElementById("deep-dive-trace");
    const elTcount     = document.getElementById("deep-dive-tool-count");
    const elAnswerWrap = document.getElementById("deep-dive-answer-wrap");
    const elAnswer     = document.getElementById("deep-dive-answer");
    const elStatus     = document.getElementById("deep-dive-status");
    const elStatusTxt  = document.getElementById("deep-dive-status-text");

    const close = () => overlay.classList.add("hidden");
    document.getElementById("deep-dive-close")?.addEventListener("click", close);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !overlay.classList.contains("hidden")) close();
    });

    // Public entry point
    window.openDeepDive = async function ({ title, question, scope = "general", context = null } = {}) {
      if (!question) return;
      elTitle.textContent    = title || "Deep Dive";
      elQuestion.textContent = question;
      elModel.classList.add("hidden");
      elModel.textContent    = "";
      elTrace.innerHTML      = "";
      elTraceWrap.classList.add("hidden");
      elTcount.textContent   = "0";
      elAnswer.innerHTML      = "";
      elAnswerWrap.classList.add("hidden");
      elStatus.classList.remove("hidden");
      elStatusTxt.textContent = "Calling agent…";
      overlay.classList.remove("hidden");

      try {
        const res = await fetch("/api/agent/deep-dive", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question, scope, context }),
        });
        if (!res.ok) {
          const errText = await res.text();
          elStatus.classList.add("hidden");
          elAnswerWrap.classList.remove("hidden");
          _renderAnswer(elAnswer, `Agent error (HTTP ${res.status}): ${errText.slice(0, 400)}`);
          return;
        }
        const data = await res.json();

        // Render evidence trace
        const trace = Array.isArray(data.trace) ? data.trace : [];
        const toolSteps = trace.filter((t) => t.kind === "tool_call");
        if (toolSteps.length) {
          elTraceWrap.classList.remove("hidden");
          elTcount.textContent = String(toolSteps.length);
          elTrace.innerHTML = toolSteps.map((t, idx) => {
            const args = t.args && typeof t.args === "object" && Object.keys(t.args).length
              ? Object.entries(t.args).map(([k, v]) => `<span class="text-Cpurple/80">${esc(k)}</span>=<span class="text-Cwhite/80">${esc(String(v))}</span>`).join("  ")
              : "";
            const result = t.result || {};
            const rowCount = result.n_total != null ? result.n_total
                            : Array.isArray(result.rows) ? result.rows.length
                            : null;
            const errMsg = result.error || result.note || "";
            const isErr  = Boolean(result.error);
            const preview = isErr
              ? `<span class="text-Cred/90 text-[9px]">⚠ ${esc(errMsg.slice(0, 80))}</span>`
              : errMsg
                ? `<span class="text-Camber/80 text-[9px]">ℹ ${esc(errMsg.slice(0, 80))}</span>`
                : rowCount != null
                  ? `<span class="text-Cgreen text-[9px] font-semibold">${rowCount} row${rowCount === 1 ? "" : "s"}</span>`
                  : `<span class="text-Cgreen text-[9px] font-semibold">ok</span>`;
            return `
              <div class="flex items-start gap-3 rounded-lg border border-white/8 bg-white/[0.03] px-3 py-2">
                <span class="text-[10px] font-mono text-Cpurple/70 shrink-0 mt-0.5 w-5 text-right">${idx}.</span>
                <div class="flex-1 min-w-0">
                  <div class="font-mono text-[11px] text-Cwhite/90 leading-snug">
                    ${esc(t.name || "?")}${args ? `<span class="text-Cmuted/60">(${args})</span>` : `<span class="text-Cmuted/60">()</span>`}
                  </div>
                  <div class="mt-0.5">${preview}</div>
                </div>
              </div>`;
          }).join("");
        }

        elStatus.classList.add("hidden");
        elAnswerWrap.classList.remove("hidden");
        _renderAnswer(elAnswer, data.answer || "(empty answer)");

        if (data.model) {
          elModel.textContent = data.model;
          elModel.classList.remove("hidden");
        }
      } catch (err) {
        elStatus.classList.add("hidden");
        elAnswerWrap.classList.remove("hidden");
        _renderAnswer(elAnswer, `Network error: ${err && err.message ? err.message : String(err)}`);
      }
    };

    // ── Structured answer renderer ──────────────────────────────
    function _renderAnswer(el, raw) {
      if (!raw) { el.textContent = "(empty answer)"; return; }

      // Section keywords the agent uses
      const SECTIONS = {
        "VERDICT":        { color: "#f43f5e", icon: "⚖" },
        "STATUS":         { color: "#f43f5e", icon: "⚖" },
        "ROOT CAUSE":     { color: "#f59e0b", icon: "🔍" },
        "EVIDENCE":       { color: "#3b82f6", icon: "📋" },
        "CONFIDENCE":     { color: "#a855f7", icon: "📊" },
        "NEXT ACTION":    { color: "#10d96e", icon: "→" },
        "NEXT 48H":       { color: "#10d96e", icon: "→" },
        "NEXT 48H ACTIONS": { color: "#10d96e", icon: "→" },
        "TOP 3 RISKS":    { color: "#f43f5e", icon: "⚠" },
        "PREDICTIONS":    { color: "#a855f7", icon: "📈" },
        "ACCURACY NOTE":  { color: "#6b7db3", icon: "ℹ" },
        "SUMMARY":        { color: "#3b82f6", icon: "📝" },
      };

      // Split on section headers — handle both "VERDICT:" and "VERDICT"
      const sectionRe = new RegExp(
        `^(${Object.keys(SECTIONS).join("|")})[:\\s]*(.*)$`, "im"
      );

      const lines = raw.split(/\n/);
      let html = "";
      let inSection = null;
      let sectionMeta = null;
      let sectionBuf = [];

      const _flushSection = () => {
        if (!inSection) return;
        const content = sectionBuf.join("\n").trim();
        if (!content) { sectionBuf = []; return; }
        const meta = SECTIONS[inSection] || { color: "#6b7db3", icon: "•" };

        // Render bullet items inside the section
        const rendered = content.split(/\n/).map((ln) => {
          ln = ln.trim();
          if (!ln) return "";
          // Detect sub-bullets (starts with -, *, •, digit+dot)
          const isBullet = /^[-*•]|^\d+[.)]\s/.test(ln);
          const clean = ln.replace(/^[-*•]\s*/, "").replace(/^\d+[.)]\s*/, "").trim();
          if (!clean) return "";
          // Bold **text** markers
          const boldified = esc(clean).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
          if (isBullet) {
            return `<div class="flex items-start gap-2 py-0.5">
              <span class="shrink-0 mt-0.5" style="color:${meta.color}">›</span>
              <span class="text-Cwhite/85 leading-relaxed">${boldified}</span>
            </div>`;
          }
          return `<p class="text-Cwhite/85 leading-relaxed py-0.5">${boldified}</p>`;
        }).filter(Boolean).join("");

        html += `<div class="rounded-lg border border-white/8 bg-white/[0.025] px-4 py-3 mb-3">
          <div class="flex items-center gap-2 mb-2">
            <span class="text-base">${meta.icon}</span>
            <span class="text-[10px] font-bold uppercase tracking-widest" style="color:${meta.color}">${esc(inSection)}</span>
          </div>
          <div class="text-[11px] leading-relaxed">${rendered}</div>
        </div>`;
        sectionBuf = [];
      };

      for (const line of lines) {
        // Try to match a section header
        let matched = false;
        for (const key of Object.keys(SECTIONS)) {
          const re = new RegExp(`^${key}[:\\s]*(.*)$`, "i");
          const m = line.match(re);
          if (m) {
            _flushSection();
            inSection = key;
            sectionMeta = SECTIONS[key];
            const rest = (m[1] || "").trim();
            if (rest) sectionBuf.push(rest);
            matched = true;
            break;
          }
        }
        if (!matched) {
          if (inSection) {
            sectionBuf.push(line);
          } else {
            // Prose before any section heading
            const clean = line.trim();
            if (clean) {
              const boldified = esc(clean).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
              html += `<p class="text-Cwhite/80 text-[11px] leading-relaxed mb-1.5">${boldified}</p>`;
            }
          }
        }
      }
      _flushSection();

      // If no sections were parsed (plain answer), show as clean paragraphs
      if (!html) {
        html = raw.split(/\n\n+/).map((para) => {
          const clean = para.trim();
          if (!clean) return "";
          return `<p class="text-Cwhite/85 text-[11px] leading-relaxed mb-2">${esc(clean).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")}</p>`;
        }).join("");
      }

      el.innerHTML = html || `<p class="text-Cmuted text-[11px]">${esc(raw)}</p>`;
    }

    // Event delegation: any [data-deep-dive] element opens the panel.
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-deep-dive]");
      if (!btn) return;
      e.preventDefault();
      e.stopPropagation();
      let context = null;
      const ctxRaw = btn.getAttribute("data-context");
      if (ctxRaw) { try { context = JSON.parse(ctxRaw); } catch { context = null; } }
      window.openDeepDive({
        title:    btn.getAttribute("data-title")    || "Deep Dive",
        question: btn.getAttribute("data-question") || "Investigate this item.",
        scope:    btn.getAttribute("data-scope")    || "general",
        context,
      });
    });
  }

  // Helper used by other modules to render an inline Deep-Dive button.
  window.deepDiveBtn = function ({ title, question, scope = "general", context = null, label = "Deep Dive" } = {}) {
    const ctxAttr = context
      ? ` data-context='${esc(JSON.stringify(context)).replace(/'/g, "&#39;")}'`
      : "";
    return `<button type="button" data-deep-dive
      data-title="${esc(title || "")}"
      data-question="${esc(question || "")}"
      data-scope="${esc(scope)}"${ctxAttr}
      class="text-[10px] font-semibold px-2 py-0.5 rounded-md border border-Cpurple/40 bg-Cpurple/10 text-Cpurple hover:bg-Cpurple/20 transition-colors whitespace-nowrap">
      🔍 ${esc(label)}
    </button>`;
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
