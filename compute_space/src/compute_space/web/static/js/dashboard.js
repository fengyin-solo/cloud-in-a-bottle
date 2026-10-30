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

// Tags selected in the multi-select chip filter (AND: an app must carry
// every selected tag). Server-rendered chips are re-bound on each poll so
// selections survive tag-vocabulary changes.
var selectedTags = {};

function rowTags(row) {
  // data-tags is a JSON array rendered server-side and refreshed on poll.
  try {
    var tags = JSON.parse(row.getAttribute('data-tags') || '[]');
    return Array.isArray(tags) ? tags : [];
  } catch (e) {
    return [];
  }
}

function syncTagFilterChips(apps) {
  // Rebuild the chip row from the union of every live app's tags, keeping
  // pressed state for tags that still exist. Tags that no app carries drop
  // out of the filter (and out of selectedTags).
  var container = document.getElementById('tag-filters');
  if (!container) return;
  var vocabulary = {};
  apps.forEach(function(a) {
    (a.tags || []).forEach(function(t) { vocabulary[t] = true; });
  });
  Object.keys(selectedTags).forEach(function(t) {
    if (!vocabulary[t]) delete selectedTags[t];
  });

  var existing = {};
  container.querySelectorAll('.tag-chip--toggle').forEach(function(chip) {
    existing[chip.getAttribute('data-tag')] = chip;
  });

  // Remove chips whose tag vanished.
  Object.keys(existing).forEach(function(tag) {
    if (!vocabulary[tag]) existing[tag].remove();
  });

  // Add chips for newly seen tags, in alphabetical order.
  Object.keys(vocabulary).sort().forEach(function(tag) {
    if (existing[tag]) return;
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'tag-chip tag-chip--toggle';
    btn.setAttribute('data-tag', tag);
    btn.textContent = tag;
    btn.setAttribute('aria-pressed', selectedTags[tag] ? 'true' : 'false');
    btn.addEventListener('click', function() { toggleTagFilter(tag); });
    container.appendChild(btn);
  });
}

function toggleTagFilter(tag) {
  if (selectedTags[tag]) {
    delete selectedTags[tag];
  } else {
    selectedTags[tag] = true;
  }
  var chip = document.querySelector('#tag-filters .tag-chip--toggle[data-tag="' + tag + '"]');
  if (chip) chip.setAttribute('aria-pressed', selectedTags[tag] ? 'true' : 'false');
  applyFilter();
}

function updateApps(apps) {
  // /api/apps returns a list of {app_id, name, status, error_message, tags}.
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
    if (Array.isArray(info.tags)) row.setAttribute('data-tags', JSON.stringify(info.tags));
  });
  syncTagFilterChips(apps);
  applyFilter();
}

// ─── Name + tag filter ───

// Visibility has three independent inputs — whether the app still exists
// (data-gone, set by the poll loop), the name substring, and the selected
// tag set — so all are resolved here rather than each writing `hidden`.
function applyFilter() {
  var input = document.getElementById('app-filter');
  var term = input ? input.value.trim().toLowerCase() : '';
  var activeTags = Object.keys(selectedTags);
  var filtering = !!(term || activeTags.length);
  var matches = 0;
  document.querySelectorAll('.app-row[data-app-name]').forEach(function(row) {
    var nameHit = !term || row.getAttribute('data-app-name').toLowerCase().indexOf(term) !== -1;
    var tags = rowTags(row);
    var tagHit = activeTags.every(function(t) { return tags.indexOf(t) !== -1; });
    row.hidden = !nameHit || !tagHit || row.dataset.gone === '1';
    if (!row.hidden) matches++;
  });
  var empty = document.getElementById('no-matches');
  if (empty) empty.hidden = !filtering || matches > 0;
}

var filterInput = document.getElementById('app-filter');
if (filterInput) filterInput.addEventListener('input', applyFilter);
document.querySelectorAll('#tag-filters .tag-chip--toggle').forEach(function(chip) {
  chip.addEventListener('click', function() {
    toggleTagFilter(chip.getAttribute('data-tag'));
  });
});

if (config.apiAppsUrl) {
  refreshApps();
  setInterval(refreshApps, 3000);
}
