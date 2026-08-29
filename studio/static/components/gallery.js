/* gallery.js — jours pliés, vignettes à la demande, recherche.

   Une SEULE délégation d'événements, posée sur #gallery-sidebar : le contenu est
   réécrit à chaque recherche et à chaque nouvelle image, et des écouteurs attachés
   aux vignettes fuiraient à chaque rendu.

   Pas de miniatures côté serveur : les PNG pleine taille sont affichés en
   `loading="lazy"`, et un jour replié ne charge rien du tout. */

const Gallery = (() => {
    "use strict";

    const state = {
        loaded: false,
        query: "",
        openDays: new Set(),
        entriesByDay: new Map(),
    };

    const body = () => document.getElementById("gallery-body");

    function esc(text) {
        return App.escapeHtml(text);
    }

    async function api(path) {
        const response = await fetch(path, { cache: "no-cache" });
        if (!response.ok) throw new Error(`${path}: ${response.status}`);
        return response.json();
    }

    // ---------- Libellés de date ----------

    function dayLabel(iso) {
        const today = new Date();
        const yesterday = new Date(today.getTime() - 86400000);
        const fmt = (d) => d.toISOString().slice(0, 10);
        if (iso === fmt(today)) return App.t("gallery.today");
        if (iso === fmt(yesterday)) return App.t("gallery.yesterday");
        try {
            return new Date(`${iso}T12:00:00`).toLocaleDateString(App.state.lang, {
                weekday: "long", day: "numeric", month: "long",
            });
        } catch (_) {
            return iso;
        }
    }

    // ---------- Rendu ----------

    function thumbHtml(entry) {
        return `<div class="thumb"
            data-id="${esc(entry.id)}"
            data-url="${esc(entry.url)}"
            data-prompt="${esc(entry.prompt)}"
            data-prompt-source="${esc(entry.prompt_source || "")}"
            data-lang="${esc(entry.lang || "")}"
            data-width="${esc(entry.width || "")}"
            data-height="${esc(entry.height || "")}"
            data-seed="${esc(entry.seed ?? "")}">
            <img loading="lazy" src="${esc(entry.url)}" alt=""></div>`;
    }

    function renderFlat(entries, emptyKey) {
        if (!entries.length) {
            body().innerHTML = `<p class="gallery-empty">${esc(App.t(emptyKey))}</p>`;
            return;
        }
        body().innerHTML = `<div class="day-grid">${entries.map(thumbHtml).join("")}</div>`;
    }

    async function renderDays() {
        let days;
        try {
            days = await api("/api/gallery/dates");
        } catch (err) {
            console.warn("[gallery]", err);
            days = [];
        }
        if (!days.length) {
            body().innerHTML = `<p class="gallery-empty">${esc(App.t("gallery.empty"))}</p>`;
            return;
        }
        // Recharger les jours restés OUVERTS dont le cache a été vidé par
        // `invalidate()` : sans cela, générer une image rendrait le jour déplié
        // entièrement vide, alors qu'il est ouvert.
        await Promise.all(days
            .filter((day) => state.openDays.has(day.date) && !state.entriesByDay.has(day.date))
            .map(async (day) => {
                state.entriesByDay.set(day.date,
                    await api(`/api/gallery?date=${encodeURIComponent(day.date)}`));
            }));

        body().innerHTML = days.map((day) => {
            const open = state.openDays.has(day.date);
            const entries = state.entriesByDay.get(day.date);
            const grid = open && entries
                ? `<div class="day-grid">${entries.map(thumbHtml).join("")}</div>`
                : "";
            return `<section class="day${open ? " open" : ""}" data-date="${esc(day.date)}">
                <button class="day-head">
                    <span class="day-caret">▶</span>
                    <span>${esc(dayLabel(day.date))}</span>
                    <span class="day-count">${day.count}</span>
                </button>${grid}</section>`;
        }).join("");
    }

    async function render() {
        if (state.query.trim()) {
            const found = await api(`/api/gallery/search?q=${encodeURIComponent(state.query)}`);
            renderFlat(found, "gallery.noResults");
            return;
        }
        await renderDays();
    }

    async function toggleDay(date) {
        if (state.openDays.has(date)) {
            state.openDays.delete(date);
        } else {
            state.openDays.add(date);
            // Les vignettes d'un jour ne sont demandées qu'à sa première ouverture.
            if (!state.entriesByDay.has(date)) {
                state.entriesByDay.set(date,
                    await api(`/api/gallery?date=${encodeURIComponent(date)}`));
            }
        }
        await render();
    }

    // ---------- Cycle de vie ----------

    /** Marque le contenu comme périmé. Appelée après une génération : le panneau
     *  peut être fermé, auquel cas rien n'est rechargé avant sa réouverture. */
    function invalidate() {
        state.entriesByDay.clear();
        if (state.loaded && !document.getElementById("gallery-sidebar").classList.contains("hidden")) {
            render();
        } else {
            state.loaded = false;
        }
    }

    function ensureLoaded() {
        if (state.loaded) return;
        state.loaded = true;
        render();
    }

    function refreshLabels() {
        if (state.loaded) render();
    }

    function init() {
        const panel = document.getElementById("gallery-sidebar");

        panel.addEventListener("click", (event) => {
            const head = event.target.closest(".day-head");
            if (head) {
                toggleDay(head.closest(".day").dataset.date);
                return;
            }
            const thumb = event.target.closest(".thumb");
            if (thumb && window.Lightbox) {
                Lightbox.open(thumb.dataset);
                return;
            }
        });

        let timer = null;
        document.getElementById("gallery-q").addEventListener("input", (event) => {
            state.query = event.target.value;
            // La recherche est locale et instantanée côté serveur, mais réécrire la
            // liste à chaque frappe fait clignoter les vignettes.
            clearTimeout(timer);
            timer = setTimeout(render, 180);
        });
    }

    return { init, render, invalidate, ensureLoaded, refreshLabels };
})();

// Exposition explicite : `const` au niveau d'un script classique ne crée PAS
// de propriété sur `window`, contrairement à `var`. Sans cette ligne, les
// gardes `if (window.Gallery)` d'app.js sont toutes fausses et le composant n'est
// jamais initialisé — sans la moindre erreur visible.
window.Gallery = Gallery;
