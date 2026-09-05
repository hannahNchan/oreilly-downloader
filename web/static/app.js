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

    // Un solo punto de salida: se decide el texto y la barra se muestra SOLO si
    // hay texto. Antes cada rama la mostraba por su cuenta, así que bastaba con
    // que una se saltara el mensaje para dejarla visible y vacía — ofreciendo
    // "Transferir todas" sin nada que transferir.
    let text = '';
    let busy = false;

    if (state.unavailable) {
        // Configurada pero sin responder: se dice, en vez de dejar creer que no
        // hay nada publicado. Y no se puede transferir a donde no responde.
        text = 'La carpeta de la biblioteca no responde (' + (state.dir || '?')
            + '). Lo descargado sigue en la caché local.';
        busy = true;
    } else if (state.running) {
        text = 'Transfiriendo ' + (state.index || 1) + ' de ' + (state.total || 1)
            + ' - ' + (state.percentage || 0) + '%';
        busy = true;
    } else if (state.failedCount) {
        text = state.failedCount + ' obra(s) no se pudieron transferir: '
            + state.firstError;
    } else if (state.localCount > 0) {
        text = state.localCount === 1
            ? '1 obra en caché, sin pasar a la biblioteca'
            : state.localCount + ' obras en caché, sin pasar a la biblioteca';
    }

    msg.textContent = text;
    btn.disabled = busy;
    btn.textContent = state.running ? 'Transfiriendo...' : 'Transferir todas';
    bar.classList.toggle('hidden', !text);
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


/* ===== Reproductor de audiolibros ==========================================
   No manejamos rutas: al abrir un audiolibro se ocultan los hijos de
   #library-main y el sidebar, y se inserta la vista del reproductor. Volver es
   quitarla y volver a mostrarlos — instantaneo y sin repedir la biblioteca. */

const PLAYER_POS_KEY = 'oi-player-pos';   // { folder: { track, time } }
const PLAYER_SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2];

let playerView = null;    // el <section> insertado, o null si esta cerrado
let playerKeys = null;    // handler de teclado, para poder quitarlo al cerrar

function readStore(key) {
    try { return JSON.parse(localStorage.getItem(key) || '{}'); }
    catch (err) { return {}; }
}

function writeStore(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); }
    catch (err) { /* cuota llena: perder la posicion no es grave */ }
}

function formatClock(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '--:--';
    const s = Math.floor(seconds % 60);
    const m = Math.floor(seconds / 60) % 60;
    const h = Math.floor(seconds / 3600);
    const ss = String(s).padStart(2, '0');
    return h > 0 ? h + ':' + String(m).padStart(2, '0') + ':' + ss : m + ':' + ss;
}

async function openPlayer(item) {
    const main = document.getElementById('library-main');
    const sidebar = document.getElementById('library-sidebar');
    if (!main || playerView) return;

    // Ocultar en vez de borrar: el input de busqueda tambien vive aqui dentro,
    // y reconstruir la rejilla al volver seria trabajo para nada.
    //
    // Se marca SOLO lo que estaba visible. Lo que ya estaba oculto por su
    // propia lógica —la barra de transferencia cuando no hay nada que pasar, el
    // estado vacío— no se toca: al salir, closePlayer destapa lo que marcó, y
    // destapar algo que nunca debió verse es inventar estado.
    Array.from(main.children).forEach(function (el) {
        if (el.classList.contains('hidden')) return;
        el.dataset.playerHidden = '1';
        el.classList.add('hidden');
    });
    if (sidebar && !sidebar.classList.contains('hidden')) {
        sidebar.dataset.playerHidden = '1';
        sidebar.classList.add('hidden');
    }

    playerView = document.createElement('section');
    playerView.id = 'player-view';
    playerView.innerHTML = '<p class="text-sm text-zinc-500">Cargando pistas...</p>';
    main.appendChild(playerView);
    window.scrollTo({ top: 0, behavior: 'smooth' });

    let data;
    try {
        const res = await fetch(API + '/api/library/tracks/' + encodeURIComponent(item.folder));
        data = await res.json();
        if (data.error) throw new Error(data.error);
        if (!data.tracks || !data.tracks.length) throw new Error('no hay pistas en disco');
    } catch (err) {
        playerView.innerHTML = '';
        const msg = document.createElement('p');
        msg.className = 'text-sm text-red-600 mb-3';
        msg.textContent = 'No se pudo abrir el reproductor: ' + err.message;
        playerView.append(msg, backButton());
        return;
    }

    renderPlayer(item, data);
}

function backButton() {
    const back = document.createElement('button');
    back.type = 'button';
    back.className = 'player-back';
    back.innerHTML = '<span aria-hidden="true">\u2190</span> Volver a la biblioteca';
    back.onclick = closePlayer;
    return back;
}

function closePlayer() {
    if (!playerView) return;
    const audio = playerView.querySelector('audio');
    if (audio) {
        savePlayerPosition();
        audio.pause();
        audio.removeAttribute('src');
        audio.load();          // corta la descarga en curso
    }
    if (playerKeys) {
        document.removeEventListener('keydown', playerKeys);
        playerKeys = null;
    }
    playerView.remove();
    playerView = null;

    const main = document.getElementById('library-main');
    const sidebar = document.getElementById('library-sidebar');
    // Se destapa exactamente lo que openPlayer tapó, ni un elemento más.
    if (main) {
        main.querySelectorAll('[data-player-hidden]').forEach(function (el) {
            el.classList.remove('hidden');
            delete el.dataset.playerHidden;
        });
    }
    if (sidebar && sidebar.dataset.playerHidden) {
        sidebar.classList.remove('hidden');
        delete sidebar.dataset.playerHidden;
    }
}

function savePlayerPosition() {
    if (!playerView) return;
    const audio = playerView.querySelector('audio');
    const folder = playerView.dataset.folder;
    if (!audio || !folder || !audio.src) return;
    const all = readStore(PLAYER_POS_KEY);
    all[folder] = { track: Number(playerView.dataset.track) || 1, time: audio.currentTime || 0 };
    writeStore(PLAYER_POS_KEY, all);
}

function renderPlayer(item, data) {
    const tracks = data.tracks;
    playerView.innerHTML = '';
    playerView.dataset.folder = item.folder;

    const audio = document.createElement('audio');
    audio.preload = 'metadata';

    // ---------- cabecera: portada + datos + controles
    const top = document.createElement('div');
    top.className = 'player-top';

    const cover = document.createElement('div');
    cover.className = 'player-cover';
    if (data.cover_url) {
        const img = document.createElement('img');
        img.src = data.cover_url;
        img.alt = '';
        img.addEventListener('error', function () { img.remove(); });
        cover.appendChild(img);
    }

    const info = document.createElement('div');
    info.className = 'player-info';

    const h2 = document.createElement('h2');
    h2.className = 'player-title';
    h2.textContent = data.title || item.title;

    const sub = document.createElement('p');
    sub.className = 'player-sub';
    sub.textContent = [(data.authors || []).join(', '), data.year,
                       tracks.length + (tracks.length === 1 ? ' capítulo' : ' capítulos')]
        .filter(Boolean).join('  ·  ');

    const now = document.createElement('p');
    now.className = 'player-now';

    // ---------- barra de progreso
    const bar = document.createElement('div');
    bar.className = 'player-bar';
    const tCur = document.createElement('span');
    tCur.className = 'player-time';
    tCur.textContent = '0:00';
    const seek = document.createElement('input');
    seek.type = 'range';
    seek.className = 'player-seek';
    seek.min = '0';
    seek.max = '1000';
    seek.value = '0';
    seek.setAttribute('aria-label', 'Posición en el capítulo');
    const tDur = document.createElement('span');
    tDur.className = 'player-time';
    tDur.textContent = '--:--';
    bar.append(tCur, seek, tDur);

    // ---------- controles
    const controls = document.createElement('div');
    controls.className = 'player-controls';

    function ctl(label, aria, cls) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'player-btn' + (cls ? ' ' + cls : '');
        b.textContent = label;
        b.setAttribute('aria-label', aria);
        return b;
    }

    const bPrev = ctl('\u23ee', 'Capítulo anterior');
    const bBack = ctl('\u21ba15', 'Atrás 15 segundos');
    const bPlay = ctl('\u25b6', 'Reproducir', 'is-primary');
    const bFwd = ctl('30\u21bb', 'Adelante 30 segundos');
    const bNext = ctl('\u23ed', 'Capítulo siguiente');

    const speed = document.createElement('select');
    speed.className = 'player-select';
    speed.setAttribute('aria-label', 'Velocidad');
    PLAYER_SPEEDS.forEach(function (v) {
        const o = document.createElement('option');
        o.value = String(v);
        o.textContent = v + '\u00d7';
        if (v === 1) o.selected = true;
        speed.appendChild(o);
    });

    const vol = document.createElement('input');
    vol.type = 'range';
    vol.className = 'player-vol';
    vol.min = '0';
    vol.max = '1';
    vol.step = '0.05';
    vol.value = '1';
    vol.setAttribute('aria-label', 'Volumen');

    controls.append(bPrev, bBack, bPlay, bFwd, bNext, speed, vol);
    info.append(h2, sub, now, bar, controls);
    top.append(cover, info);

    // ---------- índice de capítulos
    const list = document.createElement('ol');
    list.className = 'player-tracks';
    const rows = tracks.map(function (t) {
        const li = document.createElement('li');
        li.className = 'player-track';
        li.dataset.n = String(t.n);

        const num = document.createElement('span');
        num.className = 'player-track-n';
        num.textContent = String(t.n).padStart(2, '0');

        const label = document.createElement('span');
        label.className = 'player-track-title';
        label.textContent = t.title;

        const dur = document.createElement('span');
        dur.className = 'player-track-dur';
        dur.textContent = '';

        li.append(num, label, dur);
        li.onclick = function () { playTrack(t.n, true); };
        list.appendChild(li);
        return { li: li, dur: dur, meta: t };
    });

    const titled = tracks.some(function (t) { return t.titled; });
    playerView.append(backButton(), top, list, audio);
    if (!titled) {
        const nota = document.createElement('div');
        nota.className = 'player-note';

        const texto = document.createElement('p');
        texto.textContent = 'Este audiolibro se descargó antes de que se guardaran '
            + 'los nombres de los capítulos, así que el índice va numerado.';

        // Los nombres solo existen en la API de O'Reilly: los archivos se
        // renombran al publicar y el audio no lleva etiquetas. Por eso esto
        // necesita sesión válida, y lo dice si falla.
        const boton = document.createElement('button');
        boton.type = 'button';
        boton.className = 'player-note-btn';
        boton.textContent = 'Recuperar nombres de capítulos';
        boton.onclick = async function () {
            boton.disabled = true;
            boton.textContent = 'Pidiendo los nombres...';
            try {
                const res = await fetch(API + '/api/library/chapter-names', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ folder: item.folder })
                });
                const out = await res.json();
                if (out.error) throw new Error(out.error);
                boton.textContent = 'Listo: ' + out.titled + ' capítulos';
                // Se reabre para que el índice se pinte con los nombres nuevos
                closePlayer();
                openPlayer(item);
            } catch (err) {
                boton.disabled = false;
                boton.textContent = 'Reintentar';
                texto.textContent = 'No se pudieron recuperar: ' + err.message;
            }
        };

        nota.append(texto, boton);
        list.insertAdjacentElement('beforebegin', nota);
    }

    // ---------- aviso de descarga incompleta
    function warnIncomplete(text) {
        let warn = playerView.querySelector('.player-warn');
        if (!warn) {
            warn = document.createElement('p');
            warn.className = 'player-warn';
            list.insertAdjacentElement('beforebegin', warn);
        }
        warn.textContent = text;
    }

    // Si sabemos cuántos capítulos debería haber, tener menos en disco solo
    // puede significar que la descarga se cortó. Publicar eso en silencio es
    // como servir un libro truncado sin decirlo.
    if (data.incomplete) {
        const real = Number(data.audio_seconds) || 0;
        const esperado = Number(data.duration_seconds) || 0;
        const nEsperado = Number(data.expected_tracks) || 0;
        let detalle;
        if (nEsperado && nEsperado > tracks.length) {
            detalle = 'tienes ' + tracks.length + ' de ' + nEsperado + ' capítulos';
        } else if (real && esperado) {
            detalle = 'tienes ' + formatClock(real) + ' de ' + formatClock(esperado)
                + ' (' + Math.round(real / esperado * 100) + '%)';
        } else {
            detalle = 'faltan capítulos';
        }
        warnIncomplete('Descarga incompleta: ' + detalle
            + '. Descárgalo otra vez para completarlo.');
    }

    // ---------- lógica
    // Las duraciones las calcula el servidor leyendo la cabecera del MP4, así
    // que están desde el primer pintado. Antes había que abrir las 29 pistas
    // por HTTP solo para averiguarlas.
    rows.forEach(function (r) {
        r.dur.textContent = r.meta.seconds ? formatClock(r.meta.seconds) : '';
    });

    function setActive(n) {
        rows.forEach(function (r) {
            r.li.classList.toggle('is-playing', Number(r.li.dataset.n) === n);
        });
        const t = tracks[n - 1];
        now.textContent = t ? String(n).padStart(2, '0') + '  ·  ' + t.title : '';
        if ('mediaSession' in navigator && t) {
            navigator.mediaSession.metadata = new MediaMetadata({
                title: t.title,
                artist: (data.authors || []).join(', '),
                album: data.title || '',
                artwork: data.cover_url ? [{ src: data.cover_url }] : [],
            });
        }
    }

    function playTrack(n, autoplay, startAt) {
        if (n < 1 || n > tracks.length) return;
        savePlayerPosition();
        playerView.dataset.track = String(n);
        audio.src = API + tracks[n - 1].url;
        if (startAt) {
            audio.addEventListener('loadedmetadata', function once() {
                audio.removeEventListener('loadedmetadata', once);
                audio.currentTime = startAt;
            });
        }
        setActive(n);
        if (autoplay) audio.play().catch(function () { /* el navegador puede exigir gesto */ });
    }
    bPlay.onclick = function () { audio.paused ? audio.play() : audio.pause(); };
    bPrev.onclick = function () { playTrack(Number(playerView.dataset.track) - 1, true); };
    bNext.onclick = function () { playTrack(Number(playerView.dataset.track) + 1, true); };
    bBack.onclick = function () { audio.currentTime = Math.max(0, audio.currentTime - 15); };
    bFwd.onclick = function () { audio.currentTime = audio.currentTime + 30; };
    speed.onchange = function () { audio.playbackRate = Number(speed.value); };
    vol.oninput = function () { audio.volume = Number(vol.value); };

    audio.addEventListener('play', function () {
        bPlay.textContent = '\u23f8';
        bPlay.setAttribute('aria-label', 'Pausa');
    });
    audio.addEventListener('pause', function () {
        bPlay.textContent = '\u25b6';
        bPlay.setAttribute('aria-label', 'Reproducir');
        savePlayerPosition();
    });

    audio.addEventListener('loadedmetadata', function () {
        tDur.textContent = formatClock(audio.duration);
    });

    let lastSaved = 0;
    audio.addEventListener('timeupdate', function () {
        if (!audio.duration) return;
        seek.value = String(Math.round((audio.currentTime / audio.duration) * 1000));
        tCur.textContent = formatClock(audio.currentTime);
        // Guardar en cada tick seria escribir en localStorage 4 veces por
        // segundo; cada 5s es suficiente para no perder el sitio.
        if (audio.currentTime - lastSaved > 5 || audio.currentTime < lastSaved) {
            lastSaved = audio.currentTime;
            savePlayerPosition();
        }
    });

    seek.oninput = function () {
        if (audio.duration) audio.currentTime = (Number(seek.value) / 1000) * audio.duration;
    };

    // Al acabar un capítulo sigue el siguiente: es un audiolibro, no una lista
    // de canciones sueltas.
    audio.addEventListener('ended', function () {
        const next = Number(playerView.dataset.track) + 1;
        if (next <= tracks.length) playTrack(next, true);
        else savePlayerPosition();
    });

    playerKeys = function (e) {
        if (!playerView) return;
        const tag = (e.target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
        if (e.key === ' ') { e.preventDefault(); bPlay.onclick(); }
        else if (e.key === 'ArrowLeft') { e.preventDefault(); bBack.onclick(); }
        else if (e.key === 'ArrowRight') { e.preventDefault(); bFwd.onclick(); }
        else if (e.key === 'n') bNext.onclick();
        else if (e.key === 'p') bPrev.onclick();
        else if (e.key === 'Escape') closePlayer();
    };
    document.addEventListener('keydown', playerKeys);

    // Retomar donde se quedó, sin arrancar solo: reproducir sin que lo pidas es
    // de mala educación.
    const saved = readStore(PLAYER_POS_KEY)[item.folder];
    if (saved && saved.track >= 1 && saved.track <= tracks.length) {
        playTrack(saved.track, false, saved.time);
        if (saved.time > 5) {
            now.textContent += '  ·  retomando en ' + formatClock(saved.time);
        }
    } else {
        playTrack(1, false);
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
    // Cintillo de bundle: esta obra se bajo emparejada con su otra edicion.
    // El tooltip dice cual es el par y donde quedo, porque el cintillo solo no
    // explica nada a las dos semanas.
    // Y si el español es automático, el cintillo lo dice: al mes no hay forma
    // de saber si aquel EPUB lo tradujo una persona o una GPU.
    const bundleMachine = item.bundle_es_source === 'machine';
    const bundleRibbon = item.bundle_id
        ? '<span class="bundle-ribbon' + (bundleMachine ? ' is-machine' : '') + '" title="'
          + 'Parte de un bundle: ' + (item.bundle_title || item.bundle_id)
          + ' (' + (BUNDLE_LANG_LABEL[item.bundle_lang] || item.bundle_lang || '') + ')'
          + (item.bundle_complete ? ' - las dos ediciones completas' : ' - la otra edicion aun no termina')
          + (bundleMachine
             ? '. El espanol es una traduccion automatica del traductor local, no la edicion publicada'
             : '')
          + '. En output/bundles/' + item.bundle_id
          + '">' + (bundleMachine ? 'BUNDLE &middot; IA' : 'BUNDLE') + '</span>'
        : '';

    // Un audiolibro a medias se ve desde la rejilla, sin tener que abrirlo.
    const badges = bundleRibbon + (item.incomplete
        ? '<span class="fmt-badge is-partial" title="La descarga quedó a medias">INCOMPLETO</span>'
        : '') + (item.location === 'local'
        ? '<span class="fmt-badge loc-local" title="Aún no está en la biblioteca">LOCAL</span>'
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

    // Un audiolibro abre el reproductor; un libro con epub, el lector. Si no
    // hay epub (solo pdf, markdown, json...) no hay nada que leer aqui dentro,
    // asi que se cae al atajo de siempre: abrir la carpeta.
    const hasEpub = (item.formats || []).indexOf('epub') !== -1;
    div.onclick = isAudio
        ? function () { openPlayer(item); }
        : (hasEpub
            ? function () { openReader(item); }
            : function () { revealFile(item.path); });
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
    // Las secciones locales se pintan solas, sin pasar por la API
    if (ct === 'library' || ct === 'watchlist') return;

    // El orden solo existe para libros
    const sortWrap = document.getElementById('filter-sort');
    if (sortWrap) sortWrap.closest('label').style.display = ct === 'book' ? '' : 'none';
    if (searchState.query) search(searchState.query);
}

function setContentMode(mode) {
    // Cualquier navegación cierra el reproductor. Sin esto quedaba abierto por
    // detrás con los hijos de #library-main ocultos —incluido el input de
    // búsqueda, que vive ahí dentro cuando estás en la biblioteca— y tanto el
    // visor como el inicio aparecían en blanco.
    closePlayer();

    const isLibrary = mode === 'library';
    const isWatchlist = mode === 'watchlist';
    const isLocal = isLibrary || isWatchlist;   // secciones que no consultan la API

    document.getElementById('library-view').classList.toggle('is-open', isLibrary);
    const wlView = document.getElementById('watchlist-view');
    if (wlView) wlView.classList.toggle('is-open', isWatchlist);
    moveSearchInput(isLibrary);

    // El buscador remoto y sus bloques de estado no aplican a las secciones
    // locales
    ['results-bar', 'search-results', 'load-more-wrap', 'search-empty'].forEach(function (id) {
        const el = document.getElementById(id);
        if (el) el.style.display = isLocal ? 'none' : '';
    });

    // "Para después" es una lista tuya, no una consulta: sin barra de búsqueda.
    const toolbar = document.querySelector('.toolbar');
    if (toolbar) toolbar.style.display = isWatchlist ? 'none' : '';

    // El botón del menú se marca activo si estás en cualquiera de sus secciones
    const menuBtn = document.getElementById('books-menu-btn');
    if (menuBtn) menuBtn.classList.toggle('ct-active', isLocal);

    document.querySelectorAll('.ct-tab').forEach(function (t) {
        const on = t.dataset.ct === mode;
        t.classList.toggle('ct-active', on);
        if (on) t.setAttribute('aria-current', 'page');
        else t.removeAttribute('aria-current');
    });

    // Al volver del visor, el buscador recupera su estado propio
    if (!isLocal && !searchState.query) setSearchState('empty');

    const input = document.getElementById('search-input');
    input.placeholder = isLibrary
        ? 'Filtrar tu biblioteca por título, autor o editorial...'
        : 'Search by title, author, or ISBN...';

    if (isLibrary) loadLibrary();
    if (isWatchlist && typeof loadWatchlist === 'function') loadWatchlist();
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

    // El clic en la tarjeta ahora abre el lector o el reproductor, asi que el
    // atajo a la carpeta se conserva aqui: sigue siendo util para llevarte el
    // archivo a otro lado.
    const open = document.createElement('button');
    open.type = 'button';
    open.setAttribute('role', 'menuitem');
    open.textContent = 'Abrir carpeta';
    open.onclick = (e) => {
        e.stopPropagation();
        closeAllCardMenus();
        revealFile(item.path);
    };

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
        const items = [open, fav, move, del].filter(Boolean);
        const at = items.indexOf(document.activeElement);
        if (e.key === 'ArrowDown') { e.preventDefault(); items[(at + 1) % items.length].focus(); }
        else if (e.key === 'ArrowUp') { e.preventDefault(); items[(at - 1 + items.length) % items.length].focus(); }
        else if (e.key === 'Escape') { e.preventDefault(); closeAllCardMenus(); btn.focus(); }
    });

    if (move) list.append(open, fav, move, del);
    else list.append(open, fav, del);
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

/* Convierte lo que hayas pegado en un objeto de cookies, o devuelve un error
   que explique qué falta. Lo usan los DOS formularios —el de la cabecera y el
   de la modal de descargas— para que se comporten igual.

   Acepta las dos formas, porque cuál te sale depende de cómo copies:
     - JSON:            {"orm-jwt": "...", "_abck": "..."}
     - document.cookie: orm-jwt=...; _abck=...; bm_sz=...

   Antes solo valía el JSON, y ahí está el problema real: copiar la salida del
   inspector la trunca con facilidad, y entonces JSON.parse fallaba con
   "Unexpected end of JSON input" sin decir por qué. La cadena cruda no se puede
   truncar a medias sin que se note, y además se puede copiar de un tirón. */
function parseCookieBlob(text) {
    let raw = (text || '').trim();
    if (!raw) return { error: 'Pega las cookies primero.' };

    // El inspector copia con comillas alrededor y, si es JSON, con las de dentro
    // escapadas. Se limpia, en vez de rechazarlo con un error incomprensible.
    const entre = (a, b) => raw.startsWith(a) && raw.endsWith(b) && raw.length > 1;
    if (entre('"', '"') || entre("'", "'")) {
        raw = raw.slice(1, -1);
        if (raw.indexOf('\\"') !== -1) raw = raw.replace(/\\"/g, '"');
    }

    let obj;
    if (raw[0] === '{') {
        try {
            obj = JSON.parse(raw);
        } catch (e) {
            return { error: 'El JSON llegó incompleto o mal copiado (' + e.message
                + '). Prueba pegando directamente el valor de document.cookie, '
                + 'sin llaves ni comillas.' };
        }
        if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
            return { error: 'Tiene que ser un objeto JSON, no una lista.' };
        }
    } else {
        obj = {};
        raw.split(';').forEach(function (par) {
            const corte = par.indexOf('=');
            if (corte < 1) return;
            const clave = par.slice(0, corte).trim();
            if (clave) obj[clave] = par.slice(corte + 1).trim();
        });
        if (!Object.keys(obj).length) {
            return { error: 'No se reconoce nada ahí. Pega el JSON del snippet, '
                + 'o el valor de document.cookie.' };
        }
    }

    // La comprobación que faltaba: sin orm-jwt no hay sesión, y guardarlas era
    // un "éxito" que luego fallaba en la primera descarga.
    if (!obj['orm-jwt']) {
        const n = Object.keys(obj).length;
        return { error: 'Llegaron ' + n + ' cookies pero ninguna es orm-jwt, que '
            + 'es la de la sesión. Cópialas de nuevo desde una pestaña de '
            + 'learning.oreilly.com con la sesión abierta.' };
    }
    return { cookies: obj };
}

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
    const input = document.getElementById('cookie-input').value;
    const errorEl = document.getElementById('cookie-error');

    const leido = parseCookieBlob(input);
    if (leido.error) {
        errorEl.textContent = leido.error;
        errorEl.classList.remove('hidden');
        return;
    }
    const cookies = leido.cookies;

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
    // El modo múltiple necesita saber qué libros hay en pantalla y volver a
    // poner su checkbox cuando la lista se repinta (buscar, cargar más).
    if (typeof batchRegisterCard === 'function') batchRegisterCard(div, book);
    if (typeof watchlistDecorateCard === 'function') watchlistDecorateCard(div, book);
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
                        <div class="book-description text-sm text-zinc-600 leading-relaxed max-h-52 overflow-y-auto pr-2 animate-pulse-subtle">
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

                <!-- Bundle: las dos ediciones del libro, los cinco formatos, con
                     imagenes. Arranca deshabilitado y solo se habilita cuando
                     /api/book/<id>/editions confirma que existe la contraparte.
                     La nota de abajo muestra SIEMPRE con que libro emparejo:
                     unir ediciones es una conjetura y solo quien lee los dos
                     titulos sabe si acerto. -->
                <label class="bundle-option book-only flex items-start gap-2 border-t border-zinc-100 pt-4 opacity-60 cursor-not-allowed">
                    <input type="checkbox" class="bundle-toggle w-4 h-4 mt-0.5 rounded border-zinc-300 text-oreilly-blue focus:ring-oreilly-blue/20" disabled>
                    <span>
                        <span class="text-sm font-medium text-zinc-700">Bundle</span>
                        <span class="bundle-note block text-xs text-zinc-400">Buscando edición en español…</span>
                        <span class="bundle-match block text-xs"></span>
                        <span class="bundle-lock-note hidden block text-xs text-zinc-400 mt-1">Formato, capítulos, salida e imágenes los fija el bundle.</span>
                    </span>
                </label>

                <!-- Fuera del <label> a proposito: un select dentro de la
                     etiqueta del checkbox lo conmutaria al hacer clic. Y select
                     en vez de radios porque los radios comparten \`name\` en
                     toda la pagina, y eso ya nos costo una descarga. -->
                <div class="bundle-source book-only hidden pl-6 pb-1">
                    <label class="text-xs text-zinc-500">Español:
                        <select class="bundle-es-select text-xs border border-zinc-200 rounded px-1.5 py-1 ml-1 text-zinc-700 bg-white"></select>
                    </label>
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
                            <span class="block text-xs text-zinc-400 mt-1">Uses the local NLLB translation service. Code, formulas and images are left untouched.</span>
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


/* ===== Modal de progreso de un bundle =====
   Un bundle son dos descargas, asi que la modal del libro no cuenta la
   historia: el progreso de una sola no dice nada de la otra. Se lee de
   /api/queue filtrando por bundle_id -- los jobs ya lo llevan, asi que no hace
   falta un endpoint nuevo ni un canal de progreso aparte. */

const BUNDLE_LANG_LABEL = { en: 'Inglés', es: 'Español' };

// Nombres legibles de los formatos, para no enseñar claves internas.
const BUNDLE_FMT_LABEL = {
    markdown: 'Markdown', json: 'JSON', plaintext: 'Plain Text',
    pdf: 'PDF', epub: 'EPUB',
    // Pseudo-formato: solo existe en la mitad traducida.
    translation: 'Traducción',
};

function bundleFormatList(codes) {
    return (codes || []).map(function (c) { return BUNDLE_FMT_LABEL[c] || c; }).join(', ');
}

const BUNDLE_STATUS_TEXT = {
    queued: 'En cola',
    running: 'Descargando',
    // Sin afirmar la causa: el mismo estado lo produce una sesion caida y una
    // pagina que solo PARECE truncada, y el motivo real viaja en job.message.
    paused: 'Pausado',
    completed: 'Completado',
    error: 'Error',
    cancelled: 'Cancelado',
};
const BUNDLE_DONE = ['completed', 'error', 'cancelled'];

// El orden en que el downloader genera los formatos. No es cosmetico: de aqui
// sale que un formato anterior al que se esta generando ahora ya termino.
const BUNDLE_FMT_ORDER = ['markdown', 'json', 'plaintext', 'pdf', 'epub'];

// `job.phase` lleva el status crudo del downloader, y es lo UNICO que dice que
// formato esta saliendo en este momento: `job.files` no se rellena hasta que la
// descarga entera termina, asi que durante la corrida esta vacio.
const BUNDLE_PHASE_FMT = {
    generating_markdown: 'markdown',
    generating_json: 'json',
    generating_plaintext: 'plaintext',
    generating_pdf: 'pdf',
    generating_pdf_chapters: 'pdf',
    generating_epub: 'epub',
};
// Fases posteriores a los cinco formatos: si el job esta aqui, ya salieron.
const BUNDLE_PHASE_AFTER = ['generating_toon', 'generating_chunks', 'transferring'];

// Traduccion. Va ANTES de los formatos y se come casi todo el tiempo: sin una
// fila propia, las cinco barras de formato se quedarian a 0% durante media hora
// sin explicar por que.
const BUNDLE_PHASE_TRANSLATING = ['translating_titles', 'translating_chapters'];

/* Estado de la fila de Traduccion. A diferencia de las de formato, esta SI
   lleva un numero real: el trabajo publica el capitulo por el que va, asi que no
   hay que inventarse una animacion indeterminada. */
function bundleTranslationState(job) {
    if (!job) return { key: 'pending', label: 'En espera', fill: 0 };
    if (job.status === 'completed') return { key: 'done', label: 'Listo', fill: 100 };
    if (job.status === 'error' || job.status === 'cancelled') {
        return { key: 'stopped', label: 'Sin hacer', fill: 0 };
    }
    if (BUNDLE_PHASE_TRANSLATING.includes(job.phase)) {
        const total = job.total_chapters || 0;
        const pct = total ? Math.round((job.current_chapter / total) * 100) : 0;
        return { key: 'active', label: pct + '%', fill: pct, quantitative: true };
    }
    // Cualquier fase de generacion va despues de traducir, asi que si estamos
    // en una, la traduccion ya termino.
    if (BUNDLE_PHASE_FMT[job.phase] || BUNDLE_PHASE_AFTER.includes(job.phase)) {
        return { key: 'done', label: 'Listo', fill: 100 };
    }
    return { key: 'pending', label: 'En espera', fill: 0 };
}

const BUNDLE_FMT_STYLE = {
    kept:    { bar: 'bg-zinc-300',     text: 'text-zinc-400' },
    done:    { bar: 'bg-emerald-500',  text: 'text-emerald-600' },
    active:  { bar: 'bg-oreilly-blue', text: 'text-oreilly-blue' },
    pending: { bar: 'bg-zinc-300',     text: 'text-zinc-400' },
    error:   { bar: 'bg-red-500',      text: 'text-red-600' },
    stopped: { bar: 'bg-zinc-300',     text: 'text-amber-600' },
    missing: { bar: 'bg-amber-400',    text: 'text-amber-600' },
};

let bundlePoll = { timer: null, id: null };

/* Estado de UN formato dentro de UN idioma.
 *
 * `kept` es un formato que ya estaba en la carpeta del bundle: el servidor lo
 * saca de la lista a descargar, asi que ningun job lo va a mencionar nunca y
 * sin esto se quedaria en "En espera" para siempre. */
function bundleFormatState(job, fmt, kept) {
    if (kept) return { key: 'kept', label: 'Ya estaba', fill: 100 };
    if (!job) return { key: 'pending', label: 'En espera', fill: 0 };

    const failed = (job.format_errors || {})[fmt];
    if (failed) return { key: 'error', label: 'Falló', fill: 100, detail: String(failed) };
    if ((job.files || {})[fmt]) return { key: 'done', label: 'Listo', fill: 100 };

    // Termino sin dejar archivo ni error para este formato. No se dice "Listo"
    // por educacion: si no hay archivo, no hay archivo.
    if (job.status === 'completed') return { key: 'missing', label: 'Sin archivo', fill: 0 };
    if (job.status === 'error' || job.status === 'cancelled') {
        return { key: 'stopped', label: 'Sin hacer', fill: 0 };
    }

    const current = BUNDLE_PHASE_FMT[job.phase];
    if (current === fmt) return { key: 'active', label: 'Generando…', fill: 100 };
    if (current) {
        return BUNDLE_FMT_ORDER.indexOf(fmt) < BUNDLE_FMT_ORDER.indexOf(current)
            ? { key: 'done', label: 'Listo', fill: 100 }
            : { key: 'pending', label: 'En espera', fill: 0 };
    }
    if (BUNDLE_PHASE_AFTER.includes(job.phase)) return { key: 'done', label: 'Listo', fill: 100 };
    return { key: 'pending', label: 'En espera', fill: 0 };
}

/* Repinta las cinco barras de formato de una seccion. */
function paintBundleFormats(section, job) {
    const kept = section._kept || [];
    section.querySelectorAll('.bundle-fmt').forEach(function (row) {
        const fmt = row.dataset.fmt;
        const state = fmt === 'translation'
            ? bundleTranslationState(job)
            : bundleFormatState(job, fmt, kept.includes(fmt));
        const style = BUNDLE_FMT_STYLE[state.key] || BUNDLE_FMT_STYLE.pending;

        const bar = row.querySelector('.bundle-fmt-bar');
        bar.style.width = state.fill + '%';
        bar.className = 'bundle-fmt-bar h-full rounded-full transition-all duration-300 '
                      + style.bar
                      // Indeterminada a proposito: el downloader no publica el
                      // avance DENTRO de un formato, solo cual esta haciendo.
                      // Una barra que se llenara sola estaria inventando.
                      //
                      // La de traduccion es la excepcion: ahi hay capitulo
                      // actual y total, asi que la barra dice la verdad y el
                      // barrido solo distraeria.
                      + (state.key === 'active' && !state.quantitative
                         ? ' bundle-fmt-bar--active' : '');

        const label = row.querySelector('.bundle-fmt-state');
        label.className = 'bundle-fmt-state text-[0.6875rem] ' + style.text;
        label.textContent = state.label;
        label.title = state.detail || '';
    });
}

/* Una seccion por idioma: cabecera, barra global y las cinco de formato. */
function bundleSection(spec) {
    const section = document.createElement('div');
    section.dataset.jobId = spec.job_id || '';
    section.dataset.lang = spec.language || '';
    section._kept = spec.kept || [];
    section.innerHTML =
        '<div class="flex items-baseline justify-between gap-3">'
      +   '<span class="bundle-sec-lang text-xs font-semibold uppercase tracking-wide text-oreilly-blue"></span>'
      +   '<span class="bundle-sec-pct text-xs font-medium text-zinc-500">0%</span>'
      + '</div>'
      + '<p class="bundle-sec-title text-sm font-medium text-zinc-700 mt-0.5"></p>'
      + '<p class="bundle-sec-src text-[0.6875rem] mt-0.5"></p>'
      + '<div class="h-1.5 bg-zinc-100 rounded-full overflow-hidden mt-2">'
      +   '<div class="bundle-sec-bar h-full bg-oreilly-blue rounded-full transition-all duration-300" style="width:0%"></div>'
      + '</div>'
      + '<p class="bundle-sec-msg text-xs text-zinc-400 mt-1.5">En cola</p>'
      + '<div class="bundle-fmt-list mt-3 space-y-2 pl-3 border-l-2 border-zinc-100"></div>';

    // textContent y no innerHTML: los titulos vienen del catalogo de O'Reilly.
    section.querySelector('.bundle-sec-lang').textContent =
        BUNDLE_LANG_LABEL[spec.language] || String(spec.language || '').toUpperCase();
    section.querySelector('.bundle-sec-title').textContent = spec.title || '';

    // La procedencia se enseña siempre, no solo cuando es automatica: si no se
    // dice de las dos, el silencio no significa nada.
    const src = section.querySelector('.bundle-sec-src');
    src.textContent = spec.machine
        ? '\u26a0 traducido por el traductor local'
        : 'edición publicada';
    src.className = 'bundle-sec-src text-[0.6875rem] mt-0.5 '
        + (spec.machine ? 'text-amber-600' : 'text-zinc-400');
    if (spec.formats && spec.formats.length) {
        section.querySelector('.bundle-sec-msg').textContent =
            'En cola · ' + bundleFormatList(spec.formats);
    } else {
        section.querySelector('.bundle-sec-msg').textContent = 'Nada que descargar';
        section.querySelector('.bundle-sec-pct').textContent = '100%';
        section.querySelector('.bundle-sec-bar').style.width = '100%';
    }

    const list = section.querySelector('.bundle-fmt-list');
    const filas = spec.machine
        ? ['translation'].concat(BUNDLE_FMT_ORDER)
        : BUNDLE_FMT_ORDER;
    filas.forEach(function (fmt) {
        const row = document.createElement('div');
        row.className = 'bundle-fmt';
        row.dataset.fmt = fmt;
        row.innerHTML =
            '<div class="flex items-baseline justify-between gap-2">'
          +   '<span class="bundle-fmt-name text-xs font-medium text-zinc-600"></span>'
          +   '<span class="bundle-fmt-state text-[0.6875rem] text-zinc-400"></span>'
          + '</div>'
          + '<div class="h-1 bg-zinc-100 rounded-full overflow-hidden mt-1">'
          +   '<div class="bundle-fmt-bar h-full rounded-full transition-all duration-300" style="width:0%"></div>'
          + '</div>';
        row.querySelector('.bundle-fmt-name').textContent = BUNDLE_FMT_LABEL[fmt] || fmt;
        list.appendChild(row);
    });

    paintBundleFormats(section, null);
    return section;
}

function openBundleModal(result) {
    const modal = document.getElementById('bundle-modal');
    if (!modal) return;

    const jobs = result.jobs || [];
    const skipped = result.skipped || [];

    let subtitle;
    if (!jobs.length) {
        subtitle = 'Ya estaba completo: no hay nada que descargar.';
    } else {
        const detalle = jobs.map(function (j) {
            return `${BUNDLE_LANG_LABEL[j.language] || j.language}: ${bundleFormatList(j.formats)}`;
        }).join(' · ');
        // Si algo ya estaba en la carpeta se dice de frente: al volver a darle
        // a Descargar, la pregunta es "¿que baja esta vez?", no "¿que hay?".
        const parcial = skipped.length > 0 || jobs.some(function (j) {
            return (j.formats || []).length < BUNDLE_FMT_ORDER.length;
        });
        subtitle = (parcial ? 'Solo falta descargar — ' : '') + detalle;
        if (skipped.length) {
            subtitle += ` · ya completo en ${skipped.map(function (sk) {
                return BUNDLE_LANG_LABEL[sk.language] || sk.language;
            }).join(', ')}`;
        }
    }
    document.getElementById('bundle-modal-sub').textContent = subtitle;
    document.getElementById('bundle-modal-path').textContent = result.dir || '';

    // Una seccion por idioma, se vaya a bajar algo o no. Un bundle son las dos
    // ediciones: enseñar solo la mitad que falta esconde justo lo que se pidio
    // ver -- que hay ya en cada idioma y que esta entrando ahora.
    const have = (result.gap || {}).have || {};
    const specs = jobs.map(function (j) {
        return {
            job_id: j.job_id, language: j.language, title: j.title,
            formats: j.formats, kept: have[j.language] || [],
            machine: j.source === 'machine',
        };
    });
    skipped.forEach(function (sk) {
        specs.push({
            job_id: '', language: sk.language, title: sk.title,
            formats: [], kept: have[sk.language] || BUNDLE_FMT_ORDER.slice(),
            machine: result.es_source === 'machine' && sk.language !== 'en',
        });
    });
    // Ingles primero, como en la carpeta.
    specs.sort(function (a, b) {
        return (a.language === 'en' ? 0 : 1) - (b.language === 'en' ? 0 : 1);
    });

    const rows = document.getElementById('bundle-modal-rows');
    rows.innerHTML = '';
    specs.forEach(function (spec) { rows.appendChild(bundleSection(spec)); });

    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';

    bundlePoll.id = result.bundle_id;
    if (bundlePoll.timer) clearInterval(bundlePoll.timer);
    bundlePoll.timer = null;
    // Sin jobs no hay nada que sondear: la cola no va a decir nada nuevo.
    if (!jobs.length) return;
    bundlePoll.timer = setInterval(refreshBundleModal, 1000);
    refreshBundleModal();
}

/* Vuelve a abrir la ventana de un bundle que ya esta en marcha.

   La modal solo se abria al lanzar la descarga, asi que si la cerrabas o
   recargabas la pagina el bundle seguia bajando sin forma de volver a verlo. Se
   reconstruye desde /api/queue, que ya lleva bundle_id y bundle_lang en cada
   trabajo. */
async function reopenBundleModal(bundleId) {
    if (!bundleId) return;
    let snap;
    try {
        snap = await (await fetch(`${API}/api/queue`)).json();
    } catch (err) {
        return;   // sin cola no hay nada que enseñar
    }

    const jobs = (snap.jobs || []).filter(function (j) {
        return j.bundle_id === bundleId;
    });
    if (!jobs.length) return;

    const machine = jobs.some(function (j) { return !!j.target_lang; });
    openBundleModal({
        bundle_id: bundleId,
        dir: '',
        jobs: jobs.map(function (j) {
            return {
                job_id: j.id, book_id: j.book_id, language: j.bundle_lang,
                title: j.title, formats: j.formats || [],
                source: j.target_lang ? 'machine' : 'edition',
            };
        }),
        skipped: [],
        gap: {},
        es_source: machine ? 'machine' : 'edition',
    });
}

function closeBundleModal() {
    const modal = document.getElementById('bundle-modal');
    if (modal) modal.classList.add('hidden');
    document.body.style.overflow = '';
    if (bundlePoll.timer) clearInterval(bundlePoll.timer);
    bundlePoll.timer = null;
    // Cerrar la ventana NO cancela nada: la cola sigue con lo suyo.
}

async function refreshBundleModal() {
    if (!bundlePoll.id) return;

    let data;
    try {
        const res = await fetch(`${API}/api/queue`);
        data = await res.json();
    } catch (err) {
        return;  // un fallo de red no debe romper la ventana; se reintenta solo
    }

    const jobs = (data.jobs || []).filter(function (j) {
        return j.bundle_id === bundlePoll.id;
    });
    if (!jobs.length) return;

    const rows = document.getElementById('bundle-modal-rows');
    jobs.forEach(function (job) {
        // Por job_id cuando ya se conoce; por idioma la primera vez, porque el
        // servidor pudo devolver un job que ya existia en la cola.
        const section = rows.querySelector('[data-job-id="' + job.id + '"]')
                     || rows.querySelector('[data-lang="' + job.bundle_lang + '"]');
        if (!section) return;
        section.dataset.jobId = job.id;

        // La barra de arriba es la global de la descarga, y es la unica con un
        // numero real detras: los capitulos son el 90% del trabajo y no
        // pertenecen a ningun formato en concreto.
        const pct = job.percentage || 0;
        section.querySelector('.bundle-sec-title').textContent = job.title || '';
        section.querySelector('.bundle-sec-pct').textContent = pct + '%';
        section.querySelector('.bundle-sec-bar').style.width = pct + '%';

        const parts = [BUNDLE_STATUS_TEXT[job.status] || job.status];
        if (job.status === 'running') {
            if (job.total_chapters) {
                parts.push('capítulo ' + job.current_chapter + '/' + job.total_chapters);
            }
            if (job.message) parts.push(job.message);
        } else if (job.error) {
            parts.push(job.error);
        } else if (job.message) {
            // Un trabajo pausado no tiene `error`, pero si `message`, con el
            // motivo de verdad. Antes se descartaba y quedaba la etiqueta
            // genérica, que es como se acaba culpando a la sesión sin pruebas.
            parts.push(job.message);
        }
        section.querySelector('.bundle-sec-msg').textContent = parts.filter(Boolean).join(' · ');

        paintBundleFormats(section, job);
    });

    const finished = jobs.every(function (j) { return BUNDLE_DONE.includes(j.status); });
    if (!finished) return;

    if (bundlePoll.timer) clearInterval(bundlePoll.timer);
    bundlePoll.timer = null;

    const allOk = jobs.every(function (j) { return j.status === 'completed'; });
    document.getElementById('bundle-modal-sub').textContent = allOk
        ? 'Bundle completo. Ya está en la biblioteca.'
        : 'Bundle terminado con fallos. Lo que sí bajó se conservó.';

    if (typeof loadLibrary === 'function') loadLibrary();
}

(function wireBundleModal() {
    function attach() {
        const close = document.getElementById('bundle-modal-close');
        if (close) close.addEventListener('click', closeBundleModal);
        const backdrop = document.getElementById('bundle-modal-backdrop');
        if (backdrop) backdrop.addEventListener('click', closeBundleModal);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', attach);
    } else {
        attach();
    }
})();

/**
 * Bloquea los controles que el bundle decide por ti.
 *
 * Un bundle fija los cinco formatos, el libro completo, la salida combinada y
 * las imágenes. Dejar esos controles activos sería ofrecer una elección que el
 * servidor va a ignorar de todas formas, que es peor que no ofrecerla.
 */
function setBundleLock(cardElement, locked) {
    if (locked) {
        // Hay que dejar un formato marcado ANTES de deshabilitar: el submit
        // exige input[name="format"]:checked y si no encuentra ninguno sacude
        // la seccion y se planta. Y no hay ninguno marcado porque todas las
        // tarjetas comparten name="format", asi que el navegador las trata
        // como un solo grupo y solo una radio de toda la pagina puede estarlo.
        // Markdown, que es con el que arranca el bundle.
        const md = cardElement.querySelector('input[name="format"][value="markdown"]');
        if (md) {
            md.checked = true;
            // Asignar .checked no dispara 'change', y de ahi cuelga la logica
            // que ajusta el resto de la modal.
            md.dispatchEvent(new Event('change', { bubbles: true }));
        }
        const combined = cardElement.querySelector('input[name="output-style"][value="combined"]');
        if (combined) combined.checked = true;
    }

    ['.format-options', '.chapters-options', '.output-options'].forEach(function (sel) {
        const group = cardElement.querySelector(sel);
        if (!group) return;
        group.querySelectorAll('input').forEach(function (input) { input.disabled = locked; });
        group.classList.toggle('opacity-40', locked);
        group.classList.toggle('pointer-events-none', locked);
    });

    const skip = cardElement.querySelector('.skip-images');
    if (skip) {
        skip.disabled = locked;
        if (locked) skip.checked = false;
        const label = skip.closest('label');
        if (label) label.classList.toggle('opacity-40', locked);
    }

    const lang = cardElement.querySelector('.target-lang');
    if (lang) {
        lang.disabled = locked;
        const wrap = lang.closest('.book-only');
        if (wrap) wrap.classList.toggle('opacity-40', locked);
    }

    const note = cardElement.querySelector('.bundle-lock-note');
    if (note) note.classList.toggle('hidden', !locked);
}

/**
 * ¿Existe este libro en español? Habilita (o no) el checkbox de Bundle.
 *
 * Nunca contesta un sí pelado: O'Reilly le da a cada edición su propio ISBN y
 * no las enlaza entre sí, así que emparejarlas es una conjetura con puntaje.
 * Por eso el título emparejado se muestra siempre — un bundle armado con el
 * libro equivocado es peor que no tener bundle.
 */
/* Que vas a bajar con el modo elegido. Tres textos y no uno: la diferencia
   entre una traduccion publicada, una automatica y no tener español es
   justamente lo que hay que decir antes de darle a Descargar. */
function describeBundleSource(cardElement, value, data) {
    const note = cardElement.querySelector('.bundle-note');
    const match = cardElement.querySelector('.bundle-match');
    const translator = data.translator || {};

    if (value === 'edition' && data.candidate) {
        note.textContent = 'Los 5 formatos en inglés y español, con imágenes.';
        const pct = Math.round((data.score || 0) * 100);
        let line = `Emparejado con: ${data.candidate.title} (${pct}%)`;
        // El aviso importa mas que el porcentaje: un titulo identico al
        // original suele ser la misma obra listada dos veces, y el bundle
        // bajaria el mismo libro dos veces sin que se note.
        if (data.warning) line += ` — ${data.warning}`;
        match.textContent = line;
        match.className = 'bundle-match block text-xs '
            + (data.confident ? 'text-emerald-600' : 'text-amber-600');
    } else if (value === 'machine') {
        note.textContent = 'Los 5 formatos en inglés, y en español traducido '
            + 'por el traductor local.';
        match.textContent = 'Traducción automática: calidad por debajo de una '
            + 'edición publicada, usa la GPU, y el libro se descarga entero '
            + 'otra vez para traducirlo.';
        match.className = 'bundle-match block text-xs text-amber-600';
    } else {
        note.textContent = 'Los 5 formatos, sólo en inglés, con imágenes.';
        if (data.found) {
            match.textContent = 'Hay edición en español, pero has elegido no bajarla.';
        } else if (translator.available) {
            match.textContent = 'No hay edición en español en el catálogo.';
        } else {
            match.textContent = 'No hay edición en español, y el traductor '
                + 'local no está disponible.';
        }
        match.className = 'bundle-match block text-xs text-zinc-400';
    }

    applyBundleGap(data.bundle, note);
}

/* Puebla el select con lo que de verdad hay disponible.

   El orden es el de preferencia: edicion publicada > traduccion > solo ingles,
   y el primero queda elegido. "Solo inglés" esta SIEMPRE, para que ni la falta
   de edicion ni un traductor apagado puedan quitar el feature. */
function setBundleSourceOptions(cardElement, data) {
    const wrap = cardElement.querySelector('.bundle-source');
    const select = cardElement.querySelector('.bundle-es-select');
    if (!wrap || !select) return;

    const opciones = [];
    if (data.found && data.candidate) {
        opciones.push(['edition', 'Edición publicada']);
    }
    if ((data.translator || {}).available) {
        opciones.push(['machine', 'Traducir con el traductor local']);
    }
    opciones.push(['none', 'Sólo inglés']);

    select.innerHTML = '';
    opciones.forEach(function (opt) {
        const el = document.createElement('option');
        el.value = opt[0];
        el.textContent = opt[1];
        select.appendChild(el);
    });
    select.value = opciones[0][0];
    wrap.classList.remove('hidden');

    cardElement.dataset.bundleEsSource = select.value;
    select.onchange = function () {
        cardElement.dataset.bundleEsSource = select.value;
        describeBundleSource(cardElement, select.value, data);
    };
    describeBundleSource(cardElement, select.value, data);
}

/* Que falta de verdad, leido del disco. Sin esto la casilla promete diez
   archivos aunque ocho ya esten ahi. Extraido porque ahora hay dos caminos que
   llegan aqui: la edicion publicada y la traduccion. */
function applyBundleGap(gap, note) {
    if (!gap || !gap.exists || !gap.total) return;
    if (gap.complete) {
        note.textContent = `Ya está completo: ${gap.have_count}/${gap.total} `
            + 'archivos. No hay nada que descargar.';
        return;
    }
    if (gap.have_count <= 0) return;

    const parts = [];
    ['en', 'es'].forEach(function (lang) {
        const falta = (gap.missing || {})[lang] || [];
        if (falta.length) {
            parts.push(`${bundleFormatList(falta)} (${BUNDLE_LANG_LABEL[lang] || lang})`);
        }
    });
    note.textContent = `Ya tienes ${gap.have_count}/${gap.total}. `
        + `Sólo se generará: ${parts.join(' · ')}. `
        + 'El libro se descarga entero igual: los capítulos hacen falta para '
        + 'generar cualquier formato.';
}

async function checkBundleAvailability(cardElement, bookId) {
    const label = cardElement.querySelector('.bundle-option');
    const box = cardElement.querySelector('.bundle-toggle');
    const note = cardElement.querySelector('.bundle-note');
    const match = cardElement.querySelector('.bundle-match');
    if (!label || !box || !note || !match) return;

    box.checked = false;
    box.disabled = true;
    label.classList.add('opacity-60', 'cursor-not-allowed');
    label.classList.remove('cursor-pointer');
    note.textContent = 'Buscando edición en español…';
    match.textContent = '';
    setBundleLock(cardElement, false);
    const sourceWrap = cardElement.querySelector('.bundle-source');
    if (sourceWrap) sourceWrap.classList.add('hidden');

    // Marcar como imágenes obligatorias mientras el bundle esté activo: un
    // bundle promete las ilustraciones, así que las dos opciones se excluyen.
    box.onchange = () => setBundleLock(cardElement, box.checked);

    try {
        const res = await fetch(`${API}/api/book/${bookId}/editions?language=es`);
        const data = await res.json();

        // La tarjeta pudo cerrarse o cambiar mientras buscábamos.
        if (!cardElement.classList.contains('expanded')) return;

        // El checkbox se habilita SIEMPRE. Que no haya edición en español, o
        // que el traductor esté apagado, sólo quita opciones del select: un
        // bundle de un solo idioma sigue siendo útil, y bloquear el feature
        // entero por un servicio caído era justo lo que no debía pasar.
        box.disabled = false;
        label.classList.remove('opacity-60', 'cursor-not-allowed');
        label.classList.add('cursor-pointer');

        setBundleSourceOptions(cardElement, data);
    } catch (err) {
        if (!cardElement.classList.contains('expanded')) return;
        // Ni un fallo de red quita el bundle: queda "sólo inglés", que no
        // necesita saber nada del catálogo ni del traductor.
        note.textContent = 'No se pudo comprobar la edición en español.';
        box.disabled = false;
        label.classList.remove('opacity-60', 'cursor-not-allowed');
        label.classList.add('cursor-pointer');
        setBundleSourceOptions(cardElement, { found: false, translator: {} });
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

    // Sin await a proposito: la modal abre ya y el checkbox se habilita cuando
    // la busqueda vuelve. Bloquear la apertura por una peticion de red seria
    // pagar latencia en el 100% de los casos por una opcion que se usa poco.
    checkBundleAvailability(cardElement, bookId);

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

    // El titulo va en la peticion para que la cola pueda mostrarlo: sin el,
    // la modal de progreso ensena el ISBN.
    const tituloEl = cardElement.querySelector('.tile-title');
    const titulo = tituloEl ? tituloEl.textContent.trim() : '';

    const requestBody = isAudio
        ? { book_id: bookId, title: titulo, content_type: 'audiobook' }
        : { book_id: bookId, title: titulo, format: finalFormat };
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
        const bundleBox = cardElement.querySelector('.bundle-toggle');
        if (bundleBox && bundleBox.checked && !bundleBox.disabled) {
            // El servidor vuelve a resolver la contraparte: el checkbox dice
            // que existe, no cual es, y confiar en un id que viaje desde el
            // navegador seria confiar en el cliente para elegir que se baja.
            requestBody.bundle = true;
            requestBody.bundle_language = 'es';
            // De donde sale el español: la edición publicada, o este mismo
            // libro traducido. Lo decidio checkBundleAvailability al mirar el
            // catalogo; el servidor lo vuelve a comprobar de todas formas.
            requestBody.bundle_es_source =
                cardElement.dataset.bundleEsSource || 'edition';
        }
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

        if (result.bundle) {
            // La tarjeta ya no sirve: hay DOS descargas y su progreso vive en
            // la cola, no en este poll. Se cierra y se abre la ventana del
            // bundle, que lee de /api/queue filtrando por bundle_id.
            setDownloadLock(false);
            collapseBook();
            openBundleModal(result);
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

        // Terminada con éxito el botón NO vuelve a su estado inicial. Dejarlo
        // activo invitaba a pulsarlo otra vez y repetir la misma descarga.
        // El color va por clase CSS y no por utilidades de Tailwind: el
        // `disabled:` depende del orden en que se emita la variante y aquí no
        // conviene apostar a eso.
        function markDownloaded() {
            downloadBtn.classList.remove('hidden');
            downloadBtn.disabled = true;
            downloadBtn.textContent = 'Descargado';
            downloadBtn.classList.add('is-done');
            cancelBtn.classList.add('hidden');
            cancelBtn.disabled = false;
            cancelBtn.textContent = 'Cancel';
            setDownloadLock(false);

            // Si cambias cualquier opción (formato, idioma, capítulos) se
            // rehabilita: ya no sería la misma descarga, y sin esto la única
            // salida para bajarlo traducido era repetir la búsqueda.
            const panel = cardElement.querySelector('.book-expanded');
            if (panel) {
                const reenable = function () {
                    downloadBtn.disabled = false;
                    downloadBtn.textContent = 'Descargar de nuevo';
                    downloadBtn.classList.remove('is-done');
                    panel.removeEventListener('change', reenable);
                };
                panel.addEventListener('change', reenable);
            }
        }

        if (data.status === 'completed') {
            markDownloaded();
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

/* Confirmacion en linea dentro del modal. Se resuelve con la eleccion del
   usuario, para poder esperarla desde saveLibraryDir sin un dialogo del sistema. */
function confirmLibraryChange(total, actual) {
    return new Promise(function (resolve) {
        const host = document.getElementById('settings-library-state');
        host.classList.remove('hidden');
        host.className = 'settings-confirm';
        host.innerHTML = '';

        const texto = document.createElement('p');
        texto.textContent = 'Tu biblioteca actual tiene ' + total
            + (total === 1 ? ' obra en ' : ' obras en ') + actual
            + '. Cambiar de carpeta NO las mueve: se quedan donde están y la '
            + 'nueva empieza vacía.';

        const fila = document.createElement('div');
        fila.className = 'settings-confirm-row';

        const cancelar = document.createElement('button');
        cancelar.type = 'button';
        cancelar.className = 'settings-confirm-btn';
        cancelar.textContent = 'Cancelar';

        const seguir = document.createElement('button');
        seguir.type = 'button';
        seguir.className = 'settings-confirm-btn is-danger';
        seguir.textContent = 'Cambiar de todos modos';

        function cerrar(valor) {
            host.innerHTML = '';
            host.className = 'hidden';
            renderSettings();
            resolve(valor);
        }
        cancelar.onclick = function () { cerrar(false); };
        seguir.onclick = function () { cerrar(true); };

        fila.append(cancelar, seguir);
        host.append(texto, fila);
    });
}

async function saveLibraryDir(path) {
    const state = document.getElementById('settings-library-state');

    // Mover la biblioteca de sitio es una decision con consecuencias, asi que
    // se dice cuantas obras se quedan atras ANTES de tocar el ajuste.
    try {
        const res = await fetch(`${API}/api/library`);
        const data = await res.json();
        const total = data.total || 0;
        if (total > 0 && String(path || '') !== librarySettings.dir) {
            const seguir = await confirmLibraryChange(total, librarySettings.dir);
            if (!seguir) return;
        }
    } catch (err) {
        // Si no se puede contar, se sigue: el aviso es una cortesia, no un
        // requisito, y bloquear el ajuste por eso seria peor.
    }

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
            // El logo es "empezar de cero". Sin limpiar la consulta,
            // goToSection('book') relanzaba la última búsqueda aunque el input
            // se viera vacío.
            searchState.query = '';
            searchState.page = 0;
            const input = document.getElementById('search-input');
            if (input) input.value = '';
            goToSection('book');
        };
    }

    // --- menú "Mis libros" ---
    const booksBtn = document.getElementById('books-menu-btn');
    const booksMenu = document.getElementById('books-menu');

    function closeBooksMenu() {
        if (!booksMenu) return;
        booksMenu.classList.add('hidden');
        if (booksBtn) booksBtn.setAttribute('aria-expanded', 'false');
    }

    if (booksBtn && booksMenu) {
        booksBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            const abierto = booksMenu.classList.toggle('hidden');
            booksBtn.setAttribute('aria-expanded', String(!abierto));
        });
        document.addEventListener('click', closeBooksMenu);
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape') closeBooksMenu();
        });
    }

    document.querySelectorAll('.ct-tab').forEach(tab => {
        tab.addEventListener('click', (e) => {
            if (downloadInProgress) return;
            e.stopPropagation();
            const ct = tab.dataset.ct;
            closeBooksMenu();

            // Desde el reproductor, elegir "Biblioteca" es volver a la rejilla.
            if (ct === 'library' && playerView) {
                closePlayer();
                return;
            }
            // Las entradas del menú ya no alternan: ir a una sección lleva a esa
            // sección y punto. Para volver al inicio está el logo, que es lo que
            // hacía falta cuando ese comportamiento se inventó.
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
