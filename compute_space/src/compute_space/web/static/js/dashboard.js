var config = JSON.parse(document.getElementById('page-config').textContent);

// ─── App List ───
//
// Keep the server-rendered status in sync. Apps can be opened during startup;
// their own URL shows a waiting page. Deleted apps stay hidden until reload.

function refreshApps() {
  if (!config.apiAppsUrl) return;
  fetch(config.apiAppsUrl)
    .then(function(r) { return r.json(); })
    .then(updateApps)
    .catch(function() {});
}

function updateApps(apps) {
  // /api/apps returns a list of {app_id, name, status, error_message}.
  var byId = {};
  apps.forEach(function(a) { byId[a.app_id] = a; });
  document.querySelectorAll('.app-row[data-app-id]').forEach(function(row) {
    var info = byId[row.getAttribute('data-app-id')];
    if (!info) {
      row.dataset.gone = '1';
      return;
    }
    delete row.dataset.gone;
    // The dot colour is driven off data-status in CSS, so this one assignment
    // both restyles the row and records the state for anything else reading it.
    row.dataset.status = info.status;
    var statusEl = row.querySelector('.app-row__status');
    if (statusEl) statusEl.textContent = info.status;
  });
  applyFilter();
  syncBatchBar();
}

// ─── Name filter ───

// Visibility has two independent inputs — whether the app still exists
// (data-gone, set by the poll loop) and whether it matches the filter — so
// both are resolved here rather than each writing `hidden` on its own.
function applyFilter() {
  var input = document.getElementById('app-filter');
  var term = input ? input.value.trim().toLowerCase() : '';
  var matches = 0;
  document.querySelectorAll('.app-row[data-app-name]').forEach(function(row) {
    var hit = !term || row.getAttribute('data-app-name').toLowerCase().indexOf(term) !== -1;
    row.hidden = !hit || row.dataset.gone === '1';
    if (!row.hidden) matches++;
  });
  var empty = document.getElementById('no-matches');
  if (empty) empty.hidden = !term || matches > 0;
  syncBatchBar();
}

var filterInput = document.getElementById('app-filter');
if (filterInput) filterInput.addEventListener('input', applyFilter);

if (config.apiAppsUrl) {
  refreshApps();
  setInterval(refreshApps, 3000);
}

// ─── Batch stop / reload ───
//
// The owner picks several rows, confirms one dangerous action for all of
// them, and gets an independent accepted/skipped/error line per app. The
// server processes the same lifecycle transitions as the single-app
// /stop_app and /reload_app routes; reload "accepted" only means the rebuild
// was queued — its progress arrives through the status poll above.

var batchBar = document.getElementById('batch-bar');
var batchResults = document.getElementById('batch-results');
var batchStopBtn = document.getElementById('batch-stop');
var batchReloadBtn = document.getElementById('batch-reload');
var batchSelectAll = document.getElementById('batch-select-all');
var batchCount = document.getElementById('batch-count');
var batchBusy = false;

function appRows() {
  return Array.prototype.slice.call(document.querySelectorAll('.app-row[data-app-id]'));
}

// Only rows currently visible (matching the filter, not deleted mid-session)
// participate in a batch, so "Select all" while filtering never acts on apps
// the owner isn't looking at.
function selectableRows() {
  return appRows().filter(function(row) { return !row.hidden && row.dataset.gone !== '1'; });
}

function selectedRows() {
  return selectableRows().filter(function(row) {
    var cb = row.querySelector('.app-row__select');
    return cb && cb.checked;
  });
}

function syncBatchBar() {
  if (!batchBar) return;
  var selected = selectedRows();
  var selectable = selectableRows();
  if (batchCount) {
    batchCount.textContent = selected.length + ' selected';
  }
  if (batchStopBtn) batchStopBtn.disabled = batchBusy || selected.length === 0;
  if (batchReloadBtn) batchReloadBtn.disabled = batchBusy || selected.length === 0;
  if (batchSelectAll) {
    var allChecked = selectable.length > 0 &&
      selectable.every(function(row) { return row.querySelector('.app-row__select').checked; });
    batchSelectAll.checked = allChecked;
    batchSelectAll.indeterminate = !allChecked && selected.length > 0;
    batchSelectAll.disabled = batchBusy || selectable.length === 0;
  }
}

function setBatchBusy(busy, action) {
  batchBusy = busy;
  document.querySelectorAll('.app-row__select').forEach(function(cb) { cb.disabled = busy; });
  if (batchStopBtn) batchStopBtn.textContent = busy && action === 'stop' ? 'Stopping…' : 'Stop selected';
  if (batchReloadBtn) batchReloadBtn.textContent = busy && action === 'reload' ? 'Reloading…' : 'Reload selected';
  syncBatchBar();
}

function clearSelection() {
  document.querySelectorAll('.app-row__select').forEach(function(cb) { cb.checked = false; });
  syncBatchBar();
}

function renderBatchResults(action, data, nameById) {
  if (!batchResults) return;
  batchResults.textContent = '';

  var summary = document.createElement('p');
  summary.className = 'batch-results__summary';
  var counts = [data.accepted + ' accepted'];
  if (data.skipped) counts.push(data.skipped + ' skipped');
  if (data.errors) counts.push(data.errors + (data.errors === 1 ? ' error' : ' errors'));
  summary.textContent = (action === 'stop' ? 'Stop' : 'Reload') + ': ' + counts.join(', ');
  batchResults.appendChild(summary);

  var list = document.createElement('ul');
  data.results.forEach(function(item) {
    var li = document.createElement('li');
    li.className = 'batch-results__item';

    var name = document.createElement('b');
    name.textContent = nameById[item.app_id] || item.app_id;

    var status = document.createElement('span');
    status.className = 'batch-results__status batch-results__status--' + item.result;
    var label;
    if (item.result === 'accepted') {
      label = action === 'stop' ? 'Stopped' : 'Reload started';
    } else {
      label = (item.result === 'skipped' ? 'Skipped' : 'Error') + (item.reason ? ': ' + item.reason : '');
    }
    status.textContent = label;

    li.appendChild(name);
    li.appendChild(document.createTextNode(' — '));
    li.appendChild(status);
    list.appendChild(li);
  });
  batchResults.appendChild(list);

  var dismiss = document.createElement('button');
  dismiss.className = 'btn batch-results__dismiss';
  dismiss.type = 'button';
  dismiss.textContent = 'Dismiss';
  dismiss.onclick = function() {
    batchResults.hidden = true;
    batchResults.textContent = '';
  };
  batchResults.appendChild(dismiss);
  batchResults.hidden = false;
}

function runBatch(action) {
  if (!config.batchAppsUrl || batchBusy) return;
  var rows = selectedRows();
  if (!rows.length) return;

  var names = rows.map(function(row) { return row.getAttribute('data-app-name'); });
  var preview = names.slice(0, 10).join(', ') + (names.length > 10 ? ' …' : '');
  var noun = names.length === 1 ? 'app' : 'apps';
  var message;
  if (action === 'stop') {
    message = 'Stop ' + names.length + ' selected ' + noun + '?\n\n' + preview +
      '\n\nTheir containers will shut down; data and images are kept.';
  } else {
    message = 'Reload ' + names.length + ' selected ' + noun + '?\n\n' + preview +
      '\n\nEach app will rebuild and restart from the code already on disk.';
  }
  if (!window.confirm(message)) return;

  var nameById = {};
  var ids = rows.map(function(row) {
    var id = row.getAttribute('data-app-id');
    nameById[id] = row.getAttribute('data-app-name');
    return id;
  });

  setBatchBusy(true, action);
  fetch(config.batchAppsUrl, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: action, app_ids: ids}),
  })
    .then(readJsonResponse)
    .then(function(res) {
      if (!res.ok) {
        // Envelope rejection (400: empty/oversized/unknown action): nothing
        // was attempted. Keep the selection so the owner can fix and retry.
        window.alert(responseErrorMessage(res.data, 'Batch request failed'));
        return;
      }
      renderBatchResults(action, res.data, nameById);
      clearSelection();
      // Accepted reloads flip rows to 'building' immediately; pull fresh
      // status now rather than waiting up to 3s for the next poll.
      refreshApps();
    })
    .catch(function() {
      window.alert('Batch request failed');
    })
    .then(function() { setBatchBusy(false); }, function() { setBatchBusy(false); });
}

if (batchBar) {
  // Reveal the selection UI only when JS is running.
  document.body.classList.add('js-batch');

  var appList = document.getElementById('app-list');
  if (appList) {
    appList.addEventListener('change', function(event) {
      if (event.target.classList && event.target.classList.contains('app-row__select')) {
        syncBatchBar();
      }
    });
  }
  if (batchSelectAll) {
    batchSelectAll.addEventListener('change', function() {
      selectableRows().forEach(function(row) {
        row.querySelector('.app-row__select').checked = batchSelectAll.checked;
      });
      syncBatchBar();
    });
  }
  if (batchStopBtn) batchStopBtn.addEventListener('click', function() { runBatch('stop'); });
  if (batchReloadBtn) batchReloadBtn.addEventListener('click', function() { runBatch('reload'); });
  syncBatchBar();
}
