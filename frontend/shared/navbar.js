/* ===== PAGE POSITION ===== */
function resetDashboardScrollPosition() {
    if (window.location.hash) return;
    window.scrollTo({ top: 0, left: 0, behavior: 'auto' });
}

if ('scrollRestoration' in window.history) {
    window.history.scrollRestoration = 'manual';
}

resetDashboardScrollPosition();
document.addEventListener('DOMContentLoaded', () => {
    resetDashboardScrollPosition();
    window.setTimeout(resetDashboardScrollPosition, 100);
    window.setTimeout(resetDashboardScrollPosition, 500);
});
window.addEventListener('pageshow', resetDashboardScrollPosition);
window.addEventListener('load', () => {
    resetDashboardScrollPosition();
    window.setTimeout(resetDashboardScrollPosition, 0);
});

/* ===== REAL-TIME CLOCK ===== */
function updateClock() {
    const clock = document.getElementById('clock');
    if (!clock) return;

    const now = new Date();
    clock.innerText = now.toLocaleString('en-GB', {
        weekday: 'short',
        day: '2-digit',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false
    }).toUpperCase();
}

setInterval(updateClock, 1000);
updateClock();

/* ===== NAVIGATION & EXPORT LOGIC ===== */
document.addEventListener('DOMContentLoaded', () => {
    const path = window.location.pathname.toLowerCase();
    const search = window.location.search.toLowerCase();

    // 1. ACTIVE NAV DETECTION
    // Handles top-level buttons
    document.querySelectorAll('.nav-btn[data-nav]').forEach(btn => {
        const key = btn.dataset.nav;
        const isMaintenanceRoot = key === 'maintenance' && (
            path === '/'
            || path.startsWith('/maintenance/')
            || path === '/downtime'
            || path.startsWith('/downtime/')
        );
        if (path.includes(key.toLowerCase()) || isMaintenanceRoot) {
            btn.classList.add('active');
        }
    });

    // Handles Dropdown highlighting
    document.querySelectorAll('.dropdown-menu a').forEach(link => {
        const href = link.getAttribute('href');
        if (!href) return;

        const url = new URL(href, window.location.origin);
        const linkPath = url.pathname.toLowerCase();
        const linkSearch = url.search.toLowerCase();
        const matchesPath = path === linkPath || path.endsWith(linkPath);
        const matchesSearch = !linkSearch || search === linkSearch;

        if (matchesPath && matchesSearch) {
            const parentBtn = link.closest('.nav-item')?.querySelector('.nav-btn');
            if (parentBtn) parentBtn.classList.add('active');
            link.classList.add('active-link');
        }
    });

    initializeSyncCard();
});

function initializeSyncCard() {
    const pageKey = document.body?.dataset?.syncPage;
    if (!pageKey) return;
    if (pageKey === 'maintenance') return;

    const headerSelector = document.body.dataset.syncHeader || '.dash-header, .page-header, .system-status-header, main > header, body > header';
    const header = document.querySelector(headerSelector);
    if (!header) return;

    const target = header.querySelector('.header-right') || header;
    let card = header.querySelector('[data-sync-card]');

    if (!card) {
        card = document.createElement('div');
        card.className = 'sync-card';
        card.setAttribute('data-sync-card', 'true');
        card.innerHTML = `
            <span class="sync-label">Last Synced</span>
            <strong class="sync-value" data-sync-date>--</strong>
            <span class="sync-subvalue" data-sync-time>--</span>
        `;

        if (target === header) {
            const simpleTags = ['H1', 'H2', 'P'];
            const existingChildren = Array.from(header.children);
            const isSimpleTextHeader = existingChildren.length > 0 && existingChildren.every((child) => simpleTags.includes(child.tagName));

            if (isSimpleTextHeader) {
                const copy = document.createElement('div');
                copy.setAttribute('data-sync-header-copy', 'true');
                while (header.firstChild) {
                    copy.appendChild(header.firstChild);
                }
                header.appendChild(copy);
            }

            header.classList.add('sync-card-host');
            header.appendChild(card);
        } else {
            target.insertBefore(card, target.firstChild);
        }
    }

    header.querySelectorAll('.sync-note').forEach((node) => node.remove());
    document.querySelectorAll('.sync-info, .status-details').forEach((node) => {
        if (node.querySelector('#last-sync') || node.classList.contains('sync-info')) {
            node.style.display = 'none';
        }
    });

    const dateNode = card.querySelector('[data-sync-date]');
    const timeNode = card.querySelector('[data-sync-time]');
    const cacheKey = `sync-card:${pageKey}`;

    const renderCached = () => {
        try {
            const raw = sessionStorage.getItem(cacheKey);
            if (!raw) return false;
            const cached = JSON.parse(raw);
            if (!cached?.last_synced || !cached?.fetched_at) return false;
            if (Date.now() - Number(cached.fetched_at) > 60000) return false;

            const dt = new Date(cached.last_synced);
            if (Number.isNaN(dt.getTime())) return false;

            dateNode.textContent = dt.toLocaleDateString('en-GB', {
                day: '2-digit',
                month: 'short',
                year: 'numeric'
            });
            timeNode.textContent = dt.toLocaleTimeString('en-GB', {
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: false
            });
            return true;
        } catch (error) {
            console.warn('Sync card cache read failed:', error);
            return false;
        }
    };

    const render = async () => {
        try {
            const res = await fetch(`/api/page-sync/${encodeURIComponent(pageKey)}`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const payload = await res.json();
            const dt = payload?.last_synced ? new Date(payload.last_synced) : null;
            if (!dt || Number.isNaN(dt.getTime())) {
                dateNode.textContent = '--';
                timeNode.textContent = '--';
                return;
            }

            try {
                sessionStorage.setItem(cacheKey, JSON.stringify({
                    last_synced: payload.last_synced,
                    fetched_at: Date.now()
                }));
            } catch (error) {
                console.warn('Sync card cache write failed:', error);
            }

            dateNode.textContent = dt.toLocaleDateString('en-GB', {
                day: '2-digit',
                month: 'short',
                year: 'numeric'
            });
            timeNode.textContent = dt.toLocaleTimeString('en-GB', {
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit',
                hour12: false
            });
        } catch (error) {
            console.error('Sync card load failed:', error);
            dateNode.textContent = '--';
            timeNode.textContent = '--';
        }
    };

    const hadCached = renderCached();
    if (!hadCached) {
        dateNode.textContent = '--';
        timeNode.textContent = '--';
    }
    render();
    window.setInterval(render, 60000);
}
