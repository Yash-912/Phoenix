// The page's motion, kept apart from what it shows. Nothing here is needed to read the
// page: with scripting off, or with reduced motion asked for, everything is already in
// its final state.

export const reducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function easeOutCubic(t) {
  const clamped = Math.min(1, Math.max(0, t));
  return 1 - (1 - clamped) ** 3;
}

// The number shown a given fraction of the way through a count-up. Integers stay integers.
export function countAt(target, progress) {
  return Math.round(target * easeOutCubic(progress));
}

let observer = null;

// Reveal each [data-reveal] element as it scrolls into view. When the page is repainted for a
// live refresh (animate = false) nothing replays: everything is simply shown.
export function reveal(root, animate) {
  const items = [...root.querySelectorAll("[data-reveal]")];
  if (observer) observer.disconnect();
  if (!animate || reducedMotion() || typeof IntersectionObserver === "undefined") {
    items.forEach((el) => el.classList.add("is-in"));
    return;
  }
  observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-in");
          observer.unobserve(entry.target);
        }
      }
    },
    { rootMargin: "0px 0px -8% 0px", threshold: 0.05 },
  );
  items.forEach((el) => observer.observe(el));
  // A safety net: if the observer has not fired for something already on screen (a throttled
  // tab, a browser that skips frames), show it anyway. Content must never stay hidden.
  setTimeout(() => {
    items.forEach((el) => {
      if (!el.classList.contains("is-in") && el.getBoundingClientRect().top < window.innerHeight) el.classList.add("is-in");
    });
  }, 700);
}

// Count the big numerals up from zero. The final number is already in the markup.
export function countUp(root, animate) {
  if (!animate || reducedMotion()) return;
  root.querySelectorAll("[data-count]").forEach((el) => {
    const target = Number(el.dataset.count);
    if (!Number.isFinite(target) || target === 0) return;
    const started = performance.now();
    const duration = 900;
    const tick = (now) => {
      const progress = (now - started) / duration;
      el.textContent = String(countAt(target, progress));
      if (progress < 1) requestAnimationFrame(tick);
      else el.textContent = String(target);
    };
    el.textContent = "0";
    requestAnimationFrame(tick);
    setTimeout(() => { el.textContent = String(target); }, duration + 300);
  });
}

// Top bar shadow and the reading-progress line, driven by one scroll listener.
export function bindChrome() {
  const topbar = document.querySelector(".topbar");
  const progress = document.getElementById("progress");
  let queued = false;
  const update = () => {
    queued = false;
    const max = document.documentElement.scrollHeight - window.innerHeight;
    const ratio = max > 0 ? Math.min(1, window.scrollY / max) : 0;
    if (progress) progress.style.transform = `scaleX(${ratio})`;
    if (topbar) topbar.classList.toggle("is-scrolled", window.scrollY > 4);
  };
  window.addEventListener(
    "scroll",
    () => {
      if (!queued) {
        queued = true;
        requestAnimationFrame(update);
      }
    },
    { passive: true },
  );
  update();
}

// Highlight the jump-bar button for the section being read.
let spy = null;
export function watchSections(root) {
  if (spy) spy.disconnect();
  const buttons = [...root.querySelectorAll("[data-jump]")];
  if (!buttons.length || typeof IntersectionObserver === "undefined") return;
  const mark = (id) => buttons.forEach((b) => b.setAttribute("aria-current", String(b.dataset.jump === id)));
  mark(buttons[0].dataset.jump);
  spy = new IntersectionObserver(
    (entries) => {
      const visible = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
      if (visible) mark(visible.target.id.replace(/^section-/, ""));
    },
    { rootMargin: "-20% 0px -65% 0px" },
  );
  root.querySelectorAll("section[id^='section-']").forEach((el) => spy.observe(el));
}

export function jumpTo(id) {
  const target = document.getElementById(`section-${id}`);
  if (target) target.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
}
