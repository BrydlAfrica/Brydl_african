/*
 * Brydl Dashboard - small progressive enhancements.
 * Loaded with `defer` from base.html. The app's Content-Security-Policy
 * forbids inline scripts/handlers, so behaviour is attached here by
 * convention:
 * - Page-load progress bar (#brydl-progress) activates while a form or
 *   link navigates, hides when the new page is ready (or restored from
 *   the bfcache).
 * - <form data-confirm="message"> asks before submitting.
 * - Forms are double-submission guarded: a second submit event is
 *   cancelled, and submit buttons disable (with a CSS spinner from
 *   style.css) one tick AFTER the submit event. The delay matters:
 *   disabling inside the handler would drop the clicked button's
 *   name=value pair, breaking forms that rely on it (QA review's
 *   Approve/Reject buttons).
 * - Every textarea[maxlength] gets a live "characters left" counter.
 * - Ctrl/Cmd+Enter inside a textarea submits its form (labelling
 *   comfort on long answers).
 * - Bootstrap tooltips initialise anywhere they are declared.
 * - Non-error flash alerts auto-dismiss after 7 seconds.
 * - <input type="range" data-score-display="id"> mirrors its value
 *   (QA score slider).
 * - #check-all toggles .row-check checkboxes; #bulk-count mirrors the
 *   count (review queue bulk bar).
 */

/* ---- Page-load progress bar ---- */
function brydlProgress(active) {
  var bar = document.getElementById("brydl-progress");
  if (bar) {
    bar.classList.toggle("active", active);
  }
}

window.addEventListener("beforeunload", function () {
  brydlProgress(true);
});
window.addEventListener("pageshow", function () {
  brydlProgress(false);
});

/* ---- Form submissions: confirm, double-submit guard, spinner ---- */
document.addEventListener("submit", function (event) {
  var form = event.target;
  if (!(form instanceof HTMLFormElement)) {
    return;
  }
  if (form.dataset.busy === "1") {
    event.preventDefault(); // swallow impatient re-submits
    return;
  }
  if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
    event.preventDefault();
    return;
  }
  form.dataset.busy = "1";
  brydlProgress(true);
  var buttons = form.querySelectorAll('button[type="submit"]');
  // Deferred one tick so the browser serialises the form (including the
  // clicked button's name=value) BEFORE the buttons are disabled.
  window.setTimeout(function () {
    buttons.forEach(function (button) {
      button.disabled = true;
      button.setAttribute("data-loading", "true");
      button.setAttribute("aria-busy", "true");
    });
  }, 0);
});

/* Browser-back restores a page from the bfcache exactly as it looked,
 * including disabled buttons and the busy flag; reset both so the forms
 * work again without a reload. */
window.addEventListener("pageshow", function () {
  document.querySelectorAll('form[data-busy="1"]').forEach(function (form) {
    delete form.dataset.busy;
  });
  document.querySelectorAll('button[data-loading="true"]').forEach(
    function (button) {
      button.disabled = false;
      button.removeAttribute("data-loading");
      button.removeAttribute("aria-busy");
    }
  );
});

/* ---- Live character counters for every capped textarea ---- */
document.querySelectorAll("textarea[maxlength]").forEach(function (area) {
  var counter = document.createElement("div");
  counter.className = "form-text char-counter";
  var update = function () {
    var limit = parseInt(area.getAttribute("maxlength"), 10) || 0;
    if (!limit) {
      return;
    }
    var remaining = limit - area.value.length;
    counter.textContent = remaining.toLocaleString() + " characters left";
    counter.classList.toggle("near-limit", remaining <= limit * 0.1);
  };
  area.addEventListener("input", update);
  if (area.parentNode) {
    area.parentNode.insertBefore(counter, area.nextSibling);
  }
  update();
});

/* ---- Ctrl/Cmd+Enter submits the surrounding form ---- */
document.addEventListener("keydown", function (event) {
  if (
    (event.ctrlKey || event.metaKey) &&
    event.key === "Enter" &&
    event.target instanceof HTMLTextAreaElement &&
    event.target.form
  ) {
    event.preventDefault();
    if (typeof event.target.form.requestSubmit === "function") {
      event.target.form.requestSubmit();
    }
  }
});

/* ---- Bootstrap enhancements ---- */
document.addEventListener("DOMContentLoaded", function () {
  if (window.bootstrap) {
    document
      .querySelectorAll('[data-bs-toggle="tooltip"]')
      .forEach(function (el) {
        bootstrap.Tooltip.getOrCreateInstance(el);
      });
    // Success/info/warning flashes fade on their own; errors stay.
    document.querySelectorAll(".alert-dismissible").forEach(function (el) {
      if (!el.classList.contains("alert-danger")) {
        window.setTimeout(function () {
          bootstrap.Alert.getOrCreateInstance(el).close();
        }, 7000);
      }
    });
  }
});

/* ---- QA score slider mirror ---- */
document.addEventListener("input", function (event) {
  var el = event.target;
  if (
    el instanceof HTMLInputElement &&
    el.type === "range" &&
    el.dataset.scoreDisplay
  ) {
    var target = document.getElementById(el.dataset.scoreDisplay);
    if (target) {
      target.textContent = el.value;
    }
  }
});

/* ---- Review queue select-all + counter ---- */
document.addEventListener("change", function (event) {
  var el = event.target;
  if (!(el instanceof HTMLInputElement)) {
    return;
  }
  var counter = document.getElementById("bulk-count");
  if (!counter) {
    return;
  }
  if (el.id === "check-all") {
    document.querySelectorAll(".row-check").forEach(function (box) {
      box.checked = el.checked;
    });
  }
  var selected = document.querySelectorAll(".row-check:checked").length;
  counter.textContent = selected + " selected";
});

/* ---- Signature boot overlay ----
 * The overlay fades itself out via a pure-CSS timeline (see
 * style.css) so it can never strand a visitor if JS fails. Here we
 * only: skip repeats for the session, remove the node once the fade
 * completes, and enforce a hard cap. */
(function () {
  var boot = document.getElementById("brydl-boot");
  if (!boot) {
    return;
  }
  var reduced = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced) {
    boot.remove();
    return;
  }
  try {
    if (window.sessionStorage.getItem("brydl-booted")) {
      boot.remove();
      return;
    }
    window.sessionStorage.setItem("brydl-booted", "1");
  } catch (err) {
    /* Storage unavailable (hard private mode): just play it once. */
  }
  var gone = false;
  var dismiss = function () {
    if (gone) {
      return;
    }
    gone = true;
    boot.classList.add("is-done");
    window.setTimeout(function () {
      if (boot.parentNode) {
        boot.parentNode.removeChild(boot);
      }
    }, 600);
  };
  if (document.readyState === "complete") {
    window.setTimeout(dismiss, 1000);
  } else {
    window.addEventListener("load", function () {
      window.setTimeout(dismiss, 1000);
    });
  }
  window.setTimeout(dismiss, 3400); // hard cap: never stuck on the overlay
})();

/* ---- Scroll reveal for landing sections ---- */
(function () {
  var sections = document.querySelectorAll(".brydl-reveal");
  if (!sections.length) {
    return;
  }
  var reduced = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced || !("IntersectionObserver" in window)) {
    sections.forEach(function (el) {
      el.classList.add("is-visible");
    });
    return;
  }
  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        entry.target.classList.add("is-visible");
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.12 });
  sections.forEach(function (el) {
    observer.observe(el);
  });
})();

/* ---- Keyboard answer selection (task page) ----
 * Pressing 1–9 checks the Nth radio option in the answer form, for
 * fast classification/rating work. Guarded so typing in any field is
 * never hijacked. */
document.addEventListener("keydown", function (event) {
  if (event.ctrlKey || event.metaKey || event.altKey) {
    return;
  }
  if (!/^[1-9]$/.test(event.key)) {
    return;
  }
  var target = event.target;
  if (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement ||
    target.isContentEditable
  ) {
    return;
  }
  var form = document.querySelector(".answer-form");
  if (!form) {
    return;
  }
  var radios = form.querySelectorAll('input[type="radio"]');
  var index = parseInt(event.key, 10) - 1;
  if (index < radios.length) {
    radios[index].checked = true;
    radios[index].dispatchEvent(
      new Event("change", { bubbles: true })
    );
    event.preventDefault();
  }
});

/* ---- Offline resilience ----
 * Service worker (skipped in local dev so caches never mask edits),
 * an offline badge, per-task draft autosave to localStorage, and a
 * purge hook driven by server-rendered markers. Drafts live ONLY in
 * the annotator's own browser; logout wipes them (shared devices). */
(function () {
  /* 1. Service worker registration (production hosts only). */
  if (
    "serviceWorker" in navigator &&
    ["localhost", "127.0.0.1"].indexOf(location.hostname) === -1
  ) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js");
    });
  }

  /* 2. Offline badge (visible whenever the browser reports offline). */
  var badge = document.getElementById("brydl-offline-badge");
  if (badge) {
    var syncBadge = function () {
      badge.hidden = navigator.onLine;
    };
    window.addEventListener("online", syncBadge);
    window.addEventListener("offline", syncBadge);
    syncBadge();
  }

  var DRAFT_PREFIX = "brydl-draft-";

  function draftKeyOf(form) {
    return DRAFT_PREFIX + form.getAttribute("data-draft-key");
  }

  function storageGet(key) {
    try {
      var raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (err) {
      return null;
    }
  }

  function storageRemove(key) {
    try {
      localStorage.removeItem(key);
    } catch (err) { /* private mode etc.: nothing to remove */ }
  }

  /* 3. Purge markers: the server marks tasks whose draft is obsolete
   * (submission is with QA or approved). Renders on the dashboard's
   * recent-submission rows. */
  document
    .querySelectorAll("[data-draft-purge]")
    .forEach(function (marker) {
      storageRemove(
        DRAFT_PREFIX + marker.getAttribute("data-draft-purge")
      );
    });

  /* 4. Draft autosave/restore for every form carrying a draft key. */
  function editableFields(form) {
    return Array.prototype.filter.call(form.elements, function (el) {
      if (!el.name || el.name.indexOf("csrf_token") !== -1) {
        return false;
      }
      if (el.type === "submit" || el.type === "button") {
        return false;
      }
      if (el.type === "radio" || el.type === "checkbox") {
        return el.checked; // store only the picked choice
      }
      return true; // textareas and text-ish inputs
    });
  }

  var saveTimers = new WeakMap();

  function saveDraft(form) {
    var values = {};
    editableFields(form).forEach(function (el) {
      values[el.name] = el.value;
    });
    try {
      localStorage.setItem(
        draftKeyOf(form),
        JSON.stringify({ ts: Date.now(), values: values })
      );
    } catch (err) { /* storage full/blocked: online-only fallback */ }
  }

  document.addEventListener("input", function (event) {
    var form = event.target.closest
      ? event.target.closest("form[data-draft-key]")
      : null;
    if (!form) {
      return;
    }
    window.clearTimeout(saveTimers.get(form));
    saveTimers.set(form, window.setTimeout(function () {
      saveDraft(form);
    }, 400));
  });

  /* Radios/checkboxes fire change (not input) in some engines. */
  document.addEventListener("change", function (event) {
    var form = event.target.closest
      ? event.target.closest("form[data-draft-key]")
      : null;
    if (form && (event.target.type === "radio" ||
                 event.target.type === "checkbox")) {
      saveDraft(form);
    }
  });

  /* 5. Restore drafts into empty fields when the task page loads. */
  document.addEventListener("DOMContentLoaded", function () {
    document
      .querySelectorAll("form[data-draft-key]")
      .forEach(function (form) {
        var saved = storageGet(draftKeyOf(form));
        if (!saved || !saved.values) {
          return;
        }
        var restoredCount = 0;
        Object.keys(saved.values).forEach(function (name) {
          var els = form.elements[name];
          if (!els) {
            return;
          }
          // Named access returns a RadioNodeList for grouped radios
          // and the bare control for everything else — normalise.
          var isGroup = typeof els.length === "number" &&
            els.length > 0 &&
            (els[0].type === "radio" || els[0].type === "checkbox");
          var list = isGroup ? Array.prototype.slice.call(els) : [els];
          var kind = list[0].type;
          var value = saved.values[name];
          if (kind === "radio") {
            list.forEach(function (el) {
              if (el.value === value && !el.checked) {
                el.checked = true;
                restoredCount++;
              }
            });
          } else if (kind === "checkbox") {
            if (value && !list[0].checked) {
              list[0].checked = true;
              restoredCount++;
            }
          } else if (list[0].value === "" && value) {
            // Never clobber server-rendered values (bound validation
            // errors) — only fill genuinely empty fields.
            list[0].value = value;
            restoredCount++;
          }
        });
        if (restoredCount > 0) {
          var banner = document.createElement("div");
          banner.className =
            "alert alert-info py-2 brydl-draft-banner";
          banner.setAttribute("role", "status");
          banner.innerHTML =
            '<i class="bi bi-cloud-check me-1" aria-hidden="true">' +
            "</i>Saved draft restored — your earlier typing was kept" +
            " on this device.";
          form.parentNode.insertBefore(banner, form);
        }
      });
  });

  /* 6. Logout wipes every Brydl draft (shared-device hygiene). */
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form instanceof HTMLFormElement && form.action &&
        form.action.indexOf("/logout") !== -1) {
      var doomed = [];
      for (var i = 0; i < localStorage.length; i++) {
        var key = localStorage.key(i);
        if (key && key.indexOf(DRAFT_PREFIX) === 0) {
          doomed.push(key);
        }
      }
      doomed.forEach(function (key) { storageRemove(key); });
    }
  });
})();
