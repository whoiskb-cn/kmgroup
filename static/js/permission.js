(function () {
    const currentPath = (window.location.pathname || '').toLowerCase();
    const user = (localStorage.getItem('km_user') || '').trim();
    const role = (localStorage.getItem('km_role') || '').toLowerCase().trim();
    const allowedRoles = new Set(['admin', 'operator', 'hongkong', 'mainland']);
    const limitedMenuRoles = new Set(['hongkong', 'mainland']);
    const hiddenPages = ['/static/search.html', '/static/report.html', '/static/production.html'];
    const isLoginPage = currentPath === '/' || currentPath.endsWith('/index.html') || currentPath.endsWith('/static/index.html');

    const ensureLoggedIn = () => {
        if (isLoginPage) return true;
        if (!user || !role || !allowedRoles.has(role)) {
            localStorage.removeItem('km_user');
            localStorage.removeItem('km_role');
            window.location.replace('/');
            return false;
        }
        return true;
    };

    if (!ensureLoggedIn()) return;

    const shouldHideMenus = limitedMenuRoles.has(role);
    const hiddenLinkSelector = hiddenPages.map((href) => `a[href="${href}"]`).join(', ');

    const applyImmediateHideStyles = () => {
        if (!shouldHideMenus || !hiddenLinkSelector) return;
        const styleId = 'km-role-restricted-menu-style';
        if (document.getElementById(styleId)) return;
        const style = document.createElement('style');
        style.id = styleId;
        style.textContent = `${hiddenLinkSelector}{display:none !important;}`;
        (document.head || document.documentElement).appendChild(style);
    };

    applyImmediateHideStyles();

    const isBlockedPage = () => {
        return hiddenPages.some((page) => currentPath.endsWith(page));
    };

    const hideMenus = () => {
        if (!shouldHideMenus) return;
        hiddenPages.forEach((href) => {
            document.querySelectorAll(`a[href="${href}"]`).forEach((link) => {
                link.style.display = 'none';
            });
        });
    };

    if (shouldHideMenus && isBlockedPage()) {
        document.documentElement.style.visibility = 'hidden';
        window.location.replace('/static/orders.html');
        return;
    }

    window.KM_PERM = {
        user,
        role,
        isAdmin: role === 'admin',
        isHongKong: role === 'hongkong',
        isMainland: role === 'mainland',
        canModifyData: role !== 'mainland',
        canDeleteShipmentAndInventory: role !== 'mainland'
    };

    if (window.axios && window.axios.interceptors) {
        window.axios.interceptors.response.use(
            (resp) => resp,
            (err) => {
                if (err && err.response && err.response.status === 401) {
                    localStorage.clear();
                    window.location.replace('/');
                }
                return Promise.reject(err);
            }
        );
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', hideMenus);
    } else {
        hideMenus();
    }
})();
