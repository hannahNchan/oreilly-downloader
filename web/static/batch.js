/* ===========================================================================
   Descargas múltiples

   Flujo: botón "Múltiples" en los resultados -> checkbox en cada tarjeta ->
   barra flotante con el conteo -> modal con mini-cards plegables (formato,
   traducción y "Omitir imágenes" por elemento) -> "Descargar todos" -> cada
   elemento muestra su barra.

   El backend es una cola de UNO en UNO, así que solo una barra avanza a la vez
   y las demás muestran su posición. Cerrar la modal no detiene nada: queda un
   aviso flotante que la vuelve a abrir.
   =========================================================================== */

const BATCH_FORMATS = [
    ['epub', 'EPUB'],
    ['pdf', 'PDF'],
    ['markdown', 'Markdown'],
    ['plaintext', 'Texto plano'],
    ['json', 'JSON'],
    ['toon', 'TOON'],
];

const batchState = {
    active: false,              // modo selección encendido
    selected: new Map(),        // book_id -> datos del libro
    books: new Map(),           // book_id -> datos, de todo lo renderizado
    options: new Map(),         // book_id -> { format, target_lang, skip_images }
    launched: [],               // book_ids enviados a la cola
    poll: null,
};

/* --- registro de tarjetas ------------------------------------------------ */

/* Lo llama renderBookCard por cada tarjeta pintada: así el modo múltiple sabe
   qué libros hay en pantalla sin tener que re-consultar la búsqueda. */
function batchRegisterCard(card, book) {
    batchState.books.set(String(book.id), book);
    if (batchState.active) batchDecorate(card, book);
}

function batchDecorate(card, book) {
    if (card.querySelector('.batch-check')) return;
    const id = String(book.id);

    const box = document.createElement('label');
    box.className = 'batch-check';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = batchState.selected.has(id);
    input.setAttribute('aria-label', 'Seleccionar ' + (book.title || ''));
    input.onclick = function (e) { e.stopPropagation(); };
    input.onchange = function () { batchToggle(id, input.checked); };
    box.appendChild(input);
    card.appendChild(box);
    card.classList.toggle('is-selected', batchState.selected.has(id));
}

function batchToggle(id, on) {
    const book = batchState.books.get(id);
    if (on && book) batchState.selected.set(id, book);
    else batchState.selected.delete(id);

    document.querySelectorAll('#search-results .book-card').forEach(function (card) {
        if (String(card.dataset.bookId) !== id) return;
        card.classList.toggle('is-selected', on);
        const input = card.querySelector('.batch-check input');
        if (input) input.checked = on;
    });
    batchRenderActionBar();
}

/* --- modo selección ------------------------------------------------------ */

function batchSetMode(on) {
    batchState.active = on;
    if (!on) batchState.selected.clear();

    document.body.classList.toggle('batch-mode', on);
    document.querySelectorAll('#search-results .book-card').forEach(function (card) {
        const book = batchState.books.get(String(card.dataset.bookId));
        if (on && book) batchDecorate(card, book);
        if (!on) {
            const box = card.querySelector('.batch-check');
            if (box) box.remove();
            card.classList.remove('is-selected');
        }
    });

    const btn = document.getElementById('batch-toggle');
    if (btn) btn.textContent = on ? '✕ Salir de múltiples' : '☰ Múltiples';
    batchRenderActionBar();
}

function batchRenderActionBar() {
    let bar = document.getElementById('batch-bar');
    const n = batchState.selected.size;

    if (!batchState.active || n === 0) {
        if (bar) bar.remove();
        return;
    }
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'batch-bar';
        bar.innerHTML =
            '<span id="batch-count"></span>'
            + '<div class="batch-bar-actions">'
            + '<button type="button" id="batch-clear" class="batch-btn">Limpiar</button>'
            + '<button type="button" id="batch-start" class="batch-btn is-primary">'
            + 'Iniciar múltiples descargas</button></div>';
        document.body.appendChild(bar);
        bar.querySelector('#batch-clear').onclick = function () {
            [...batchState.selected.keys()].forEach(function (id) { batchToggle(id, false); });
        };
        bar.querySelector('#batch-start').onclick = batchOpenModal;
    }
    bar.querySelector('#batch-count').textContent =
        n === 1 ? '1 seleccionado' : n + ' seleccionados';
}

/* --- modal --------------------------------------------------------------- */

/* Modal de PROGRESO. Se construye desde la cola del servidor, que es quien
   sabe la verdad: así funciona vengas de donde vengas —selección múltiple,
   "Para después" o la ficha de un libro suelto—. Antes salía de
   `batchState.launched`, que solo se rellena en el flujo de selección, y por
   eso el aviso flotante no abría nada si habías descargado desde otro sitio. */
/* Estados que siguen vivos. Lo demás es histórico y no pinta aquí. */
const BATCH_ACTIVOS = ['queued', 'running', 'paused'];

/* La cola trae book_id y título; se adaptan a la forma que espera la fila.
   `job_id` viaja porque es la identidad real: un mismo libro puede tener un
   intento cancelado y otro en curso, y con book_id las dos filas se refrescaban
   desde el mismo trabajo y salían duplicadas y con el mismo estado. */
function batchItemFromJob(j) {
    return {
        id: String(j.book_id),
        job_id: j.id,
        title: j.title || j.book_id,
        content_type: j.content_type,
        publishers: [],
        issued: null,
    };
}

async function batchOpenQueueModal() {
    let snap;
    try {
        const res = await fetch(API + '/api/queue');
        snap = await res.json();
    } catch (err) {
        return;
    }
    // Solo las descargas de ahora. El servidor guarda también las terminadas
    // —las necesita para otras cosas— y al abrir la modal aparecía el histórico
    // entero de la sesión: descargas de hace rato marcadas "Descargado" y, con
    // ellas, avisos de errores ya resueltos.
    const jobs = (snap.jobs || []).filter(function (j) {
        return BATCH_ACTIVOS.indexOf(j.status) !== -1;
    });
    if (!jobs.length) return;

    batchRenderModal(jobs.map(batchItemFromJob), true);
}

/* Modal de SELECCIÓN, antes de lanzar nada. */
function batchOpenModal() {
    const items = [...batchState.selected.values()];
    if (!items.length) return;
    batchRenderModal(items, false);
}

function batchRenderModal(items, launched) {
    let modal = document.getElementById('batch-modal');
    if (modal) modal.remove();

    modal = document.createElement('div');
    modal.id = 'batch-modal';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.innerHTML =
        '<div class="batch-shell">'
        + '<header class="batch-head"><h3 id="batch-title"></h3></header>'
        + '<div class="batch-list" id="batch-list"></div>'
        + '<footer class="batch-foot" id="batch-foot"></footer>'
        + '</div>';
    document.body.appendChild(modal);
    document.body.classList.add('batch-open');

    const list = modal.querySelector('#batch-list');
    items.forEach(function (book) { list.appendChild(batchItemRow(book, !!launched)); });

    batchRenderModalChrome(!!launched, items.length);
    if (launched) batchStartPolling();
}

function batchRenderModalChrome(running, count) {
    const title = document.getElementById('batch-title');
    const foot = document.getElementById('batch-foot');
    if (!title || !foot) return;

    title.textContent = running
        ? 'Descargando ' + count + (count === 1 ? ' elemento' : ' elementos')
        : 'Descargar ' + count + (count === 1 ? ' elemento' : ' elementos');

    foot.innerHTML = '';
    if (running) {
        const cerrar = document.createElement('button');
        cerrar.type = 'button';
        cerrar.className = 'batch-btn';
        cerrar.textContent = 'Cerrar';
        // Cerrar no cancela: la cola sigue y queda el aviso flotante.
        cerrar.onclick = batchCloseModal;
        foot.appendChild(cerrar);
    } else {
        const cancelar = document.createElement('button');
        cancelar.type = 'button';
        cancelar.className = 'batch-btn';
        cancelar.textContent = 'Cancelar';
        cancelar.onclick = batchCloseModal;

        const lanzar = document.createElement('button');
        lanzar.type = 'button';
        lanzar.className = 'batch-btn is-primary';
        lanzar.textContent = 'Descargar todos';
        lanzar.onclick = batchLaunch;

        foot.append(cancelar, lanzar);
    }
}

function batchItemRow(book, running) {
    const id = String(book.id);
    const isAudio = (book.content_type || searchState.contentType) === 'audiobook';

    const row = document.createElement('article');
    row.className = 'batch-item';
    row.dataset.bookId = id;
    if (book.job_id) row.dataset.jobId = book.job_id;

    const head = document.createElement('div');
    head.className = 'batch-item-head';

    // Un audiolibro no tiene nada que configurar, así que no se despliega:
    // una flecha que abre un panel vacío es una promesa incumplida.
    const caret = document.createElement('span');
    caret.className = 'batch-caret';
    caret.textContent = (isAudio || running) ? '' : '▸';

    const title = document.createElement('span');
    title.className = 'batch-item-title';
    title.textContent = book.title || id;

    const meta = document.createElement('span');
    meta.className = 'batch-item-meta';
    meta.textContent = [isAudio ? 'AUDIO' : 'EPUB',
                        (book.publishers || [])[0], book.issued || book.year]
        .filter(Boolean).join(' · ');

    head.append(caret, title, meta);
    row.appendChild(head);

    if (!isAudio && !running) {
        const panel = document.createElement('div');
        panel.className = 'batch-item-panel hidden';
        panel.innerHTML =
            '<label class="batch-field">Formato'
            + '<select class="batch-format">'
            + BATCH_FORMATS.map(function (f) {
                return '<option value="' + f[0] + '">' + f[1] + '</option>';
            }).join('')
            + '</select></label>'
            + '<label class="batch-field">Traducir'
            + '<select class="batch-lang">'
            + '<option value="original">Original (sin traducir)</option>'
            + '<option value="es-LATAM">Español (Latinoamérica)</option>'
            + '</select></label>'
            + '<label class="batch-skip"><input type="checkbox" class="batch-skip-images">'
            + '<span>Omitir imágenes'
            + '<span class="batch-hint">Más rápido y ligero, pero sin ilustraciones</span>'
            + '</span></label>';
        row.appendChild(panel);

        head.onclick = function () {
            const open = panel.classList.toggle('hidden');
            caret.textContent = open ? '▸' : '▾';
        };

        const save = function () {
            batchState.options.set(id, {
                format: panel.querySelector('.batch-format').value,
                target_lang: panel.querySelector('.batch-lang').value,
                skip_images: panel.querySelector('.batch-skip-images').checked,
            });
        };
        panel.querySelectorAll('select, input').forEach(function (el) {
            el.addEventListener('change', save);
        });
        save();   // deja el valor por defecto registrado
    }

    if (running) {
        const prog = document.createElement('div');
        prog.className = 'batch-progress';
        prog.innerHTML =
            '<div class="batch-bar-track"><div class="batch-bar-fill"></div></div>'
            + '<span class="batch-state">En cola</span>'
            + '<button type="button" class="batch-btn is-small batch-cancel">Quitar</button>';
        row.appendChild(prog);
    }
    return row;
}

function batchCloseModal() {
    const modal = document.getElementById('batch-modal');
    if (modal) modal.remove();
    document.body.classList.remove('batch-open');
    batchRenderPill();
}

/* --- lanzar -------------------------------------------------------------- */

async function batchLaunch() {
    const items = [...batchState.selected.values()];
    batchState.launched = items.map(function (b) { return String(b.id); });

    for (const book of items) {
        const id = String(book.id);
        const isAudio = (book.content_type || searchState.contentType) === 'audiobook';
        const opts = batchState.options.get(id) || {};
        const body = { book_id: id, title: book.title, transfer: true };

        if (isAudio) {
            body.content_type = 'audiobook';
        } else {
            body.format = opts.format || 'epub';
            if (opts.skip_images) body.skip_images = true;
            if (opts.target_lang && opts.target_lang !== 'original') {
                body.target_lang = opts.target_lang;
            }
        }
        try {
            await fetch(API + '/api/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        } catch (err) {
            console.error('No se pudo encolar', id, err);
        }
    }

    batchSetMode(false);
    batchOpenQueueModal();
}

/* --- seguimiento --------------------------------------------------------- */

function batchStartPolling() {
    if (batchState.poll) return;
    batchState.poll = setInterval(batchTick, 700);
    batchTick();
}

function batchStopPolling() {
    clearInterval(batchState.poll);
    batchState.poll = null;
}

async function batchTick() {
    let snap;
    try {
        const res = await fetch(API + '/api/queue');
        snap = await res.json();
    } catch (err) {
        return;
    }

    const porJob = new Map();
    const porLibro = new Map();
    (snap.jobs || []).forEach(function (j) {
        porJob.set(j.id, j);
        porLibro.set(String(j.book_id), j);
    });

    // Descargas lanzadas con la modal ya abierta (desde "Para después", por
    // ejemplo): se adoptan, en vez de correr sin que se vean.
    const lista = document.getElementById('batch-list');
    if (lista) {
        (snap.jobs || []).forEach(function (j) {
            if (BATCH_ACTIVOS.indexOf(j.status) === -1) return;
            if (lista.querySelector('[data-job-id="' + j.id + '"]')) return;
            lista.appendChild(batchItemRow(batchItemFromJob(j), true));
        });
        const titulo = document.getElementById('batch-title');
        const n = lista.querySelectorAll('.batch-item').length;
        if (titulo) {
            // Un bundle son dos mitades: llamarlo "1 elemento" sin decir
            // que es un bundle es lo que hacia que pareciera otra cosa.
            const hayBundle = (snap.jobs || []).some(function (j) {
                return j.bundle_id && BATCH_ACTIVOS.indexOf(j.status) !== -1;
            });
            titulo.textContent = 'Descargando ' + n
                + (n === 1 ? ' elemento' : ' elementos')
                + (hayBundle ? ' \u00b7 bundle' : '');
        }
    }

    document.querySelectorAll('#batch-modal .batch-item').forEach(function (row) {
        // Por trabajo primero; por libro solo como red de seguridad para las
        // filas que nacieron de la selección y aún no tienen job_id.
        const job = porJob.get(row.dataset.jobId) || porLibro.get(row.dataset.bookId);
        const fill = row.querySelector('.batch-bar-fill');
        const state = row.querySelector('.batch-state');
        const cancel = row.querySelector('.batch-cancel');
        if (!job || !fill) return;

        fill.style.width = (job.percentage || 0) + '%';
        row.dataset.status = job.status;
        state.textContent = batchLabel(job);
        batchRenderBundleTag(row, job);
        fill.classList.toggle('is-paused', job.status === 'paused');
        fill.classList.toggle('is-error', job.status === 'error');

        if (cancel) {
            const done = ['completed', 'error', 'cancelled'].indexOf(job.status) !== -1;
            cancel.classList.toggle('hidden', done);
            cancel.textContent = job.status === 'running' ? 'Cancelar' : 'Quitar';
            cancel.onclick = function () {
                fetch(API + '/api/queue/' + job.id + '/cancel', { method: 'POST' });
            };
        }
    });

    // Sesión caducada: la cola entera está parada esperando cookies.
    const pausado = (snap.jobs || []).filter(function (j) {
        return j.status === 'paused';
    })[0];
    batchRenderPausedNotice(snap.paused, pausado);
    batchRenderSpectatorNotice(snap);
    batchRenderPill(snap);

    if (!snap.active) {
        batchStopPolling();
        if (typeof loadLibrary === 'function') loadLibrary({ refresh: true });
    }
}

/* Marca la fila como mitad de un bundle, y deja volver a su ventana.

   Sin esto un bundle en marcha se veia como una descarga suelta: la modal del
   bundle solo se abria al lanzarla, asi que al cerrarla o recargar la pagina no
   habia forma de volver a verla. */
function batchRenderBundleTag(row, job) {
    let tag = row.querySelector('.batch-bundle-tag');
    if (!job.bundle_id) {
        if (tag) tag.remove();
        return;
    }
    if (!tag) {
        tag = document.createElement('button');
        tag.type = 'button';
        tag.className = 'batch-bundle-tag';
        tag.title = 'Parte de un bundle. Abre su ventana de progreso.';
        const state = row.querySelector('.batch-state');
        if (state && state.parentNode) state.parentNode.insertBefore(tag, state);
        else row.appendChild(tag);
    }
    const idioma = { en: 'Inglés', es: 'Español' }[job.bundle_lang]
        || job.bundle_lang || '';
    tag.textContent = 'BUNDLE' + (idioma ? ' · ' + idioma : '')
        + (job.target_lang ? ' · IA' : '');
    tag.onclick = function () {
        if (typeof reopenBundleModal === 'function') {
            reopenBundleModal(job.bundle_id);
        }
    };
}

function batchLabel(job) {
    if (job.status === 'queued') {
        return job.position ? 'En cola · ' + job.position + 'º' : 'En cola';
    }
    if (job.status === 'paused') return 'Esperando cookies';
    if (job.status === 'completed') return 'Descargado';
    if (job.status === 'cancelled') return 'Cancelado';
    if (job.status === 'error') return 'Error: ' + (job.error || '').slice(0, 60);
    if (job.message) return job.message;
    return (job.percentage || 0) + '%';
}

/* Este servidor sirve la cola pero no la ejecuta, porque la tiene otro proceso
   apuntando al mismo data/. Sin decirlo, la lista se quedaba en "En cola" para
   siempre bajo un título que decía "Descargando" — que es exactamente lo que
   parecía roto. Se reintenta tomarla cada pocos segundos, así que el aviso se
   quita solo en cuanto el otro servidor cierre. */
function batchRenderSpectatorNotice(snap) {
    const foot = document.getElementById('batch-foot');
    if (!foot) return;
    let aviso = document.getElementById('batch-spectator');

    // `owner` no existe en servidores viejos: sin el dato no se afirma nada.
    const espectador = snap.owner === false;
    if (!espectador) {
        if (aviso) aviso.remove();
        return;
    }
    if (aviso) return;

    aviso = document.createElement('div');
    aviso.id = 'batch-spectator';
    aviso.className = 'batch-paused';
    const texto = document.createElement('p');
    texto.className = 'batch-paused-text';
    texto.textContent = 'Hay otro servidor de la app con la cola tomada, así que '
        + 'aquí no se descarga nada: solo se muestra. Cierra el otro y esto '
        + 'continúa solo, sin reiniciar.';
    aviso.appendChild(texto);
    foot.parentNode.insertBefore(aviso, foot);
}

/* El formulario de cookies va DENTRO de esta modal, no en la suya.
   El modal de cookies es z-50 y este z-70, así que se abría por detrás y
   parecía que el botón no hacía nada. Y estando aquí no hay que cerrar nada
   para arreglar la sesión: pegas, pulsas y la descarga sigue sola. */
function batchRenderPausedNotice(paused, job) {
    const foot = document.getElementById('batch-foot');
    if (!foot) return;
    let aviso = document.getElementById('batch-paused');

    if (!paused) {
        if (aviso) aviso.remove();
        return;
    }
    if (aviso) return;   // no se repinta: borraría lo que estés escribiendo

    aviso = document.createElement('div');
    aviso.id = 'batch-paused';
    aviso.className = 'batch-paused';

    const texto = document.createElement('p');
    texto.className = 'batch-paused-text';
    // El motivo REAL viaja en job.message, con los bytes que llegó a devolver
    // O'Reilly. Antes se tiraba y se afirmaba "la sesión caducó", que es una
    // suposición: el mismo síntoma lo produce un libro que no está completo en
    // la cuenta, y ahí no hay cookies que arreglen nada.
    const motivo = (job && job.message) ? String(job.message) : '';
    texto.textContent = (motivo ? motivo + ' '
        : (job && job.title
            ? 'La descarga de "' + job.title + '" se detuvo pidiendo cookies. '
            : 'La descarga se detuvo pidiendo cookies. '))
        + 'Lo descargado se conserva y sigue desde donde iba. En una pestaña de '
        + 'learning.oreilly.com abre la consola (F12), pega esto, Enter, y '
        + 'pégalo aquí abajo:';

    // El snippet y su botón de copiar, en una fila propia. Seleccionar a mano
    // una línea larga es justo cómo se acaba pegando un JSON truncado.
    const fila = document.createElement('div');
    fila.className = 'batch-paused-row';

    const ayuda = document.createElement('code');
    ayuda.className = 'batch-paused-snippet';
    // `copy()` las deja en el portapapeles directamente. Lo de antes imprimia
    // un JSON larguisimo en la consola, y copiarlo a mano de la consola es
    // exactamente como acaba llegando truncado.
    ayuda.textContent = 'copy(document.cookie)';

    const copiar = document.createElement('button');
    copiar.type = 'button';
    copiar.className = 'batch-btn is-small batch-paused-copy';
    copiar.textContent = 'Copiar';
    copiar.onclick = async function () {
        try {
            await navigator.clipboard.writeText(ayuda.textContent);
            copiar.textContent = 'Copiado';
        } catch (err) {
            // Sin permiso de portapapeles: al menos se deja seleccionado
            const rango = document.createRange();
            rango.selectNodeContents(ayuda);
            window.getSelection().removeAllRanges();
            window.getSelection().addRange(rango);
            copiar.textContent = 'Selecciona y copia';
        }
        setTimeout(function () { copiar.textContent = 'Copiar'; }, 2000);
    };
    fila.append(ayuda, copiar);

    const campo = document.createElement('textarea');
    campo.className = 'batch-paused-input';
    campo.rows = 3;
    campo.placeholder = 'orm-jwt=...; _abck=...    (o el JSON, las dos valen)';
    campo.setAttribute('aria-label', 'JSON de cookies');

    const error = document.createElement('p');
    error.className = 'batch-paused-error hidden';

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'batch-btn is-primary';
    btn.textContent = 'Actualizar cookies y continuar';
    btn.onclick = async function () {
        error.classList.add('hidden');
        const leido = parseCookieBlob(campo.value);
        if (leido.error) {
            error.textContent = leido.error;
            error.classList.remove('hidden');
            return;
        }
        const cookies = leido.cookies;

        btn.disabled = true;
        btn.textContent = 'Actualizando...';
        try {
            const res = await fetch(API + '/api/cookies', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(cookies),
            });
            const data = await res.json();
            if (data.error) throw new Error(data.error);

            // Guardadas no es lo mismo que válidas. Se pregunta al servidor y se
            // dice la verdad aquí mismo, en vez de cantar éxito y dejar que la
            // descarga vuelva a estrellarse dos segundos después.
            let sesion = null;
            try {
                sesion = await (await fetch(API + '/api/status')).json();
            } catch (e) { /* sin respuesta, se sigue con lo que hay */ }

            if (typeof checkAuth === 'function') checkAuth();

            if (sesion && sesion.valid === false) {
                btn.disabled = false;
                btn.textContent = 'Actualizar cookies y continuar';
                error.textContent = 'Se guardaron, pero el servidor sigue viendo '
                    + 'la sesión como no válida ('
                    + (sesion.reason || 'motivo desconocido') + '). El orm-jwt '
                    + 'que pegaste probablemente ya había caducado: recárgala en '
                    + 'learning.oreilly.com y cópialas otra vez.';
                error.classList.remove('hidden');
                return;
            }

            // El servidor devuelve cuántas descargas ha despausado; el aviso se
            // quita solo en el siguiente sondeo, cuando la cola deje de estarlo.
            btn.textContent = data.resumed
                ? 'Reanudando ' + data.resumed + '...'
                : 'Cookies actualizadas';
            campo.value = '';
            batchTick();
        } catch (err) {
            btn.disabled = false;
            btn.textContent = 'Reintentar';
            error.textContent = 'No se pudieron guardar: ' + err.message;
            error.classList.remove('hidden');
        }
    };

    aviso.append(texto, fila, campo, error, btn);
    foot.insertAdjacentElement('beforebegin', aviso);
    campo.focus();
}

/* --- aviso flotante al cerrar la modal ----------------------------------- */

function batchRenderPill(snap) {
    let pill = document.getElementById('batch-pill');
    const abierta = !!document.getElementById('batch-modal');
    const activos = snap ? snap.active : (pill ? Number(pill.dataset.active || 0) : 0);

    if (abierta || !activos) {
        if (pill) pill.remove();
        return;
    }
    if (!pill) {
        pill = document.createElement('button');
        pill.type = 'button';
        pill.id = 'batch-pill';
        pill.onclick = batchOpenQueueModal;
        document.body.appendChild(pill);
    }
    pill.dataset.active = String(activos);
    pill.innerHTML = '<span class="batch-pill-dot"></span>'
        + activos + (activos === 1 ? ' descarga activa' : ' descargas activas');
    if (!batchState.poll) batchStartPolling();
}

/* --- arranque ------------------------------------------------------------ */

function batchInstallToggle() {
    const bar = document.getElementById('results-bar');
    if (!bar || document.getElementById('batch-toggle')) return;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'batch-toggle';
    btn.className = 'batch-btn';
    btn.textContent = '☰ Múltiples';
    btn.onclick = function () { batchSetMode(!batchState.active); };
    bar.appendChild(btn);
}

document.addEventListener('DOMContentLoaded', function () {
    batchInstallToggle();
    // Si quedaron descargas de una sesión anterior, se ven al entrar.
    batchTick();
});
