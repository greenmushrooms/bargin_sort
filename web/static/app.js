/* The only client-side code in the project, and deliberately this small.
   htmx does the fetching and swapping; these four functions exist because a
   filter lives in a hidden input that several unrelated controls need to set. */

/** Point the list at one wishlist row (or all of them, for ""). */
function selectGroup(slug) {
  setFilter('slug', slug);
  document.querySelectorAll('.wl-group').forEach(function (el) {
    el.classList.toggle('active', (el.dataset.slug || '') === slug);
  });
}

/** Filter the list by verdict. "" is every verdict, "unread" is no row yet. */
function setStatus(status) {
  setFilter('status', status);
  document.querySelectorAll('.chips .chip').forEach(function (el) {
    el.classList.toggle('on', el.getAttribute('onclick') === "setStatus('" + status + "')");
  });
}

/**
 * Write one field of the filter form and ask the list to reload.
 *
 * The event goes to body rather than to the list directly because the sidebar
 * listens for its own refreshes on the same bus, and a filter change moves the
 * "active" highlight there too.
 */
function setFilter(name, value) {
  var form = document.getElementById('filter-form');
  form.querySelector('[name=' + name + ']').value = value;
  htmx.trigger(document.body, 'filter-changed');
}

/** Show which row the detail pane belongs to. */
function markSelected(row) {
  document.querySelectorAll('.lot-row.selected').forEach(function (el) {
    el.classList.remove('selected');
  });
  row.classList.add('selected');
}

/*
 * On a phone the three panes stack, so the detail pane sits below the list and
 * a tap loads a lot the user cannot see. Bring it into view — but only when the
 * layout has actually collapsed; on a desktop the pane is already beside the
 * list and scrolling would be a jolt for no reason.
 */
document.body.addEventListener('htmx:afterSwap', function (e) {
  if (e.target.id !== 'detail') return;
  if (!window.matchMedia('(max-width: 820px)').matches) return;
  e.target.scrollIntoView({ behavior: 'smooth', block: 'start' });
});

/* A failed fetch must say so. Without this an htmx error is a click that
   silently does nothing, which is indistinguishable from a slow one. */
document.body.addEventListener('htmx:responseError', function (e) {
  showError('Server said ' + e.detail.xhr.status + ' for ' + e.detail.requestConfig.path);
});
document.body.addEventListener('htmx:sendError', function () {
  showError('Could not reach the server — is uvicorn still running?');
});

function showError(message) {
  var banner = document.getElementById('error-banner');
  if (!banner) return;
  banner.querySelector('.eb-msg').textContent = message;
  banner.hidden = false;
}
