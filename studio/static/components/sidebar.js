/* sidebar.js — ouverture et fermeture du panneau galerie.

   Séparé de gallery.js : l'un décide de la visibilité du panneau, l'autre de son
   contenu. Le contenu n'est chargé qu'à la première ouverture, pour ne pas tirer
   la liste des jours au démarrage. */

const Sidebar = (() => {
    "use strict";

    const MOBILE_QUERY = "(max-width: 780px)";
    let panel = null;

    function isOpen() {
        return panel && !panel.classList.contains("hidden");
    }

    function open() {
        panel.classList.remove("hidden");
        if (window.Gallery) Gallery.ensureLoaded();
    }

    function close() {
        panel.classList.add("hidden");
    }

    function toggle() {
        if (isOpen()) close(); else open();
    }

    function init() {
        panel = document.getElementById("gallery-sidebar");
        document.getElementById("gallery-toggle").addEventListener("click", toggle);
        document.getElementById("gallery-close").addEventListener("click", close);

        document.addEventListener("keydown", (event) => {
            // Échap ferme la galerie, mais seulement si la lightbox ne l'a pas déjà
            // consommé : deux couches superposées, une seule doit répondre.
            if (event.key !== "Escape" || !isOpen()) return;
            if (window.Lightbox && Lightbox.isOpen()) return;
            close();
        });

        // En superposition (mobile), un clic hors du panneau le ferme — le geste
        // attendu pour un tiroir. En deux colonnes, le panneau reste ouvert.
        document.addEventListener("click", (event) => {
            if (!isOpen() || !window.matchMedia(MOBILE_QUERY).matches) return;
            if (panel.contains(event.target)) return;
            if (event.target.closest("#gallery-toggle")) return;
            close();
        });
    }

    return { init, open, close, toggle, isOpen };
})();

// Exposition explicite : `const` au niveau d'un script classique ne crée PAS
// de propriété sur `window`, contrairement à `var`. Sans cette ligne, les
// gardes `if (window.Sidebar)` d'app.js sont toutes fausses et le composant n'est
// jamais initialisé — sans la moindre erreur visible.
window.Sidebar = Sidebar;
