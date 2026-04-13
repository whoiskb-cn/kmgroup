(function () {
    const KEY = "km_theme";
    const ZOOM_KEY = "km_global_zoom";
    const root = document.documentElement;

    function normalize(theme) {
        return theme === "day" ? "day" : "dark";
    }

    function normalizeZoom(zoom) {
        const parsed = parseFloat(zoom);
        if (!Number.isFinite(parsed)) return 1;
        return Math.min(1.5, Math.max(0.8, parsed));
    }

    function applyTheme(theme) {
        const normalized = normalize(theme);
        root.setAttribute("data-km-theme", normalized);
        localStorage.setItem(KEY, normalized);
        window.dispatchEvent(new CustomEvent("km-theme-change", { detail: { theme: normalized } }));
    }

    function applyZoom(zoom) {
        const normalized = normalizeZoom(zoom);
        root.style.setProperty("--km-global-zoom", String(normalized));
        root.style.fontSize = (16 * normalized) + "px";
        localStorage.setItem(ZOOM_KEY, String(normalized));
        window.dispatchEvent(new CustomEvent("km-zoom-change", { detail: { zoom: normalized } }));
        return normalized;
    }

    function updateToggleMeta() {
        const isDay = root.getAttribute("data-km-theme") === "day";
        const title = isDay ? "切换到夜间主题" : "切换到白天主题";
        document.querySelectorAll("[data-km-theme-label]").forEach((el) => {
            el.textContent = "";
        });
        document.querySelectorAll("[data-km-theme-toggle]").forEach((el) => {
            el.setAttribute("title", title);
            el.setAttribute("aria-label", title);
        });
    }

    function init() {
        const saved = normalize(localStorage.getItem(KEY));
        applyTheme(saved);
        applyZoom(localStorage.getItem(ZOOM_KEY));
        updateToggleMeta();
    }

    window.KMTheme = {
        get() {
            return normalize(root.getAttribute("data-km-theme"));
        },
        set(theme) {
            applyTheme(theme);
            updateToggleMeta();
        },
        toggle() {
            const next = this.get() === "day" ? "dark" : "day";
            this.set(next);
        },
        getZoom() {
            return normalizeZoom(localStorage.getItem(ZOOM_KEY));
        },
        setZoom(zoom) {
            return applyZoom(zoom);
        }
    };

    applyZoom(localStorage.getItem(ZOOM_KEY));

    document.addEventListener("click", function (event) {
        const toggle = event.target.closest("[data-km-theme-toggle]");
        if (!toggle) return;
        window.KMTheme.toggle();
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
