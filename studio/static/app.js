/* app.js — WebSocket, i18n, composition et suivi des générations.

   `init()` est écrit d'un bloc et n'appelle que des initialisateurs qui existent :
   dans le studio d'origine, un appel sans garde à un module absent faisait lever
   init() en entier — WebSocket non connecté, galerie vide, aucune erreur visible
   hors console. Chaque composant optionnel est donc testé avant d'être appelé. */

const App = (() => {
    "use strict";

    const state = {
        config: null,
        lang: "en",
        strings: {},
        fallback: {},      // en.json : filet pour toute clé absente d'une locale
        ws: null,
        wsTimer: null,
        conversations: new Set(),
        lot: 1,
    };

    // ---------- i18n ----------

    function t(key) {
        return state.strings[key] ?? state.fallback[key] ?? key;
    }

    async function loadLocale(code) {
        try {
            const response = await fetch(`/static/i18n/${code}.json`, { cache: "no-cache" });
            if (!response.ok) throw new Error(response.status);
            return await response.json();
        } catch (err) {
            console.warn(`[i18n] locale ${code} indisponible`, err);
            return null;
        }
    }

    function applyI18n() {
        document.querySelectorAll("[data-i18n]").forEach((el) => {
            el.textContent = t(el.dataset.i18n);
        });
        // `data-i18n-attr="placeholder:cle"`, plusieurs paires séparées par des virgules.
        document.querySelectorAll("[data-i18n-attr]").forEach((el) => {
            el.dataset.i18nAttr.split(",").forEach((pair) => {
                const [attr, key] = pair.split(":").map((s) => s.trim());
                if (attr && key) el.setAttribute(attr, t(key));
            });
        });
        document.title = t("app.title");
    }

    async function setLanguage(code, persist = true) {
        const strings = await loadLocale(code);
        if (strings) {
            state.lang = code;
            state.strings = strings;
        }
        if (persist) {
            try { localStorage.setItem("lang", state.lang); } catch (_) { /* mode privé */ }
        }
        const rtl = (state.config?.rtl_languages || []).includes(state.lang);
        document.documentElement.lang = state.lang;
        document.documentElement.dir = rtl ? "rtl" : "ltr";
        applyI18n();
        updateWsDot();
        if (window.Gallery) Gallery.refreshLabels();
    }

    function initialLanguage(available) {
        let saved = null;
        try { saved = localStorage.getItem("lang"); } catch (_) { /* mode privé */ }
        if (saved && available.includes(saved)) return saved;
        const navLang = (navigator.language || "en").slice(0, 2).toLowerCase();
        return available.includes(navLang) ? navLang : "en";
    }

    function initLangSelect() {
        const select = document.getElementById("lang-select");
        const languages = state.config?.languages || { en: "English" };
        select.innerHTML = Object.entries(languages)
            .map(([code, name]) => `<option value="${code}">${escapeHtml(name)}</option>`)
            .join("");
        select.value = state.lang;
        select.addEventListener("change", () => setLanguage(select.value));
    }

    // ---------- WebSocket ----------

    function updateWsDot() {
        const dot = document.getElementById("ws-dot");
        const online = state.ws && state.ws.readyState === WebSocket.OPEN;
        dot.classList.toggle("online", !!online);
        dot.title = t(online ? "header.connected" : "header.disconnected");
    }

    function connectWS() {
        const scheme = location.protocol === "https:" ? "wss" : "ws";
        const socket = new WebSocket(`${scheme}://${location.host}/ws`);
        state.ws = socket;

        socket.addEventListener("open", updateWsDot);
        socket.addEventListener("message", (event) => {
            let data;
            try { data = JSON.parse(event.data); } catch (_) { return; }
            handleEvent(data);
        });
        // Reconnexion à 3 s : le serveur redémarre plus souvent qu'on ne recharge
        // la page, et une interface muette après un restart n'aide personne.
        const retry = () => {
            updateWsDot();
            clearTimeout(state.wsTimer);
            state.wsTimer = setTimeout(connectWS, 3000);
        };
        socket.addEventListener("close", retry);
        socket.addEventListener("error", () => socket.close());
    }

    function handleEvent(event) {
        const conv = event.conversation_id;
        // Un second onglet reçoit tout : on ne traite que nos propres conversations.
        if (conv && !state.conversations.has(conv)) return;

        switch (event.type) {
            case "translated":
                addTranslated(conv, event.translated);
                break;
            case "progress":
                updatePlaceholderProgress(conv, event.item_index, event.value);
                break;
            case "tool_result":
                if (event.media && event.media.length) {
                    showMedia(conv, event.item_index, event.media[0]);
                } else {
                    failPlaceholder(conv, event.item_index, event.text || t("status.error"));
                }
                break;
            case "cancelled":
                failPlaceholder(conv, event.item_index, t("status.cancelled"));
                break;
            case "error":
                addError(event.message || t("status.error"));
                break;
            case "done":
                state.conversations.delete(conv);
                break;
            default:
                break;
        }
    }

    // ---------- Messages ----------

    function escapeHtml(text) {
        return String(text ?? "").replace(/[&<>"']/g, (c) => (
            { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
        ));
    }

    function messagesEl() {
        return document.getElementById("messages");
    }

    function scrollToEnd() {
        const box = messagesEl();
        box.scrollTop = box.scrollHeight;
    }

    function hideEmptyState() {
        document.getElementById("empty-state")?.classList.add("hidden");
    }

    /** Un envoi = un bloc, qui contiendra le prompt, la traduction et les images.
     *  Le bloc est créé avant l'appel réseau (pour que le prompt s'affiche tout de
     *  suite) et reçoit son `conversation_id` dès que le serveur le donne. */
    function openBlock(text) {
        hideEmptyState();
        const block = document.createElement("div");
        block.className = "msg-block";
        const user = document.createElement("div");
        user.className = "msg-user";
        user.textContent = text;
        block.appendChild(user);
        messagesEl().appendChild(block);
        scrollToEnd();
        return block;
    }

    function blockOf(conversationId) {
        return document.querySelector(`.msg-block[data-conv-id="${conversationId}"]`);
    }

    function addTranslated(conversationId, translated) {
        const block = blockOf(conversationId);
        if (!block) return;
        const div = document.createElement("div");
        div.className = "msg-translated";
        div.innerHTML = `<span class="tr-key">${escapeHtml(t("translated.label"))} · </span>`
            + escapeHtml(translated);
        // Insérée AVANT les images : l'événement `translated` arrive par le
        // WebSocket, souvent après que la réponse HTTP a déjà fait poser les
        // carrés de chargement. Sans point d'ancrage, la traduction s'afficherait
        // sous les images qu'elle a servi à produire.
        block.insertBefore(div, block.querySelector(".msg-media"));
        scrollToEnd();
    }

    function addError(message, block = null) {
        const div = document.createElement("div");
        div.className = "msg-error";
        div.textContent = message;
        (block || messagesEl()).appendChild(div);
        scrollToEnd();
    }

    // ---------- Placeholders ----------

    function createPlaceholders(block, conversationId, count, width, height) {
        const wrap = document.createElement("div");
        wrap.className = "msg-media";
        // Une image occupe toute la cellule, deux et plus se rangent sur deux
        // colonnes : au-delà, les vignettes deviendraient trop petites pour juger
        // du résultat, ce qui est pourtant tout l'intérêt d'un lot.
        wrap.style.setProperty("--cols", count === 1 ? "1" : "2");
        for (let i = 0; i < count; i += 1) {
            const cell = document.createElement("div");
            cell.className = "msg-media-placeholder";
            cell.dataset.convId = conversationId;
            cell.dataset.itemIndex = String(i);
            cell.style.setProperty("--progress", "0");
            cell.style.setProperty("--ratio", `${width} / ${height}`);
            cell.innerHTML = `<span class="ph-pct">${escapeHtml(t("status.waiting"))}</span>`
                + `<button class="ph-cancel" title="${escapeHtml(t("status.cancel"))}">✕</button>`;
            wrap.appendChild(cell);
        }
        block.appendChild(wrap);
        scrollToEnd();
    }

    function findPlaceholder(conversationId, index) {
        return document.querySelector(
            `.msg-media-placeholder[data-conv-id="${conversationId}"][data-item-index="${index}"]`);
    }

    function updatePlaceholderProgress(conversationId, index, value) {
        const cell = findPlaceholder(conversationId, index);
        if (!cell) return;
        const pct = Math.max(0, Math.min(1, Number(value) || 0));
        cell.style.setProperty("--progress", String(pct));
        const label = cell.querySelector(".ph-pct");
        if (label) label.textContent = `${Math.round(pct * 100)} %`;
    }

    function failPlaceholder(conversationId, index, message) {
        const cell = findPlaceholder(conversationId, index);
        if (!cell) return;
        cell.style.setProperty("--progress", "0");
        cell.innerHTML = `<span class="ph-pct">${escapeHtml(message)}</span>`;
    }

    function showMedia(conversationId, index, media) {
        const cell = findPlaceholder(conversationId, index);
        const item = buildMediaContainer(media);
        if (cell) {
            cell.replaceWith(item);
        } else {
            const wrap = document.createElement("div");
            wrap.className = "msg-media";
            wrap.appendChild(item);
            (blockOf(conversationId) || messagesEl()).appendChild(wrap);
        }
        scrollToEnd();
        if (window.Gallery) Gallery.invalidate();
    }

    function buildMediaContainer(media) {
        const div = document.createElement("div");
        div.className = "msg-media-item";
        div.style.setProperty("--ratio", `${media.width || 1} / ${media.height || 1}`);
        const img = document.createElement("img");
        img.loading = "lazy";
        img.src = media.url;
        img.alt = media.prompt || "";
        // Toutes les métadonnées voyagent sur l'élément : la lightbox n'a alors
        // besoin d'aucun appel serveur pour s'ouvrir.
        Object.assign(div.dataset, {
            id: media.entry_id || "",
            url: media.url,
            prompt: media.prompt || "",
            promptSource: media.prompt_source || "",
            lang: media.lang || "",
            width: media.width || "",
            height: media.height || "",
            seed: media.seed ?? "",
        });
        div.appendChild(img);
        return div;
    }

    async function cancelGeneration(conversationId, index) {
        const body = new FormData();
        body.append("conversation_id", conversationId);
        body.append("item_index", String(index));
        try {
            await fetch("/api/generate/cancel", { method: "POST", body });
        } catch (err) {
            console.warn("annulation impossible", err);
        }
    }

    // ---------- Options ----------

    function snapSide(value) {
        const cfg = state.config || {};
        const step = cfg.side_step || 16;
        const min = cfg.min_side || 256;
        const max = cfg.max_side || 2048;
        let side = parseInt(value, 10);
        if (!Number.isFinite(side)) side = cfg.default_width || 1024;
        side = Math.max(min, Math.min(max, side));
        return Math.max(min, Math.min(max, Math.round(side / step) * step));
    }

    function initDimensions() {
        ["opt-width", "opt-height"].forEach((id) => {
            const input = document.getElementById(id);
            input.value = id === "opt-width"
                ? (state.config?.default_width || 1024)
                : (state.config?.default_height || 1024);
            // Resnap au blur, pas à la frappe : arrondir pendant la saisie
            // empêcherait purement et simplement de taper « 1536 ».
            input.addEventListener("blur", () => { input.value = snapSide(input.value); });
        });
    }

    function initLot() {
        const group = document.getElementById("lot-group");
        const max = state.config?.max_lot || 4;
        group.innerHTML = "";
        for (let n = 1; n <= max; n += 1) {
            const button = document.createElement("button");
            button.className = "lot-btn" + (n === state.lot ? " active" : "");
            button.textContent = String(n);
            button.dataset.lot = String(n);
            group.appendChild(button);
        }
        group.addEventListener("click", (event) => {
            const button = event.target.closest(".lot-btn");
            if (!button) return;
            state.lot = Number(button.dataset.lot);
            group.querySelectorAll(".lot-btn").forEach((b) => {
                b.classList.toggle("active", b === button);
            });
        });
    }

    /** Restaure prompt, langue et dimensions. Le seed n'en fait pas partie : il
     *  est tiré à chaque génération, et « réutiliser ces réglages » sert à refaire
     *  une variation, pas à recréer la même image. */
    function applySettings({ prompt, lang, width, height }) {
        const field = document.getElementById("prompt");
        field.value = prompt || "";
        autoGrowTextarea(field);
        if (lang && state.config?.languages?.[lang]) {
            document.getElementById("lang-select").value = lang;
            setLanguage(lang);
        }
        if (width) document.getElementById("opt-width").value = snapSide(width);
        if (height) document.getElementById("opt-height").value = snapSide(height);
        field.focus();
    }

    function autoGrowTextarea(el) {
        el.style.height = "auto";
        el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
    }

    // ---------- Envoi ----------

    async function send() {
        const field = document.getElementById("prompt");
        const prompt = field.value.trim();
        if (!prompt) return;

        const width = snapSide(document.getElementById("opt-width").value);
        const height = snapSide(document.getElementById("opt-height").value);
        document.getElementById("opt-width").value = width;
        document.getElementById("opt-height").value = height;
        const lot = state.lot;

        const body = new FormData();
        body.append("prompt", prompt);
        body.append("lang", state.lang);
        body.append("width", String(width));
        body.append("height", String(height));
        body.append("lot", String(lot));
        // `seed` n'est pas envoyé : le serveur en tire un et le renvoie avec
        // l'image. L'API et le MCP acceptent toujours le paramètre.

        const block = openBlock(prompt);
        field.value = "";
        autoGrowTextarea(field);

        let data;
        try {
            const response = await fetch("/api/generate", { method: "POST", body });
            data = await response.json();
            if (!response.ok) throw new Error(data.detail || response.status);
        } catch (err) {
            addError(String(err.message || err), block);
            return;
        }
        block.dataset.convId = data.conversation_id;
        state.conversations.add(data.conversation_id);
        createPlaceholders(block, data.conversation_id, lot, width, height);
    }

    // ---------- Démarrage ----------

    async function init() {
        try {
            state.config = await (await fetch("/api/config")).json();
        } catch (err) {
            console.error("[config] indisponible", err);
            state.config = { languages: { en: "English" }, rtl_languages: [] };
        }

        state.fallback = (await loadLocale("en")) || {};
        const available = Object.keys(state.config.languages || { en: 1 });
        await setLanguage(initialLanguage(available), false);
        initLangSelect();

        initDimensions();
        initLot();

        if (state.config.translation_available === false) {
            document.getElementById("no-translation").classList.remove("hidden");
        }

        const field = document.getElementById("prompt");
        field.addEventListener("input", () => autoGrowTextarea(field));
        field.addEventListener("keydown", (event) => {
            // Entrée envoie, Maj+Entrée passe à la ligne : c'est la convention des
            // zones de saisie de chat, et le prompt est souvent multiligne.
            if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                send();
            }
        });
        document.getElementById("send").addEventListener("click", send);

        // Une seule délégation pour tout le fil : les cellules sont créées et
        // remplacées en permanence, y attacher des écouteurs les ferait fuir.
        messagesEl().addEventListener("click", (event) => {
            const cancel = event.target.closest(".ph-cancel");
            if (cancel) {
                const cell = cancel.closest(".msg-media-placeholder");
                cancelGeneration(cell.dataset.convId, Number(cell.dataset.itemIndex));
                return;
            }
            const item = event.target.closest(".msg-media-item");
            if (item && window.Lightbox) Lightbox.open(item.dataset);
        });

        if (window.Sidebar) Sidebar.init();
        if (window.Gallery) Gallery.init();
        if (window.Lightbox) Lightbox.init();

        connectWS();
    }

    document.addEventListener("DOMContentLoaded", init);

    return { t, escapeHtml, applySettings, setLanguage, state };
})();

// Exposition explicite : `const` au niveau d'un script classique ne crée PAS
// de propriété sur `window`, contrairement à `var`. Sans cette ligne, les
// gardes `if (window.App)` d'app.js sont toutes fausses et le composant n'est
// jamais initialisé — sans la moindre erreur visible.
window.App = App;
