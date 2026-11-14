/**
 * ZakoWhat motion layer — small, optional, dependency-free.
 *
 * - Never hides content on its own: below-the-fold reveals only apply once <html> carries `zw-motion`,
 *   which is set here and only when the visitor has not asked for reduced motion. If this file fails
 *   to load, every element simply stays visible.
 * - Purely visual: it adds and removes classes. It never touches timers, submissions or scores and
 *   never waits for an animation before anything else can happen.
 *
 * Hooks (attributes in the templates):
 *   data-reveal                 fades/rises in once when it first enters the viewport
 *   style="--reveal-delay: …"   optional per-element stagger
 *   data-motion-enter           replays a soft entrance when its text content changes (question text)
 *   data-motion-tick            gives a short emphasis when its number changes (scores, streak)
 */
(function () {
  "use strict";

  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce || !("IntersectionObserver" in window)) return;

  var root = document.documentElement;

  function replay(el, cls) {
    el.classList.remove(cls);
    void el.offsetWidth;            // restart the CSS animation (one cheap reflow, only on a real change)
    el.classList.add(cls);
  }

  function setupReveals() {
    var items = Array.prototype.slice.call(document.querySelectorAll("[data-reveal]"));
    if (!items.length) return;
    var fold = window.innerHeight || document.documentElement.clientHeight;
    var pending = items.filter(function (el) {
      // Anything already on screen is shown as-is: no flash of hidden content on load.
      if (el.getBoundingClientRect().top < fold * 0.92) { el.classList.add("is-revealed"); return false; }
      return true;
    });
    root.classList.add("zw-motion");
    if (!pending.length) return;

    var observerAlive = false;
    function revealAll() {
      pending.forEach(function (el) { el.classList.add("is-revealed"); });
    }
    // Safety nets: content must never stay hidden. If the observer does not report within 1.5 s
    // (a page that is not being drawn, an unusual renderer) or the page is printed, show everything.
    setTimeout(function () { if (!observerAlive) revealAll(); }, 1500);
    window.addEventListener("beforeprint", revealAll);

    var io = new IntersectionObserver(function (entries) {
      observerAlive = true;
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-revealed");
        io.unobserve(entry.target);           // once only: scrolling back never replays it
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
    pending.forEach(function (el) { io.observe(el); });
  }

  function watchText(selector, cls) {
    Array.prototype.forEach.call(document.querySelectorAll(selector), function (el) {
      var last = (el.textContent || "").trim();
      new MutationObserver(function () {
        var now = (el.textContent || "").trim();
        if (now === last) return;              // re-renders with identical content stay still
        var previous = last;
        last = now;
        if (!previous || !now) return;         // first fill and clearing are not "changes"
        replay(el, cls);
      }).observe(el, { childList: true, characterData: true, subtree: true });
    });
  }

  function init() {
    setupReveals();
    watchText("[data-motion-enter]", "zw-enter");
    watchText("[data-motion-tick]", "zw-tick");
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
