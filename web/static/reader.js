/* ===========================================================================
   Lector de EPUB embebido (epub.js, vendorizado en static/vendor/)

   Se abre como modal a pantalla completa desde una tarjeta de la biblioteca.
   Por decisión de producto SOLO se cierra con el botón de la barra: ni clic
   fuera, ni Escape. Por eso la barra —y su botón de cerrar— se pintan ANTES de
   empezar a cargar el libro: si el epub estuviera corrupto y la carga fallara,
   el botón ya existe y no te quedas encerrada.
   =========================================================================== */

const READER_POS_KEY = 'oi-reader-pos';      // { folder: cfi }
const READER_PREFS_KEY = 'oi-reader-prefs';  // { fontSize, theme, flow, spread, font }

const READER_THEMES = {
    light: { name: 'Claro', bg: '#ffffff', fg: '#18181b' },
    sepia: { name: 'Sepia', bg: '#f8f2e4', fg: '#4b3f2f' },
    dark: { name: 'Oscuro', bg: '#1a1a1f', fg: '#d4d4d8' },
};

const READER_FONTS = {
    original: 'Original del libro',
    serif: 'Georgia, serif',
    sans: '"DM Sans", system-ui, sans-serif',
    mono: '"JetBrains Mono", ui-monospace, monospace',
};

let reader = null;   // estado del lector abierto, o null

function readerPrefs() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(READER_PREFS_KEY) || '{}'); }
    catch (err) { saved = {}; }
    return Object.assign(
        { fontSize: 100, theme: 'light', flow: 'paginated', spread: 'auto', font: 'original' },
        saved
    );
}

function saveReaderPrefs(prefs) {
    try { localStorage.setItem(READER_PREFS_KEY, JSON.stringify(prefs)); }
    catch (err) { /* cuota */ }
}

function readerStore() {
    try { return JSON.parse(localStorage.getItem(READER_POS_KEY) || '{}'); }
    catch (err) { return {}; }
}

/* ---------- construcción del armazón ------------------------------------ */

function buildReaderShell(item) {
    const modal = document.createElement('div');
    modal.id = 'reader-modal';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-label', 'Lector: ' + (item.title || ''));

    modal.innerHTML = `
      <div class="reader-shell">
        <header class="reader-bar">
          <button type="button" class="reader-btn is-close" id="reader-close"
                  aria-label="Cerrar el lector">✕ Cerrar</button>
          <button type="button" class="reader-btn" id="reader-toc"
                  aria-label="Índice" aria-pressed="false">☰ Índice</button>

          <span class="reader-title" id="reader-title"></span>

          <div class="reader-tools">
            <button type="button" class="reader-btn" id="reader-find" aria-label="Buscar en el libro">🔍</button>
            <button type="button" class="reader-btn" id="reader-mark" aria-label="Marcar esta página">🔖</button>

            <span class="reader-group" role="group" aria-label="Tamaño de letra">
              <button type="button" class="reader-btn" id="reader-smaller" aria-label="Reducir letra">A−</button>
              <span class="reader-fontsize" id="reader-fontsize">100%</span>
              <button type="button" class="reader-btn" id="reader-bigger" aria-label="Aumentar letra">A+</button>
            </span>

            <select class="reader-select" id="reader-font" aria-label="Tipografía"></select>
            <select class="reader-select" id="reader-theme" aria-label="Tema"></select>
            <select class="reader-select" id="reader-flow" aria-label="Disposición">
              <option value="paginated">Paginado</option>
              <option value="scrolled-doc">Desplazamiento</option>
            </select>
            <select class="reader-select" id="reader-spread" aria-label="Páginas">
              <option value="auto">Doble página</option>
              <option value="none">Una página</option>
            </select>
          </div>
        </header>

        <div class="reader-find hidden" id="reader-find-panel">
          <input type="search" id="reader-find-input" placeholder="Buscar en todo el libro..."
                 aria-label="Texto a buscar">
          <button type="button" class="reader-btn" id="reader-find-go">Buscar</button>
          <span class="reader-find-count" id="reader-find-count"></span>
          <div class="reader-find-results" id="reader-find-results"></div>
        </div>

        <div class="reader-body">
          <nav class="reader-toc hidden" id="reader-toc-panel" aria-label="Índice del libro"></nav>
          <div class="reader-stage">
            <button type="button" class="reader-page-btn is-prev" id="reader-prev"
                    aria-label="Página anterior">‹</button>
            <div class="reader-viewport" id="reader-viewport"></div>
            <button type="button" class="reader-page-btn is-next" id="reader-next"
                    aria-label="Página siguiente">›</button>
            <div class="reader-loader" id="reader-loader">
              <span class="reader-spinner" aria-hidden="true"></span>
              <p class="reader-loader-text" id="reader-loader-text">Abriendo el libro...</p>
            </div>
          </div>
        </div>

        <footer class="reader-foot">
          <span class="reader-chapter" id="reader-chapter"></span>
          <input type="range" class="reader-progress" id="reader-progress"
                 min="0" max="1000" value="0" aria-label="Progreso de lectura" disabled>
          <span class="reader-percent" id="reader-percent">--%</span>
        </footer>
      </div>`;

    document.body.appendChild(modal);
    document.body.classList.add('reader-open');
    // El cierre se enlaza YA, antes de tocar el epub.
    modal.querySelector('#reader-close').onclick = closeReader;
    return modal;
}

function readerLoading(text) {
    const el = document.getElementById('reader-loader-text');
    if (el) el.textContent = text;
}

function readerLoaderDone() {
    const el = document.getElementById('reader-loader');
    if (el) el.classList.add('is-done');
}

/* ---------- apertura ------------------------------------------------------ */

async function openReader(item) {
    if (reader) return;
    const modal = buildReaderShell(item);
    document.getElementById('reader-title').textContent = item.title || '';

    reader = { folder: item.folder, modal: modal, book: null, rendition: null,
               prefs: readerPrefs(), keys: null };

    try {
        readerLoading('Descargando el archivo...');
        const url = API + '/api/library/file/' + encodeURIComponent(item.folder) + '/epub';
        const res = await fetch(url);
        if (!res.ok) throw new Error('el servidor respondió ' + res.status);
        const buffer = await res.arrayBuffer();

        readerLoading('Abriendo el libro...');
        // Se le pasa el ArrayBuffer y no la URL: así epub.js no tiene que
        // adivinar el formato por la extensión, que nuestra ruta no tiene.
        const book = ePub(buffer);
        reader.book = book;
        await book.ready;

        readerLoading('Preparando la vista...');
        const rendition = book.renderTo('reader-viewport', {
            width: '100%',
            height: '100%',
            flow: reader.prefs.flow,
            spread: reader.prefs.spread,
            allowScriptedContent: false,
        });
        reader.rendition = rendition;

        Object.keys(READER_THEMES).forEach(function (key) {
            const t = READER_THEMES[key];
            rendition.themes.register(key, {
                body: { background: t.bg, color: t.fg },
                a: { color: '#0073e6' },
            });
        });
        applyReaderPrefs();

        const saved = readerStore()[item.folder];
        await rendition.display(saved || undefined);
        readerLoaderDone();

        wireReaderControls();
        await buildReaderToc();

        // El pie se rellena de inmediato: `relocated` se suscribe dentro de
        // wireReaderControls, o sea DESPUÉS del primer display(), así que ese
        // primer evento no llega a nuestro handler y el capítulo se quedaba en
        // blanco hasta que pasabas una página.
        updateReaderProgress(rendition.currentLocation());

        // Las "locations" son lo que permite tener porcentaje y barra de
        // progreso. Es costoso en libros grandes, así que va después de que ya
        // puedas leer, y la barra se habilita cuando termina.
        readerLoading('Calculando páginas...');
        book.locations.generate(1600).then(function () {
            const bar = document.getElementById('reader-progress');
            if (bar) bar.disabled = false;
            updateReaderProgress(rendition.currentLocation());
        }).catch(function () { /* sin locations se lee igual, sin % */ });

    } catch (err) {
        readerLoaderDone();
        const stage = document.querySelector('.reader-stage');
        if (stage) {
            const p = document.createElement('p');
            p.className = 'reader-error';
            p.textContent = 'No se pudo abrir el libro: ' + err.message
                + '. Puedes cerrar el lector con el botón de arriba.';
            stage.appendChild(p);
        }
    }
}

function closeReader() {
    if (!reader) return;
    saveReaderPosition();
    if (reader.keys) document.removeEventListener('keydown', reader.keys);
    try { if (reader.rendition) reader.rendition.destroy(); } catch (err) { /* ya destruido */ }
    try { if (reader.book) reader.book.destroy(); } catch (err) { /* ya destruido */ }
    reader.modal.remove();
    document.body.classList.remove('reader-open');
    reader = null;
}

function saveReaderPosition() {
    if (!reader || !reader.rendition) return;
    const loc = reader.rendition.currentLocation();
    const cfi = loc && loc.start && loc.start.cfi;
    if (!cfi) return;
    const all = readerStore();
    all[reader.folder] = cfi;
    try { localStorage.setItem(READER_POS_KEY, JSON.stringify(all)); }
    catch (err) { /* cuota */ }
}

/* ---------- preferencias -------------------------------------------------- */

function applyReaderPrefs() {
    const p = reader.prefs;
    const r = reader.rendition;
    r.themes.select(p.theme);
    r.themes.fontSize(p.fontSize + '%');
    if (p.font !== 'original') r.themes.font(READER_FONTS[p.font]);
    else r.themes.font('');

    const modal = reader.modal;
    modal.dataset.theme = p.theme;
    const size = modal.querySelector('#reader-fontsize');
    if (size) size.textContent = p.fontSize + '%';
    saveReaderPrefs(p);
}

/* ---------- índice -------------------------------------------------------- */

async function buildReaderToc() {
    const panel = document.getElementById('reader-toc-panel');
    const nav = await reader.book.loaded.navigation;
    panel.innerHTML = '';

    function render(items, depth) {
        items.forEach(function (entry) {
            const a = document.createElement('button');
            a.type = 'button';
            a.className = 'reader-toc-item';
            a.style.paddingLeft = (12 + depth * 14) + 'px';
            a.textContent = entry.label.trim() || '(sin título)';
            a.onclick = function () { reader.rendition.display(entry.href); };
            panel.appendChild(a);
            if (entry.subitems && entry.subitems.length) render(entry.subitems, depth + 1);
        });
    }
    render(nav.toc || [], 0);
    if (!panel.children.length) {
        panel.innerHTML = '<p class="reader-toc-empty">Este libro no trae índice.</p>';
    }
}

/* ---------- búsqueda ------------------------------------------------------ */

async function readerSearch(query) {
    const box = document.getElementById('reader-find-results');
    const count = document.getElementById('reader-find-count');
    box.innerHTML = '';
    if (!query || query.length < 3) {
        count.textContent = 'Escribe al menos 3 letras.';
        return;
    }
    count.textContent = 'Buscando...';

    const book = reader.book;
    const results = [];
    // epub.js no trae búsqueda global: hay que recorrer el spine y buscar en
    // cada sección. Se hace en serie para no cargar el libro entero de golpe.
    for (const section of book.spine.spineItems) {
        try {
            await section.load(book.load.bind(book));
            const found = section.find(query);
            found.forEach(function (f) { results.push(f); });
            section.unload();
        } catch (err) { /* sección ilegible: se salta */ }
        if (results.length > 200) break;
    }

    count.textContent = results.length
        ? results.length + ' resultado(s)'
        : 'Sin resultados.';
    results.forEach(function (r) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'reader-find-hit';
        b.textContent = r.excerpt.trim();
        b.onclick = function () { reader.rendition.display(r.cfi); };
        box.appendChild(b);
    });
}

/* ---------- progreso ------------------------------------------------------ */

function updateReaderProgress(loc) {
    if (!loc || !loc.start) return;
    const book = reader.book;
    const pct = document.getElementById('reader-percent');
    const bar = document.getElementById('reader-progress');
    const chap = document.getElementById('reader-chapter');

    if (book.locations && book.locations.length()) {
        const p = book.locations.percentageFromCfi(loc.start.cfi);
        if (typeof p === 'number' && isFinite(p)) {
            pct.textContent = Math.round(p * 100) + '%';
            bar.value = String(Math.round(p * 1000));
        }
    }
    const item = book.spine.get(loc.start.href);
    if (item && book.navigation) {
        const entry = book.navigation.get(item.href);
        chap.textContent = entry && entry.label ? entry.label.trim() : '';
    }
}

/* ---------- controles ----------------------------------------------------- */

function wireReaderControls() {
    const modal = reader.modal;
    const r = reader.rendition;
    const p = reader.prefs;

    const $ = function (id) { return modal.querySelector('#' + id); };

    $('reader-prev').onclick = function () { r.prev(); };
    $('reader-next').onclick = function () { r.next(); };

    $('reader-toc').onclick = function () {
        const panel = $('reader-toc-panel');
        const open = panel.classList.toggle('hidden');
        $('reader-toc').setAttribute('aria-pressed', String(!open));
    };

    $('reader-find').onclick = function () {
        const panel = $('reader-find-panel');
        panel.classList.toggle('hidden');
        if (!panel.classList.contains('hidden')) $('reader-find-input').focus();
    };
    $('reader-find-go').onclick = function () { readerSearch($('reader-find-input').value.trim()); };
    $('reader-find-input').onkeydown = function (e) {
        if (e.key === 'Enter') readerSearch(e.target.value.trim());
    };

    $('reader-smaller').onclick = function () {
        p.fontSize = Math.max(60, p.fontSize - 10); applyReaderPrefs();
    };
    $('reader-bigger').onclick = function () {
        p.fontSize = Math.min(250, p.fontSize + 10); applyReaderPrefs();
    };

    const fontSel = $('reader-font');
    Object.keys(READER_FONTS).forEach(function (key) {
        const o = document.createElement('option');
        o.value = key;
        o.textContent = key === 'original' ? READER_FONTS[key] : key;
        fontSel.appendChild(o);
    });
    fontSel.value = p.font;
    fontSel.onchange = function () { p.font = fontSel.value; applyReaderPrefs(); };

    const themeSel = $('reader-theme');
    Object.keys(READER_THEMES).forEach(function (key) {
        const o = document.createElement('option');
        o.value = key;
        o.textContent = READER_THEMES[key].name;
        themeSel.appendChild(o);
    });
    themeSel.value = p.theme;
    themeSel.onchange = function () { p.theme = themeSel.value; applyReaderPrefs(); };

    const flowSel = $('reader-flow');
    flowSel.value = p.flow;
    flowSel.onchange = function () {
        p.flow = flowSel.value; saveReaderPrefs(p);
        r.flow(p.flow);
    };

    const spreadSel = $('reader-spread');
    spreadSel.value = p.spread;
    spreadSel.onchange = function () {
        p.spread = spreadSel.value; saveReaderPrefs(p);
        r.spread(p.spread);
    };

    // Marcadores: epub.js resalta por CFI, así que un marcador es un CFI con
    // subrayado. Se guardan junto a la posición.
    $('reader-mark').onclick = function () {
        const loc = r.currentLocation();
        if (!loc || !loc.start) return;
        try {
            r.annotations.highlight(loc.start.cfi, {}, function () {
                r.display(loc.start.cfi);
            });
            $('reader-mark').classList.add('is-on');
            setTimeout(function () { $('reader-mark').classList.remove('is-on'); }, 900);
        } catch (err) { /* ya marcado */ }
    };

    $('reader-progress').oninput = function (e) {
        const book = reader.book;
        if (!book.locations || !book.locations.length()) return;
        const cfi = book.locations.cfiFromPercentage(Number(e.target.value) / 1000);
        if (cfi) r.display(cfi);
    };

    r.on('relocated', function (loc) {
        updateReaderProgress(loc);
        saveReaderPosition();
    });

    // Teclado, también dentro del iframe del contenido
    reader.keys = function (e) {
        const tag = (e.target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
        if (e.key === 'ArrowLeft') r.prev();
        else if (e.key === 'ArrowRight') r.next();
    };
    document.addEventListener('keydown', reader.keys);
    r.on('keydown', reader.keys);
}
