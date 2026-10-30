var config = JSON.parse(document.getElementById('page-config').textContent);

// ─── Rename ───

function editName() {
  document.getElementById('name-display').hidden = true;
  document.getElementById('name-edit').hidden = false;
  document.getElementById('name-input').focus();
  document.getElementById('name-error').textContent = '';
}

function cancelName() {
  document.getElementById('name-edit').hidden = true;
  document.getElementById('name-display').hidden = false;
}

function saveName() {
  var input = document.getElementById('name-input');
  var errEl = document.getElementById('name-error');
  fetch(config.renameAppUrl, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: input.value}),
  })
    .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
    .then(function(res) {
      if (!res.ok) { errEl.textContent = responseErrorMessage(res.data, 'Failed to rename app'); return; }
      // The detail URL is keyed by name, so a rename changes it.
      window.location.href = '/app_detail/' + encodeURIComponent(res.data.name);
    });
}

// ─── Edit git upstream ───

function updateSaveRemoteState() {
  var input = document.getElementById('remote-input');
  document.getElementById('save-remote-btn').disabled = input.value === input.defaultValue;
}

function editRemote() {
  document.getElementById('remote-display').hidden = true;
  document.getElementById('remote-edit').hidden = false;
  var input = document.getElementById('remote-input');
  // Reopen from the saved upstream so Save starts disabled until edited.
  input.value = input.defaultValue;
  document.getElementById('remote-error').textContent = '';
  updateSaveRemoteState();
  input.focus();
}

function cancelRemote() {
  document.getElementById('remote-edit').hidden = true;
  document.getElementById('remote-display').hidden = false;
}

function saveRemote() {
  var input = document.getElementById('remote-input');
  var errEl = document.getElementById('remote-error');
  errEl.textContent = '';
  fetch(config.setAppRemoteUrl, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({repo_url: input.value}),
  })
    .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
    .then(function(res) {
      if (!res.ok) {
        errEl.textContent = responseErrorMessage(res.data, 'Failed to save');
        return;
      }
      // Upstream is saved: collapse the edit form back to the read-only view,
      // then reuse the oauth-aware update flow (may redirect for github auth).
      var displayCode = document.querySelector('#remote-display code');
      if (displayCode) displayCode.textContent = res.data.repo_url;
      input.defaultValue = res.data.repo_url;
      cancelRemote();
      appAction(config.reloadAppUrl, {update: true}, {label: 'Updating & reloading'});
    })
    .catch(function() { errEl.textContent = 'Failed to save'; });
}

// ─── App Actions (stop, reload, remove) ───

function setActionsBusy(label) {
  var container = document.getElementById('app-action-buttons');
  if (!container) return null;
  var buttons = container.querySelectorAll('button');
  buttons.forEach(function(b) { b.disabled = true; });
  var msg = document.getElementById('app-action-msg');
  if (msg) {
    msg.style.color = 'var(--fg-status-warning)';
    msg.textContent = label + '\u2026';
  }
  return function clear(errText) {
    buttons.forEach(function(b) { b.disabled = false; });
    if (msg) {
      if (errText) {
        msg.style.color = 'var(--fg-status-negative)';
        msg.textContent = errText;
      } else {
        msg.textContent = '';
      }
    }
  };
}

function appAction(url, data, opts) {
  // opts: { isRemove?: bool, label?: string }. isRemove navigates to /dashboard
  // on success rather than reloading the (now-gone) detail page.
  opts = opts || {};
  var label = opts.label || (opts.isRemove ? 'Removing' : 'Working');
  var clear = setActionsBusy(label);
  fetch(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data || {}),
  })
    .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
    .then(function(res) {
      // Hand off to full-page review when manifest changes require approval;
      // re-issues reload with approve_new_permissions once approved.
      if (res.ok && res.data && res.data.review_required) {
        if (clear) clear();
        try {
          sessionStorage.setItem('openhost.updateReview.' + config.appId, JSON.stringify({
            settings_changed: res.data.settings_changed || [],
            permissions_required: res.data.permissions_required || [],
          }));
        } catch (e) { /* sessionStorage unavailable; review page shows a fallback */ }
        window.location.href = config.updateReviewUrl;
        return;
      }
      if (!res.ok) {
        var msg = responseErrorMessage(res.data, 'Request failed');
        if (clear) clear(msg);
        alert(msg);
        return;
      }
      if (opts.isRemove) { window.location.href = '/dashboard'; }
      else { location.reload(); }
    })
    .catch(function() {
      if (clear) clear('Request failed');
      alert('Request failed');
    });
}

// ─── Toast ───

function showToast(message, actions) {
    var existing = document.querySelector('.toast');
    if (existing) existing.remove();
    var toast = document.createElement('div');
    toast.className = 'toast';
    var p = document.createElement('p');
    p.textContent = message;
    toast.appendChild(p);
    var actionsDiv = document.createElement('div');
    actionsDiv.className = 'toast-actions';
    actions.forEach(function(a) {
        var btn = document.createElement('button');
        btn.className = 'btn' + (a.primary ? ' btn--primary' : '');
        btn.textContent = a.label;
        btn.onclick = function() { toast.remove(); a.onClick(); };
        actionsDiv.appendChild(btn);
    });
    toast.appendChild(actionsDiv);
    document.body.appendChild(toast);
    return toast;
}

function clearCacheAndReload() {
    showToast('Clearing build cache...', []);
    fetch(config.dropBuildCacheUrl, {method: 'POST', credentials: 'same-origin'})
        .then(readJsonResponse)
        .then(function(res) {
            if (!res.ok) {
                var data = res.data;
                alert('Failed to clear cache: ' + responseErrorMessage(data, 'unknown error'));
                return;
            }
            appAction(config.reloadAppUrl, null, {label: 'Reloading'});
        })
        .catch(function() { alert('Failed to clear cache'); });
}

// ─── Logs & Status Polling ───

(function() {
    var logEl = document.getElementById('app-logs');
    var statusEl = document.getElementById('app-status');
    var appStatus = config.appStatus;
    var nextUrl = config.nextUrl;
    var toastKey = 'cache-toast-shown-' + config.appStatusUrl;

    // The container the stream follows; when it changes we reset to the new logs.
    var streamContainerId = null;

    var MAX_LOG_CHARS = 2 * 1024 * 1024;
    var logPrimed = false;
    // Set when a (re)connect's first message arrives; the next flush replaces the pane's
    // contents in one paint rather than blanking it first (which flashed the scroll to top).
    var needsReset = false;

    // Buffer incoming lines and flush once per animation frame: the backlog arrives
    // one WebSocket message per line, so batching avoids reflowing the <pre> each line.
    var pending = [];
    var pendingLen = 0;
    var flushQueued = false;
    var logLen = 0;

    function isNearBottom(el) {
        return el.scrollHeight - el.scrollTop - el.clientHeight < 50;
    }

    function hasSelectionIn(el) {
        var sel = window.getSelection();
        return !!(sel && !sel.isCollapsed && el.contains(sel.anchorNode));
    }

    function flushLog() {
        flushQueued = false;
        if (!pending.length) return;
        var wasAtBottom = isNearBottom(logEl);
        var text = pending.join('');
        pending = [];
        pendingLen = 0;
        if (needsReset) {
            // Replace the stale contents in the same flush that refills them, so the
            // emptied <pre> never paints on its own and jerks the scroll to the top.
            needsReset = false;
            logEl.textContent = text;
            logLen = text.length;
        } else {
            // A text node leaves existing content and any active selection intact.
            logEl.appendChild(document.createTextNode(text));
            logLen += text.length;
        }
        if (logLen > MAX_LOG_CHARS && !hasSelectionIn(logEl)) {
            var trimmed = logEl.textContent.slice(-MAX_LOG_CHARS);
            logEl.textContent = trimmed;
            logLen = trimmed.length;
        }
        if (wasAtBottom) logEl.scrollTop = logEl.scrollHeight;
    }

    function appendLog(text) {
        if (!text) return;
        pending.push(text);
        pendingLen += text.length;
        // If frames are paused (backgrounded tab) drop all but the most recent MAX_LOG_CHARS.
        while (pending.length > 1 && pendingLen - pending[0].length > MAX_LOG_CHARS) {
            pendingLen -= pending.shift().length;
        }
        if (!flushQueued) {
            flushQueued = true;
            requestAnimationFrame(flushLog);
        }
    }

    // A 4401 close means the session is gone; reconnecting would just loop through
    // handshake/close every 2s, so go re-authenticate and come back here.
    function redirectToLogin() {
        window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname);
    }

    // Tracked so a reset can tell the old socket's onclose not to reconnect.
    var currentWs = null;

    function startLogStream() {
        var proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        var ws = new WebSocket(proto + '//' + window.location.host + config.appLogsStreamUrl);
        currentWs = ws;
        ws.onmessage = function(e) {
            if (ws !== currentWs) return;  // superseded by a reset; don't consume the reprime
            // First message of a (re)connect replays the tail: drop anything buffered from
            // the old socket and let the next flush swap in the fresh contents atomically.
            if (!logPrimed) {
                logPrimed = true;
                needsReset = true;
                pending = []; pendingLen = 0;
            }
            appendLog(e.data + '\n');
        };
        ws.onclose = function(e) {
            if (ws !== currentWs) return;  // superseded by a reset
            if (e.code === 4401) { redirectToLogin(); return; }
            // The server holds the socket open past end-of-log, so a close is a real
            // drop — reconnect (which re-primes to replay the tail).
            logPrimed = false;
            setTimeout(startLogStream, 2000);
        };
        ws.onerror = function() { ws.close(); };
    }

    function resetLogStream() {
        var old = currentWs;
        logPrimed = false;
        startLogStream();  // reassigns currentWs
        if (old && old !== currentWs) old.close();
    }

    function showCacheCorruptToast() {
        if (sessionStorage.getItem(toastKey)) return;
        sessionStorage.setItem(toastKey, '1');
        showToast(
            'Container build cache is corrupted. Clear it and rebuild?',
            [
                { label: 'Clear Cache & Rebuild', primary: true, onClick: clearCacheAndReload },
                { label: 'Dismiss', primary: false, onClick: function() {} }
            ]
        );
    }

    // Disables the action buttons while removing; re-enabled only if teardown
    // fails (status 'error'). A successful teardown 404s and redirects instead.
    var clearRemovingChrome = null;
    function applyRemovingChrome() {
        if (clearRemovingChrome) return;
        clearRemovingChrome = setActionsBusy('Removing');
    }
    function clearRemovingChromeIfApplied(errText) {
        if (!clearRemovingChrome) return;
        clearRemovingChrome(errText || null);
        clearRemovingChrome = null;
    }

    function handleStatus(data) {
        if (!data) return;
        if (data.status !== appStatus) {
            appStatus = data.status;
            statusEl.textContent = appStatus;
            statusEl.className = 'status-value status-' + appStatus;
            // Re-pull history only on a real transition, not on every poll,
            // so a stable status never generates history traffic.
            if (typeof refreshStatusHistory === 'function') refreshStatusHistory();
        }
        // Adopt the first container_id silently; reset only on a later change.
        if (data.container_id && data.container_id !== streamContainerId) {
            if (streamContainerId !== null) resetLogStream();
            streamContainerId = data.container_id;
        }
        if (appStatus === 'removing') {
            applyRemovingChrome();
        } else {
            clearRemovingChromeIfApplied(
                appStatus === 'error' ? (data.error || 'Removal failed') : null
            );
        }
        if (appStatus === 'running' && nextUrl) {
            window.location.href = nextUrl;
        }
        if (appStatus === 'error' && data.error_kind === 'build_cache_corrupt') {
            showCacheCorruptToast();
        }
        var errorRow = document.getElementById('app-error-row');
        var errorCell = document.getElementById('app-error-cell');
        if (errorRow && errorCell) {
            if (appStatus === 'error' && data.error) {
                errorCell.textContent = data.error;
                errorRow.hidden = false;
            } else {
                errorRow.hidden = true;
                errorCell.textContent = '';
            }
        }
    }

    function pollStatus() {
        if (!config.appStatusUrl) return;
        fetch(config.appStatusUrl, { credentials: 'same-origin' })
            .then(function(r) {
                if (r.status === 401) { redirectToLogin(); return null; }
                if (r.status === 404) { window.location.href = '/dashboard'; return null; }
                if (!r.ok) return null;
                return r.json();
            })
            .then(function(data) {
                if (data) handleStatus(data);
            })
            .catch(function() {});
    }

    if (appStatus === 'removing') {
        applyRemovingChrome();
    }

    logEl.scrollTop = logEl.scrollHeight;

    // Stream regardless of status: a stopped/errored app still has a build log to replay.
    startLogStream();

    pollStatus();
    setInterval(pollStatus, 1000);
})();

// Grey out Save until the git upstream input actually differs from what's saved.
var remoteInput = document.getElementById('remote-input');
if (remoteInput) remoteInput.addEventListener('input', updateSaveRemoteState);

// ─── Status history ───
//
// Fetched once on load and again only when the 1s status poll above observes a
// real transition, so polling a stable app never generates history traffic.

var STATUS_SOURCE_LABELS = {
  install: 'App install',
  reload: 'Manual reload',
  update: 'Update and reload',
  owner_stop: 'Stopped by owner',
  owner_remove: 'Removal requested',
  startup_recovery: 'Startup recovery',
  storage_guard: 'Storage guard',
  primary_domain_promotion: 'Primary domain change',
  rename_restart: 'Restart after rename',
  archive_migration: 'Archive storage migration',
  system: 'System'
};

function historySourceLabel(source) {
  return STATUS_SOURCE_LABELS[source] || source;
}

// changed_at is stored as UTC 'YYYY-MM-DD HH:MM:SS' (SQLite datetime('now')).
function historyRelativeTime(value) {
  var when = new Date(value.replace(' ', 'T') + 'Z');
  if (isNaN(when.getTime())) return value;
  var future = Date.now() - when.getTime() < 0;
  var seconds = Math.abs(Math.round((Date.now() - when.getTime()) / 1000));
  if (seconds < 45) return 'just now';
  var amount;
  var minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    amount = minutes + ' minute' + (minutes === 1 ? '' : 's');
  } else {
    var hours = Math.round(minutes / 60);
    if (hours < 48) {
      amount = hours + ' hour' + (hours === 1 ? '' : 's');
    } else {
      var days = Math.round(hours / 24);
      if (days < 30) {
        amount = days + ' day' + (days === 1 ? '' : 's');
      } else {
        return when.toISOString().slice(0, 10);
      }
    }
  }
  return future ? 'in ' + amount : amount + ' ago';
}

function historyStatusSpan(status) {
  var span = document.createElement('span');
  span.className = 'status-value status-' + status;
  span.textContent = status;
  return span;
}

function historyEventRow(event) {
  var li = document.createElement('li');
  li.className = 'status-history__item';

  var transition = document.createElement('span');
  transition.className = 'status-history__transition';
  if (event.from_status) {
    transition.appendChild(historyStatusSpan(event.from_status));
    transition.appendChild(document.createTextNode(' \u2192 '));
  } else {
    var created = document.createElement('span');
    created.className = 'status-history__birth';
    created.textContent = 'created';
    transition.appendChild(created);
    transition.appendChild(document.createTextNode(' \u2192 '));
  }
  transition.appendChild(historyStatusSpan(event.to_status));
  li.appendChild(transition);

  var meta = document.createElement('span');
  meta.className = 'status-history__meta';
  var time = document.createElement('time');
  time.dateTime = event.changed_at.replace(' ', 'T') + 'Z';
  time.title = event.changed_at + ' UTC';
  time.textContent = historyRelativeTime(event.changed_at);
  meta.appendChild(time);
  meta.appendChild(document.createTextNode(' \u00b7 via ' + historySourceLabel(event.source)));
  li.appendChild(meta);

  if (event.error_message) {
    var err = document.createElement('p');
    err.className = 'status-history__error';
    err.textContent = event.error_message;
    li.appendChild(err);
  }
  return li;
}

function renderStatusHistory(data) {
  var panel = document.getElementById('app-status-history');
  if (!panel || !data) return;
  var loadingEl = document.getElementById('status-history-loading');
  var cluesEl = document.getElementById('status-history-clues');
  var lastEl = document.getElementById('status-history-last');
  var emptyEl = document.getElementById('status-history-empty');
  var listEl = document.getElementById('status-history-list');
  var truncatedEl = document.getElementById('status-history-truncated');
  if (loadingEl) loadingEl.hidden = true;

  var events = data.events || [];
  var clues = data.failure_clues || {};

  if (cluesEl) {
    if (clues.error_count >= 2) {
      var clueText = 'This app has failed ' + clues.error_count + ' times in the recent history.';
      if (clues.last_error_at) clueText += ' Last failure: ' + historyRelativeTime(clues.last_error_at) + '.';
      if (clues.attempts_since_last_running > 0) {
        clueText += ' ' + clues.attempts_since_last_running + ' start attempt'
          + (clues.attempts_since_last_running === 1 ? '' : 's') + ' since it last ran successfully.';
      }
      cluesEl.textContent = clueText;
      cluesEl.hidden = false;
    } else {
      cluesEl.textContent = '';
      cluesEl.hidden = true;
    }
  }

  if (events.length === 0) {
    if (emptyEl) emptyEl.hidden = false;
    if (lastEl) lastEl.hidden = true;
    if (listEl) { listEl.hidden = true; listEl.textContent = ''; }
  } else {
    if (emptyEl) emptyEl.hidden = true;

    var latest = events[events.length - 1];
    if (lastEl) {
      lastEl.textContent = '';
      lastEl.appendChild(document.createTextNode('Last change: '));
      if (latest.from_status) {
        lastEl.appendChild(historyStatusSpan(latest.from_status));
        lastEl.appendChild(document.createTextNode(' \u2192 '));
      }
      lastEl.appendChild(historyStatusSpan(latest.to_status));
      lastEl.appendChild(
        document.createTextNode(', ' + historyRelativeTime(latest.changed_at) + ', via ' + historySourceLabel(latest.source))
      );
      lastEl.hidden = false;
    }

    if (listEl) {
      listEl.textContent = '';
      // API serves oldest to newest; the timeline reads bottom-up, newest last.
      events.forEach(function(event) { listEl.appendChild(historyEventRow(event)); });
      listEl.hidden = false;
    }
  }

  if (truncatedEl) truncatedEl.hidden = !data.truncated;
}

function refreshStatusHistory() {
  if (!config.appStatusHistoryUrl) return;
  fetch(config.appStatusHistoryUrl, {credentials: 'same-origin'})
    .then(function(r) {
      if (r.status === 401) {
        window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname);
        return null;
      }
      if (r.status === 404) { window.location.href = '/dashboard'; return null; }
      if (!r.ok) return null;
      return r.json();
    })
    .then(function(data) { renderStatusHistory(data); })
    .catch(function() {});
}

refreshStatusHistory();
