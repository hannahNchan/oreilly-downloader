/* ===========================================================================
   "Para después": títulos guardados para descargar más tarde.

   Ojo con el nombre: en la app ya existían "favoritos", que son la estrella
   sobre obras que YA tienes en disco, para fijarlas arriba en la biblioteca.
   Esto es lo contrario — títulos que aún no has descargado. Por eso no se
   llaman igual.

   La lista vive en el servidor (data/watchlist.json), no en localStorage: es
   una decisión tuya sobre qué quieres leer, no un detalle de este navegador.
   =========================================================================== */

const watchlistState = {
    ids: new Set(),      // lo guardado, para pintar el cintillo en la búsqueda
    items: [],           // la lista completa, ya anotada por el servidor
};

/* --- tarjetas de búsqueda ------------------------------------------------ */

/* Lo llama renderBookCard: añade el botón de marcador y el cintillo. */
function watchlistDecorateCard(card, book) {
    const id = String(book.id);
    if (book.in_watchlist) watchlistState.ids.add(id);

    const cover = card.querySelector('.cover-wrap');
    if (!cover || cover.querySelector('.wl-btn')) return;

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'wl-btn';
    btn.dataset.bookId = id;
    btn.onclick = function (e) {
        e.stopPropagation();
        watchlistToggle(book);
    };
    cover.appendChild(btn);
    watchlistPaintCard(card, id);
}

function watchlistPaintCard(card, id) {
    const saved = watchlistState.ids.has(id);
    const btn = card.querySelector('.wl-btn');
    if (btn) {
        btn.classList.toggle('is-on', saved);
        btn.textContent = saved ? '★' : '☆';
        btn.title = saved ? 'Quitar de Para después' : 'Descargar para más tarde';
        btn.setAttribute('aria-label', btn.title);
        btn.setAttribute('aria-pressed', String(saved));
    }

    // Los dos tipos de tarjeta guardan sus cintillos en sitios distintos: la de
    // búsqueda en `.card-meta.tile-ribbon` (junto al de "En la biblioteca") y la
    // de la biblioteca en `.tile-formats`, con los de formato.
    const host = card.querySelector('.tile-formats')
        || card.querySelector('.card-meta.tile-ribbon');
    if (!host) return;

    let ribbon = host.querySelector('.wl-ribbon');
    if (saved && !ribbon) {
        ribbon = document.createElement('span');
        ribbon.className = 'fmt-badge wl-ribbon';
        ribbon.textContent = 'PARA DESPUÉS';
        host.prepend(ribbon);
    } else if (!saved && ribbon) {
        ribbon.remove();
    }
}

async function watchlistToggle(book) {
    const id = String(book.id);
    const saved = watchlistState.ids.has(id);

    try {
        if (saved) {
            await fetch(API + '/api/watchlist/' + encodeURIComponent(id) + '/remove',
                        { method: 'POST' });
            watchlistState.ids.delete(id);
        } else {
            await fetch(API + '/api/watchlist', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    book_id: id,
                    title: book.title,
                    authors: book.authors || [],
                    publishers: book.publishers || [],
                    year: book.issued || book.year || null,
                    content_type: book.content_type || searchState.contentType,
                    cover_url: book.cover_url || null,
                }),
            });
            watchlistState.ids.add(id);
        }
    } catch (err) {
        console.error('No se pudo actualizar Para después:', err);
        return;
    }

    // Todas las tarjetas del mismo título, no solo la pulsada
    document.querySelectorAll('.book-card').forEach(function (card) {
        if (String(card.dataset.bookId) === id) watchlistPaintCard(card, id);
    });
    watchlistRefreshCount();
}

/* --- la vista ------------------------------------------------------------ */

async function loadWatchlist() {
    const host = document.getElementById('watchlist-list');
    const empty = document.getElementById('watchlist-empty');
    if (!host) return;

    let items = [];
    try {
        const res = await fetch(API + '/api/watchlist');
        items = (await res.json()).items || [];
    } catch (err) {
        console.error('No se pudo leer Para después:', err);
    }

    watchlistState.items = items;
    watchlistState.ids = new Set(items.map(function (i) { return String(i.book_id); }));
    watchlistRefreshCount();

    host.innerHTML = '';
    document.getElementById('watchlist-count').textContent =
        items.length === 1 ? '1 título guardado' : items.length + ' títulos guardados';

    // El botón solo existe si hay algo que limpiar, y dice cuántos son antes
    // de que pulses nada.
    const hechos = items.filter(function (i) { return i.downloaded; });
    const limpiar = document.getElementById('watchlist-clear');
    if (limpiar) {
        limpiar.classList.toggle('hidden', hechos.length === 0);
        limpiar.textContent = 'Limpiar descargados (' + hechos.length + ')';
        limpiar.onclick = function () { watchlistConfirmClear(hechos.length); };
    }

    if (!items.length) {
        empty.classList.remove('hidden');
        empty.textContent = 'Aquí se guardan los títulos que quieres bajar luego. '
            + 'Busca un libro y pulsa la estrella de su portada.';
        return;
    }
    empty.classList.add('hidden');
    items.forEach(function (item) { host.appendChild(watchlistRow(item)); });
}

function watchlistRow(item) {
    const row = document.createElement('article');
    row.className = 'wl-row';
    row.dataset.bookId = item.book_id;

    const cover = document.createElement('div');
    cover.className = 'wl-cover';
    if (item.cover_url) {
        const img = document.createElement('img');
        img.src = item.cover_url;
        img.alt = '';
        img.loading = 'lazy';
        img.addEventListener('error', function () { img.remove(); });
        cover.appendChild(img);
    }

    const info = document.createElement('div');
    info.className = 'wl-info';

    const title = document.createElement('p');
    title.className = 'wl-title';
    title.textContent = item.title || item.book_id;

    const meta = document.createElement('p');
    meta.className = 'wl-meta';
    meta.textContent = [(item.authors || []).join(', '),
                        (item.publishers || [])[0], item.year]
        .filter(Boolean).join(' · ');

    const state = document.createElement('p');
    state.className = 'wl-state';
    if (item.downloaded) {
        state.classList.add('is-done');
        state.textContent = '✓ Descargado';
    } else if (item.job_status) {
        state.textContent = item.job_status === 'queued'
            ? 'En cola'
            : (item.job_status === 'paused'
                ? 'Esperando cookies'
                : 'Descargando ' + (item.job_percentage || 0) + '%');
    } else if (item.content_type === 'audiobook') {
        state.textContent = 'Audiolibro';
    }

    info.append(title, meta, state);

    const actions = document.createElement('div');
    actions.className = 'wl-actions';

    // Descargar solo si falta y no está ya en marcha: ofrecer un botón que no
    // hace nada es peor que no ofrecerlo.
    if (!item.downloaded && !item.job_status) {
        const dl = document.createElement('button');
        dl.type = 'button';
        dl.className = 'wl-btn-action is-primary';
        dl.textContent = 'Descargar';
        dl.onclick = function () { watchlistDownload(item, dl); };
        actions.appendChild(dl);
    }

    if (item.downloaded) {
        const ver = document.createElement('button');
        ver.type = 'button';
        ver.className = 'wl-btn-action';
        ver.textContent = 'Ver en biblioteca';
        ver.onclick = function () { goToSection('library'); };
        actions.appendChild(ver);
    }

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'wl-btn-action is-danger';
    del.textContent = 'Borrar de la lista';
    del.onclick = async function () {
        await fetch(API + '/api/watchlist/' + encodeURIComponent(item.book_id) + '/remove',
                    { method: 'POST' });
        watchlistState.ids.delete(String(item.book_id));
        loadWatchlist();
        document.querySelectorAll('.book-card').forEach(function (card) {
            if (String(card.dataset.bookId) === String(item.book_id)) {
                watchlistPaintCard(card, String(item.book_id));
            }
        });
    };
    actions.appendChild(del);

    row.append(cover, info, actions);
    return row;
}

async function watchlistDownload(item, btn) {
    btn.disabled = true;
    btn.textContent = 'Encolando...';
    const body = { book_id: item.book_id, title: item.title, transfer: true };
    if (item.content_type === 'audiobook') body.content_type = 'audiobook';
    else body.format = 'epub';

    try {
        await fetch(API + '/api/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
    } catch (err) {
        btn.disabled = false;
        btn.textContent = 'Reintentar';
        return;
    }
    // La cola es la fuente de verdad del progreso; aquí solo se refresca.
    loadWatchlist();
    if (typeof batchStartPolling === 'function') batchStartPolling();
}

/* --- limpiar los ya descargados ------------------------------------------ */

/* Borra de la lista, no del disco. Se avisa antes porque es una acción sobre
   varios elementos a la vez y no hay deshacer: la lista es una decisión tuya
   acumulada, no algo que se pueda reconstruir. */
function watchlistConfirmClear(cuantos) {
    const previo = document.getElementById('wl-confirm');
    if (previo) previo.remove();

    const overlay = document.createElement('div');
    overlay.id = 'wl-confirm';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.innerHTML =
        '<div class="batch-shell">'
        + '<header class="batch-head"><h3>Limpiar descargados</h3></header>'
        + '<p class="wl-confirm-text">Se eliminarán <strong>' + cuantos
        + '</strong> ' + (cuantos === 1 ? 'elemento' : 'elementos')
        + ' de la lista.<br>Los libros siguen en tu biblioteca: solo dejan de '
        + 'estar en “Para después”.</p>'
        + '<footer class="batch-foot"></footer>'
        + '</div>';

    const cerrar = function () {
        overlay.remove();
        document.removeEventListener('keydown', porTecla);
    };
    const porTecla = function (e) { if (e.key === 'Escape') cerrar(); };

    const cancelar = document.createElement('button');
    cancelar.type = 'button';
    cancelar.className = 'batch-btn';
    cancelar.textContent = 'Cancelar';
    cancelar.onclick = cerrar;

    const seguir = document.createElement('button');
    seguir.type = 'button';
    seguir.className = 'batch-btn is-primary';
    seguir.textContent = 'Continuar';
    seguir.onclick = async function () {
        seguir.disabled = true;
        cancelar.disabled = true;
        seguir.textContent = 'Limpiando...';
        try {
            await fetch(API + '/api/watchlist/clear-downloaded', { method: 'POST' });
        } catch (err) {
            seguir.disabled = false;
            cancelar.disabled = false;
            seguir.textContent = 'Reintentar';
            return;
        }
        cerrar();
        loadWatchlist();
        // Las tarjetas de búsqueda visibles llevan el cintillo "PARA DESPUÉS":
        // se repintan para que no siga puesto sobre algo que ya salió.
        document.querySelectorAll('.book-card').forEach(function (card) {
            watchlistPaintCard(card, String(card.dataset.bookId));
        });
    };

    overlay.querySelector('.batch-foot').append(cancelar, seguir);
    // Clic fuera = cancelar; dentro del panel no debe cerrarse.
    overlay.onclick = function (e) { if (e.target === overlay) cerrar(); };
    document.addEventListener('keydown', porTecla);

    document.body.appendChild(overlay);
    cancelar.focus();   // el foco va a la salida, no a la acción destructiva
}

/* --- contadores del menú ------------------------------------------------- */

async function watchlistRefreshCount() {
    const badge = document.getElementById('nav-watchlist-count');
    if (!badge) return;
    let n = watchlistState.ids.size;
    if (!watchlistState.items.length) {
        try {
            const res = await fetch(API + '/api/watchlist');
            const items = (await res.json()).items || [];
            watchlistState.ids = new Set(items.map(function (i) { return String(i.book_id); }));
            n = items.length;
        } catch (err) { /* sin contador se sigue funcionando */ }
    }
    badge.textContent = n ? String(n) : '';
}

document.addEventListener('DOMContentLoaded', watchlistRefreshCount);
