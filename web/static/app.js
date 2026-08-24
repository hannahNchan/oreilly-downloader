/**
 * O'Reilly Ingest - Frontend Application
 * Redesigned with Tailwind CSS
 */

const API = '';
let currentExpandedCard = null;
let selectedResultIndex = -1;
// Ajustes que vienen del servidor. `dir` es la carpeta donde vive la
// biblioteca; `available` es false si esta configurada pero no responde.
let librarySettings = { dir: '', default: '', isDefault: true, available: true };
// Preferencias globales de descarga. Antes vivian en la modal de cada libro, lo
// que obligaba a re-elegirlas en cada descarga; ahora se guardan en el servidor.
let downloadPrefs = { transferAfter: true };
let cacheDir = '';
const chaptersCache = {};

// While a download/translation runs, the UI is locked to the active card so
// the user can't navigate away and lose the progress view. The work itself
// runs on the server regardless, but the UX keeps them focused on it.
let downloadInProgress = false;


/* ===== Visor de biblioteca local =====
   Todo el filtrado ocurre en el servidor sobre el indice de `output/`, sin
   tocar la red de O'Reilly. Los conteos de cada facet son del total de la
   biblioteca (no de lo ya filtrado), para que se vea cuanto hay disponible
   antes de elegir un valor. */

const FACET_LABELS = {
    location: 'Ubicación',
    content_type: 'Tipo',
    language: 'Idioma',
    year: 'Año',
    publishers: 'Editorial',
    authors: 'Autor',
    formats: 'Formatos en disco',
};

const FACET_VALUE_LABELS = {
    book: 'Libros',
    audiobook: 'Audiolibros',
    en: 'Inglés',
    es: 'Español',
    __none__: '(sin dato)',
    library: 'En la biblioteca',
    local: 'En caché',
};

// Los facets con muchos valores (autores) se recortan y se expanden a demanda
const FACET_PREVIEW = 6;
const libraryState = { q: '', facets: {}, expanded: {}, sort: 'title' };

function facetValueLabel(value) {
    return FACET_VALUE_LABELS[value] || value;
}

function formatSize(bytes) {
    if (!bytes) return '';
    const gb = bytes / 1073741824;
    return gb >= 1 ? gb.toFixed(2) + ' GB' : Math.round(bytes / 1048576) + ' MB';
}

function libraryFilterCount() {
    return Object.values(libraryState.facets).reduce((n, v) => n + v.length, 0);
}

/* --- Transferencia local -> Alexandría -------------------------------------
   La cola vive en el servidor (una obra a la vez); aqui solo se refleja su
   estado. Se sondea solo mientras hay algo en curso. */

let transferPoll = null;

function renderTransferBar(state) {
    const bar = document.getElementById('transfer-bar');
    const msg = document.getElementById('transfer-msg');
    const btn = document.getElementById('transfer-all-btn');
    if (!bar) return;

    // Configurada pero sin responder: se dice, en vez de dejar creer que no
    // hay nada publicado.
    if (state.unavailable) {
        bar.classList.remove('hidden');
        btn.disabled = true;
        btn.textContent = 'Transferir todas';
        msg.textContent = 'La carpeta de la biblioteca no responde ('
            + (state.dir || '?') + '). Lo descargado sigue en la caché local.';
        return;
    }

    if (state.running) {
        bar.classList.remove('hidden');
        btn.disabled = true;
        btn.textContent = 'Transfiriendo...';
        const n = state.index || 1;
        msg.textContent = 'Transfiriendo ' + n + ' de ' + (state.total || 1)
            + ' - ' + (state.percentage || 0) + '%';
        return;
    }

    btn.disabled = false;
    btn.textContent = 'Transferir todas';

    if (state.failedCount) {
        bar.classList.remove('hidden');
        msg.textContent = state.failedCount + ' obra(s) no se pudieron transferir: '
            + state.firstError;
        return;
    }
    if (state.localCount) {
        bar.classList.remove('hidden');
        msg.textContent = state.localCount === 1
            ? '1 obra en caché, sin pasar a la biblioteca'
            : state.localCount + ' obras en caché, sin pasar a la biblioteca';
        return;
    }
    bar.classList.add('hidden');
}

async function pollTransfer() {
    try {
        const res = await fetch(API + '/api/library/transfer');
        const st = await res.json();
        if (st.status === 'transferring') {
            renderTransferBar({ running: true, index: st.index, total: st.total,
                                percentage: st.percentage });
            return;
        }
        // Terminada: se para el sondeo y se recarga para ver el cambio de sitio
        clearInterval(transferPoll);
        transferPoll = null;
        const failed = Object.keys(st.failed || {});
        if (failed.length) {
            renderTransferBar({ failedCount: failed.length,
                                firstError: st.failed[failed[0]] });
        }
        loadLibrary({ refresh: true });
    } catch (err) {
        clearInterval(transferPoll);
        transferPoll = null;
    }
}

async function startTransfer(payload) {
    try {
        const res = await fetch(API + '/api/library/transfer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (data.error) {
            renderTransferBar({ failedCount: 1, firstError: data.error });
            return;
        }
        renderTransferBar({ running: true, index: 1, total: data.count, percentage: 0 });
        if (transferPoll) clearInterval(transferPoll);
        transferPoll = setInterval(pollTransfer, 700);
    } catch (err) {
        renderTransferBar({ failedCount: 1, firstError: 'No se pudo contactar al servidor' });
    }
}

async function loadLibrary(options) {
    const refresh = !!(options && options.refresh);
    const grid = document.getElementById('library-grid');
    const empty = document.getElementById('library-empty');

    const params = new URLSearchParams();
    if (libraryState.q) params.set('q', libraryState.q);
    for (const [key, values] of Object.entries(libraryState.facets)) {
        if (values.length) params.set(key, values.join(','));
    }
    if (libraryState.sort && libraryState.sort !== 'title') {
        params.set('sort', libraryState.sort);
    }
    if (refresh) params.set('refresh', '1');

    try {
        const res = await fetch(API + '/api/library?' + params);
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        const visible = (data.items || []).filter(function (it) {
            return !readSet(HIDDEN_KEY).has(it.folder);
        }).length;
        document.getElementById('library-count').textContent =
            visible === data.total
                ? data.total + ' en tu biblioteca'
                : visible + ' de ' + data.total + ' en tu biblioteca';
        document.getElementById('library-size').textContent = formatSize(data.total_bytes);

        // El conteo sale del facet, que cuenta sobre TODA la biblioteca: asi la
        // barra no desaparece al filtrar por Alexandría.
        const locFacet = (data.facets && data.facets.location) || [];
        const localEntry = locFacet.filter(function (v) { return v.value === 'local'; })[0];
        if (!transferPoll) {
            renderTransferBar({
                localCount: localEntry ? localEntry.count : 0,
                unavailable: data.library_available === false,
                dir: data.library_dir
            });
        }

        // Los ocultados con "Eliminar de la biblioteca" solo se filtran aqui:
        // el contenido sigue en disco.
        const hidden = readSet(HIDDEN_KEY);
        const favs = readSet(FAV_KEY);
        let items = (data.items || []).filter(function (it) { return !hidden.has(it.folder); });
        // Los favoritos primero, respetando el orden elegido dentro de cada grupo
        items = items.filter(i => favs.has(i.folder)).concat(items.filter(i => !favs.has(i.folder)));

        grid.innerHTML = '';
        items.forEach(function (item) {
            grid.appendChild(libraryTile(item));
        });

        const none = !items.length;
        empty.classList.toggle('hidden', !none);
        if (none) {
            empty.textContent = data.total
                ? 'Ningún elemento coincide con esos filtros.'
                : 'Tu biblioteca está vacía: descarga algo primero.';
        }

        renderFacets(data.facets || {});
        const sbCount = document.getElementById('sidebar-count');
        if (sbCount) {
            const n = libraryFilterCount();
            sbCount.textContent = n;
            sbCount.classList.toggle('hidden', !n);
        }
        document.getElementById('library-clear')
            .classList.toggle('hidden', !libraryFilterCount() && !libraryState.q);
    } catch (err) {
        grid.innerHTML = '';
        empty.classList.remove('hidden');
        empty.textContent = 'No se pudo leer la biblioteca: ' + err.message;
    }
}

function libraryTile(item) {
    const div = document.createElement('article');
    div.className = 'book-card group bg-white rounded-xl border border-zinc-200 overflow-hidden transition-all duration-200 hover:border-zinc-300 hover:shadow-card-hover cursor-pointer';
    div.dataset.folder = item.folder;

    const isAudio = item.content_type === 'audiobook';
    const badge = isAudio
        ? '<span class="tile-duration">' + item.tracks + ' pistas</span>'
        : '';
    const cover = item.cover_url
        ? '<img src="' + item.cover_url + '" alt="" class="tile-blur" aria-hidden="true" loading="lazy">'
          + '<img src="' + item.cover_url + '" alt="" class="tile-img" loading="lazy">'
        : '<div class="tile-img"></div>';

    const fallback = [item.year, formatSize(item.size_bytes)].filter(Boolean).join(' - ');

    // Todos los formatos presentes en disco, no solo el primero
    const formats = (item.formats && item.formats.length)
        ? item.formats
        : (isAudio ? ['audio'] : []);
    const badges = (item.location === 'local'
        ? '<span class="fmt-badge loc-local" title="Aún no está en Alexandría">LOCAL</span>'
        : '') + formats.map(function (f) {
        return '<span class="fmt-badge fmt-' + f + '">' + f.toUpperCase() + '</span>';
    }).join('');

    div.innerHTML =
        '<div class="book-summary">'
        + '<div class="cover-wrap tile-cover">' + cover + badge
        + '<div class="tile-formats">' + badges + '</div>'
        + '</div>'
        + '<div class="tile-meta">'
        + '<h3 class="tile-title"></h3>'
        + '<p class="tile-author"></p>'
        + '</div></div>';

    // textContent para que titulos con < o & no rompan el markup
    div.querySelector('.tile-title').textContent = item.title;
    div.querySelector('.tile-author').textContent =
        (item.authors || []).join(', ') || fallback || '-';

    const coverBox = div.querySelector('.tile-cover');

    // Si la portada falla (borrada, sin red para la remota) queda el hueco
    // neutro; el icono de imagen rota del navegador se ve peor que nada.
    coverBox.querySelectorAll('img').forEach(function (img) {
        img.addEventListener('error', function () {
            coverBox.querySelectorAll('img').forEach(function (i) { i.remove(); });
            if (!coverBox.querySelector('.tile-img')) {
                const ph = document.createElement('div');
                ph.className = 'tile-img';
                coverBox.prepend(ph);
            }
        });
    });

    // El menu se cuelga de la TARJETA, no de la portada: .tile-cover recorta
    // (overflow:hidden, para que el difuminado no se salga del cuadro) y con
    // tres opciones el panel es mas alto que la portada, asi que ahi dentro
    // salia cortado a la mitad.
    div.appendChild(buildCardMenu(item));
    if (readSet(FAV_KEY).has(item.folder)) {
        const star = document.createElement('span');
        star.className = 'fav-star';
        star.textContent = '\u2605';
        star.setAttribute('aria-label', 'Favorito');
        coverBox.appendChild(star);
    }

    // Un clic abre la carpeta en el explorador: es un visor de biblioteca,
    // no un lector, asi que lo util es llevarte al archivo.
    div.onclick = function () { revealFile(item.path); };
    div.title = item.title + '\n' + item.path;
    return div;
}

function renderFacets(facets) {
    const host = document.getElementById('library-facets');
    host.innerHTML = '';

    for (const [key, values] of Object.entries(facets)) {
        if (!values.length) continue;
        const selected = libraryState.facets[key] || [];
        const showAll = libraryState.expanded[key];
        const visible = showAll ? values : values.slice(0, FACET_PREVIEW);

        const block = document.createElement('div');
        const heading = document.createElement('p');
        heading.className = 'text-[0.6875rem] font-semibold uppercase tracking-wide text-zinc-400 mb-1.5';
        heading.textContent = FACET_LABELS[key] || key;
        block.appendChild(heading);

        const list = document.createElement('div');
        list.className = 'space-y-1';
        visible.forEach(function (v) {
            const row = document.createElement('label');
            row.className = 'flex items-center gap-2 text-sm text-zinc-600 cursor-pointer hover:text-zinc-900';

            const box = document.createElement('input');
            box.type = 'checkbox';
            box.className = 'w-3.5 h-3.5 rounded border-zinc-300 flex-shrink-0';
            box.checked = selected.indexOf(v.value) !== -1;
            box.onchange = function () {
                const cur = libraryState.facets[key] || [];
                libraryState.facets[key] = box.checked
                    ? cur.concat([v.value])
                    : cur.filter(function (x) { return x !== v.value; });
                if (!libraryState.facets[key].length) delete libraryState.facets[key];
                loadLibrary();
            };

            const name = document.createElement('span');
            name.className = 'flex-1 truncate';
            name.textContent = facetValueLabel(v.value);
            name.title = facetValueLabel(v.value);

            const count = document.createElement('span');
            count.className = 'text-xs text-zinc-400';
            count.textContent = v.count;

            row.append(box, name, count);
            list.appendChild(row);
        });
        block.appendChild(list);

        if (values.length > FACET_PREVIEW) {
            const more = document.createElement('button');
            more.className = 'facet-more mt-1 text-xs text-oreilly-blue hover:underline';
            more.textContent = showAll
                ? 'Ver menos'
                : 'Ver ' + (values.length - FACET_PREVIEW) + ' más';
            more.onclick = function () {
                libraryState.expanded[key] = !showAll;
                renderFacets(facets);
            };
            block.appendChild(more);
        }

        host.appendChild(block);
    }
}

function moveSearchInput(toLibrary) {
    const search = document.querySelector('.toolbar-search');
    const toolbar = document.querySelector('.toolbar');
    const libMain = document.getElementById('library-main');
    if (!search || !toolbar || !libMain) return;

    if (toLibrary) {
        // Primer hijo de library-main para que quede sobre la barra de conteo
        if (search.parentElement !== libMain) libMain.prepend(search);
        search.classList.add('is-docked');
        toolbar.classList.add('is-empty');
    } else {
        if (search.parentElement !== toolbar) toolbar.prepend(search);
        search.classList.remove('is-docked');
        toolbar.classList.remove('is-empty');
    }
}

function goToSection(ct) {
    searchState.contentType = ct;
    document.querySelectorAll('.ct-tab').forEach(t => {
        t.classList.toggle('ct-active', t.dataset.ct === ct);
    });

    setContentMode(ct);
    if (ct === 'library') return;   // el visor local se pinta solo

    // El orden solo existe para libros
    const sortWrap = document.getElementById('filter-sort');
    if (sortWrap) sortWrap.closest('label').style.display = ct === 'book' ? '' : 'none';
    if (searchState.query) search(searchState.query);
}

function setContentMode(mode) {
    const isLibrary = mode === 'library';
    document.getElementById('library-view').classList.toggle('is-open', isLibrary);
    moveSearchInput(isLibrary);

    // El buscador remoto y sus bloques de estado no aplican al visor local
    ['results-bar', 'search-results', 'load-more-wrap', 'search-empty'].forEach(function (id) {
        const el = document.getElementById(id);
        if (el) el.style.display = isLibrary ? 'none' : '';
    });

    document.querySelectorAll('.ct-tab').forEach(function (t) {
        const on = t.dataset.ct === mode;
        t.classList.toggle('ct-active', on);
        if (on) t.setAttribute('aria-current', 'page');
        else t.removeAttribute('aria-current');
    });

    // Al volver del visor, el buscador recupera su estado propio
    if (!isLibrary && !searchState.query) setSearchState('empty');

    const input = document.getElementById('search-input');
    input.placeholder = isLibrary
        ? 'Filtrar tu biblioteca por título, autor o editorial...'
        : 'Search by title, author, or ISBN...';

    if (isLibrary) loadLibrary();
}


/* ===== Tema claro/oscuro =====
   El estado vive en la clase `dark` del <html>, que es lo que leen tanto
   Tailwind (darkMode: 'class') como el remapeo de style.css. La eleccion se
   guarda en localStorage; si no hay nada guardado se sigue al sistema. */

function applyTheme(dark) {
    document.documentElement.classList.toggle('dark', dark);
    try {
        localStorage.setItem('theme', dark ? 'dark' : 'light');
    } catch (e) {
        /* localStorage puede fallar en modo privado: el tema sigue aplicado */
    }
}

function initTheme() {
    const btn = document.getElementById('theme-toggle');
    if (btn) {
        btn.onclick = () => applyTheme(!document.documentElement.classList.contains('dark'));
    }

    // Si el usuario no ha elegido, se sigue al sistema en vivo
    let stored = null;
    try { stored = localStorage.getItem('theme'); } catch (e) { /* noop */ }
    if (!stored && window.matchMedia) {
        window.matchMedia('(prefers-color-scheme: dark)')
            .addEventListener('change', (e) => {
                document.documentElement.classList.toggle('dark', e.matches);
            });
    }
}


/* ===== Estados del buscador =====
   El input vive siempre en el mismo sitio: lo que cambia debajo es el bloque
   que ocupa el hueco (empty state -> skeletons -> grilla). Asi nada se mueve
   al pasar de un estado a otro. */

const SKELETON_COUNT = 12;

function setSearchState(state) {
    const empty = document.getElementById('search-empty');
    const bar = document.getElementById('results-bar');
    const grid = document.getElementById('search-results');
    const more = document.getElementById('load-more-wrap');
    if (!empty || !grid) return;

    empty.classList.toggle('hidden', state !== 'empty');
    if (state === 'empty') {
        // La barra NO se oculta: contiene los filtros de la consulta y el
        // usuario debe poder elegirlos antes de la primera busqueda. Solo se
        // vacia el conteo, que si depende de haber buscado.
        document.getElementById('results-count').textContent = '';
        more.classList.add('hidden');
        grid.innerHTML = '';
    }
}

function showSkeletons(grid, count) {
    grid.innerHTML = '';
    for (let i = 0; i < (count || SKELETON_COUNT); i++) {
        const s = document.createElement('div');
        s.className = 'skeleton-tile';
        grid.appendChild(s);
    }
}

/* ===== Acciones por titulo (favoritos / ocultar) =====
   El estado vive en sessionStorage: "Eliminar de la biblioteca" solo la quita
   de esta vista, NO borra nada del disco. */

const FAV_KEY = 'library:favorites';
const HIDDEN_KEY = 'library:hidden';

function readSet(key) {
    try {
        return new Set(JSON.parse(sessionStorage.getItem(key) || '[]'));
    } catch (e) {
        return new Set();
    }
}

function writeSet(key, set) {
    try {
        sessionStorage.setItem(key, JSON.stringify([...set]));
    } catch (e) {
        /* sessionStorage puede fallar en modo privado */
    }
}

function toggleInSet(key, value) {
    const set = readSet(key);
    if (set.has(value)) set.delete(value); else set.add(value);
    writeSet(key, set);
    return set.has(value);
}

function closeAllCardMenus() {
    document.querySelectorAll('.card-menu.is-open').forEach(m => {
        m.classList.remove('is-open');
        const btn = m.querySelector('.card-menu-btn');
        if (btn) btn.setAttribute('aria-expanded', 'false');
    });
}

function buildCardMenu(item) {
    const wrap = document.createElement('div');
    wrap.className = 'card-menu';

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'card-menu-btn';
    btn.setAttribute('aria-label', 'Acciones para ' + item.title);
    btn.setAttribute('aria-haspopup', 'true');
    btn.setAttribute('aria-expanded', 'false');
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">'
        + '<circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="12" cy="19" r="2"/></svg>';

    const list = document.createElement('div');
    list.className = 'card-menu-list';
    list.setAttribute('role', 'menu');

    const isFav = readSet(FAV_KEY).has(item.folder);
    const fav = document.createElement('button');
    fav.type = 'button';
    fav.setAttribute('role', 'menuitem');
    fav.textContent = isFav ? 'Quitar de favoritos' : 'Marcar como favorito';

    // Solo para lo que sigue en cache: pasar algo que ya esta publicado no
    // significaria nada, asi que la opcion no aparece.
    const move = item.location === 'local' ? document.createElement('button') : null;
    if (move) {
        move.type = 'button';
        move.setAttribute('role', 'menuitem');
        move.textContent = 'Pasar a la biblioteca';
        move.onclick = (e) => {
            e.stopPropagation();
            closeAllCardMenus();
            startTransfer({ folder: item.folder });
        };
    }

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'is-danger';
    del.setAttribute('role', 'menuitem');
    del.textContent = 'Eliminar de la biblioteca';

    fav.onclick = (e) => {
        e.stopPropagation();
        toggleInSet(FAV_KEY, item.folder);
        closeAllCardMenus();
        loadLibrary();
    };
    del.onclick = (e) => {
        e.stopPropagation();
        toggleInSet(HIDDEN_KEY, item.folder);
        closeAllCardMenus();
        loadLibrary();
    };

    btn.onclick = (e) => {
        e.stopPropagation();
        const open = wrap.classList.contains('is-open');
        closeAllCardMenus();
        wrap.classList.toggle('is-open', !open);
        btn.setAttribute('aria-expanded', String(!open));
        if (!open) fav.focus();
    };

    // Navegacion con teclado dentro del menu
    list.addEventListener('keydown', (e) => {
        const items = [fav, move, del].filter(Boolean);
        const at = items.indexOf(document.activeElement);
        if (e.key === 'ArrowDown') { e.preventDefault(); items[(at + 1) % items.length].focus(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); items[(at - 1 + items.length) % items.length].focus(); }
        else if (e.key === 'Escape') { e.preventDefault(); closeAllCardMenus(); btn.focus(); }
    });

    if (move) list.append(fav, move, del);
    else list.append(fav, del);
    wrap.append(btn, list);
    return wrap;
}

// Cerrar los menus al hacer clic fuera o con Escape
document.addEventListener('click', closeAllCardMenus);
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeAllCardMenus();
});

function setDownloadLock(locked) {
    downloadInProgress = locked;

    const searchInput = document.getElementById('search-input');
    if (searchInput) {
        searchInput.disabled = locked;
        searchInput.classList.toggle('opacity-50', locked);
        searchInput.classList.toggle('cursor-not-allowed', locked);
    }

    // Dim and disable every card except the one that's downloading.
    document.querySelectorAll('.book-card').forEach(card => {
        if (card !== currentExpandedCard) {
            card.classList.toggle('opacity-40', locked);
            card.classList.toggle('pointer-events-none', locked);
        }
    });
}

/**
 * Get high-resolution cover URL for expanded view.
 * O'Reilly provides larger covers at /covers/urn:orm:book:{id}/400w/
 */
function getHighResCoverUrl(bookId) {
    return `https://learning.oreilly.com/covers/urn:orm:book:${bookId}/400w/`;
}

/* ===== Estado de sesión =====
   El cookie orm-jwt es un JWT con claim `exp`, y /api/status ya devuelve
   `expires_at`. Por eso NO hace falta polling: se arma una cuenta regresiva
   local (puro reloj del navegador, cero peticiones) y sólo se revalida
   contra el servidor por eventos: al volver a la pestaña, al recuperar red,
   y una única vez cuando el contador llega a cero. */

const AUTH_REASONS = {
    token_expired: 'Sesión expirada',
    not_authenticated: 'Sin autenticar',
    subscription_expired: 'Suscripción expirada',
    invalid_token: 'Cookie inválida',
};

const EXPIRY_WARN_MS = 10 * 60 * 1000; // últimos 10 min: aviso en ámbar
// El servidor da el token por inválido 60s antes del `exp` real
// (ver HttpClient.get_jwt_status), así que el contador usa el mismo margen
// para no mostrar "válida" cuando el backend ya rechazaría la petición.
const EXPIRY_SKEW_MS = 60 * 1000;

const authState = { valid: false, expiresAt: null };
let expiryTicker = null;

function formatRemaining(ms) {
    const total = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${String(s).padStart(2, '0')}s`;
    return `${s}s`;
}

function paintAuth(kind, text) {
    const el = document.getElementById('auth-status');
    if (!el) return;
    const loginBtn = document.getElementById('login-btn');
    const dot = el.querySelector('.status-dot');
    const label = el.querySelector('.status-text');

    const tones = {
        ok:   ['bg-emerald-500', 'text-emerald-600'],
        warn: ['bg-amber-500', 'text-amber-600'],
        bad:  ['bg-red-500', 'text-red-600'],
    };
    const [dotCls, textCls] = tones[kind] || tones.bad;

    if (label) label.textContent = text;
    if (dot) dot.className = `status-dot w-2 h-2 rounded-full ${dotCls}`;
    el.className = `flex items-center gap-2 text-sm ${textCls}`;
    // Ofrecer "Set Cookies" en cuanto la sesión deja de estar sana
    if (loginBtn) loginBtn.classList.toggle('hidden', kind === 'ok');
}

function stopExpiryTicker() {
    if (expiryTicker) {
        clearInterval(expiryTicker);
        expiryTicker = null;
    }
}

function startExpiryTicker() {
    stopExpiryTicker();
    if (!authState.expiresAt) return;

    const tick = () => {
        const left = authState.expiresAt - Date.now();

        if (left <= 0) {
            stopExpiryTicker();
            authState.valid = false;
            paintAuth('bad', 'Sesión expirada');
            checkAuth(); // una sola confirmación con el servidor
            return;
        }

        const soon = left < EXPIRY_WARN_MS;
        paintAuth(
            soon ? 'warn' : 'ok',
            (soon ? 'Expira en ' : 'Sesión válida · ') + formatRemaining(left)
        );
    };

    tick();
    expiryTicker = setInterval(tick, 1000);
}

async function checkAuth() {
    try {
        const res = await fetch(`${API}/api/status`);
        const data = await res.json();

        authState.valid = !!data.valid;
        const parsed = data.expires_at ? Date.parse(data.expires_at) : NaN;
        authState.expiresAt = Number.isNaN(parsed) ? null : parsed - EXPIRY_SKEW_MS;

        if (data.valid && authState.expiresAt) {
            startExpiryTicker(); // el reloj local se encarga del resto
            return;
        }

        stopExpiryTicker();
        if (data.valid) {
            paintAuth('ok', 'Sesión válida'); // válida, pero sin exp conocido
        } else {
            paintAuth('bad', AUTH_REASONS[data.reason] || data.reason || 'Sesión inválida');
        }
    } catch (err) {
        console.error('Auth check failed:', err);
    }
}

// Revalidación por eventos (en lugar de polling)
document.addEventListener('visibilitychange', () => {
    if (!document.hidden) checkAuth();
});
window.addEventListener('online', checkAuth);

function showCookieModal() {
    document.getElementById('cookie-modal').classList.remove('hidden');
    document.getElementById('cookie-input').value = '';
    document.getElementById('cookie-error').classList.add('hidden');
    document.body.style.overflow = 'hidden';
}

function hideCookieModal() {
    document.getElementById('cookie-modal').classList.add('hidden');
    document.body.style.overflow = '';
}

async function saveCookies() {
    const input = document.getElementById('cookie-input').value.trim();
    const errorEl = document.getElementById('cookie-error');

    if (!input) {
        errorEl.textContent = 'Please paste your cookie JSON';
        errorEl.classList.remove('hidden');
        return;
    }

    let cookies;
    try {
        cookies = JSON.parse(input);
        if (typeof cookies !== 'object' || Array.isArray(cookies)) {
            throw new Error('Must be a JSON object');
        }
    } catch (e) {
        errorEl.textContent = 'Invalid JSON format: ' + e.message;
        errorEl.classList.remove('hidden');
        return;
    }

    try {
        const res = await fetch(`${API}/api/cookies`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cookies)
        });
        const data = await res.json();

        if (data.error) {
            errorEl.textContent = data.error;
            errorEl.classList.remove('hidden');
            return;
        }

        hideCookieModal();
        checkAuth();
    } catch (err) {
        errorEl.textContent = 'Failed to save cookies';
        errorEl.classList.remove('hidden');
    }
}

async function loadDefaultOutputDir() {
    try {
        const res = await fetch(`${API}/api/settings`);
        const data = await res.json();
        librarySettings = {
            dir: data.library_dir || '',
            default: data.library_default || '',
            isDefault: !!data.library_is_default,
            available: data.library_available !== false
        };
        downloadPrefs = { transferAfter: data.transfer_after !== false };
        cacheDir = data.output_dir || '';
        renderSettings();
    } catch (err) {
        console.error('No se pudieron cargar los ajustes:', err);
    }
}

/* Los ajustes viven en un solo sitio (el menu de ajustes), asi que pintarlos es
   pintar esa modal. Nada de esto se decide por descarga. */
function renderSettings() {
    const input = document.getElementById('settings-library-dir');
    if (!input) return;

    input.value = librarySettings.dir || 'Cargando...';
    document.getElementById('settings-cache-dir').textContent = cacheDir || 'output/';
    document.getElementById('settings-reset-btn')
        .classList.toggle('hidden', librarySettings.isDefault);

    // Configurada pero sin responder: se dice aqui tambien, que es donde el
    // usuario viene a mirar cuando algo no cuadra.
    const state = document.getElementById('settings-library-state');
    if (librarySettings.available) {
        state.classList.add('hidden');
    } else {
        state.classList.remove('hidden');
        state.className = 'mt-2 text-xs font-medium text-red-600';
        state.textContent = 'Esta carpeta no responde. Lo descargado se queda en la caché.';
    }

    document.getElementById('settings-transfer-after').checked = downloadPrefs.transferAfter;
}

function flashSaved() {
    const tag = document.getElementById('settings-saved');
    if (!tag) return;
    tag.style.opacity = '1';
    setTimeout(function () { tag.style.opacity = '0'; }, 1400);
}

function openSettings() {
    renderSettings();
    document.getElementById('settings-modal').classList.remove('hidden');
    document.getElementById('settings-browse-btn').focus();
}

function closeSettings() {
    document.getElementById('settings-modal').classList.add('hidden');
}

async function savePrefs() {
    downloadPrefs = {
        transferAfter: document.getElementById('settings-transfer-after').checked
    };
    try {
        await fetch(`${API}/api/settings`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ transfer_after: downloadPrefs.transferAfter })
        });
        flashSaved();
    } catch (err) {
        console.error('No se pudieron guardar las preferencias:', err);
    }
}

/* ===== Búsqueda paginada =====
   La API de O'Reilly devuelve `total` y `next`. Se pide una página a la vez y
   "Cargar más" va agregando. Ojo: los formatos que no son libro (video,
   audiobook, article) se filtran en el backend, así que una página puede traer
   menos ítems que el límite pedido — o ninguno — y aun así haber más páginas.
   Por eso el botón se rige por `has_more`, no por cuántas tarjetas llegaron. */

const searchState = {
    query: '',
    page: 0,
    perPage: 25,
    language: '',   // '' = Todos -> no se manda el parámetro
    sort: 'relevance',
    contentType: 'book',   // 'book' (EPUB) | 'audiobook' (M4A)
    loaded: 0,
    total: 0,
    hasMore: false,
    loading: false,
    refilter: false,   // filtro cambiado durante una peticion en vuelo
};

function renderBookCard(book, container) {
    const div = document.createElement('article');
    div.className = 'book-card group bg-white rounded-xl border border-zinc-200 overflow-hidden transition-all duration-200 hover:border-zinc-300 hover:shadow-card-hover';
    if (book.in_library) div.classList.add('in-library');
    div.dataset.bookId = book.id;
    div.innerHTML = createBookCardHTML(book);
    setupBookCardEvents(div, book);
    container.appendChild(div);
}

function updateResultsBar() {
    const bar = document.getElementById('results-bar');
    const count = document.getElementById('results-count');
    const wrap = document.getElementById('load-more-wrap');
    const hint = document.getElementById('load-more-hint');
    const btn = document.getElementById('load-more');

    const hasAny = searchState.loaded > 0;
    const emptyBlock = document.getElementById('search-empty');
    if (emptyBlock && hasAny) emptyBlock.classList.add('hidden');

    // La barra siempre esta; lo que aparece o no es el conteo
    count.textContent = hasAny
        ? (searchState.total
            ? `Mostrando ${searchState.loaded} de ${searchState.total.toLocaleString('es-MX')} libros`
            : `Mostrando ${searchState.loaded} libros`)
        : '';

    wrap.classList.toggle('hidden', !searchState.hasMore || !hasAny);
    if (btn) {
        btn.disabled = searchState.loading;
        btn.textContent = searchState.loading ? 'Cargando…' : 'Cargar más';
    }
    if (hint) {
        const left = Math.max(0, searchState.total - searchState.loaded);
        hint.textContent = left ? `quedan ~${left.toLocaleString('es-MX')}` : '';
    }
}

async function search(query, { append = false } = {}) {
    const loader = document.getElementById('search-loader');
    const container = document.getElementById('search-results');

    if (searchState.loading) return;

    if (!append) {
        searchState.query = query;
        searchState.page = 0;
        searchState.loaded = 0;
        searchState.total = 0;
        searchState.hasMore = false;
        // Skeletons en el sitio de la grilla: el input no se mueve
        document.getElementById('search-empty').classList.add('hidden');
        showSkeletons(container);
        container.classList.remove('has-expanded');
        currentExpandedCard = null;
        selectedResultIndex = -1;
    }

    searchState.loading = true;
    updateResultsBar();
    loader.classList.remove('hidden');

    try {
        // Los filtros vacíos se OMITEN: mandar `language=` podría filtrar a
        // cero en vez de traer todo.
        let url = `${API}/api/search?q=${encodeURIComponent(searchState.query)}`
            + `&limit=${searchState.perPage}&page=${searchState.page}`;
        if (searchState.contentType !== 'book') {
            url += `&content_type=${encodeURIComponent(searchState.contentType)}`;
        }
        if (searchState.language) url += `&language=${encodeURIComponent(searchState.language)}`;
        if (searchState.sort && searchState.sort !== 'relevance') {
            url += `&sort=${encodeURIComponent(searchState.sort)}`;
        }
        const res = await fetch(url);
        const data = await res.json();

        loader.classList.add('hidden');
        searchState.loading = false;
        searchState.total = data.total || 0;
        searchState.hasMore = !!data.has_more;

        const results = data.results || [];
        if (searchState.page === 0 && !append) container.innerHTML = '';
        results.forEach(book => renderBookCard(book, container));
        searchState.loaded += results.length;

        if (searchState.loaded === 0 && !searchState.hasMore) {
            container.innerHTML = `
                <div class="text-center py-16 text-zinc-500">
                    <p class="text-lg">No books found for "${searchState.query}"</p>
                    <p class="text-sm mt-2 text-zinc-400">Try a different search term or ISBN</p>
                </div>
            `;
        }

        // Página sin libros pero con más páginas: seguir sola hasta encontrar
        // algo, si no el usuario vería "Cargar más" sin efecto visible.
        if (results.length === 0 && searchState.hasMore) {
            searchState.page += 1;
            updateResultsBar();
            return search(searchState.query, { append: true });
        }

        updateResultsBar();

        // Si el usuario cambio un filtro mientras cargaba, se repite ahora
        if (searchState.refilter) {
            searchState.refilter = false;
            return search(searchState.query);
        }
    } catch (err) {
        loader.classList.add('hidden');
        searchState.loading = false;
        updateResultsBar();
        if (!append) {
            container.innerHTML = `
                <div class="text-center py-16 text-red-600">
                    <p>Search failed. Please try again.</p>
                </div>
            `;
        }
    }
}

/* Cambiar cualquier filtro es una CONSULTA NUEVA: se reinicia a page 0 y se
   limpia el grid. No se re-filtra lo ya cargado. */
function applyFilterChange() {
    updateClearFiltersButton();
    if (!searchState.query) return;   // sin texto no hay nada que consultar

    // search() ignora llamadas mientras hay una peticion en vuelo. Sin esto,
    // cambiar un filtro durante la carga se descartaba en silencio y la
    // grilla quedaba sin corresponder a los selects.
    if (searchState.loading) {
        searchState.refilter = true;
        return;
    }
    search(searchState.query);
}

function updateClearFiltersButton() {
    const btn = document.getElementById('clear-filters');
    if (!btn) return;
    const active = !!searchState.language || searchState.sort !== 'relevance';
    btn.classList.toggle('hidden', !active);
}

async function loadSearchFilters() {
    // El vocabulario lo define el backend para no duplicarlo aquí.
    try {
        const res = await fetch(`${API}/api/search-filters`);
        const data = await res.json();
        const sel = document.getElementById('filter-language');
        if (!sel || !data.languages) return;
        for (const [code, label] of Object.entries(data.languages)) {
            const opt = document.createElement('option');
            opt.value = code;
            opt.textContent = label;
            sel.appendChild(opt);
        }
    } catch (err) {
        console.error('No se pudieron cargar los filtros:', err);
    }
}

function loadMoreResults() {
    if (!searchState.hasMore || searchState.loading) return;
    searchState.page += 1;
    search(searchState.query, { append: true });
}


const CHECK_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5"><path d="M20 6L9 17l-5-5"/></svg>';

// Cintillo para los libros que ya están en la carpeta de salida.
const LIBRARY_BADGE_HTML =
    `<span class="library-ribbon" title="Ya está descargado en tu carpeta de salida">${CHECK_SVG}En la biblioteca</span>`;

const COVER_CHECK_HTML = `<span class="cover-check">${CHECK_SVG}</span>`;

function formatDuration(seconds) {
    if (!seconds) return '';
    const h = Math.floor(seconds / 3600);
    const m = Math.round((seconds % 3600) / 60);
    return h ? `${h} h ${m} min` : `${m} min`;
}

function createBookCardHTML(book) {
    return `
        <!-- Tile (colapsado) -->
        <div class="book-summary cursor-pointer">
            <div class="cover-wrap tile-cover">
                <!-- Copia borrosa de la portada para rellenar el cuadrado sin recortar -->
                <img src="${book.cover_url}" alt="" class="tile-blur" aria-hidden="true" loading="lazy">
                <img src="${book.cover_url}" alt="${book.title}" class="tile-img" loading="lazy">
                ${book.in_library ? COVER_CHECK_HTML : ''}
                ${book.duration_seconds ? `<span class="tile-duration">${formatDuration(book.duration_seconds)}</span>` : ''}
                <div class="card-meta tile-ribbon">
                    ${book.in_library ? LIBRARY_BADGE_HTML : ''}
                </div>
            </div>
            <div class="tile-meta">
                <h3 class="tile-title">${book.title}</h3>
                <p class="tile-author">${book.authors?.join(', ') || 'Unknown Author'}</p>
            </div>
        </div>

        <!-- Expanded Content -->
        <div class="book-expanded hidden">
            <!-- Close Button -->
            <button class="close-btn absolute top-4 right-4 w-8 h-8 flex items-center justify-center bg-zinc-100 hover:bg-zinc-200 rounded-full transition-colors z-10">
                <svg class="w-4 h-4 text-zinc-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M18 6L6 18M6 6l12 12"/>
                </svg>
            </button>

            <div class="relative px-5 pb-5 pt-2 border-t border-zinc-100 animate-fade-in">
                <!-- Book Detail -->
                <div class="flex gap-5 py-5">
                    <img class="w-24 h-32 object-cover rounded-lg shadow-md flex-shrink-0" src="${getHighResCoverUrl(book.id)}" alt="${book.title} cover">
                    <div class="flex-1 min-w-0">
                        <h2 class="text-xl font-semibold text-zinc-900 leading-tight mb-1">${book.title}</h2>
                        <p class="text-[0.9375rem] text-zinc-500 mb-3">by ${book.authors?.join(', ') || 'Unknown Author'}</p>
                        <p class="text-sm text-zinc-500 mb-0.5">
                            <span class="text-zinc-400">Publisher:</span>
                            <span class="publisher-value text-zinc-500 animate-pulse-subtle">Loading...</span>
                        </p>
                        <p class="text-sm text-zinc-500 mb-3">
                            <span class="pages-label text-zinc-400">Pages:</span>
                            <span class="pages-value text-zinc-500 animate-pulse-subtle">Loading...</span>
                        </p>
                        <div class="book-description text-sm text-zinc-600 leading-relaxed max-h-20 overflow-y-auto pr-2 animate-pulse-subtle">
                            Loading description...
                        </div>
                    </div>
                </div>

                <!-- Format & Scope Section -->
                <div class="py-5 border-t border-zinc-100">
                    <!-- Step 1: Format Selection -->
                    <div class="mb-5">
                        <h4 class="flex items-center gap-2 text-[0.6875rem] font-semibold uppercase tracking-wide text-zinc-400 mb-3">
                            <span class="inline-flex items-center justify-center w-[18px] h-[18px] bg-oreilly-blue text-white text-[0.625rem] font-bold rounded-full">1</span>
                            Format
                        </h4>
                        <div class="format-options flex flex-wrap gap-1.5">
                            <label class="format-option cursor-pointer">
                                <input type="radio" name="format" value="markdown" checked class="sr-only peer">
                                <span class="flex items-center gap-1.5 px-3 py-2 bg-zinc-50 border border-zinc-200 rounded-lg text-sm font-medium text-zinc-600 transition-all peer-checked:border-oreilly-blue peer-checked:bg-oreilly-blue-light peer-checked:text-oreilly-blue-dark hover:bg-white hover:border-zinc-300">
                                    <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                        <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/>
                                        <path d="M7 15V9l2.5 3L12 9v6"/>
                                        <path d="M17 9v6l-2-2"/>
                                    </svg>
                                    Markdown
                                </span>
                            </label>
                            <label class="format-option cursor-pointer">
                                <input type="radio" name="format" value="json" class="sr-only peer">
                                <span class="flex items-center gap-1.5 px-3 py-2 bg-zinc-50 border border-zinc-200 rounded-lg text-sm font-medium text-zinc-600 transition-all peer-checked:border-oreilly-blue peer-checked:bg-oreilly-blue-light peer-checked:text-oreilly-blue-dark hover:bg-white hover:border-zinc-300">
                                    <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                        <path d="M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5c0 1.1.9 2 2 2h1"/>
                                        <path d="M16 3h1a2 2 0 0 1 2 2v5a2 2 0 0 0 2 2 2 2 0 0 0-2 2v5a2 2 0 0 1-2 2h-1"/>
                                    </svg>
                                    JSON
                                </span>
                            </label>
                            <label class="format-option cursor-pointer relative">
                                <input type="radio" name="format" value="toon" class="sr-only peer">
                                <span class="flex items-center gap-1.5 px-3 py-2 bg-zinc-50 border border-zinc-200 rounded-lg text-sm font-medium text-zinc-600 transition-all peer-checked:border-oreilly-blue peer-checked:bg-oreilly-blue-light peer-checked:text-oreilly-blue-dark hover:bg-white hover:border-zinc-300">
                                    <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                        <path d="M4 7V5a1 1 0 0 1 1-1h2"/>
                                        <path d="M17 4h2a1 1 0 0 1 1 1v2"/>
                                        <path d="M20 17v2a1 1 0 0 1-1 1h-2"/>
                                        <path d="M7 20H5a1 1 0 0 1-1-1v-2"/>
                                        <line x1="8" y1="12" x2="16" y2="12"/>
                                    </svg>
                                    TOON
                                </span>
                                <span class="absolute -top-1.5 -right-1.5 text-[0.5rem] font-bold uppercase px-1 py-px bg-emerald-500 text-white rounded shadow-sm peer-checked:bg-oreilly-blue">LLM</span>
                            </label>
                            <label class="format-option cursor-pointer">
                                <input type="radio" name="format" value="plaintext" class="sr-only peer">
                                <span class="flex items-center gap-1.5 px-3 py-2 bg-zinc-50 border border-zinc-200 rounded-lg text-sm font-medium text-zinc-600 transition-all peer-checked:border-oreilly-blue peer-checked:bg-oreilly-blue-light peer-checked:text-oreilly-blue-dark hover:bg-white hover:border-zinc-300">
                                    <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                                        <polyline points="14 2 14 8 20 8"/>
                                        <line x1="16" y1="13" x2="8" y2="13"/>
                                    </svg>
                                    Plain Text
                                </span>
                            </label>
                            <label class="format-option cursor-pointer">
                                <input type="radio" name="format" value="pdf" class="sr-only peer">
                                <span class="flex items-center gap-1.5 px-3 py-2 bg-zinc-50 border border-zinc-200 rounded-lg text-sm font-medium text-zinc-600 transition-all peer-checked:border-oreilly-blue peer-checked:bg-oreilly-blue-light peer-checked:text-oreilly-blue-dark hover:bg-white hover:border-zinc-300">
                                    <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                                        <polyline points="14 2 14 8 20 8"/>
                                        <line x1="16" y1="13" x2="8" y2="13"/>
                                        <line x1="16" y1="17" x2="8" y2="17"/>
                                    </svg>
                                    PDF
                                </span>
                            </label>
                            <label class="format-option cursor-pointer relative">
                                <input type="radio" name="format" value="chunks" class="sr-only peer">
                                <span class="flex items-center gap-1.5 px-3 py-2 bg-zinc-50 border border-zinc-200 rounded-lg text-sm font-medium text-zinc-600 transition-all peer-checked:border-oreilly-blue peer-checked:bg-oreilly-blue-light peer-checked:text-oreilly-blue-dark hover:bg-white hover:border-zinc-300">
                                    <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                        <rect x="3" y="3" width="7" height="7"/>
                                        <rect x="14" y="3" width="7" height="7"/>
                                        <rect x="3" y="14" width="7" height="7"/>
                                        <rect x="14" y="14" width="7" height="7"/>
                                    </svg>
                                    Chunks
                                </span>
                                <span class="absolute -top-1.5 -right-1.5 text-[0.5rem] font-bold uppercase px-1 py-px bg-emerald-500 text-white rounded shadow-sm peer-checked:bg-oreilly-blue">RAG</span>
                            </label>
                            <label class="format-option cursor-pointer">
                                <input type="radio" name="format" value="epub" class="sr-only peer">
                                <span class="flex items-center gap-1.5 px-3 py-2 bg-zinc-50 border border-zinc-200 rounded-lg text-sm font-medium text-zinc-600 transition-all peer-checked:border-oreilly-blue peer-checked:bg-oreilly-blue-light peer-checked:text-oreilly-blue-dark hover:bg-white hover:border-zinc-300">
                                    <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                        <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                                        <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
                                    </svg>
                                    EPUB
                                </span>
                            </label>
                        </div>
                    </div>

                    <!-- Step 2: Chapters Selection -->
                    <div class="chapters-selection">
                        <h4 class="flex items-center gap-2 text-[0.6875rem] font-semibold uppercase tracking-wide text-zinc-400 mb-3">
                            <span class="inline-flex items-center justify-center w-[18px] h-[18px] bg-oreilly-blue text-white text-[0.625rem] font-bold rounded-full">2</span>
                            Chapters
                        </h4>
                        <div class="chapters-options grid grid-cols-2 gap-2">
                            <label class="chapters-option cursor-pointer">
                                <input type="radio" name="chapters-scope" value="all" checked class="sr-only peer">
                                <span class="flex items-center gap-3 p-3 bg-zinc-50 border border-zinc-200 rounded-lg transition-all peer-checked:border-oreilly-blue peer-checked:bg-oreilly-blue-light hover:bg-white hover:border-zinc-300">
                                    <span class="flex items-center justify-center w-8 h-8 bg-white rounded-md shadow-sm">
                                        <svg class="w-4 h-4 text-zinc-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                            <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/>
                                            <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>
                                        </svg>
                                    </span>
                                    <span class="flex flex-col min-w-0">
                                        <span class="text-sm font-medium text-zinc-700">All Chapters</span>
                                        <span class="text-[0.6875rem] text-zinc-400">Download everything</span>
                                    </span>
                                </span>
                            </label>
                            <label class="chapters-option cursor-pointer">
                                <input type="radio" name="chapters-scope" value="select" class="sr-only peer">
                                <span class="flex items-center gap-3 p-3 bg-zinc-50 border border-zinc-200 rounded-lg transition-all peer-checked:border-oreilly-blue peer-checked:bg-oreilly-blue-light hover:bg-white hover:border-zinc-300">
                                    <span class="flex items-center justify-center w-8 h-8 bg-white rounded-md shadow-sm">
                                        <svg class="w-4 h-4 text-zinc-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                            <path d="M9 11l3 3L22 4"/>
                                            <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
                                        </svg>
                                    </span>
                                    <span class="flex flex-col min-w-0">
                                        <span class="text-sm font-medium text-zinc-700">Select Chapters</span>
                                        <span class="text-[0.6875rem] text-zinc-400">Pick which ones</span>
                                    </span>
                                </span>
                            </label>
                        </div>
                    </div>

                    <!-- Chapter Picker -->
                    <div class="chapters-picker hidden mt-4 p-4 bg-zinc-50 rounded-xl border border-zinc-200">
                        <div class="flex items-center justify-between pb-3 border-b border-zinc-200 mb-3">
                            <span class="chapters-summary text-sm font-medium text-zinc-600">All chapters</span>
                            <div class="flex gap-1">
                                <button class="select-all-btn px-2 py-1 text-xs font-medium text-oreilly-blue hover:bg-oreilly-blue-light rounded transition-colors">All</button>
                                <button class="select-none-btn px-2 py-1 text-xs font-medium text-oreilly-blue hover:bg-oreilly-blue-light rounded transition-colors">None</button>
                            </div>
                        </div>
                        <div class="chapters-list max-h-52 overflow-y-auto space-y-0.5"></div>
                    </div>

                    <!-- Step 3: Output Structure -->
                    <div class="output-selection mt-5">
                        <h4 class="flex items-center gap-2 text-[0.6875rem] font-semibold uppercase tracking-wide text-zinc-400 mb-3">
                            <span class="inline-flex items-center justify-center w-[18px] h-[18px] bg-oreilly-blue text-white text-[0.625rem] font-bold rounded-full">3</span>
                            Output
                        </h4>
                        <div class="output-options grid grid-cols-2 gap-2">
                            <label class="output-option cursor-pointer">
                                <input type="radio" name="output-style" value="combined" checked class="sr-only peer">
                                <span class="flex items-center gap-3 p-3 bg-zinc-50 border border-zinc-200 rounded-lg transition-all peer-checked:border-oreilly-blue peer-checked:bg-oreilly-blue-light hover:bg-white hover:border-zinc-300">
                                    <span class="flex items-center justify-center w-8 h-8 bg-white rounded-md shadow-sm">
                                        <svg class="w-4 h-4 text-zinc-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                                            <polyline points="14 2 14 8 20 8"/>
                                        </svg>
                                    </span>
                                    <span class="flex flex-col min-w-0">
                                        <span class="text-sm font-medium text-zinc-700">Combined</span>
                                        <span class="text-[0.6875rem] text-zinc-400">One book file</span>
                                    </span>
                                </span>
                            </label>
                            <label class="output-option cursor-pointer">
                                <input type="radio" name="output-style" value="separate" class="sr-only peer">
                                <span class="flex items-center gap-3 p-3 bg-zinc-50 border border-zinc-200 rounded-lg transition-all peer-checked:border-oreilly-blue peer-checked:bg-oreilly-blue-light hover:bg-white hover:border-zinc-300">
                                    <span class="flex items-center justify-center w-8 h-8 bg-white rounded-md shadow-sm">
                                        <svg class="w-4 h-4 text-zinc-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                            <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>
                                        </svg>
                                    </span>
                                    <span class="flex flex-col min-w-0">
                                        <span class="text-sm font-medium text-zinc-700">Separate</span>
                                        <span class="text-[0.6875rem] text-zinc-400">One file per chapter</span>
                                    </span>
                                </span>
                            </label>
                        </div>
                        <!-- Output locked notice -->
                        <div class="output-locked-notice hidden flex items-center gap-2 p-3 mt-2 bg-zinc-50 border border-dashed border-zinc-200 rounded-lg text-sm text-zinc-500">
                            <svg class="w-4 h-4 flex-shrink-0 text-zinc-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                                <circle cx="12" cy="12" r="10"/>
                                <line x1="12" y1="16" x2="12" y2="12"/>
                                <line x1="12" y1="8" x2="12.01" y2="8"/>
                            </svg>
                            <span>Combined only for this format</span>
                        </div>
                    </div>
                </div>

                <!-- Va en el cuerpo de la modal, NO dentro de Advanced Options:
                     se decide en cada descarga, asi que tiene que verse sin
                     desplegar nada. Solo para libros; un audiolibro no tiene
                     imagenes que omitir. -->
                <label class="book-only flex items-start gap-2 cursor-pointer border-t border-zinc-100 pt-4">
                    <input type="checkbox" class="skip-images w-4 h-4 mt-0.5 rounded border-zinc-300 text-oreilly-blue focus:ring-oreilly-blue/20">
                    <span>
                        <span class="text-sm font-medium text-zinc-700">Omitir imágenes</span>
                        <span class="block text-xs text-zinc-400">Más rápido y archivos más pequeños, pero el epub queda sin ilustraciones.</span>
                    </span>
                </label>

                <!-- Advanced Options -->
                <details class="advanced-options border-t border-zinc-100 pt-4">
                    <summary class="flex items-center gap-1.5 text-sm font-medium text-zinc-500 cursor-pointer select-none py-1 hover:text-zinc-700 transition-colors">
                        <svg class="toggle-icon w-3.5 h-3.5 transition-transform duration-150" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M9 18l6-6-6-6"/>
                        </svg>
                        Advanced Options
                    </summary>
                    <div class="pt-4 space-y-4">
                        <div class="book-only">
                            <label class="block text-[0.6875rem] font-semibold uppercase tracking-wide text-zinc-400 mb-2">Translate (local LLM)</label>
                            <select class="target-lang w-full px-3 py-2 text-sm bg-zinc-50 border border-zinc-200 rounded-lg text-zinc-600 focus:outline-none focus:border-oreilly-blue focus:bg-white transition-colors">
                                <option value="original" selected>Original (no translation)</option>
                                <option value="es-LATAM">Español (Latinoamérica)</option>
                            </select>
                            <span class="block text-xs text-zinc-400 mt-1">Uses your local Ollama model. Slower; code is left untouched.</span>
                        </div>

                        <div class="chunking-options hidden flex gap-4 p-4 bg-zinc-50 rounded-lg">
                            <div class="flex-1">
                                <label class="block text-[0.6875rem] font-semibold uppercase tracking-wide text-zinc-400 mb-2">Chunk Size (tokens)</label>
                                <input type="number" class="chunk-size-input w-full px-3 py-2 text-sm border border-zinc-200 rounded-lg focus:outline-none focus:border-oreilly-blue transition-colors" value="4000" min="500" max="16000">
                            </div>
                            <div class="flex-1">
                                <label class="block text-[0.6875rem] font-semibold uppercase tracking-wide text-zinc-400 mb-2">Overlap (tokens)</label>
                                <input type="number" class="chunk-overlap-input w-full px-3 py-2 text-sm border border-zinc-200 rounded-lg focus:outline-none focus:border-oreilly-blue transition-colors" value="200" min="0" max="1000">
                            </div>
                        </div>
                    </div>
                </details>

                <!-- Progress Section -->
                <div class="progress-section hidden py-5 border-t border-zinc-100">
                    <div class="flex justify-between items-center mb-2">
                        <span class="progress-label text-sm font-medium text-zinc-700">Downloading...</span>
                        <span class="progress-percent text-sm font-semibold text-oreilly-blue">0%</span>
                    </div>
                    <div class="h-1.5 bg-zinc-200 rounded-full overflow-hidden">
                        <div class="progress-fill h-full bg-oreilly-blue rounded-full transition-all duration-300" style="width: 0%"></div>
                    </div>
                    <p class="progress-status mt-2 text-sm text-zinc-500"></p>
                </div>

                <!-- Result Section -->
                <div class="result-section hidden py-5 border-t border-zinc-100">
                    <div class="flex items-center gap-2 mb-4 text-emerald-600 font-medium">
                        <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/>
                            <polyline points="22 4 12 14.01 9 11.01"/>
                        </svg>
                        <span>Download Complete</span>
                    </div>
                    <div class="result-files space-y-2"></div>
                </div>

                <!-- Action Bar -->
                <div class="flex justify-end gap-2 pt-5 border-t border-zinc-100">
                    <button class="cancel-btn hidden px-5 py-2 text-sm font-medium text-zinc-600 bg-white border border-zinc-300 rounded-lg hover:bg-zinc-50 transition-colors">Cancel</button>
                    <button class="download-btn px-6 py-2 text-sm font-medium text-white bg-oreilly-blue hover:bg-oreilly-blue-dark rounded-lg transition-colors disabled:bg-zinc-300 disabled:cursor-not-allowed">Download</button>
                </div>
            </div>
        </div>
    `;
}

function setupBookCardEvents(div, book) {
    // Click on summary to expand
    div.querySelector('.book-summary').onclick = () => expandBook(div, book.id);

    // Close button
    div.querySelector('.close-btn').onclick = (e) => {
        e.stopPropagation();
        collapseBook();
    };

    // Download button
    div.querySelector('.download-btn').onclick = (e) => {
        e.stopPropagation();
        download(div);
    };

    // Cancel button
    div.querySelector('.cancel-btn').onclick = (e) => {
        e.stopPropagation();
        cancelDownload(div);
    };

    // Format selection - update scope visibility
    div.querySelectorAll('input[name="format"]').forEach(radio => {
        radio.addEventListener('change', (e) => {
            handleFormatChange(div, e.target.value, book.id);
        });
    });

    // Chapters scope selection - show/hide chapter picker
    div.querySelectorAll('input[name="chapters-scope"]').forEach(radio => {
        radio.addEventListener('change', (e) => {
            handleChaptersScopeChange(div, e.target.value, book.id);
        });
    });

    // Output style selection
    div.querySelectorAll('input[name="output-style"]').forEach(radio => {
        radio.addEventListener('change', (e) => {
            // No special handling needed, just tracks selection
        });
    });

    // Chapter selection buttons
    div.querySelector('.select-all-btn').onclick = (e) => {
        e.stopPropagation();
        selectAllChapters(div, true);
    };
    div.querySelector('.select-none-btn').onclick = (e) => {
        e.stopPropagation();
        selectAllChapters(div, false);
    };

    // Advanced options toggle icon rotation
    const advancedOptions = div.querySelector('.advanced-options');
    advancedOptions.addEventListener('toggle', () => {
        const icon = advancedOptions.querySelector('.toggle-icon');
        if (advancedOptions.open) {
            icon.style.transform = 'rotate(90deg)';
        } else {
            icon.style.transform = 'rotate(0deg)';
        }
    });
}

// Formats that only support combined output (entire book as single file)
const BOOK_ONLY_FORMATS = ['epub', 'chunks'];

// Structured single-file formats: chapter selection works, but output is always one combined file
const SINGLE_FILE_FORMATS = ['json', 'toon'];

function handleFormatChange(cardElement, format, bookId) {
    const outputSelection = cardElement.querySelector('.output-selection');
    const outputOptions = cardElement.querySelector('.output-options');
    const lockedNotice = cardElement.querySelector('.output-locked-notice');
    const chunkingOptions = cardElement.querySelector('.chunking-options');
    const chaptersPicker = cardElement.querySelector('.chapters-picker');

    // Show/hide chunking options
    chunkingOptions.classList.toggle('hidden', format !== 'chunks');

    if (BOOK_ONLY_FORMATS.includes(format) || SINGLE_FILE_FORMATS.includes(format)) {
        // Lock to "Combined" for EPUB/Chunks (no chapter output) and JSON/TOON
        // (structured single-file formats) - hide output options, show notice
        outputOptions.classList.add('hidden');
        lockedNotice.classList.remove('hidden');

        // Reset to combined output
        const combinedRadio = cardElement.querySelector('input[name="output-style"][value="combined"]');
        if (combinedRadio) combinedRadio.checked = true;
    } else {
        // Show all output options
        outputOptions.classList.remove('hidden');
        lockedNotice.classList.add('hidden');
    }

    // Check current chapters scope and show chapter picker if needed
    const currentChaptersScope = cardElement.querySelector('input[name="chapters-scope"]:checked')?.value;
    if (currentChaptersScope === 'select') {
        loadChaptersIfNeeded(cardElement, bookId);
        chaptersPicker.classList.remove('hidden');
    }
}

function handleChaptersScopeChange(cardElement, chaptersScope, bookId) {
    const chaptersPicker = cardElement.querySelector('.chapters-picker');

    if (chaptersScope === 'select') {
        loadChaptersIfNeeded(cardElement, bookId);
        chaptersPicker.classList.remove('hidden');
    } else {
        chaptersPicker.classList.add('hidden');
    }
}

async function loadChaptersIfNeeded(cardElement, bookId) {
    if (chaptersCache[bookId]) {
        // Already loaded
        if (cardElement.querySelector('.chapters-list').children.length === 0) {
            renderChapters(cardElement, chaptersCache[bookId]);
        }
        return;
    }

    const listContainer = cardElement.querySelector('.chapters-list');
    listContainer.innerHTML = '<p class="text-sm text-zinc-400 animate-pulse-subtle py-2">Loading chapters...</p>';

    try {
        const endpoint = searchState.contentType === 'audiobook' ? 'audiobook' : 'book';
        const res = await fetch(`${API}/api/${endpoint}/${bookId}/chapters`);
        const data = await res.json();
        chaptersCache[bookId] = data.chapters;
        renderChapters(cardElement, data.chapters);
    } catch (err) {
        listContainer.innerHTML = '<p class="text-sm text-red-600 py-2">Failed to load chapters</p>';
    }
}

async function expandBook(cardElement, bookId) {
    // Don't allow switching to a different book while one is downloading.
    if (downloadInProgress && currentExpandedCard && currentExpandedCard !== cardElement) {
        return;
    }
    if (currentExpandedCard && currentExpandedCard !== cardElement) {
        collapseBook();
    }

    if (cardElement.classList.contains('expanded')) {
        return;
    }

    const expanded = cardElement.querySelector('.book-expanded');

    // Add expanded styles
    cardElement.classList.add('expanded');
    cardElement.classList.remove('hover:border-zinc-300', 'hover:shadow-card-hover');
    cardElement.classList.add('border-oreilly-blue', 'shadow-card-expanded');

    // Rotate expand icon
    const expandIcon = cardElement.querySelector('.expand-icon');
    if (expandIcon) expandIcon.style.transform = 'rotate(180deg)';

    expanded.classList.remove('hidden');
    document.getElementById('search-results').classList.add('has-expanded');
    currentExpandedCard = cardElement;

    // El panel flota centrado en pantalla: no hace falta desplazar la lista.
    // Se oscurece el fondo y se congela su scroll mientras está abierto.
    document.getElementById('card-backdrop').classList.remove('hidden');
    document.body.style.overflow = 'hidden';

    // Resume progress if this book already has a download/translation running
    // in the background (closing the card only stops polling, not the work).
    try {
        const pr = await fetch(`${API}/api/progress`);
        const pdata = await pr.json();
        const active = pdata.status &&
            !['completed', 'error', 'cancelled'].includes(pdata.status);
        if (active && pdata.book_id === bookId) {
            cardElement.querySelector('.progress-section').classList.remove('hidden');
            cardElement.querySelector('.download-btn').classList.add('hidden');
            cardElement.querySelector('.cancel-btn').classList.remove('hidden');
            pollProgress(cardElement);
        }
    } catch (e) { /* no active download */ }

    // La carpeta y las preferencias son globales: no hay nada que sincronizar
    // al abrir la tarjeta.

    // Un audiolibro no tiene formatos de salida ni "combined/separate":
    // se baja un .m4a por capítulo. Se ocultan esos pasos.
    const isAudio = searchState.contentType === 'audiobook';
    const formatSection = expanded.querySelector('.format-selection')
        || expanded.querySelector('.output-selection')?.parentElement?.querySelector('div');
    expanded.querySelectorAll('.output-selection').forEach(el => {
        el.style.display = isAudio ? 'none' : '';
    });
    // Traducción y "skip images" son del pipeline de texto: no aplican a audio
    expanded.querySelectorAll('.book-only').forEach(el => {
        el.style.display = isAudio ? 'none' : '';
    });
    const pagesLabel = expanded.querySelector('.pages-label');
    if (pagesLabel) pagesLabel.textContent = isAudio ? 'Duración:' : 'Pages:';
    const fmtRadios = expanded.querySelector('input[name="format"]');
    if (fmtRadios) {
        const fmtBlock = fmtRadios.closest('.format-selection')
            || fmtRadios.closest('div')?.parentElement;
        if (fmtBlock) fmtBlock.style.display = isAudio ? 'none' : '';
    }

    // Fetch book details
    try {
        const endpoint = isAudio ? 'audiobook' : 'book';
        const res = await fetch(`${API}/api/${endpoint}/${bookId}`);
        const book = await res.json();

        const publisherEl = expanded.querySelector('.publisher-value');
        const pagesEl = expanded.querySelector('.pages-value');
        const descEl = expanded.querySelector('.book-description');

        publisherEl.textContent = book.publishers?.join(', ') || 'Unknown';
        publisherEl.classList.remove('animate-pulse-subtle');

        // Para audio la métrica útil es la duración, no las páginas
        pagesEl.textContent = isAudio
            ? (formatDuration(book.duration_seconds) || 'N/A')
            : (book.virtual_pages || 'N/A');
        pagesEl.classList.remove('animate-pulse-subtle');

        descEl.innerHTML = book.description || (isAudio
            ? `${book.chapters_count || 0} capítulos · ${formatDuration(book.duration_seconds)}`
            : 'No description available.');
        descEl.classList.remove('animate-pulse-subtle');
    } catch (error) {
        const descEl = expanded.querySelector('.book-description');
        descEl.textContent = 'Failed to load details.';
        descEl.classList.remove('animate-pulse-subtle');
    }
}

function collapseBook() {
    // Locked while a download/translation is running — stay on the active card.
    if (downloadInProgress) return;
    if (currentExpandedCard) {
        const expanded = currentExpandedCard.querySelector('.book-expanded');

        // Remove expanded styles
        currentExpandedCard.classList.remove('expanded', 'border-oreilly-blue', 'shadow-card-expanded');
        currentExpandedCard.classList.add('hover:border-zinc-300', 'hover:shadow-card-hover');

        // Reset expand icon
        const expandIcon = currentExpandedCard.querySelector('.expand-icon');
        if (expandIcon) expandIcon.style.transform = 'rotate(0deg)';

        expanded.classList.add('hidden');

        document.getElementById('search-results').classList.remove('has-expanded');
        document.getElementById('card-backdrop').classList.add('hidden');
        document.body.style.overflow = '';
        currentExpandedCard = null;
    }
}

function renderChapters(cardElement, chapters) {
    const listContainer = cardElement.querySelector('.chapters-list');

    listContainer.innerHTML = chapters.map((ch) => `
        <label class="chapter-item flex items-center gap-3 px-2 py-2 rounded-lg cursor-pointer hover:bg-zinc-100 transition-colors">
            <input type="checkbox" class="chapter-checkbox w-4 h-4 rounded border-zinc-300 text-oreilly-blue focus:ring-oreilly-blue/20" data-index="${ch.index}" checked>
            <span class="flex-1 text-sm text-zinc-700 truncate">${ch.title || 'Chapter ' + (ch.index + 1)}</span>
            ${ch.pages ? `<span class="text-xs text-zinc-400 flex-shrink-0">${ch.pages}p</span>` : ''}
        </label>
    `).join('');

    updateChapterCount(cardElement);

    listContainer.querySelectorAll('.chapter-checkbox').forEach(cb => {
        cb.addEventListener('change', () => updateChapterCount(cardElement));
    });
}

function updateChapterCount(cardElement) {
    const checkboxes = cardElement.querySelectorAll('.chapter-checkbox');
    const checked = cardElement.querySelectorAll('.chapter-checkbox:checked');
    const summaryEl = cardElement.querySelector('.chapters-summary');

    if (checked.length === checkboxes.length) {
        summaryEl.textContent = `All ${checkboxes.length} chapters`;
    } else if (checked.length === 0) {
        summaryEl.textContent = 'No chapters selected';
    } else {
        summaryEl.textContent = `${checked.length} of ${checkboxes.length} chapters`;
    }
}

function selectAllChapters(cardElement, selectAll) {
    cardElement.querySelectorAll('.chapter-checkbox').forEach(cb => cb.checked = selectAll);
    updateChapterCount(cardElement);
}

async function download(cardElement) {
    const bookId = cardElement.dataset.bookId;

    // Un audiolibro no tiene formato de salida ni combined/separate: se baja un
    // .m4a por capítulo. Antes pasaba por la validación de formato y, como el
    // bloque está oculto, no había radio marcado y la descarga se abortaba en
    // silencio. Los audiolibros se saltan toda esa lógica.
    const isAudio = searchState.contentType === 'audiobook';

    let format = null;
    let finalFormat = null;

    if (!isAudio) {
        const formatRadio = cardElement.querySelector('input[name="format"]:checked');
        format = formatRadio ? formatRadio.value : null;

        if (!format) {
            const formatOptions = cardElement.querySelector('.format-options');
            if (formatOptions) {
                formatOptions.classList.add('animate-shake');
                setTimeout(() => formatOptions.classList.remove('animate-shake'), 500);
            }
            return;
        }

        const outputStyleRadio = cardElement.querySelector('input[name="output-style"]:checked');
        const outputStyle = outputStyleRadio ? outputStyleRadio.value : 'combined';

        finalFormat = format;
        if (outputStyle === 'separate' && !BOOK_ONLY_FORMATS.includes(format) && !SINGLE_FILE_FORMATS.includes(format)) {
            finalFormat = `${format}-chapters`;
        }
    }

    // La selección de capítulos sí aplica a ambos tipos
    const chaptersScopeRadio = cardElement.querySelector('input[name="chapters-scope"]:checked');
    const chaptersScope = chaptersScopeRadio ? chaptersScopeRadio.value : 'all';

    // Get selected chapters if chapters scope is 'select'
    let selectedChapters = null;
    if (chaptersScope === 'select') {
        const chapterCheckboxes = cardElement.querySelectorAll('.chapter-checkbox');
        const checkedBoxes = cardElement.querySelectorAll('.chapter-checkbox:checked');

        if (checkedBoxes.length === 0) {
            // No chapters selected - shake the chapter picker
            const chaptersPicker = cardElement.querySelector('.chapters-picker');
            chaptersPicker.classList.add('animate-shake');
            setTimeout(() => chaptersPicker.classList.remove('animate-shake'), 500);
            return;
        }

        if (checkedBoxes.length < chapterCheckboxes.length) {
            selectedChapters = Array.from(checkedBoxes).map(cb => parseInt(cb.dataset.index));
        }
        // Note: separate/combined is determined by outputStyle, not by chapter selection
    }

    const progressSection = cardElement.querySelector('.progress-section');
    const resultSection = cardElement.querySelector('.result-section');
    const downloadBtn = cardElement.querySelector('.download-btn');
    const cancelBtn = cardElement.querySelector('.cancel-btn');
    const progressFill = cardElement.querySelector('.progress-fill');

    progressSection.classList.remove('hidden');
    resultSection.classList.add('hidden');
    downloadBtn.classList.add('hidden');
    cancelBtn.classList.remove('hidden');
    progressFill.style.width = '0%';

    // Lock the UI to this card until the download/translation finishes.
    setDownloadLock(true);

    const requestBody = isAudio
        ? { book_id: bookId, content_type: 'audiobook' }
        : { book_id: bookId, format: finalFormat };
    if (selectedChapters !== null) {
        requestBody.chapters = selectedChapters;
    }
    if (!isAudio && format === 'chunks') {
        const chunkSize = parseInt(cardElement.querySelector('.chunk-size-input').value) || 4000;
        const chunkOverlap = parseInt(cardElement.querySelector('.chunk-overlap-input').value) || 200;
        requestBody.chunking = {
            chunk_size: chunkSize,
            overlap: chunkOverlap
        };
    }
    if (!isAudio) {
        // Por libro: hay títulos que sin sus figuras no se entienden y otros
        // donde las imágenes son puro peso.
        if (cardElement.querySelector('.skip-images').checked) {
            requestBody.skip_images = true;
        }
        const targetLangEl = cardElement.querySelector('.target-lang');
        if (targetLangEl && targetLangEl.value && targetLangEl.value !== 'original') {
            requestBody.target_lang = targetLangEl.value;
        }
    }

    // La descarga va a la cache en los dos casos; esta preferencia decide si al
    // terminar se pasa a la biblioteca o se queda esperando.
    requestBody.transfer = downloadPrefs.transferAfter;

    try {
        const res = await fetch(`${API}/api/download`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody)
        });

        const result = await res.json();

        if (result.error) {
            cardElement.querySelector('.progress-status').textContent = `Error: ${result.error}`;
            downloadBtn.classList.remove('hidden');
            cancelBtn.classList.add('hidden');
            setDownloadLock(false);  // POST rejected (e.g. already in progress) — unlock
            return;
        }

        pollProgress(cardElement);
    } catch (err) {
        cardElement.querySelector('.progress-status').textContent = 'Download failed. Please try again.';
        downloadBtn.classList.remove('hidden');
        cancelBtn.classList.add('hidden');
        setDownloadLock(false);  // request failed — unlock
    }
}

async function cancelDownload(cardElement) {
    const cancelBtn = cardElement.querySelector('.cancel-btn');
    cancelBtn.disabled = true;
    cancelBtn.textContent = 'Cancelling...';

    try {
        await fetch(`${API}/api/cancel`, { method: 'POST' });
    } catch (err) {
        console.error('Cancel request failed:', err);
    }
}

function formatETA(seconds) {
    if (seconds < 60) return `${seconds}s`;
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    if (mins < 60) return secs > 0 ? `${mins}m ${secs}s` : `${mins}m`;
    const hours = Math.floor(mins / 60);
    const remainMins = mins % 60;
    return `${hours}h ${remainMins}m`;
}

async function pollProgress(cardElement) {
    try {
        const res = await fetch(`${API}/api/progress`);
        const data = await res.json();

        const progressFill = cardElement.querySelector('.progress-fill');
        const progressStatus = cardElement.querySelector('.progress-status');
        const progressPercent = cardElement.querySelector('.progress-percent');
        const progressSection = cardElement.querySelector('.progress-section');
        const resultSection = cardElement.querySelector('.result-section');
        const downloadBtn = cardElement.querySelector('.download-btn');
        const cancelBtn = cardElement.querySelector('.cancel-btn');

        let status = data.status || 'waiting';
        const details = [];

        // Human-readable phase label shown above the bar. Distinguishes the
        // translation phase so the user knows why it's slow.
        const STATUS_LABELS = {
            starting: 'Starting...',
            fetching_metadata: 'Fetching metadata...',
            fetching_chapters: 'Fetching chapters...',
            downloading_cover: 'Downloading cover...',
            transferring: 'Pasando a la biblioteca...',
            processing_chapters: 'Downloading chapters...',
            downloading_audio: '🎧 Descargando audio...',
            translating_chapters: '🌐 Translating (local LLM)...',
            downloading_assets: 'Downloading images & styles...',
            generating_epub: 'Building EPUB...',
            generating_markdown: 'Building Markdown...',
            generating_pdf: 'Building PDF...',
            generating_pdf_chapters: 'Building PDFs...',
            generating_plaintext: 'Building text...',
            generating_json: 'Building JSON...',
            generating_toon: 'Building TOON...',
            generating_chunks: 'Building chunks...',
            completed: 'Done',
        };
        const progressLabel = cardElement.querySelector('.progress-label');
        if (progressLabel && STATUS_LABELS[data.status]) {
            progressLabel.textContent = STATUS_LABELS[data.status];
        }

        if (data.current_chapter && data.total_chapters) {
            details.push(`Chapter ${data.current_chapter}/${data.total_chapters}`);
        }

        if (typeof data.percentage === 'number') {
            progressFill.style.width = `${data.percentage}%`;
            progressPercent.textContent = `${data.percentage}%`;
        }

        if (data.eta_seconds && data.eta_seconds > 0) {
            details.push(`~${formatETA(data.eta_seconds)} remaining`);
        }

        if (data.chapter_title) {
            const title = data.chapter_title.length > 40
                ? data.chapter_title.substring(0, 40) + '...'
                : data.chapter_title;
            status = title;
        }

        // During translation, surface the backend message (e.g. "Traduciendo
        // capítulo X/Y") so the phase is unmistakable.
        if ((data.status === 'translating_chapters' || data.status === 'downloading_audio')
            && data.message) {
            details.unshift(data.message);
        }

        progressStatus.textContent = details.length > 0 ? details.join(' • ') : status;

        function restoreButtons() {
            downloadBtn.classList.remove('hidden');
            downloadBtn.disabled = false;
            cancelBtn.classList.add('hidden');
            cancelBtn.disabled = false;
            cancelBtn.textContent = 'Cancel';
            setDownloadLock(false);  // unlock the rest of the UI
        }

        if (data.status === 'completed') {
            restoreButtons();
            progressSection.classList.add('hidden');
            resultSection.classList.remove('hidden');

            // The book now lives in the output dir: mark it right away so the
            // user doesn't have to re-run the search to see it.
            cardElement.classList.add('in-library');
            const meta = cardElement.querySelector('.card-meta');
            if (meta && !meta.querySelector('.library-ribbon')) {
                meta.insertAdjacentHTML('beforeend', LIBRARY_BADGE_HTML);
            }
            const coverWrap = cardElement.querySelector('.cover-wrap');
            if (coverWrap && !coverWrap.querySelector('.cover-check')) {
                coverWrap.insertAdjacentHTML('beforeend', COVER_CHECK_HTML);
            }

            let filesHTML = '';
            if (data.epub) filesHTML += createFileResultHTML('EPUB', data.epub);
            if (data.pdf) {
                if (Array.isArray(data.pdf)) {
                    filesHTML += `<div class="flex items-center gap-3 px-4 py-3 bg-zinc-50 rounded-lg text-sm"><span class="font-medium text-zinc-700 min-w-[70px]">PDF</span><span class="flex-1 font-mono text-xs text-zinc-500 truncate">${data.pdf.length} chapter files</span></div>`;
                } else {
                    filesHTML += createFileResultHTML('PDF', data.pdf);
                }
            }
            if (data.markdown) filesHTML += createFileResultHTML('Markdown', data.markdown);
            if (data.plaintext) filesHTML += createFileResultHTML('Plain Text', data.plaintext);
            if (data.json) filesHTML += createFileResultHTML('JSON', data.json);
            if (data.chunks) filesHTML += createFileResultHTML('Chunks', data.chunks);

            if (data.format_errors) {
                const failed = Object.entries(data.format_errors)
                    .map(([fmt, msg]) => `<div class="mt-1"><strong>${fmt}</strong>: ${msg}</div>`)
                    .join('');
                filesHTML += `
                    <div class="flex gap-2 px-4 py-3 mt-2 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-800">
                        <span>&#9888;</span>
                        <div><strong>Algunos formatos no se generaron:</strong>${failed}</div>
                    </div>`;
            }

            cardElement.querySelector('.result-files').innerHTML = filesHTML;
        } else if (data.status === 'error') {
            restoreButtons();
            progressStatus.textContent = `Error: ${data.error}`;
        } else if (data.status === 'cancelled') {
            restoreButtons();
            progressSection.classList.add('hidden');
        } else {
            progressSection.classList.remove('hidden');
            resultSection.classList.add('hidden');
            downloadBtn.classList.add('hidden');
            cancelBtn.classList.remove('hidden');
            setTimeout(() => pollProgress(cardElement), 500);
        }
    } catch (err) {
        console.error('Progress polling failed:', err);
        setTimeout(() => pollProgress(cardElement), 1000);
    }
}

function createFileResultHTML(label, path) {
    const escapedPath = path.replace(/'/g, "\\'");
    return `
        <div class="flex items-center gap-3 px-4 py-3 bg-zinc-50 rounded-lg text-sm">
            <span class="font-medium text-zinc-700 min-w-[70px]">${label}</span>
            <span class="flex-1 font-mono text-xs text-zinc-500 truncate" title="${path}">${path}</span>
            <button class="px-2 py-1 text-xs font-medium text-oreilly-blue hover:bg-oreilly-blue-light rounded transition-colors" onclick="revealFile('${escapedPath}')">Reveal</button>
        </div>
    `;
}

async function revealFile(path) {
    try {
        const res = await fetch(`${API}/api/reveal`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path })
        });
        const data = await res.json();
        if (data.error) {
            console.error('Reveal failed:', data.error);
        }
    } catch (err) {
        console.error('Reveal request failed:', err);
    }
}

async function browseLibraryDir() {
    const browseBtn = document.getElementById('settings-browse-btn');
    browseBtn.disabled = true;
    browseBtn.textContent = 'Abriendo...';

    try {
        const res = await fetch(`${API}/api/settings/library-dir`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ browse: true })
        });
        const data = await res.json();
        // El selector solo devuelve la ruta; guardarla es un segundo paso, para
        // que cancelar el dialogo no cambie nada.
        if (data.success && data.path) await saveLibraryDir(data.path);
    } catch (err) {
        console.error('El selector de carpetas falló:', err);
    }

    browseBtn.disabled = false;
    browseBtn.textContent = 'Examinar';
}

async function saveLibraryDir(path) {
    const state = document.getElementById('settings-library-state');
    try {
        const res = await fetch(`${API}/api/settings/library-dir`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: path })
        });
        const data = await res.json();
        if (data.error) {
            state.classList.remove('hidden');
            state.className = 'mt-2 text-xs font-medium text-red-600';
            state.textContent = data.error;
            return;
        }
        librarySettings.dir = data.path;
        librarySettings.isDefault = !!data.is_default;
        librarySettings.available = true;
        renderSettings();
        flashSaved();
        // La biblioteca cambio de sitio: lo que se ve tiene que reflejarlo
        if (typeof loadLibrary === 'function') loadLibrary({ refresh: true });
    } catch (err) {
        console.error('No se pudo guardar la carpeta:', err);
    }
}

function updateSelectedResult() {
    const results = document.querySelectorAll('.book-card');
    results.forEach((r, i) => {
        if (i === selectedResultIndex) {
            r.classList.add('ring-2', 'ring-oreilly-blue/30');
        } else {
            r.classList.remove('ring-2', 'ring-oreilly-blue/30');
        }
    });
    if (selectedResultIndex >= 0 && results[selectedResultIndex]) {
        results[selectedResultIndex].scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
}

document.addEventListener('DOMContentLoaded', () => {
    // Auth
    checkAuth();
    loadDefaultOutputDir();

    // Cookie modal
    document.getElementById('login-btn').onclick = showCookieModal;
    document.getElementById('cancel-modal-btn').onclick = hideCookieModal;
    document.getElementById('save-cookies-btn').onclick = saveCookies;
    document.getElementById('cookie-modal').onclick = (e) => {
        if (e.target.id === 'cookie-modal') hideCookieModal();
    };

    // Resultados por página: al cambiarlo, se repite la búsqueda desde cero
    const perPageSelect = document.getElementById('per-page');
    if (perPageSelect) {
        searchState.perPage = Number(perPageSelect.value) || 25;
        perPageSelect.addEventListener('change', () => {
            searchState.perPage = Number(perPageSelect.value) || 25;
            if (searchState.query) search(searchState.query);
        });
    }

    const loadMoreBtn = document.getElementById('load-more');
    if (loadMoreBtn) loadMoreBtn.onclick = loadMoreResults;

    const langSelect = document.getElementById('filter-language');
    if (langSelect) {
        langSelect.addEventListener('change', () => {
            searchState.language = langSelect.value;
            applyFilterChange();
        });
    }

    const sortSelect = document.getElementById('filter-sort');
    if (sortSelect) {
        searchState.sort = sortSelect.value || 'relevance';
        sortSelect.addEventListener('change', () => {
            searchState.sort = sortSelect.value || 'relevance';
            applyFilterChange();
        });
    }

    const clearBtn = document.getElementById('clear-filters');
    if (clearBtn) {
        clearBtn.onclick = () => {
            searchState.language = '';
            searchState.sort = 'relevance';
            if (langSelect) langSelect.value = '';
            if (sortSelect) sortSelect.value = 'relevance';
            applyFilterChange();
        };
    }

    // Conmutador Libros / Audiolibros: cambia el tipo y repite la consulta
    // Volver al inicio desde cualquier seccion
    const homeLink = document.getElementById('home-link');
    if (homeLink) {
        homeLink.onclick = () => {
            if (downloadInProgress) return;
            goToSection('book');
        };
    }

    document.querySelectorAll('.ct-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            if (downloadInProgress) return;
            let ct = tab.dataset.ct;
            // "Mi biblioteca" alterna: pulsarlo estando activo vuelve a Libros
            if (ct === searchState.contentType) {
                if (ct !== 'library') return;
                ct = 'book';
            }
            goToSection(ct);
        });
    });

    initTheme();

    document.querySelectorAll('.suggest-chip').forEach(function (chip) {
        chip.onclick = function () {
            const input = document.getElementById('search-input');
            input.value = chip.dataset.q;
            input.dispatchEvent(new Event('input', { bubbles: true }));
            input.focus();
        };
    });

    const sbToggle = document.getElementById('sidebar-toggle');
    if (sbToggle) {
        sbToggle.onclick = function () {
            const bar = document.getElementById('library-sidebar');
            const open = bar.classList.toggle('is-expanded');
            sbToggle.setAttribute('aria-expanded', String(open));
        };
    }

    setSearchState('empty');

    const libSort = document.getElementById('library-sort');
    if (libSort) {
        libSort.addEventListener('change', () => {
            libraryState.sort = libSort.value || 'title';
            loadLibrary();
        });
    }

    const transferAll = document.getElementById('transfer-all-btn');
    if (transferAll) {
        // `all` lo resuelve el servidor sobre el conjunto local completo, no
        // sobre lo que este filtrado en pantalla.
        transferAll.addEventListener('click', () => startTransfer({ all: true }));
    }

    // --- Menú de ajustes ---
    const settingsBtn = document.getElementById('settings-btn');
    if (settingsBtn) settingsBtn.addEventListener('click', openSettings);

    const settingsClose = document.getElementById('settings-close-btn');
    if (settingsClose) settingsClose.addEventListener('click', closeSettings);

    document.querySelectorAll('[data-close-settings]').forEach(function (el) {
        el.addEventListener('click', closeSettings);
    });

    const settingsBrowse = document.getElementById('settings-browse-btn');
    if (settingsBrowse) settingsBrowse.addEventListener('click', () => browseLibraryDir());

    const settingsReset = document.getElementById('settings-reset-btn');
    // Ruta vacía = el default dentro de output, que lo resuelve el servidor;
    // así no hay que escribirla a mano.
    if (settingsReset) settingsReset.addEventListener('click', () => saveLibraryDir(''));

    // Se guarda al vuelo: sin botón de guardar no hay estado a medias entre lo
    // que ves marcado y lo que el servidor tiene.
    ['settings-transfer-after'].forEach(function (id) {
        const box = document.getElementById(id);
        if (box) box.addEventListener('change', savePrefs);
    });

    document.addEventListener('keydown', function (e) {
        const modal = document.getElementById('settings-modal');
        if (e.key === 'Escape' && modal && !modal.classList.contains('hidden')) {
            closeSettings();
        }
    });

    const libClear = document.getElementById('library-clear');
    if (libClear) libClear.onclick = () => {
        libraryState.facets = {};
        libraryState.q = '';
        document.getElementById('search-input').value = '';
        loadLibrary();
    };

    const libRefresh = document.getElementById('library-refresh');
    if (libRefresh) libRefresh.onclick = async () => {
        libRefresh.disabled = true;
        libRefresh.textContent = 'Reindexando...';
        await loadLibrary({ refresh: true });
        libRefresh.disabled = false;
        libRefresh.textContent = 'Reindexar carpeta';
    };

    loadSearchFilters();

    // Search
    let searchTimeout;
    const searchInput = document.getElementById('search-input');

    searchInput.addEventListener('input', (e) => {
        clearTimeout(searchTimeout);
        const query = e.target.value.trim();

        // En la biblioteca el filtrado es local: responde de inmediato
        if (searchState.contentType === 'library') {
            libraryState.q = query;
            searchTimeout = setTimeout(loadLibrary, 120);
            return;
        }

        if (query.length >= 2) {
            searchTimeout = setTimeout(() => search(query), 300);
        } else if (query.length === 0) {
            currentExpandedCard = null;
            searchState.loaded = 0;
            searchState.total = 0;
            searchState.hasMore = false;
            searchState.query = '';
            setSearchState('empty');
        }
    });

    // Click outside to collapse
    document.addEventListener('click', (e) => {
        if (currentExpandedCard && !currentExpandedCard.contains(e.target)) {
            collapseBook();
        }
    });

    // Keyboard navigation
    document.addEventListener('keydown', (e) => {
        const results = document.querySelectorAll('.book-card');
        const searchInput = document.getElementById('search-input');

        if (e.key === 'Escape') {
            if (currentExpandedCard) {
                collapseBook();
                e.preventDefault();
            }
            return;
        }

        if (e.key === 'Enter' && document.activeElement === searchInput) {
            clearTimeout(searchTimeout);
            const query = searchInput.value.trim();
            if (query.length >= 2) {
                search(query);
            }
            e.preventDefault();
            return;
        }

        if (!results.length || currentExpandedCard) return;

        if (e.key === 'ArrowDown') {
            e.preventDefault();
            selectedResultIndex = Math.min(selectedResultIndex + 1, results.length - 1);
            updateSelectedResult();
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            selectedResultIndex = Math.max(selectedResultIndex - 1, 0);
            updateSelectedResult();
        } else if (e.key === 'Enter' && selectedResultIndex >= 0) {
            e.preventDefault();
            const selected = results[selectedResultIndex];
            if (selected) {
                expandBook(selected, selected.dataset.bookId);
            }
        }
    });
});
