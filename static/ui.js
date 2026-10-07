// The only hand-written browser code on the site. Everything else is HTML
// attributes (HTMX and Alpine.js). It does three things HTML can't:
//   1. Drag to pan: click-and-drag moves the timeline and student windows.
//   2. Centring: scrolls the timeline to today (or to the date kept when
//      zooming), and gives the zoom buttons that date.
//   3. The background grid's "rush" on each page load.
// If anything here fails, the page still works: scroll bars and the scroll
// wheel always move the timeline and windows too.

// ---------------------------------------------------------------------------
// 1. Drag to pan. Any element with a data-pan attribute can be dragged with
// the mouse. A press that moves less than 6 pixels is a normal click (so
// cells and class boxes still open); a longer movement pans instead, and the
// click that ends it is swallowed. Touch screens already scroll with fingers,
// so only the mouse is handled here.
// ---------------------------------------------------------------------------
(() => {
  const CLICK_DISTANCE = 6;
  let drag = null; // {frame, x, y, left, top, moved}

  document.addEventListener("pointerdown", (event) => {
    const frame = event.target.closest("[data-pan]");
    if (!frame || event.pointerType !== "mouse" || event.button !== 0) return;
    if (event.target.closest("input, textarea, select, button")) return;
    drag = { frame, x: event.clientX, y: event.clientY, left: frame.scrollLeft, top: frame.scrollTop, moved: false };
  });

  document.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < CLICK_DISTANCE) return;
    drag.moved = true;
    drag.frame.classList.add("panning");
    drag.frame.scrollLeft = drag.left - dx;
    drag.frame.scrollTop = drag.top - dy;
    event.preventDefault();
  });

  document.addEventListener("pointerup", () => {
    if (!drag) return;
    const wasPan = drag.moved;
    drag.frame.classList.remove("panning");
    drag = null;
    if (wasPan) {
      // Swallow the click that follows a pan, so nothing opens by accident.
      const swallow = (event) => { event.stopPropagation(); event.preventDefault(); };
      document.addEventListener("click", swallow, { capture: true, once: true });
      setTimeout(() => document.removeEventListener("click", swallow, { capture: true }), 0);
    }
  });

  // Stop the browser dragging links and images instead of panning.
  document.addEventListener("dragstart", (event) => {
    if (event.target.closest("[data-pan]")) event.preventDefault();
  });
})();

// ---------------------------------------------------------------------------
// 2. Centring. The server (timeline.py) works out every position in pixels;
// the timeline element says which x position belongs in the middle of the
// screen (data-center-x). Buttons with data-scroll-to="today" or "start"
// scroll their target frame back (the "Today" and "Re-centre" buttons).
// ---------------------------------------------------------------------------
function centreTimeline() {
  const frame = document.getElementById("timeline-frame");
  const canvas = frame && frame.querySelector("[data-center-x]");
  if (!canvas) return;
  frame.scrollLeft = Number(canvas.dataset.centerX) - frame.clientWidth / 2;
}

// The month (as a number with a fraction) now in the middle of the timeline.
// The zoom buttons send it, so zooming keeps the same date in the middle.
function timelineCenter() {
  const frame = document.getElementById("timeline-frame");
  const canvas = frame && frame.querySelector("[data-center-x]");
  if (!canvas) return "";
  const middle = frame.scrollLeft + frame.clientWidth / 2;
  return Number(canvas.dataset.firstMonth) + middle / Number(canvas.dataset.pxPerMonth);
}

document.addEventListener("DOMContentLoaded", centreTimeline);
document.addEventListener("htmx:afterSettle", (event) => {
  if (event.target.id === "timeline-frame" || event.target.closest?.("#timeline-frame")) centreTimeline();
});
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-scroll-to]");
  if (!button) return;
  const frame = document.getElementById(button.dataset.target);
  if (!frame) return;
  if (button.dataset.scrollTo === "today") {
    const canvas = frame.querySelector("[data-today-x]");
    frame.scrollTo({ left: Number(canvas.dataset.todayX) - frame.clientWidth / 2, behavior: "smooth" });
  } else {
    frame.scrollTo({ left: 0, top: 0, behavior: "smooth" });
  }
});

// ---------------------------------------------------------------------------
// 3. The background grid (body::before in theme.css) drifts slowly upwards
// with a CSS animation called "grid-drift" (one loop = one square). Every page
// is a fresh start for the browser, so on its own the grid would jump back on
// each page. This carries on from where the previous page left it (kept for
// this browser tab only, in sessionStorage), then plays a "rush": about 2,800
// pixels in 1.3 seconds, easing back into the slow drift from where it ends.
// ---------------------------------------------------------------------------
(() => {
  const RUSH_SECONDS = 1.3;
  const RUSH_LOOPS = 58; // 58 squares of 48px: about 2,800 pixels
  const KEY = "gridPosition";

  let drift;
  try {
    drift = document.getAnimations().find((a) => a.animationName === "grid-drift");
  } catch (error) {
    return;
  }
  if (!drift) return; // e.g. "reduce motion" is on: the grid stays still
  const loopMs = drift.effect.getComputedTiming().duration;
  const position = () => ((drift.currentTime || 0) / loopMs) % 1; // in loops, 0 to 1
  const moveTo = (loops) => { drift.currentTime = (((loops % 1) + 1) % 1) * loopMs; };

  // Pick up from the previous page, including the time spent loading this one.
  let start = 0;
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY) || "null");
    if (saved) start = saved.position + (Date.now() - saved.at) / loopMs;
  } catch (error) {}
  moveTo(start);

  const save = () => {
    try {
      sessionStorage.setItem(KEY, JSON.stringify({ position: position(), at: Date.now() }));
    } catch (error) {}
  };
  addEventListener("pagehide", save);
  document.addEventListener("visibilitychange", () => document.hidden && save());

  // The rush: fast at first, easing out into the normal drift.
  drift.pause();
  let began = null;
  const step = (now) => {
    if (began === null) began = now;
    const elapsed = now - began;
    const t = Math.min(elapsed / (RUSH_SECONDS * 1000), 1);
    const eased = 1 - Math.pow(1 - t, 3);
    moveTo(start + RUSH_LOOPS * eased + elapsed / loopMs);
    if (t < 1) requestAnimationFrame(step);
    else drift.play();
  };
  requestAnimationFrame(step);
})();
