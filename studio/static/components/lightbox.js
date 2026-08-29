/* lightbox.js — image en grand, métadonnées, quatre actions.

   Toutes les données viennent du `dataset` de la vignette cliquée : ouvrir la
   lightbox ne demande donc rien au serveur. Les dimensions affichées, elles, sont
   lues sur l'image réellement chargée (`naturalWidth`), pas sur la valeur
   enregistrée — c'est ce qui est vrai à l'écran.

   Le seed est enregistré avec chaque image mais n'est pas montré ici : il ne sert
   qu'à qui reproduit une génération par l'API ou le MCP. */

const Lightbox = (() => {
    "use strict";

    let root = null;
    let current = null;          // dataset de l'image ouverte
    let deleteArmed = false;     // suppression en deux temps

    const $ = (id) => document.getElementById(id);

    function isOpen() {
        return root && !root.classList.contains("hidden");
    }

    function filenameFor(data) {
        const base = (data.promptSource || data.prompt || "image")
            .toLowerCase()
            .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
            .replace(/[^a-z0-9]+/g, "-")
            .replace(/^-+|-+$/g, "")
            .slice(0, 60) || "image";
        return `${base}.png`;
    }

    function renderMeta(data) {
        $("meta-prompt").textContent = data.prompt || "";
        const hasSource = !!(data.promptSource && data.promptSource !== data.prompt);
        $("meta-source-line").classList.toggle("hidden", !hasSource);
        $("meta-source").textContent = data.promptSource || "";
        $("meta-lang").textContent = App.state.config?.languages?.[data.lang] || data.lang || "—";

        const img = $("lightbox-img");
        const setSize = () => {
            const w = img.naturalWidth || data.width || "?";
            const h = img.naturalHeight || data.height || "?";
            $("meta-size").textContent = `${w} × ${h}`;
        };
        img.complete ? setSize() : img.addEventListener("load", setSize, { once: true });
    }

    function resetDelete() {
        deleteArmed = false;
        const button = $("lb-delete");
        button.classList.remove("confirming");
        button.textContent = App.t("action.delete");
    }

    function open(data) {
        current = { ...data };
        $("lightbox-img").src = data.url;
        $("lightbox-img").alt = data.prompt || "";
        $("lb-download").href = data.url;
        // Téléchargement en same-origin : `<a download>` suffit, aucune route
        // serveur n'est nécessaire. Le nom lisible est construit ici.
        $("lb-download").setAttribute("download", filenameFor(data));
        renderMeta(data);
        resetDelete();
        root.classList.remove("hidden");
    }

    function close() {
        root.classList.add("hidden");
        $("lightbox-img").src = "";
        current = null;
        resetDelete();
    }

    async function remove() {
        if (!current?.id) return;
        // Deux temps : le premier clic arme, le second supprime. Une boîte de
        // dialogue masquerait justement l'image dont on décide du sort.
        if (!deleteArmed) {
            deleteArmed = true;
            const button = $("lb-delete");
            button.classList.add("confirming");
            button.textContent = App.t("action.confirmDelete");
            setTimeout(() => { if (deleteArmed) resetDelete(); }, 4000);
            return;
        }
        await fetch(`/api/gallery/${current.id}`, { method: "DELETE" });
        document.querySelectorAll(`[data-id="${current.id}"]`).forEach((el) => {
            el.closest(".thumb, .msg-media-item")?.remove();
        });
        close();
        if (window.Gallery) Gallery.invalidate();
    }

    function init() {
        root = document.getElementById("lightbox");

        root.addEventListener("click", (event) => {
            if (event.target.closest("[data-close]")) { close(); return; }
            if (event.target.closest("#lb-delete")) { remove(); return; }
            if (event.target.closest("#lb-reuse")) {
                App.applySettings({
                    prompt: current.promptSource || current.prompt,
                    lang: current.lang,
                    width: current.width,
                    height: current.height,
                });
                close();
            }
        });

        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && isOpen()) close();
        });
    }

    return { init, open, close, isOpen };
})();

// Exposition explicite : `const` au niveau d'un script classique ne crée PAS
// de propriété sur `window`, contrairement à `var`. Sans cette ligne, les
// gardes `if (window.Lightbox)` d'app.js sont toutes fausses et le composant n'est
// jamais initialisé — sans la moindre erreur visible.
window.Lightbox = Lightbox;
