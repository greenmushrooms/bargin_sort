/* Selection and navigation for the triage list.
 *
 * Tap a row to open it in the detail pane. Hold a row to enter selection mode,
 * the way Outlook and Gmail do it: the held row is taken with you, and every
 * tap after that toggles. The verdict buttons live in the fixed top bar and
 * act on whatever is selected.
 *
 * The checkboxes are real inputs that sit hidden in the DOM at all times, so
 * the top bar posts the form through htmx's hx-include and the server sees an
 * ordinary form submission either way.
 */
(function () {
  "use strict";

  // Long enough not to fire while a thumb is starting a scroll, short enough
  // not to feel broken. A press that wanders further than SLOP is a scroll.
  var HOLD_MS = 500, SLOP_PX = 10;

  var timer = null, startX = 0, startY = 0, longFired = false;

  // The row a range is measured from: the last one clicked without shift.
  // Outlook keeps this anchor put while you shift-click around it, so the
  // range can be widened and narrowed without re-picking the start.
  var anchor = null;

  // Event targets are not always elements -- a click can land on a text node
  // or the document itself, and `.closest` on those throws, which would take
  // every row interaction down with it and look exactly like "the JS is dead".
  function near(target, sel) {
    var el = target;
    if (el && el.nodeType === 3) el = el.parentElement;
    return el && el.nodeType === 1 && el.closest ? el.closest(sel) : null;
  }

  function root()  { return document.querySelector("[data-select-root]"); }
  function bar()   { return document.getElementById("actionbar"); }
  function boxes() {
    var r = root();
    return r ? Array.prototype.slice.call(r.querySelectorAll('input[name="lot"]')) : [];
  }
  function selecting() { var r = root(); return !!r && r.classList.contains("selecting"); }

  function rows() {
    var r = root();
    return r ? Array.prototype.slice.call(r.querySelectorAll(".trow")) : [];
  }

  function indexOf(row) { return rows().indexOf(row); }

  // Shift-click selects everything between the anchor and the clicked row,
  // inclusive. It only ever adds: in Outlook a range extends the selection
  // rather than replacing it, so a range picked after other rows keeps them.
  function selectRange(row) {
    var all = rows(), to = all.indexOf(row);
    if (to < 0) return;
    var from = (anchor === null || anchor >= all.length) ? to : anchor;
    var lo = Math.min(from, to), hi = Math.max(from, to);
    for (var i = lo; i <= hi; i++) {
      var box = all[i].querySelector('input[name="lot"]');
      if (box) box.checked = true;
    }
    sync();
  }

  function sync() {
    var r = root(), b = bar();
    if (!b) return;
    var n = r ? boxes().filter(function (x) { return x.checked; }).length : 0;

    var label = b.querySelector("[data-count]");
    if (label) label.textContent = n;
    b.setAttribute("data-empty", n === 0 ? "true" : "false");

    boxes().forEach(function (x) {
      var row = x.closest(".trow");
      if (row) row.classList.toggle("picked", x.checked);
    });

    // An empty selection is not a mode worth being in -- the bar would just be
    // a row of controls that do nothing.
    if (selecting() && n === 0) exit();
  }

  function enter(row) {
    var r = root(); if (!r) return;
    r.classList.add("selecting");
    var b = row && row.querySelector('input[name="lot"]');
    if (b) b.checked = true;
    if (navigator.vibrate) { try { navigator.vibrate(12); } catch (e) {} }
    sync();
  }

  function exit() {
    anchor = null;
    var r = root();
    if (r) {
      r.classList.remove("selecting");
      boxes().forEach(function (x) { x.checked = false; });
      r.querySelectorAll(".trow.picked").forEach(function (t) { t.classList.remove("picked"); });
    }
    var b = bar();
    if (b) {
      b.setAttribute("data-empty", "true");
      var label = b.querySelector("[data-count]");
      if (label) label.textContent = "0";
    }
  }

  function toggle(row) {
    var b = row.querySelector('input[name="lot"]');
    if (!b) return;
    b.checked = !b.checked;
    sync();
  }

  function open(row) {
    document.querySelectorAll(".trow.current").forEach(function (t) { t.classList.remove("current"); });
    row.classList.add("current");
    var pane = document.querySelector(".pane-detail");
    // Below the breakpoint the detail pane is hidden until it holds something,
    // so the list keeps the full width until there is a reason not to.
    if (pane) pane.classList.add("open");
    if (row.dataset.href && window.htmx) {
      window.htmx.ajax("GET", row.dataset.href, { target: "#detail", swap: "innerHTML" });
    }
  }

  function cancelHold() { if (timer) { clearTimeout(timer); timer = null; } }

  document.addEventListener("pointerdown", function (e) {
    var row = near(e.target, ".trow");
    if (!row) return;
    longFired = false;
    startX = e.clientX; startY = e.clientY;
    timer = setTimeout(function () {
      timer = null; longFired = true;
      anchor = indexOf(row);
      if (!selecting()) enter(row); else toggle(row);
    }, HOLD_MS);
  });

  document.addEventListener("pointermove", function (e) {
    if (!timer) return;
    if (Math.abs(e.clientX - startX) > SLOP_PX || Math.abs(e.clientY - startY) > SLOP_PX) cancelHold();
  });

  ["pointerup", "pointercancel", "pointerleave"].forEach(function (ev) {
    document.addEventListener(ev, cancelHold);
  });

  document.addEventListener("click", function (e) {
    // The click that trails a completed hold belongs to that gesture, not to a
    // new one -- letting it through would toggle the row straight back off.
    if (longFired) { longFired = false; e.preventDefault(); e.stopPropagation(); return; }

    if (near(e.target, "[data-select-cancel]")) { exit(); return; }
    if (near(e.target, "a, button, input, select")) return;

    var row = near(e.target, ".trow");
    if (!row) return;

    if (e.shiftKey) {
      // A range implies selection, so shift-click turns the mode on rather
      // than needing a long press first -- the same as clicking into a mail
      // list and shift-clicking down it.
      e.preventDefault();
      var r = root();
      if (r) r.classList.add("selecting");
      selectRange(row);
      return;
    }

    if (selecting()) { e.preventDefault(); anchor = indexOf(row); toggle(row); return; }
    anchor = indexOf(row);
    open(row);
  });

  // A held press otherwise raises the OS text-selection menu on touch, landing
  // on top of the mode the hold just entered.
  document.addEventListener("contextmenu", function (e) {
    if (near(e.target, ".trow")) e.preventDefault();
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && selecting()) exit();
  });

  // htmx replaces the list after a bulk action, taking the mode with it. That
  // is right: the verdicts were applied, so the selection is spent.
  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.target && e.target.id === "lots") exit(); else sync();
  });

  sync();
})();

/* Pane splitter + overlay measurement.
 * Kept in its own IIFE so a failure here cannot take row selection with it. */
(function () {
  "use strict";

  // The overlay bar is out of flow, so the panes need to know how tall it is.
  // Measured rather than hard-coded because the bar wraps to more rows on a
  // narrow window, and a stale number would hide the top of the list.
  var bar = document.querySelector(".topbar");
  function measure() {
    if (!bar) return;
    document.documentElement.style.setProperty("--topbar-h", bar.offsetHeight + "px");
  }
  measure();
  if (window.ResizeObserver && bar) new ResizeObserver(measure).observe(bar);
  window.addEventListener("resize", measure);

  var split = document.getElementById("splitter");
  var panes = document.querySelector(".panes");
  if (!split || !panes) return;

  var KEY = "bargin.listWidth";
  // Never let a drag collapse a pane to nothing -- a pane you cannot see is a
  // pane you cannot drag back.
  var MIN = 22, MAX = 78;

  function apply(pct) {
    pct = Math.max(MIN, Math.min(MAX, pct));
    document.documentElement.style.setProperty("--list-w", pct + "%");
    return pct;
  }

  try {
    var saved = parseFloat(localStorage.getItem(KEY));
    if (!isNaN(saved)) apply(saved);
  } catch (e) { /* private mode, or storage blocked -- the default is fine */ }

  var dragging = false;

  function move(e) {
    if (!dragging) return;
    var box = panes.getBoundingClientRect();
    apply(((e.clientX - box.left) / box.width) * 100);
  }

  function stop() {
    if (!dragging) return;
    dragging = false;
    split.classList.remove("dragging");
    document.body.classList.remove("resizing");
    try {
      var cur = document.documentElement.style.getPropertyValue("--list-w");
      if (cur) localStorage.setItem(KEY, parseFloat(cur));
    } catch (e) {}
  }

  split.addEventListener("pointerdown", function (e) {
    dragging = true;
    split.classList.add("dragging");
    document.body.classList.add("resizing");
    // Capture so the drag survives the pointer leaving the 10px handle, which
    // it will immediately.
    if (split.setPointerCapture) { try { split.setPointerCapture(e.pointerId); } catch (err) {} }
    e.preventDefault();
  });
  split.addEventListener("pointermove", move);
  split.addEventListener("pointerup", stop);
  split.addEventListener("pointercancel", stop);
  window.addEventListener("pointerup", stop);

  // Keyboard, because a drag handle that only takes a mouse is not reachable.
  split.addEventListener("keydown", function (e) {
    var cur = parseFloat(document.documentElement.style.getPropertyValue("--list-w")) || 52;
    if (e.key === "ArrowLeft") { apply(cur - 2); }
    else if (e.key === "ArrowRight") { apply(cur + 2); }
    else return;
    e.preventDefault();
    stop();
  });
})();
