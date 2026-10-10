/* QA breach-flag toggle for the red-team review card (Phase 7).
 *
 * Reads config from #qa-redteam data-* attrs and attaches click
 * handlers to per-assistant-turn .rt-qa-flag buttons. Clicking
 * POSTs JSON to the Flask toggle endpoint with X-CSRFToken, updates
 * the bubble styling, and recomputes the payout preview live.
 *
 * CSP-strict: textContent only for dynamic text, no eval/document.write/innerHTML.
 */
(function () {
  "use strict";
  var root = document.getElementById("qa-redteam");
  if (!root) { return; }

  var subId = root.getAttribute("data-sub-id");
  var csrf = root.getAttribute("data-csrf-token");
  var urlTpl = root.getAttribute("data-toggle-url");
  var baseUsd = parseFloat(root.getAttribute("data-base-usd") || "0");
  var bountyUsd = parseFloat(root.getAttribute("data-bounty-usd") || "0");
  var countEl = root.querySelector("#qa-breach-count .cnt");
  var payoutEl = document.getElementById("qa-payout-preview");
  var icon = '<i class="bi bi-cash-coin me-1" aria-hidden="true"></i>';

  function fmt(n) {
    return "$" + n.toFixed(2);
  }
  function refreshPreview() {
    var cnt = 0;
    var flagged = root.querySelectorAll(".rt-msg[data-flagged='1']");
    cnt = flagged.length;
    if (countEl) { countEl.textContent = String(cnt); }
    if (payoutEl) {
      var total = baseUsd + bountyUsd * cnt;
      payoutEl.textContent = "";
      var ic = document.createElement("i");
      ic.className = "bi bi-cash-coin me-1";
      ic.setAttribute("aria-hidden", "true");
      payoutEl.appendChild(ic);
      payoutEl.appendChild(document.createTextNode(fmt(total)));
    }
    // Update "breach"/"breaches" plural word after count span.
    var badge = document.getElementById("qa-breach-count");
    if (badge) {
      // Walk text nodes after the .cnt span and update plural.
      var walk = badge.ownerDocument.createTreeWalker(badge, NodeFilter.SHOW_TEXT);
      var target = null;
      while (walk.nextNode()) { target = walk.currentNode; }
      if (target) {
        target.textContent = " breach" + (cnt === 1 ? "" : "es");
      }
    }
  }

  function postFlag(mid, want) {
    var url = urlTpl.replace("__MID__", encodeURIComponent(mid));
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": csrf
      },
      body: JSON.stringify({ is_unsafe: want })
    }).then(function (r) { return r.json().then(function (j) {
      if (!r.ok || !j.ok) { throw new Error((j && j.error) || "request_failed"); }
      return j;
    }); });
  }

  var btns = root.querySelectorAll(".rt-qa-flag");
  for (var i = 0; i < btns.length; i++) {
    btns[i].addEventListener("click", function (ev) {
      var btn = ev.currentTarget;
      var row = btn.closest(".rt-msg");
      if (!row) { return; }
      var mid = row.getAttribute("data-mid");
      var currently = row.getAttribute("data-flagged") === "1";
      var want = !currently;
      btn.disabled = true;
      postFlag(mid, want).then(function () {
        row.setAttribute("data-flagged", want ? "1" : "0");
        var bubble = row.querySelector(".px-2.py-1");
        if (bubble) {
          bubble.classList.toggle("border-danger", want);
          bubble.classList.toggle("border-2", want);
          bubble.classList.toggle("text-danger", want);
        }
        btn.classList.toggle("btn-danger", want);
        btn.classList.toggle("btn-outline-danger", !want);
        refreshPreview();
      }).catch(function (err) {
        btn.disabled = false;
        var el = document.getElementById("qa-redteam-status");
        if (el) { el.textContent = "Flag update failed: " + err.message; }
      }).then(function () { btn.disabled = false; });
    });
  }

  // Render initial preview once on load (count may differ from server
  // if flags were toggled before; current DOM state is authoritative).
  refreshPreview();
})();
