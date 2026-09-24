/* Permanent library deletion: visible-result selection, confirmation and progress. */

(function () {
    const state = {
        visible: new Map(),
        selected: new Map(),
        signature: '',
        pending: [],
        deleting: false,
        poll: null,
        refreshed: false
    };

    function key(item) {
        return (item.location || '') + ':' + item.folder;
    }

    function el(id) {
        return document.getElementById(id);
    }

    function syncControls() {
        document.querySelectorAll('.library-item-check').forEach(function (box) {
            const card = box.closest('.book-card');
            box.checked = state.selected.has(
                (card.dataset.location || '') + ':' + card.dataset.folder);
        });

        const all = el('library-select-all');
        const selectedVisible = [...state.visible.keys()].filter(k => state.selected.has(k)).length;
        if (all) {
            all.checked = state.visible.size > 0 && selectedVisible === state.visible.size;
            all.indeterminate = selectedVisible > 0 && selectedVisible < state.visible.size;
            all.disabled = state.visible.size === 0 || state.deleting;
        }

        const actions = el('library-actions-btn');
        if (actions) {
            actions.disabled = state.selected.size === 0 || state.deleting;
            actions.textContent = state.selected.size
                ? 'Acciones (' + state.selected.size + ')'
                : 'Acciones';
        }
    }

    window.libraryDeleteSetVisible = function (items) {
        const next = new Map((items || []).map(item => [key(item), item]));
        const signature = [...next.keys()].sort().join('|');
        if (state.signature && state.signature !== signature) state.selected.clear();
        state.signature = signature;
        state.visible = next;
        for (const selectedKey of [...state.selected.keys()]) {
            if (!next.has(selectedKey)) state.selected.delete(selectedKey);
        }
        syncControls();
    };

    window.libraryDeleteToggle = function (item, checked) {
        if (checked) state.selected.set(key(item), item);
        else state.selected.delete(key(item));
        syncControls();
    };

    function closeActions() {
        const menu = el('library-actions-menu');
        const button = el('library-actions-btn');
        if (menu) menu.classList.add('hidden');
        if (button) button.setAttribute('aria-expanded', 'false');
    }

    function renderConfirmation(items) {
        el('library-delete-message').textContent = items.length === 1
            ? '¿Estás segura de eliminar este elemento de la biblioteca? Esta acción lo eliminará permanentemente y no se puede deshacer.'
            : '¿Estás segura de eliminar estos ' + items.length + ' elementos de la biblioteca? Esta acción los eliminará permanentemente y no se puede deshacer.';

        const preview = el('library-delete-preview');
        preview.innerHTML = '';
        const list = document.createElement('ul');
        items.slice(0, 8).forEach(function (item) {
            const row = document.createElement('li');
            row.textContent = item.title || item.folder;
            list.appendChild(row);
        });
        if (items.length > 8) {
            const row = document.createElement('li');
            row.textContent = 'y ' + (items.length - 8) + ' más…';
            list.appendChild(row);
        }
        preview.appendChild(list);
    }

    window.openLibraryDeleteModal = function (items) {
        if (state.deleting || !items || !items.length) return;
        state.pending = items.slice();
        state.refreshed = false;
        renderConfirmation(state.pending);
        el('library-delete-preview').classList.remove('hidden');
        el('library-delete-progress').classList.add('hidden');
        el('library-delete-current').classList.remove('is-error');
        el('library-delete-errors').classList.add('hidden');
        el('library-delete-errors').textContent = '';
        el('library-delete-cancel').classList.remove('hidden');
        el('library-delete-cancel').disabled = false;
        const confirm = el('library-delete-confirm');
        confirm.disabled = false;
        confirm.textContent = 'Aceptar y eliminar';
        confirm.dataset.mode = 'confirm';
        el('library-delete-modal').classList.remove('hidden');
        confirm.focus();
        closeActions();
    };

    function closeModal() {
        if (state.deleting) return;
        el('library-delete-modal').classList.add('hidden');
        state.pending = [];
    }

    async function beginDelete() {
        if (state.deleting || !state.pending.length) return;
        state.deleting = true;
        syncControls();
        el('library-delete-preview').classList.add('hidden');
        el('library-delete-progress').classList.remove('hidden');
        el('library-delete-current').textContent = 'Preparando eliminación…';
        el('library-delete-counter').textContent = '0 de ' + state.pending.length;
        el('library-delete-bar').style.width = '0%';
        el('library-delete-cancel').disabled = true;
        el('library-delete-confirm').disabled = true;

        try {
            const response = await fetch(API + '/api/library/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    items: state.pending.map(item => ({
                        folder: item.folder,
                        location: item.location
                    }))
                })
            });
            const data = await response.json();
            if (!response.ok || data.error) throw new Error(data.error || 'No se pudo iniciar');
            pollDelete();
        } catch (error) {
            state.deleting = false;
            el('library-delete-current').textContent = error.message;
            el('library-delete-current').classList.add('is-error');
            el('library-delete-cancel').disabled = false;
            el('library-delete-confirm').disabled = false;
            syncControls();
        }
    }

    async function pollDelete() {
        clearTimeout(state.poll);
        try {
            const response = await fetch(API + '/api/library/delete');
            const progress = await response.json();
            if (progress.status === 'deleting') {
                el('library-delete-current').textContent = progress.current
                    ? 'Eliminando: ' + progress.current
                    : 'Eliminando…';
                el('library-delete-counter').textContent =
                    (progress.index || 0) + ' de ' + (progress.total || state.pending.length);
                el('library-delete-bar').style.width = (progress.percentage || 0) + '%';
                state.poll = setTimeout(pollDelete, 250);
                return;
            }
            if (progress.status === 'completed') {
                finishDelete(progress);
                return;
            }
            throw new Error('El servidor no informó el progreso de la eliminación');
        } catch (error) {
            state.deleting = false;
            el('library-delete-current').textContent = error.message;
            el('library-delete-current').classList.add('is-error');
            el('library-delete-cancel').disabled = false;
            syncControls();
        }
    }

    async function finishDelete(progress) {
        state.deleting = false;
        const done = progress.done || [];
        const failed = progress.failed || {};
        el('library-delete-bar').style.width = '100%';
        el('library-delete-counter').textContent = done.length + ' de ' + progress.total;
        el('library-delete-current').textContent = done.length === progress.total
            ? 'Eliminación completada.'
            : 'Se eliminaron ' + done.length + ' de ' + progress.total + ' elementos.';

        const errors = el('library-delete-errors');
        const messages = Object.entries(failed).map(function (entry) {
            return entry[0] + ': ' + entry[1];
        });
        if (progress.index_error) messages.push('Índice: ' + progress.index_error);
        if (messages.length) {
            errors.textContent = messages.join('\n');
            errors.classList.remove('hidden');
        }

        done.forEach(function (item) {
            for (const selectedKey of [...state.selected.keys()]) {
                if (selectedKey.endsWith(':' + item.folder)) state.selected.delete(selectedKey);
            }
        });
        state.pending = [];
        el('library-delete-cancel').classList.add('hidden');
        const confirm = el('library-delete-confirm');
        confirm.disabled = false;
        confirm.textContent = 'Cerrar';
        confirm.dataset.mode = 'close';
        syncControls();

        if (!state.refreshed && typeof loadLibrary === 'function') {
            state.refreshed = true;
            await loadLibrary({ refresh: true });
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        const all = el('library-select-all');
        if (all) all.addEventListener('change', function () {
            state.selected.clear();
            if (all.checked) {
                state.visible.forEach(function (item, itemKey) {
                    state.selected.set(itemKey, item);
                });
            }
            syncControls();
        });

        const actions = el('library-actions-btn');
        if (actions) actions.addEventListener('click', function (event) {
            event.stopPropagation();
            if (actions.disabled) return;
            const menu = el('library-actions-menu');
            const opening = menu.classList.contains('hidden');
            menu.classList.toggle('hidden', !opening);
            actions.setAttribute('aria-expanded', String(opening));
        });

        el('library-delete-selected').addEventListener('click', function () {
            openLibraryDeleteModal([...state.selected.values()]);
        });
        el('library-delete-cancel').addEventListener('click', closeModal);
        el('library-delete-backdrop').addEventListener('click', closeModal);
        el('library-delete-confirm').addEventListener('click', function () {
            if (this.dataset.mode === 'close') closeModal();
            else beginDelete();
        });

        document.addEventListener('click', closeActions);
        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                closeActions();
                closeModal();
            }
        });
        syncControls();
    });
}());
