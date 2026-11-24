(() => {
    const CACHE_PREFIX = 'sb_api_cache_v2:';
    const CLIENT_QUEUE_KEY = 'sb_client_pending_ops_v1';
    const nativeFetch = window.fetch.bind(window);

    // Purge any legacy v1 browser cache entries so zero old pre-restart demo responses remain
    try {
        Object.keys(localStorage).forEach((k) => {
            if (k.startsWith('sb_api_cache_v1:')) localStorage.removeItem(k);
        });
    } catch {}

    let currentState = {
        status: navigator.onLine ? 'online' : 'offline',
        online: navigator.onLine,
        syncing: false,
        pending_count: 0,
        failed_count: 0,
        unsynced_total: 0,
        cached_counts: {},
        recent_ops: [],
        last_synced_at: null,
        last_sync_error: null
    };
    let wasOffline = !navigator.onLine;
    let syncInFlight = false;
    let initialWarmupDone = false;

    const readClientQueue = () => {
        try {
            const raw = localStorage.getItem(CLIENT_QUEUE_KEY);
            const parsed = raw ? JSON.parse(raw) : [];
            return Array.isArray(parsed) ? parsed : [];
        } catch {
            return [];
        }
    };

    const writeClientQueue = (ops) => {
        try {
            localStorage.setItem(CLIENT_QUEUE_KEY, JSON.stringify(ops));
        } catch {
            // Ignore quota errors
        }
    };

    const cacheApiResponse = (path, payload) => {
        try {
            localStorage.setItem(CACHE_PREFIX + path, JSON.stringify({
                saved_at: new Date().toISOString(),
                payload
            }));
        } catch {
            // Ignore storage quota issues
        }
    };

    const readCachedApiResponse = (path) => {
        try {
            const raw = localStorage.getItem(CACHE_PREFIX + path);
            if (!raw) return null;
            const parsed = JSON.parse(raw);
            return parsed ? parsed.payload : null;
        } catch {
            return null;
        }
    };

    const formatSyncTime = (iso) => {
        if (!iso) return 'Not yet synced';
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return String(iso);
        return d.toLocaleString('en-US', {
            month: 'short',
            day: 'numeric',
            hour: 'numeric',
            minute: '2-digit'
        });
    };

    const renderSyncUI = () => {
        const pill = document.querySelector('#sync-status-pill');
        const label = document.querySelector('#sync-status-label');
        const dot = document.querySelector('#sync-status-dot');
        const popover = document.querySelector('#sync-status-popover');
        const clientQueue = readClientQueue();
        const totalPending = (currentState.unsynced_total || 0) + clientQueue.length;

        let effectiveStatus = currentState.status || 'online';
        if (syncInFlight || currentState.syncing) {
            effectiveStatus = 'syncing';
        } else if (!navigator.onLine || currentState.online === false) {
            effectiveStatus = 'offline';
        } else if ((currentState.failed_count || 0) > 0) {
            effectiveStatus = 'sync_failed';
        } else {
            effectiveStatus = 'online';
        }

        if (pill) {
            pill.classList.remove('is-online', 'is-offline', 'is-syncing', 'is-failed');
            if (effectiveStatus === 'syncing') {
                pill.classList.add('is-syncing');
                if (label) label.textContent = 'Syncing...';
            } else if (effectiveStatus === 'offline') {
                pill.classList.add('is-offline');
                if (label) label.textContent = totalPending > 0 ? `Offline · ${totalPending} pending` : 'Offline';
            } else if (effectiveStatus === 'sync_failed') {
                pill.classList.add('is-failed');
                if (label) label.textContent = `Sync Failed (${currentState.failed_count})`;
            } else {
                pill.classList.add('is-online');
                if (label) label.textContent = totalPending > 0 ? `Online · ${totalPending} pending` : 'Online';
            }
            pill.setAttribute('data-sync-state', effectiveStatus);
        }

        if (dot) {
            dot.classList.toggle('spin', effectiveStatus === 'syncing');
        }

        if (popover) {
            const stateTitle = popover.querySelector('#sync-pop-state');
            const lastTime = popover.querySelector('#sync-pop-last-time');
            const pendingEl = popover.querySelector('#sync-pop-pending');
            const cachedEl = popover.querySelector('#sync-pop-cached');
            const errEl = popover.querySelector('#sync-pop-error');
            const syncNowBtn = popover.querySelector('#sync-now-btn');

            if (stateTitle) {
                const labels = {
                    online: 'Online — Connected to Central Database',
                    offline: 'Offline Mode — Changes Saved Locally',
                    syncing: 'Syncing — Uploading & Refreshing Data...',
                    sync_failed: 'Sync Failed — Tap Retry to Sync Again'
                };
                stateTitle.textContent = labels[effectiveStatus] || 'Online';
                stateTitle.className = `sync-pop-state is-${effectiveStatus === 'sync_failed' ? 'failed' : effectiveStatus}`;
            }
            if (lastTime) lastTime.textContent = formatSyncTime(currentState.last_synced_at);
            if (pendingEl) {
                pendingEl.textContent = totalPending === 0
                    ? 'All changes synchronized'
                    : `${totalPending} local change${totalPending === 1 ? '' : 's'} waiting to sync`;
            }
            if (cachedEl) {
                const cc = currentState.cached_counts || {};
                const parts = [
                    `Residents: ${cc.residents || 0}`,
                    `Documents: ${cc.documents || 0}`,
                    `Concerns: ${cc.concerns || 0}`,
                    `Announcements: ${cc.announcements || 0}`
                ];
                cachedEl.textContent = parts.join(' · ');
            }
            if (errEl) {
                const errMsg = currentState.last_sync_error;
                if (effectiveStatus === 'sync_failed' && errMsg) {
                    errEl.textContent = errMsg;
                    errEl.hidden = false;
                } else {
                    errEl.hidden = true;
                }
            }
            if (syncNowBtn) {
                syncNowBtn.disabled = effectiveStatus === 'syncing';
                syncNowBtn.textContent = effectiveStatus === 'syncing'
                    ? 'Syncing...'
                    : (effectiveStatus === 'sync_failed' ? 'Retry Sync' : 'Sync Now');
            }
        }
    };

    const replayBrowserQueue = async () => {
        const queue = readClientQueue();
        if (!queue.length) return 0;
        const remaining = [];
        let replayed = 0;
        for (const op of queue) {
            try {
                const resp = await nativeFetch(op.url, {
                    method: op.method,
                    headers: { 'Content-Type': 'application/json', 'X-Client-Op-Id': op.op_id },
                    body: op.body
                });
                if (resp.ok || (resp.status >= 400 && resp.status < 500)) {
                    replayed += 1;
                } else {
                    remaining.push(op);
                }
            } catch {
                remaining.push(op);
            }
        }
        writeClientQueue(remaining);
        return replayed;
    };

    const notifyViewsRefreshed = () => {
        window.dispatchEvent(new CustomEvent('smartbarangay:synced', { detail: { ...currentState } }));
    };

    const runSync = async ({ silent = false } = {}) => {
        if (syncInFlight) return currentState;
        if (!navigator.onLine) {
            currentState.online = false;
            currentState.status = 'offline';
            renderSyncUI();
            if (!silent && typeof window.showToast === 'function') {
                window.showToast('You are currently offline. Changes remain safely stored locally.');
            }
            return currentState;
        }
        syncInFlight = true;
        currentState.syncing = true;
        currentState.status = 'syncing';
        renderSyncUI();
        try {
            const clientReplayed = await replayBrowserQueue();
            const resp = await nativeFetch('/api/sync/run', { method: 'POST' });
            if (!resp.ok) throw new Error(`Sync endpoint returned ${resp.status}`);
            const body = await resp.json();
            const data = body.data || {};
            currentState = { ...currentState, ...data, syncing: false };
            wasOffline = !currentState.online;
            renderSyncUI();

            const pushedTotal = (data.pushed || 0) + clientReplayed;
            if (!silent && typeof window.showToast === 'function') {
                if (currentState.status === 'offline') {
                    window.showToast('Central database unreachable — still in Offline Mode.');
                } else if (currentState.status === 'sync_failed') {
                    window.showToast(`Sync completed with ${data.failed || 1} failed change(s). Click Sync Status to retry.`);
                } else if (pushedTotal > 0) {
                    const dupNote = data.skipped_duplicates ? ` (${data.skipped_duplicates} duplicate prevented)` : '';
                    window.showToast(`Synchronized ${pushedTotal} offline change${pushedTotal === 1 ? '' : 's'} with central database${dupNote}.`);
                } else {
                    window.showToast('Data synchronized with central database.');
                }
            }
            if (currentState.online) {
                notifyViewsRefreshed();
            }
        } catch (err) {
            currentState.syncing = false;
            currentState.online = false;
            currentState.status = navigator.onLine ? 'sync_failed' : 'offline';
            currentState.last_sync_error = err?.message || 'Unable to reach server';
            renderSyncUI();
        } finally {
            syncInFlight = false;
            currentState.syncing = false;
            renderSyncUI();
        }
        return currentState;
    };

    const refreshSyncStatus = async (probe = false) => {
        if (!navigator.onLine) {
            wasOffline = true;
            currentState.online = false;
            currentState.status = 'offline';
            renderSyncUI();
            return currentState;
        }
        try {
            const resp = await nativeFetch(`/api/sync/status${probe ? '?probe=1' : ''}`);
            if (!resp.ok) return currentState;
            const body = await resp.json();
            const data = body.data || {};
            const nowOnline = Boolean(data.online);
            const hasPending = (data.unsynced_total || 0) > 0 || readClientQueue().length > 0;
            const totalCached = Object.values(data.cached_counts || {}).reduce((acc, n) => acc + Number(n || 0), 0);
            const reconnected = wasOffline && nowOnline;

            currentState = { ...currentState, ...data };
            wasOffline = !nowOnline;
            renderSyncUI();

            // Automatically sync when connection returns, when pending changes exist, or on initial online load
            // so the offline SQLite cache always mirrors the exact online Supabase data 1-to-1
            if (nowOnline && (reconnected || hasPending || !initialWarmupDone)) {
                initialWarmupDone = true;
                await runSync({ silent: !reconnected && !hasPending });
            }
        } catch {
            wasOffline = true;
            currentState.online = false;
            currentState.status = 'offline';
            renderSyncUI();
        }
        return currentState;
    };

    // Transparent fetch interceptor for browser-side resilience & live sync badge updates
    window.fetch = async (input, init = {}) => {
        const rawUrl = typeof input === 'string' ? input : (input && input.url ? input.url : '');
        let pathname = '';
        try {
            const parsed = new URL(rawUrl, window.location.origin);
            if (parsed.origin === window.location.origin) {
                pathname = parsed.pathname + parsed.search;
            }
        } catch {
            pathname = rawUrl;
        }
        const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
        const isApi = pathname.startsWith('/api/');
        const isSyncApi = pathname.startsWith('/api/sync/');

        if (!isApi || isSyncApi) {
            return nativeFetch(input, init);
        }

        if (method === 'GET') {
            try {
                const res = await nativeFetch(input, init);
                if (res.ok) {
                    const clone = res.clone();
                    clone.json().then((json) => cacheApiResponse(pathname, json)).catch(() => {});
                }
                return res;
            } catch (networkErr) {
                const cached = readCachedApiResponse(pathname);
                currentState.online = false;
                currentState.status = 'offline';
                wasOffline = true;
                renderSyncUI();
                if (cached) {
                    return new Response(JSON.stringify(cached), {
                        status: 200,
                        headers: { 'Content-Type': 'application/json', 'X-Offline-Cache': '1' }
                    });
                }
                throw networkErr;
            }
        }

        // Write requests (POST / PATCH / PUT / DELETE)
        try {
            const res = await nativeFetch(input, init);
            // Refresh server sync status immediately so offline SQLite queue count reflects in the topbar badge
            window.setTimeout(() => refreshSyncStatus(false), 180);
            return res;
        } catch (networkErr) {
            // If even the local server is temporarily unreachable, queue locally in browser storage
            if (['POST', 'PATCH', 'PUT', 'DELETE'].includes(method) && !pathname.startsWith('/api/login') && !pathname.startsWith('/api/logout')) {
                const queue = readClientQueue();
                const opId = `cli-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
                const bodyStr = typeof init.body === 'string' ? init.body : JSON.stringify(init.body || {});
                queue.push({ op_id: opId, url: pathname, method, body: bodyStr, created_at: new Date().toISOString() });
                writeClientQueue(queue);
                currentState.online = false;
                currentState.status = 'offline';
                wasOffline = true;
                renderSyncUI();
                let parsedBody = {};
                try { parsedBody = JSON.parse(bodyStr); } catch {}
                return new Response(JSON.stringify({
                    data: { ...parsedBody, id: Date.now(), _offline_pending: true },
                    offline_queued: true
                }), {
                    status: 200,
                    headers: { 'Content-Type': 'application/json', 'X-Offline-Queued': '1' }
                });
            }
            throw networkErr;
        }
    };

    // Register Service Worker for offline page reload support
    if ('serviceWorker' in navigator) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
        });
    }

    // Wire UI & automatic listeners once DOM is ready
    document.addEventListener('DOMContentLoaded', () => {
        const pill = document.querySelector('#sync-status-pill');
        const popover = document.querySelector('#sync-status-popover');
        const closeBtn = document.querySelector('#sync-pop-close');
        const syncNowBtn = document.querySelector('#sync-now-btn');

        if (pill && popover) {
            pill.addEventListener('click', (e) => {
                e.stopPropagation();
                const opening = popover.hidden;
                popover.hidden = !opening;
                pill.setAttribute('aria-expanded', String(opening));
                if (opening) refreshSyncStatus(true);
            });
            closeBtn?.addEventListener('click', () => {
                popover.hidden = true;
                pill.setAttribute('aria-expanded', 'false');
            });
            document.addEventListener('click', (e) => {
                if (!e.target.closest('.sync-status-wrap')) {
                    popover.hidden = true;
                    pill.setAttribute('aria-expanded', 'false');
                }
            });
        }
        syncNowBtn?.addEventListener('click', () => runSync({ silent: false }));

        window.addEventListener('online', () => {
            renderSyncUI();
            runSync({ silent: false });
        });
        window.addEventListener('offline', () => {
            wasOffline = true;
            currentState.online = false;
            currentState.status = 'offline';
            renderSyncUI();
            if (typeof window.showToast === 'function') {
                window.showToast('Offline Mode active. Previously synced data stays accessible and new changes are saved locally.');
            }
        });

        renderSyncUI();
        // Initial probe + warmup snapshot
        window.setTimeout(() => refreshSyncStatus(true), 700);
        // Periodic background connectivity & auto-sync monitor every 20s
        window.setInterval(() => refreshSyncStatus(true), 20000);
    });

    window.smartBarangaySync = {
        runSync,
        refreshSyncStatus,
        getState: () => ({ ...currentState })
    };
})();
