/* ===== Transferir a CWA (Calibre-Web Automated) =====

   Cuatro estados, y son distintos a proposito:

     sin ruta   -> se pide, porque sin destino no hay nada que listar
     no escribe -> se dice QUE falla, que es lo unico accionable
     lista      -> los EPUB sin repetidos, con seleccionar todo
     enviando   -> progreso, y el aviso de que la ingesta la hace CWA

   Va en su propio archivo y no en app.js, que ya es lo bastante grande. */

let cwaItems = [];
let cwaPoll = null;

function cwaFmtSize(bytes) {
    const mb = (bytes || 0) / (1024 * 1024);
    return mb >= 1 ? mb.toFixed(1) + ' MB' : Math.round((bytes || 0) / 1024) + ' KB';
}

function cwaEl(id) {
    return document.getElementById(id);
}

function cwaSetAction(text, onclick, hidden) {
    const btn = cwaEl('cwa-modal-action');
    if (!btn) return;
    btn.textContent = text || '';
    btn.onclick = onclick || null;
    btn.classList.toggle('hidden', !!hidden);
    btn.disabled = false;
}

/* --- estado 1: no hay ruta ------------------------------------------- */
function cwaRenderSetup(status) {
    cwaEl('cwa-modal-sub').textContent = status.configured
        ? 'Cambia la carpeta de ingesta de CWA.'
        : 'No hay ningún CWA configurado.';

    const body = cwaEl('cwa-modal-body');
    body.innerHTML =
        '<label class="block text-sm font-medium text-zinc-700">'
      +   'Carpeta de ingesta de CWA'
      + '</label>'
      + '<input id="cwa-path-input" type="text" class="mt-1 w-full px-3 py-2 border '
      +   'border-zinc-200 rounded-lg text-sm" '
      +   'placeholder="\\\\192.168.100.2\\cwa-ingest">'
      + '<p class="mt-2 text-xs text-zinc-500">'
      +   'Una ruta de carpeta: un recurso de red o una unidad mapeada. La '
      +   'dirección web de CWA sirve para abrirlo en el navegador, no para '
      +   'dejarle archivos.'
      + '</p>'
      + '<p id="cwa-path-error" class="mt-2 text-xs text-red-600 hidden"></p>';

    const input = cwaEl('cwa-path-input');
    input.value = status.path || '';
    input.focus();

    const guardar = async function () {
        const error = cwaEl('cwa-path-error');
        error.classList.add('hidden');
        const btn = cwaEl('cwa-modal-action');
        btn.disabled = true;
        btn.textContent = 'Comprobando…';
        try {
            const res = await fetch(`${API}/api/cwa/path`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: input.value }),
            });
            const data = await res.json();
            if (!data.ok) {
                error.textContent = data.reason || 'No se pudo usar esa ruta.';
                error.classList.remove('hidden');
                cwaSetAction('Comprobar', guardar, false);
                return;
            }
            cwaLoad();
        } catch (err) {
            error.textContent = 'No se pudo contactar con el servidor.';
            error.classList.remove('hidden');
            cwaSetAction('Comprobar', guardar, false);
        }
    };

    cwaSetAction('Comprobar', guardar, false);
    input.onkeydown = function (e) { if (e.key === 'Enter') guardar(); };
}

/* --- estado 2: hay ruta pero no se puede escribir -------------------- */
function cwaRenderBroken(status) {
    cwaEl('cwa-modal-sub').textContent = 'No se puede escribir en la carpeta de CWA.';

    const body = cwaEl('cwa-modal-body');
    body.innerHTML =
        '<div class="p-3 rounded-lg bg-amber-50 border border-amber-200">'
      +   '<p class="text-sm font-medium text-amber-900 break-all" id="cwa-broken-path"></p>'
      +   '<p class="text-xs text-amber-800 mt-1" id="cwa-broken-reason"></p>'
      + '</div>';
    cwaEl('cwa-broken-path').textContent = status.path || '';
    cwaEl('cwa-broken-reason').textContent = status.reason || '';

    cwaSetAction('Cambiar ruta', function () { cwaRenderSetup(status); }, false);
}

/* --- estado 3: la lista ---------------------------------------------- */
function cwaRenderList(data) {
    cwaItems = data.items || [];

    const partes = [`${data.unique} EPUB`];
    if (data.duplicates) partes.push(`${data.duplicates} duplicado(s) descartado(s)`);
    if (data.already_sent) partes.push(`${data.already_sent} ya enviado(s)`);
    cwaEl('cwa-modal-sub').textContent = partes.join(' · ');

    const body = cwaEl('cwa-modal-body');
    if (!cwaItems.length) {
        body.innerHTML = '<p class="text-sm text-zinc-500">No hay ningún EPUB en '
            + 'la biblioteca ni en los bundles.</p>';
        cwaSetAction('', null, true);
        return;
    }

    body.innerHTML =
        '<p class="text-xs text-zinc-400 break-all mb-3" id="cwa-dest"></p>'
      + '<label class="flex items-center gap-2 pb-2 border-b border-zinc-100">'
      +   '<input type="checkbox" id="cwa-all" class="w-4 h-4 rounded border-zinc-300 '
      +     'text-oreilly-blue">'
      +   '<span class="text-sm font-medium text-zinc-700">Seleccionar todo</span>'
      +   '<span class="ml-auto text-xs text-zinc-500" id="cwa-selected"></span>'
      + '</label>'
      + '<div id="cwa-list" class="max-h-80 overflow-y-auto divide-y divide-zinc-100"></div>';

    cwaEl('cwa-dest').textContent = 'Destino: ' + ((data.status || {}).path || '');

    const lista = cwaEl('cwa-list');
    cwaItems.forEach(function (item, i) {
        const fila = document.createElement('label');
        fila.className = 'flex items-start gap-2 py-2 cursor-pointer';
        fila.innerHTML =
            '<input type="checkbox" class="cwa-item w-4 h-4 mt-0.5 rounded '
          +   'border-zinc-300 text-oreilly-blue">'
          + '<span class="min-w-0 flex-1">'
          +   '<span class="block text-sm text-zinc-700 truncate cwa-t"></span>'
          +   '<span class="block text-xs text-zinc-400 cwa-m"></span>'
          + '</span>'
          + '<span class="text-xs text-zinc-400 whitespace-nowrap cwa-s"></span>';

        const box = fila.querySelector('.cwa-item');
        box.dataset.index = String(i);
        // Lo ya enviado se ve pero no se marca: asi compruebas que no falta
        // nada, sin reenviarlo sin querer.
        box.checked = !item.sent;
        box.onchange = cwaRefreshCount;

        fila.querySelector('.cwa-t').textContent = item.title || '(sin título)';

        const meta = [];
        if ((item.authors || []).length) meta.push(item.authors[0]);
        if (item.language) meta.push(item.language.toUpperCase());
        meta.push(item.source === 'library' ? 'biblioteca' : 'bundle');
        if ((item.duplicates || []).length) {
            meta.push(`también en ${item.duplicates.length} sitio(s) más`);
        }
        if (item.sent) meta.push('ya enviado');
        fila.querySelector('.cwa-m').textContent = meta.join(' · ');
        // El nombre con el que llegara a CWA, que no es `book.epub`.
        fila.querySelector('.cwa-m').title = 'Se enviará como: ' + (item.filename || '');

        fila.querySelector('.cwa-s').textContent = cwaFmtSize(item.size);
        lista.appendChild(fila);
    });

    cwaEl('cwa-all').onchange = function () {
        const marcar = cwaEl('cwa-all').checked;
        document.querySelectorAll('#cwa-list .cwa-item').forEach(function (b) {
            b.checked = marcar;
        });
        cwaRefreshCount();
    };

    cwaRefreshCount();
}

function cwaSelectedKeys() {
    const keys = [];
    document.querySelectorAll('#cwa-list .cwa-item').forEach(function (b) {
        if (b.checked) {
            const item = cwaItems[parseInt(b.dataset.index, 10)];
            if (item) keys.push(item.key);
        }
    });
    return keys;
}

function cwaRefreshCount() {
    const keys = cwaSelectedKeys();
    const etiqueta = cwaEl('cwa-selected');
    if (etiqueta) etiqueta.textContent = `${keys.length} seleccionado(s)`;

    const todas = document.querySelectorAll('#cwa-list .cwa-item');
    const all = cwaEl('cwa-all');
    if (all) all.checked = todas.length > 0 && keys.length === todas.length;

    cwaSetAction(`Enviar ${keys.length}`, cwaStartTransfer, false);
    const btn = cwaEl('cwa-modal-action');
    if (btn) btn.disabled = keys.length === 0;
}

/* --- estado 4: enviando ---------------------------------------------- */
async function cwaStartTransfer() {
    const keys = cwaSelectedKeys();
    if (!keys.length) return;

    const btn = cwaEl('cwa-modal-action');
    btn.disabled = true;
    btn.textContent = 'Enviando…';

    try {
        const res = await fetch(`${API}/api/cwa/transfer`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ keys: keys }),
        });
        const data = await res.json();
        if (data.error) {
            cwaEl('cwa-modal-sub').textContent = data.error;
            cwaRefreshCount();
            return;
        }
    } catch (err) {
        cwaEl('cwa-modal-sub').textContent = 'No se pudo iniciar la transferencia.';
        cwaRefreshCount();
        return;
    }

    cwaRenderProgress();
}

function cwaRenderProgress() {
    cwaEl('cwa-modal-sub').textContent = 'Copiando a la carpeta de CWA…';
    cwaEl('cwa-modal-body').innerHTML =
        '<div class="flex items-baseline justify-between gap-3">'
      +   '<span class="text-sm text-zinc-600" id="cwa-prog-current"></span>'
      +   '<span class="text-xs font-medium text-zinc-500" id="cwa-prog-count"></span>'
      + '</div>'
      + '<div class="h-2 bg-zinc-100 rounded-full overflow-hidden mt-2">'
      +   '<div id="cwa-prog-bar" class="h-full bg-oreilly-blue rounded-full '
      +     'transition-all duration-300" style="width:0%"></div>'
      + '</div>'
      + '<p class="text-xs text-zinc-500 mt-3">CWA los recoge de la carpeta por su '
      +   'cuenta, así que pueden tardar unos minutos en aparecer en su biblioteca.</p>'
      + '<div id="cwa-prog-failed" class="mt-3 space-y-1"></div>';

    cwaSetAction('', null, true);
    if (cwaPoll) clearInterval(cwaPoll);
    cwaPoll = setInterval(cwaTickProgress, 700);
    cwaTickProgress();
}

async function cwaTickProgress() {
    let p;
    try {
        p = await (await fetch(`${API}/api/cwa/transfer`)).json();
    } catch (err) {
        return;   // un fallo de red no rompe la ventana, se reintenta solo
    }

    const total = p.total || 0;
    const done = p.done || 0;
    const pct = total ? Math.round((done / total) * 100) : 0;

    const bar = cwaEl('cwa-prog-bar');
    if (bar) bar.style.width = pct + '%';
    const cuenta = cwaEl('cwa-prog-count');
    if (cuenta) cuenta.textContent = `${done} / ${total}`;
    const actual = cwaEl('cwa-prog-current');
    if (actual) actual.textContent = p.current || (p.finished ? 'Terminado' : '');

    const fallidos = cwaEl('cwa-prog-failed');
    if (fallidos && (p.failed || []).length) {
        fallidos.innerHTML = '';
        p.failed.forEach(function (f) {
            const linea = document.createElement('p');
            linea.className = 'text-xs text-red-600';
            linea.textContent = `${f.title}: ${f.reason}`;
            fallidos.appendChild(linea);
        });
    }

    if (!p.finished) return;

    clearInterval(cwaPoll);
    cwaPoll = null;
    const fallos = (p.failed || []).length;
    cwaEl('cwa-modal-sub').textContent = fallos
        ? `${(p.ok || []).length} enviados, ${fallos} con error.`
        : `${(p.ok || []).length} enviados a CWA.`;
    cwaSetAction('Ver la lista', cwaLoad, false);
}

/* --- carga y apertura ------------------------------------------------ */
async function cwaLoad() {
    cwaEl('cwa-modal-sub').textContent = 'Buscando EPUB…';
    cwaEl('cwa-modal-body').innerHTML =
        '<p class="text-sm text-zinc-400">Leyendo la biblioteca y los bundles…</p>';
    cwaSetAction('', null, true);

    let data;
    try {
        data = await (await fetch(`${API}/api/cwa/inventory`)).json();
    } catch (err) {
        cwaEl('cwa-modal-sub').textContent = 'No se pudo contactar con el servidor.';
        cwaEl('cwa-modal-body').innerHTML = '';
        return;
    }

    const status = data.status || {};
    if (!status.configured) {
        cwaRenderSetup(status);
    } else if (!status.ok) {
        cwaRenderBroken(status);
    } else if (data.error) {
        cwaEl('cwa-modal-sub').textContent = data.error;
    } else {
        cwaRenderList(data);
    }
}

function openCwaModal() {
    const modal = cwaEl('cwa-modal');
    if (!modal) return;
    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
    cwaLoad();
}

function closeCwaModal() {
    const modal = cwaEl('cwa-modal');
    if (modal) modal.classList.add('hidden');
    document.body.style.overflow = '';
    if (cwaPoll) clearInterval(cwaPoll);
    cwaPoll = null;
    // Cerrar NO cancela la copia: sigue en el servidor, como la cola.
    if (typeof loadLibrary === 'function') loadLibrary();
}

(function wireCwa() {
    function attach() {
        const abrir = cwaEl('cwa-open');
        if (abrir) abrir.addEventListener('click', openCwaModal);
        const cerrar = cwaEl('cwa-modal-close');
        if (cerrar) cerrar.addEventListener('click', closeCwaModal);
        const fondo = cwaEl('cwa-modal-backdrop');
        if (fondo) fondo.addEventListener('click', closeCwaModal);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', attach);
    } else {
        attach();
    }
})();
