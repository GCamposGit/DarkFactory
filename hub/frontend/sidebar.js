// DarkHub vertical sidebar: off-canvas toggle, overlay, focus handling and
// active-link tracking (T2 / "Transformar menu superior em menu lateral").
(function () {
  "use strict";

  const sidebar = document.getElementById("dh-sidebar");
  const toggle = document.getElementById("dh-sidebar-toggle");
  const overlay = document.getElementById("dh-sidebar-overlay");

  if (!sidebar || !toggle || !overlay) {
    return;
  }

  const DESKTOP_QUERY = "(min-width: 1024px)";
  const navLinks = Array.from(sidebar.querySelectorAll(".dh-nav-link[data-dh-section]"));

  function isSidebarOpen() {
    return sidebar.classList.contains("is-open");
  }

  function openSidebar() {
    sidebar.classList.add("is-open");
    overlay.classList.add("is-open");
    toggle.setAttribute("aria-expanded", "true");
  }

  function closeSidebar(options) {
    const restoreFocus = !options || options.restoreFocus !== false;
    sidebar.classList.remove("is-open");
    overlay.classList.remove("is-open");
    toggle.setAttribute("aria-expanded", "false");
    if (restoreFocus && document.activeElement && sidebar.contains(document.activeElement)) {
      toggle.focus();
    }
  }

  toggle.addEventListener("click", function () {
    if (isSidebarOpen()) {
      closeSidebar();
    } else {
      openSidebar();
    }
  });

  overlay.addEventListener("click", function () {
    closeSidebar();
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && isSidebarOpen()) {
      closeSidebar();
    }
  });

  window.matchMedia(DESKTOP_QUERY).addEventListener("change", function (event) {
    if (event.matches) {
      closeSidebar({ restoreFocus: false });
    }
  });

  function setActiveLink(sectionId) {
    if (!sectionId) {
      return;
    }
    navLinks.forEach(function (link) {
      if (link.dataset.dhSection === sectionId) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  }

  navLinks.forEach(function (link) {
    link.addEventListener("click", function () {
      const sectionId = link.dataset.dhSection;
      setActiveLink(sectionId);
      if (sectionId && window.location.hash !== "#" + sectionId) {
        window.history.replaceState(null, "", "#" + sectionId);
      }
      if (!window.matchMedia(DESKTOP_QUERY).matches) {
        closeSidebar({ restoreFocus: false });
      }
    });
  });

  function applyActiveFromHash() {
    const hash = window.location.hash.replace(/^#/, "");
    if (hash) {
      setActiveLink(hash);
    }
  }

  window.addEventListener("hashchange", applyActiveFromHash);
  applyActiveFromHash();

  // Sections referenced by the "scroll to section" nav links are rendered
  // into <main> at runtime by infra.js/studio.js/harness.js, so the observed
  // ids are not guaranteed to exist yet when this script runs.
  const observedSectionIds = navLinks.map(function (link) { return link.dataset.dhSection; });

  const mainEl = document.querySelector(".dh-main main");
  const sectionObserver = new IntersectionObserver(
    function (entries) {
      entries
        .filter(function (entry) { return entry.isIntersecting; })
        .forEach(function (entry) {
          setActiveLink(entry.target.id);
        });
    },
    { root: null, rootMargin: "-45% 0px -45% 0px", threshold: 0 }
  );

  const observedIds = new Set();

  function observeKnownSections() {
    observedSectionIds.forEach(function (sectionId) {
      if (observedIds.has(sectionId)) {
        return;
      }
      const el = document.getElementById(sectionId);
      if (el) {
        sectionObserver.observe(el);
        observedIds.add(sectionId);
      }
    });
  }

  observeKnownSections();

  if (mainEl) {
    const mutationObserver = new MutationObserver(function () {
      observeKnownSections();
    });
    mutationObserver.observe(mainEl, { childList: true, subtree: true });
  }
})();
