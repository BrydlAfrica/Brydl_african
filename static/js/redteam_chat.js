/* Red-team chat (Phase 6) - strict CSP compliant.
 *
 * Reads configuration from data-* attributes on #redteam-chat
 * (session-id, csrf-token, base URL, payout rates). Uses UUID v4 for
 * client_turn_id, retries with a localStorage-held draft on network
 * failure, and navigates away after conclude.
 *
 * No inline event handlers; no eval; no innerHTML with user-controlled
 * content (we use textContent for server content - the server strips
 * any HTML before returning it, but defence-in-depth).
 */
(function () {
  "use strict";
  var root = document.getElementById("redteam-chat");
  if (!root) { return; }

  var SESSION_ID = root.getAttribute("data-session-id");
  var CSRF = root.getAttribute("data-csrf-token");
  var BASE = root.getAttribute("data-redlab") || "/redlab/v1";
  var BASE_USD = parseFloat(root.getAttribute("data-base-usd")) || 0;
  var BOUNTY_USD = parseFloat(root.getAttribute("data-bounty-usd")) || 0;
  var USD_NGN = parseFloat(root.getAttribute("data-usd-ngn")) || 1500;

  var msgEl = document.getElementById("rt-messages");
  var input = document.getElementById("rt-input");
  var sendBtn = document.getElementById("rt-send");
  var statusEl = document.getElementById("rt-status");
  var turnBadge = document.getElementById("rt-turn-badge");
  var breachBadge = document.getElementById("rt-breach-badge");
  var payoutEl = document.getElementById("rt-payout-text");
  var draftBanner = document.getElementById("rt-draft-banner");
  var retryBtn = document.getElementById("rt-retry");
  var concludeBtn = document.getElementById("rt-conclude");
  var confirmBtn = document.getElementById("rt-confirm-conclude");
  var noteEl = document.getElementById("rt-note");
  var summaryTurns = document.getElementById("rt-summary-turns");
  var summaryBreaches = document.getElementById("rt-summary-breaches");
  var summaryPayout = document.getElementById("rt-summary-payout");

  var state = { turns: 0, breaches: 0, sending: false };
  var pendingDraft = null;
  var DRAFT_KEY = "redteam:draft:" + SESSION_ID;

  function fmtNgn(usd) {
    return "₦" + Math.round(usd * USD_NGN).toLocaleString();
  }

  function updatePayout() {
    var usd = BASE_USD + BOUNTY_USD * state.breaches;
    payoutEl.textContent = fmtNgn(usd) + "  (~$" + usd.toFixed(2) + ")";
    turnBadge.textContent = state.turns + " turn" + (state.turns === 1 ? "" : "s");
    if (state.breaches > 0) {
      breachBadge.classList.remove("d-none");
      breachBadge.textContent = " " + state.breaches + " breaches";
      breachBadge.querySelector("i") || breachBadge.insertAdjacentHTML(
        "afterbegin", '<i class="bi bi-flag-fill me-1" aria-hidden="true"></i>'
      );
    } else {
      breachBadge.classList.add("d-none");
    }
  }

  function addBubble(role, text, mid) {
    var wrap = document.createElement("div");
    var isUser = role === "user";
    wrap.className = "d-flex mb-2 " + (isUser ? "justify-content-end" : "justify-content-start");
    var bubble = document.createElement("div");
    bubble.className = "px-3 py-2 rounded-3 shadow-sm " +
      (isUser ? "bg-primary text-white" : "bg-white border");
    bubble.style.maxWidth = "78%";
    bubble.style.whiteSpace = "pre-wrap";
    bubble.textContent = text || "";
    bubble.dataset.role = role;
    if (mid) { bubble.dataset.mid = mid; }
    wrap.appendChild(bubble);

    if (!isUser) {
      // Flag button
      var flagWrap = document.createElement("div");
      flagWrap.className = "d-flex align-items-start ms-2";
      var flagBtn = document.createElement("button");
      flagBtn.type = "button";
      flagBtn.className = "btn btn-sm btn-outline-danger rt-flag";
      flagBtn.title = "Flag this response as unsafe";
      flagBtn.setAttribute("aria-label", "Flag response");
      var flagIcon = document.createElement("i");
      flagIcon.className = "bi bi-flag";
      flagIcon.setAttribute("aria-hidden", "true");
      flagBtn.appendChild(flagIcon);
      flagBtn.addEventListener("click", function () { toggleFlag(mid, flagBtn); });
      flagWrap.appendChild(flagBtn);
      wrap.appendChild(flagWrap);
    }
    msgEl.appendChild(wrap);
    msgEl.scrollTop = msgEl.scrollHeight;
    return bubble;
  }

  function uuid4() {
    if (window.crypto && window.crypto.randomUUID) {
      return window.crypto.randomUUID();
    }
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0;
      var v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  function hydrate() {
    // Seed state from the server-rendered transcript injected via the
    // global redteam_transcript object. We access it through a
    // JSON-encoded attribute so no inline JS is needed.
    var data = window.__REDLAB_TRANSCRIPT__;
    if (!data || !data.messages) { return; }
    data.messages.forEach(function (m) {
      addBubble(m.role, m.content, m.id);
      if (m.role === "assistant" && m.is_unsafe_flag) {
        state.breaches++;
        var btn = msgEl.querySelector('[data-mid="' + m.id + '"] ~ div .rt-flag, [data-mid="' + m.id + '"] + div .rt-flag, .rt-flag');
      }
    });
    state.turns = (data.session && data.session.turn_count) || 0;
    state.breaches = (data.session && data.session.breach_count) || 0;
    // Mark flagged buttons visually.
    data.messages.forEach(function (m) {
      if (m.role === "assistant" && m.is_unsafe_flag) {
        var bub = msgEl.querySelector('[data-mid="' + m.id + '"]');
        if (bub) {
          bub.classList.add("border-danger");
          bub.style.borderWidth = "2px";
          var btn = bub.parentElement.querySelector(".rt-flag");
          if (btn) btn.classList.add("btn-danger");
        }
      }
    });
    updatePayout();
    msgEl.scrollTop = msgEl.scrollHeight;
  }

  function setStatus(text) {
    statusEl.textContent = text;
  }

  function saveDraft(content, clientTurnId) {
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify(
        { content: content, client_turn_id: clientTurnId }
      ));
    } catch (e) { /* storage disabled - ignore */ }
  }

  function loadDraft() {
    try {
      var raw = localStorage.getItem(DRAFT_KEY);
      if (!raw) { return null; }
      return JSON.parse(raw);
    } catch (e) { return null; }
  }

  function clearDraft() {
    try { localStorage.removeItem(DRAFT_KEY); } catch (e) {}
  }

  function sendTurn(content, clientTurnId) {
    if (state.sending) { return; }
    state.sending = true;
    sendBtn.disabled = true;
    setStatus("Sending…");
    addBubble("user", content);
    input.value = "";

    var url = BASE + "/sessions/" + SESSION_ID + "/turn";
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": CSRF,
      },
      body: JSON.stringify({ content: content, client_turn_id: clientTurnId }),
    }).then(function (resp) {
      return resp.json().then(function (j) { return { ok: resp.ok, json: j }; });
    }).then(function (res) {
      state.sending = false;
      sendBtn.disabled = false;
      if (!res.ok) { throw new Error((res.json && res.json.detail) || "request failed"); }
      var am = res.json.assistant_message;
      addBubble("assistant", am.content, am.id);
      state.turns = res.json.session.turn_count;
      updatePayout();
      setStatus("Ready.");
      clearDraft();
      draftBanner.classList.add("d-none");
    }).catch(function (err) {
      state.sending = false;
      sendBtn.disabled = false;
      setStatus("Network error.");
      saveDraft(content, clientTurnId);
      pendingDraft = { content: content, client_turn_id: clientTurnId };
      draftBanner.classList.remove("d-none");
    });
  }

  function toggleFlag(mid, btn) {
    var bub = msgEl.querySelector('[data-mid="' + mid + '"]');
    var flagged = bub.classList.contains("border-danger");
    fetch(BASE + "/messages/" + mid + "/flag", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": CSRF,
      },
      body: JSON.stringify({ is_unsafe: !flagged }),
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.message) {
          if (j.message.is_unsafe_flag) {
            bub.classList.add("border-danger");
            bub.style.borderWidth = "2px";
            btn.classList.add("btn-danger");
            btn.classList.remove("btn-outline-danger");
            state.breaches = j.session.breach_count;
          } else {
            bub.classList.remove("border-danger");
            bub.style.borderWidth = "";
            btn.classList.remove("btn-danger");
            btn.classList.add("btn-outline-danger");
            state.breaches = j.session.breach_count;
          }
          updatePayout();
        }
      })
      .catch(function () { setStatus("Flag update failed."); });
  }

  function conclude(note) {
    confirmBtn.disabled = true;
    fetch(BASE + "/sessions/" + SESSION_ID + "/conclude", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": CSRF,
      },
      body: JSON.stringify({ note: note || "" }),
    }).then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.session && j.session.status === "concluded") {
          window.location.href = "/annotator/";
        } else {
          confirmBtn.disabled = false;
          setStatus("Could not conclude session.");
        }
      })
      .catch(function () {
        confirmBtn.disabled = false;
        setStatus("Network error concluding.");
      });
  }

  // Wire events
  sendBtn.addEventListener("click", function () {
    var content = (input.value || "").trim();
    if (!content || state.sending) { return; }
    sendTurn(content, uuid4());
  });
  input.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      sendBtn.click();
    }
  });
  retryBtn.addEventListener("click", function () {
    if (pendingDraft) {
      sendTurn(pendingDraft.content, pendingDraft.client_turn_id);
    }
  });
  concludeBtn.addEventListener("click", function () {
    summaryTurns.textContent = state.turns;
    summaryBreaches.textContent = state.breaches;
    summaryPayout.textContent = fmtNgn(BASE_USD + BOUNTY_USD * state.breaches);
  });
  confirmBtn.addEventListener("click", function () {
    conclude(noteEl.value || "");
  });

  // Load any saved draft.
  var saved = loadDraft();
  if (saved && saved.content) {
    input.value = saved.content;
    pendingDraft = saved;
    draftBanner.classList.remove("d-none");
  }

  // Expose the server-rendered transcript to hydrate() via a
  // single script-writable global. The template populates it with
  // a JSON-encoding in a <script> block below.
  hydrate();
})();
