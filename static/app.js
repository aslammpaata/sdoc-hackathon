/* SDOC — progressive enhancement only.
   Every page works with this file absent: navigation, server filters, opening a
   case and submitting a decision are all plain HTML. Nothing here fetches. */
(function () {
  "use strict";

  var store = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } }
  };

  /* ------------------------------------------------------------ theme */
  (function theme() {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    var root = document.documentElement;
    function current() {
      var set = root.getAttribute("data-theme");
      if (set) return set;
      return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    }
    function sync() { btn.setAttribute("aria-pressed", current() === "light" ? "true" : "false"); }
    sync();
    btn.addEventListener("click", function () {
      var next = current() === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      store.set("sdoc-theme", next);
      sync();
    });
  })();

  /* ------------------------------------------------------------ copy buttons */
  (function copy() {
    var live = null;
    document.addEventListener("click", function (ev) {
      var btn = ev.target.closest ? ev.target.closest(".copy") : null;
      if (!btn) return;
      var value = btn.getAttribute("data-copy") || "";
      var done = function (ok) {
        btn.textContent = ok ? "Copied" : "Press Ctrl+C";
        if (!live) {
          live = document.createElement("div");
          live.className = "vh";
          live.setAttribute("role", "status");
          live.setAttribute("aria-live", "polite");
          document.body.appendChild(live);
        }
        live.textContent = ok ? "Copied to clipboard" : "Copy failed — select the text and copy manually";
        setTimeout(function () { btn.textContent = "Copy"; }, 2000);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(value).then(function () { done(true); }, function () { done(false); });
      } else {
        done(false);
      }
    });
  })();

  /* ------------------------------------------------------------ cases explorer */
  (function explorer() {
    var table = document.getElementById("cases-table");
    var tools = document.getElementById("client-tools");
    if (!table || !tools) return;

    var tbody = table.tBodies[0];
    var all = Array.prototype.slice.call(tbody.rows);
    if (!all.length) return;

    var search = document.getElementById("client-search");
    var sizeSel = document.getElementById("page-size");
    var densitySel = document.getElementById("density");
    var countOut = document.getElementById("client-count");
    var pager = document.getElementById("pager");
    var prev = document.getElementById("prev-page");
    var next = document.getElementById("next-page");
    var info = document.getElementById("page-info");

    tools.hidden = false;
    if (pager) pager.hidden = false;

    // cache a lowercased haystack per row once
    all.forEach(function (tr) { tr._hay = (tr.textContent || "").toLowerCase(); });

    var matched = all.slice();
    var page = 0;

    var savedSize = store.get("sdoc-page-size");
    if (savedSize && sizeSel) {
      for (var i = 0; i < sizeSel.options.length; i++) {
        if (sizeSel.options[i].value === savedSize || sizeSel.options[i].text === savedSize) { sizeSel.selectedIndex = i; }
      }
    }
    var savedDensity = store.get("sdoc-density");
    if (savedDensity && densitySel) { densitySel.value = savedDensity; }
    applyDensity();

    function perPage() {
      var v = sizeSel ? parseInt(sizeSel.value || sizeSel.options[sizeSel.selectedIndex].text, 10) : 50;
      return isNaN(v) ? 50 : v;
    }
    function applyDensity() {
      if (!densitySel) return;
      table.classList.toggle("dense", densitySel.value === "dense");
    }
    function render() {
      var size = perPage();
      var pages = Math.max(1, Math.ceil(matched.length / size));
      if (page >= pages) page = pages - 1;
      if (page < 0) page = 0;
      var from = page * size;
      var to = from + size;

      all.forEach(function (tr) { tr.hidden = true; });
      matched.slice(from, to).forEach(function (tr) { tr.hidden = false; });

      if (info) {
        info.textContent = matched.length
          ? "Showing " + (from + 1) + "–" + Math.min(to, matched.length) + " of " + matched.length
          : "Nothing matches";
      }
      if (prev) prev.disabled = page === 0;
      if (next) next.disabled = page >= pages - 1;
      if (pager) pager.hidden = matched.length <= size && page === 0;
    }
    function filter() {
      var q = search ? search.value.trim().toLowerCase() : "";
      matched = q ? all.filter(function (tr) { return tr._hay.indexOf(q) !== -1; }) : all.slice();
      page = 0;
      if (countOut) {
        countOut.textContent = q
          ? matched.length + " of " + all.length + " rows match “" + q + "”"
          : "";
      }
      render();
    }

    if (search) {
      var timer = null;
      search.addEventListener("input", function () {
        clearTimeout(timer);
        timer = setTimeout(filter, 120);
      });
    }
    if (sizeSel) sizeSel.addEventListener("change", function () { store.set("sdoc-page-size", sizeSel.value); page = 0; render(); });
    if (densitySel) densitySel.addEventListener("change", function () { store.set("sdoc-density", densitySel.value); applyDensity(); });
    if (prev) prev.addEventListener("click", function () { page--; render(); table.scrollIntoView({ block: "start" }); });
    if (next) next.addEventListener("click", function () { page++; render(); table.scrollIntoView({ block: "start" }); });

    /* sorting — stable, over the rows already in the DOM */
    var headers = table.tHead ? Array.prototype.slice.call(table.tHead.rows[0].cells) : [];
    headers.forEach(function (th, index) {
      if (!th.getAttribute("data-key")) return;
      th.classList.add("sortable");
      th.tabIndex = 0;
      th.setAttribute("role", "button");
      var activate = function () {
        var dir = th.getAttribute("aria-sort") === "ascending" ? -1 : 1;
        headers.forEach(function (h) { h.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", dir === 1 ? "ascending" : "descending");
        var keyed = all.map(function (tr, i) {
          var cell = tr.cells[index];
          return { tr: tr, i: i, k: (cell ? cell.textContent : "").trim().toLowerCase() };
        });
        keyed.sort(function (a, b) {
          if (a.k === b.k) return a.i - b.i;          // stable
          return a.k > b.k ? dir : -dir;
        });
        var frag = document.createDocumentFragment();
        keyed.forEach(function (o) { frag.appendChild(o.tr); });
        tbody.appendChild(frag);
        all = keyed.map(function (o) { return o.tr; });
        filter();
      };
      th.addEventListener("click", activate);
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); }
      });
    });

    filter();
  })();

  /* ------------------------------------------------------------ review queue chips */
  (function queue() {
    var bar = document.getElementById("reason-filter");
    var list = document.getElementById("queue");
    if (!bar || !list) return;
    bar.hidden = false;
    var out = document.getElementById("reason-count");
    var items = Array.prototype.slice.call(list.children);
    var buttons = Array.prototype.slice.call(bar.querySelectorAll("button[data-reason]"));
    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var want = btn.getAttribute("data-reason");
        buttons.forEach(function (b) {
          var on = b === btn;
          b.setAttribute("aria-pressed", on ? "true" : "false");
          b.classList.toggle("ghost", !on);
        });
        var shown = 0;
        items.forEach(function (li) {
          var hit = !want || li.getAttribute("data-reason") === want;
          li.hidden = !hit;
          if (hit) shown++;
        });
        if (out) out.textContent = shown + " case" + (shown === 1 ? "" : "s") + " shown";
      });
    });
  })();

  /* ------------------------------------------------------------ decision form safety */
  (function decide() {
    var forms = document.querySelectorAll("form[data-decide-form]");
    Array.prototype.forEach.call(forms, function (form) {
      var radios = form.querySelectorAll("input[data-status]");
      var boxes = form.querySelectorAll('input[name="defect_fields"]');
      var defectSet = form.querySelector("[data-defects]");
      var defectErr = form.querySelector("[data-defect-error]");
      var submit = form.querySelector("[data-submit]");
      var hint = form.querySelector("[data-submit-hint]");
      var confirmBox = form.querySelector("[data-confirm]");
      var confirmBody = form.querySelector("[data-confirm-body]");
      var confirmed = false;

      var startStatus = (function () {
        var r = form.querySelector("input[data-status]:checked");
        return r ? r.value : "";
      })();
      var startBoxes = Array.prototype.filter.call(boxes, function (b) { return b.checked; })
        .map(function (b) { return b.value; }).join(",");

      function chosen() {
        var r = form.querySelector("input[data-status]:checked");
        return r ? r.value : "";
      }
      function checkedFields() {
        return Array.prototype.filter.call(boxes, function (b) { return b.checked; }).map(function (b) { return b.value; });
      }
      function syncDefects() {
        var isMismatch = chosen() === "MISMATCH";
        // OK / NEEDS_REVIEW carry no defect fields — matches what the server records.
        Array.prototype.forEach.call(boxes, function (b) {
          b.disabled = !isMismatch;
          if (!isMismatch) b.checked = false;
        });
        if (defectSet) defectSet.style.opacity = isMismatch ? "1" : ".55";
        if (defectErr && !isMismatch) defectErr.hidden = true;
      }
      function dirty() {
        var reviewer = form.querySelector('input[name="resolved_by"]');
        var note = form.querySelector('input[name="note"]');
        return chosen() !== startStatus
          || checkedFields().join(",") !== startBoxes
          || (reviewer && reviewer.value.trim() !== "")
          || (note && note.value.trim() !== "");
      }
      function resetConfirm() {
        confirmed = false;
        if (confirmBox) confirmBox.hidden = true;
        if (submit) submit.textContent = "Save decision";
      }
      function syncSubmit() {
        if (!submit) return;
        var ok = dirty();
        submit.disabled = !ok;
        if (hint) {
          hint.textContent = ok
            ? "Writes to Firestore; the queue and submission.json update immediately."
            : "Change the status, the fields or add your name to enable saving.";
        }
      }

      function row(dl, label, value) {
        var dt = document.createElement("dt");
        dt.textContent = label;
        var dd = document.createElement("dd");
        dd.textContent = value;          // textContent, never innerHTML
        dl.appendChild(dt);
        dl.appendChild(dd);
      }

      form.addEventListener("change", function () { syncDefects(); syncSubmit(); resetConfirm(); });
      form.addEventListener("input", function () { syncSubmit(); resetConfirm(); });

      form.addEventListener("submit", function (ev) {
        var status = chosen();
        var fieldsPicked = checkedFields();

        if (status === "MISMATCH" && fieldsPicked.length === 0) {
          ev.preventDefault();
          if (defectErr) { defectErr.hidden = false; }
          if (boxes.length) boxes[0].focus();
          return;
        }
        if (defectErr) defectErr.hidden = true;

        if (!confirmed) {
          ev.preventDefault();
          if (confirmBody) {
            while (confirmBody.firstChild) confirmBody.removeChild(confirmBody.firstChild);
            var reviewer = form.querySelector('input[name="resolved_by"]');
            var note = form.querySelector('input[name="note"]');
            row(confirmBody, "Status", (startStatus || "none") + "  →  " + status);
            row(confirmBody, "Fields that differ", fieldsPicked.length ? fieldsPicked.join(", ") : "none");
            row(confirmBody, "Reviewer", reviewer && reviewer.value.trim() ? reviewer.value.trim() : "(required)");
            if (note && note.value.trim()) row(confirmBody, "Note", note.value.trim());
          }
          if (confirmBox) {
            confirmBox.hidden = false;
            confirmBox.scrollIntoView({ block: "nearest" });
          }
          confirmed = true;
          if (submit) { submit.textContent = "Confirm and save"; submit.focus(); }
          return;
        }
        // second submit: let the native form post exactly as written in the HTML
      });

      syncDefects();
      syncSubmit();
    });
  })();
})();
