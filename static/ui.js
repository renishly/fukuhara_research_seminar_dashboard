// The only hand-written browser code on the site. Everything else is HTML
// attributes (HTMX and Alpine.js). It does three things HTML can't:
//   1. Drag to pan: click-and-drag moves the timeline and student windows.
//   2. Centring: scrolls the timeline to today (or to the date kept when
//      zooming), and gives the zoom buttons that date.
//   3. The background grid's "rush" on each page load.
//   4. Drop zones: dropping files onto upload fields.
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
    event.preventDefault(); // don't start selecting text (clicks still work)
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
// screen (data-center-x). Buttons with data-scroll-to="today" or "home"
// scroll their target frame back (the "Today" and "Re-centre" buttons).
// The student window (data-plane) is a 2D plane with empty space around its
// cells; "home" is the view that shows the cells (centred if they fit).
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

function planeHome(frame) {
  const content = frame.querySelector(".sw-content");
  if (!content) return { left: 0, top: 0 };
  const MARGIN = 24;
  const axis = (start, size, view) => (size + 2 * MARGIN > view ? start - MARGIN : start + size / 2 - view / 2);
  return {
    left: axis(content.offsetLeft, content.offsetWidth, frame.clientWidth),
    top: axis(content.offsetTop, content.offsetHeight, frame.clientHeight),
  };
}

// Show the cells when a window first appears (page load or a tab switch), but
// not when its cells refresh every 15 s, so nobody is pulled back mid-move.
function homeNewPlanes() {
  document.querySelectorAll("[data-plane]:not([data-homed])").forEach((frame) => {
    const home = planeHome(frame);
    frame.scrollLeft = home.left;
    frame.scrollTop = home.top;
    frame.dataset.homed = "yes";
  });
}

document.addEventListener("DOMContentLoaded", homeNewPlanes);
document.addEventListener("htmx:afterSettle", homeNewPlanes);
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
    frame.scrollTo({ ...planeHome(frame), behavior: "smooth" });
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

// ---------------------------------------------------------------------------
// 4. Drop zones (templates/partials/dropzone.html). Files dropped on a
// [data-dropzone] go into its file input (added to the ones already chosen,
// or replacing it for single-file fields), and the chosen files are listed,
// each with a button to take it out. Files dropped anywhere else are ignored,
// so a near miss doesn't open the file in the browser and lose the form.
// Uses event delegation, so pop-ups loaded later by HTMX work too.
// ---------------------------------------------------------------------------
(() => {
  const hasFiles = (event) => event.dataTransfer && Array.from(event.dataTransfer.types).includes("Files");
  const zoneOf = (target) => target instanceof Element ? target.closest("[data-dropzone]") : null;
  const usable = (zone) => zone && !zone.querySelector("input[type=file]").disabled;

  function humanSize(bytes) {
    const units = ["B", "KB", "MB", "GB"];
    let size = bytes, unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    return `${unit ? size.toFixed(1) : size} ${units[unit]}`;
  }

  function setFiles(input, files) {
    const transfer = new DataTransfer();
    files.forEach((file) => transfer.items.add(file));
    input.files = transfer.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function showList(zone) {
    const input = zone.querySelector("input[type=file]");
    const list = zone.querySelector("[data-dropzone-list]");
    list.replaceChildren(...Array.from(input.files).map((file, index) => {
      const item = document.createElement("li");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = file.name;
      const size = document.createElement("span");
      size.className = "size";
      size.textContent = humanSize(file.size);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn-ghost btn-xs";
      remove.textContent = "\u00d7";
      remove.setAttribute("aria-label", `Remove ${file.name}`);
      remove.dataset.dropzoneRemove = index;
      item.append(name, size, remove);
      return item;
    }));
  }

  let over = null; // the zone files are being dragged over
  const highlight = (zone) => {
    if (over === zone) return;
    if (over) over.classList.remove("dragging");
    over = zone;
    if (over) over.classList.add("dragging");
  };

  document.addEventListener("dragover", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault(); // never let the browser open a dropped file
    const zone = zoneOf(event.target);
    event.dataTransfer.dropEffect = usable(zone) ? "copy" : "none";
    highlight(usable(zone) ? zone : null);
  });
  document.addEventListener("dragleave", (event) => {
    if (!event.relatedTarget || !zoneOf(event.relatedTarget)) highlight(null);
  });
  document.addEventListener("drop", (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    highlight(null);
    const zone = zoneOf(event.target);
    if (!usable(zone)) return;
    const input = zone.querySelector("input[type=file]");
    const dropped = Array.from(event.dataTransfer.files);
    setFiles(input, input.multiple ? [...Array.from(input.files), ...dropped] : dropped.slice(0, 1));
  });

  // Choosing with the file chooser adds to the files already chosen too
  // (browsers would replace them): remember them when the chooser opens.
  document.addEventListener("change", (event) => {
    const zone = zoneOf(event.target);
    if (!zone || event.target.type !== "file") return;
    const input = event.target;
    if (event.isTrusted && input.multiple && zone.keptFiles && zone.keptFiles.length) {
      const kept = zone.keptFiles;
      zone.keptFiles = null;
      setFiles(input, [...kept, ...Array.from(input.files)]); // comes back here, untrusted
      return;
    }
    showList(zone);
  });
  document.addEventListener("click", (event) => {
    const button = event.target instanceof Element && event.target.closest("[data-dropzone-remove]");
    if (!button) {
      const zone = zoneOf(event.target);
      if (zone) zone.keptFiles = Array.from(zone.querySelector("input[type=file]").files);
      return;
    }
    event.preventDefault(); // don't open the file chooser
    const input = zoneOf(button).querySelector("input[type=file]");
    const keep = Array.from(input.files).filter((_, index) => index !== Number(button.dataset.dropzoneRemove));
    setFiles(input, keep);
  });
  // A form reset (e.g. after an Inbox upload) empties the inputs: empty the lists too.
  document.addEventListener("reset", (event) => {
    setTimeout(() => event.target.querySelectorAll("[data-dropzone]").forEach(showList), 0);
  });
})();

// Agent names on cards stay on one line ([data-marquee]): a name too long for
// its space slides slowly back and forth so all of it can be read, while the
// tags beside it stay put. Measured again after every HTMX refresh and when
// the window is resized.
(function () {
  function measure(root) {
    (root || document).querySelectorAll("[data-marquee]").forEach((box) => {
      const inner = box.firstElementChild;
      if (!inner) return;
      const distance = inner.scrollWidth - box.clientWidth;
      const overflowing = distance > 2;
      box.classList.toggle("is-overflowing", overflowing);
      if (overflowing) {
        box.style.setProperty("--marquee-distance", distance + 8 + "px");
        // About 30 pixels a second, plus the pauses at each end.
        box.style.setProperty("--marquee-time", Math.max(4, distance / 30 + 3) + "s");
      }
    });
  }
  document.addEventListener("DOMContentLoaded", () => measure());
  document.addEventListener("htmx:afterSettle", (event) => measure(event.target));
  let resizing;
  window.addEventListener("resize", () => {
    clearTimeout(resizing);
    resizing = setTimeout(() => measure(), 150);
  });
})();

// Countdowns ([data-countdown] = an ISO time): hh:mm:ss until then, ticking
// every second; "due now" once it's passed. Picks up new ones after each
// HTMX refresh by looking them up on every tick.
(function () {
  function pad(n) { return String(n).padStart(2, "0"); }
  function tick() {
    const now = Date.now();
    document.querySelectorAll("[data-countdown]").forEach((el) => {
      const left = Math.round((Date.parse(el.dataset.countdown) - now) / 1000);
      if (Number.isNaN(left)) return;
      if (left <= 0) {
        el.textContent = "due now";
        // A run is due: reload the live feed once, so its "typing" lines appear.
        if (el.hasAttribute("data-refresh") && !el.dataset.fired) {
          el.dataset.fired = "1";
          const feed = document.getElementById("live-feed-body");
          if (feed && window.htmx) htmx.trigger(feed, "lf-refresh");
        }
        return;
      }
      const h = Math.floor(left / 3600), m = Math.floor((left % 3600) / 60), s = left % 60;
      el.textContent = (h >= 100 ? h : pad(h)) + ":" + pad(m) + ":" + pad(s);
    });
  }
  // While someone is "typing" in the live feed, check every 5 seconds whether it came in.
  let lastTypingCheck = Date.now();
  function watchTyping() {
    const feed = document.getElementById("live-feed-body");
    if (!feed || !window.htmx || !feed.querySelector(".lf-typing")) { lastTypingCheck = Date.now(); return; }
    if (Date.now() - lastTypingCheck >= 5000 && document.visibilityState === "visible") {
      lastTypingCheck = Date.now();
      htmx.trigger(feed, "lf-refresh");
    }
  }
  document.addEventListener("DOMContentLoaded", tick);
  document.addEventListener("htmx:afterSettle", tick);
  setInterval(() => { tick(); watchTyping(); }, 1000);
})();

// The skill map graph (partials/skill_map.html, [data-sg-root]).
// - Click a skill node: open it; click a category node: that category (HTMX).
// - "Draw a path": click skill nodes, or press and drag across them, to pick
//   them in order; the path is drawn through them, listed below, and
//   "Download zip" sends their ids to the server (skills by category folder).
// - "Highlight": dims everyone else's skill nodes.
(function () {
  const SVG = "http://www.w3.org/2000/svg";

  function setUp(root) {
    if (root.dataset.sgReady) return;
    root.dataset.sgReady = "1";
    const svg = root.querySelector("[data-skill-graph]");
    const panel = root.querySelector("[data-sg-panel]");
    const modeButton = root.querySelector("[data-sg-mode]");
    const hint = root.querySelector("[data-sg-hint]");
    const route = root.querySelector("[data-sg-route]");
    const orderLayer = root.querySelector("[data-sg-order]");
    const ownerSelect = root.parentElement.querySelector("[data-sg-owner]");
    let drawing = false, dragging = false;
    const path = [];
    // Where a path may go next from each skill (along the map's lines), from the server.
    const following = JSON.parse(svg.dataset.sgNext || "{}");
    const canGo = (from, to) => (following[from] || []).map(String).includes(String(to));

    const nodeOf = (el) => el && el.closest && el.closest("[data-sg-skill]");
    // Where a node is now (it may have been dragged: data-px / data-py), else where it was drawn.
    const info = (node) => ({ id: node.dataset.sgSkill, x: +(node.dataset.px || node.dataset.x),
                              y: +(node.dataset.py || node.dataset.y),
                              name: node.dataset.name, category: node.dataset.category, node });

    function draw() {
      route.setAttribute("d", path.map((p, i) => (i ? "L" : "M") + p.x + "," + p.y).join(" "));
      orderLayer.replaceChildren();
      root.querySelectorAll(".sg-picked").forEach((n) => n.classList.remove("sg-picked"));
      path.forEach((p, i) => {
        p.node.classList.add("sg-picked");
        const tag = document.createElementNS(SVG, "text");
        tag.setAttribute("x", p.x); tag.setAttribute("y", p.y + 24);
        tag.setAttribute("class", "sg-order"); tag.textContent = i + 1;
        orderLayer.appendChild(tag);
      });
      // Show where the path can go next; dim the rest.
      const last = path[path.length - 1];
      root.querySelectorAll("[data-sg-skill]").forEach((n) => {
        const reachable = drawing && last && canGo(last.id, n.dataset.sgSkill);
        n.classList.toggle("sg-next", !!reachable);
        n.classList.toggle("sg-blocked", !!(drawing && last && !reachable && !n.classList.contains("sg-picked")));
      });
      root.querySelector("[data-sg-count]").textContent = path.length;
      root.querySelector("[data-sg-download]").disabled = path.length === 0;
      const list = root.querySelector("[data-sg-list]");
      list.replaceChildren(...path.map((p) => {
        const li = document.createElement("li");
        li.textContent = p.name + (p.category === "misc" ? " (no category)" : " (category " + p.category + ")");
        return li;
      }));
      root.querySelector("[data-sg-inputs]").replaceChildren(...path.map((p) => {
        const input = document.createElement("input");
        input.type = "hidden"; input.name = "ids"; input.value = p.id;
        return input;
      }));
    }

    function pick(node, toggleLast) {
      const p = info(node);
      const last = path[path.length - 1];
      if (last && last.id === p.id) { if (toggleLast) { path.pop(); draw(); } return; }
      if (path.some((q) => q.id === p.id)) return; // each skill once
      if (last && !canGo(last.id, p.id)) {  // not along the map's lines: refuse it
        if (toggleLast) {
          node.classList.add("sg-refused");
          setTimeout(() => node.classList.remove("sg-refused"), 600);
          hint.textContent = "Can't go there from " + last.name + ": follow the lines left to right.";
        }
        return;
      }
      path.push(p); draw();
    }

    function setMode(on) {
      drawing = on;
      svg.classList.toggle("sg-drawing", on);
      modeButton.setAttribute("aria-pressed", on ? "true" : "false");
      modeButton.classList.toggle("btn-primary", on);
      panel.hidden = !on;
      draw();
      hint.textContent = on ? "Click or drag across skill nodes to pick them, in order." :
                              "Click a skill to open it, or a category to see it across the class.";
    }

    modeButton.addEventListener("click", () => setMode(!drawing));
    root.querySelector("[data-sg-undo]").addEventListener("click", () => { path.pop(); draw(); });
    root.querySelector("[data-sg-clear]").addEventListener("click", () => { path.length = 0; draw(); });

    // The path follows nodes that are being dragged around.
    root.addEventListener("sg-moved", () => {
      if (!path.length) return;
      path.forEach((p) => { const q = info(p.node); p.x = q.x; p.y = q.y; });
      route.setAttribute("d", path.map((p, i) => (i ? "L" : "M") + p.x + "," + p.y).join(" "));
      orderLayer.querySelectorAll("text").forEach((tag, i) => { tag.setAttribute("x", path[i].x); tag.setAttribute("y", path[i].y + 24); });
    });

    svg.addEventListener("click", (event) => {
      if (root.dataset.sgDragged === "1") {  // that was a drag, not a click
        root.dataset.sgDragged = "";
        event.stopPropagation(); event.preventDefault();
        return;
      }
      const node = nodeOf(event.target);
      if (drawing) {
        event.stopPropagation(); event.preventDefault(); // categories don't open while drawing
        return;
      }
      if (node && window.htmx) {
        event.stopPropagation();
        htmx.ajax("GET", "/skills/" + node.dataset.sgSkill, { target: "#modal" });
      }
    }, true);
    svg.addEventListener("keydown", (event) => {
      const node = nodeOf(event.target);
      if (!node || (event.key !== "Enter" && event.key !== " ")) return;
      event.preventDefault();
      if (drawing) pick(node, true); else htmx.ajax("GET", "/skills/" + node.dataset.sgSkill, { target: "#modal" });
    });
    svg.addEventListener("pointerdown", (event) => {
      if (!drawing) return;
      const node = nodeOf(event.target);
      if (!node) return;
      event.preventDefault();
      dragging = true;
      pick(node, true);
    });
    svg.addEventListener("pointermove", (event) => {
      if (!drawing || !dragging) return;
      const node = nodeOf(document.elementFromPoint(event.clientX, event.clientY));
      if (node) pick(node, false);
    });
    window.addEventListener("pointerup", () => { dragging = false; });

    if (ownerSelect) {
      ownerSelect.addEventListener("change", () => {
        const owner = ownerSelect.value;
        root.querySelectorAll("[data-sg-skill]").forEach((n) => {
          n.classList.toggle("sg-dim", owner !== "" && n.dataset.owner !== owner);
        });
      });
    }
  }

  function setUpAll() { document.querySelectorAll("[data-sg-root]").forEach(setUp); }
  document.addEventListener("DOMContentLoaded", setUpAll);
  document.addEventListener("htmx:afterSettle", setUpAll);
})();

// A vote on a skill (the server's HX-Trigger "skill-voted"): resize its node
// on the skill map and update its total on the market cards, straight away.
document.body.addEventListener("skill-voted", (event) => {
  const { id, score, r } = event.detail;
  document.querySelectorAll('[data-sg-skill="' + id + '"]').forEach((node) => {
    const dot = node.querySelector("circle");
    const x = +node.dataset.x, y = +node.dataset.y;
    dot.setAttribute("r", r);
    const badge = node.querySelector(".sg-new");
    if (badge) {
      badge.querySelector("circle").setAttribute("cx", x + r * 0.75);
      badge.querySelector("circle").setAttribute("cy", y - r * 0.75);
      badge.querySelector("text").setAttribute("x", x + r * 0.75);
      badge.querySelector("text").setAttribute("y", y - r * 0.75);
    }
    node.dataset.score = score;  // the hover label reads it
  });
  document.querySelectorAll('[data-vote-for="' + id + '"]').forEach((span) => {
    span.textContent = score > 0 ? "▲+" + score : score < 0 ? "▼" + score : "±0";
    span.classList.toggle("text-success", score > 0);
    span.classList.toggle("text-error", score < 0);
  });
});


// Dragging on the skill map, with a little physics. Skill nodes and category
// nodes can be moved; a category carries its skills, and every line follows.
// Each node may rest anywhere within a "slack" distance of where it was drawn;
// dragged further, it's pulled back to that edge by a spring when let go.
// Skill nodes stay inside their category; miscellaneous ones stay outside.
// The animation only runs while something is moving.
(function () {
  const SLACK = { skill: 40, category: 70 };
  const SPRING = 90, DAMPING = 0.86, STEP = 1 / 60;

  function smooth(points) {
    let d = "M" + points[0][0].toFixed(1) + "," + points[0][1].toFixed(1);
    for (let i = 1; i < points.length; i++) {
      const [x0, y0] = points[i - 1], [x1, y1] = points[i], mid = (x0 + x1) / 2;
      d += " C" + mid.toFixed(1) + "," + y0.toFixed(1) + " " + mid.toFixed(1) + "," + y1.toFixed(1) + " " + x1.toFixed(1) + "," + y1.toFixed(1);
    }
    return d;
  }

  function setUp(root) {
    if (root.dataset.sgPhysics) return;
    root.dataset.sgPhysics = "1";
    const svg = root.querySelector("[data-skill-graph]");
    const cats = {}, skills = {};
    svg.querySelectorAll("[data-sg-cat]").forEach((el) => {
      cats[el.dataset.sgCat] = { kind: "category", el, cx: +el.dataset.cx, cy: +el.dataset.cy, r: +el.dataset.r,
                                 ox: 0, oy: 0, vx: 0, vy: 0 };
    });
    svg.querySelectorAll("[data-sg-skill]").forEach((el) => {
      const cat = el.dataset.category === "misc" ? null : cats[el.dataset.category] || null;
      skills[el.dataset.sgSkill] = { kind: "skill", el, x: +el.dataset.x, y: +el.dataset.y, r: +el.dataset.r || 11,
                                     cat, ox: 0, oy: 0, vx: 0, vy: 0 };
    });
    const mains = [...svg.querySelectorAll(".sg-main")];
    const lanes = [...svg.querySelectorAll(".sg-inner")];
    const miscLines = [...svg.querySelectorAll(".sg-misc-edge")];

    const at = (s) => [s.x + s.ox + (s.cat ? s.cat.ox : 0), s.y + s.oy + (s.cat ? s.cat.oy : 0)];

    function keepIn(s) {
      if (s.cat) {  // inside its category
        const room = s.cat.r - s.r - 4, dx = s.x + s.ox - s.cat.cx, dy = s.y + s.oy - s.cat.cy;
        const d = Math.hypot(dx, dy);
        if (d > room && d > 0) { s.ox += dx * (room / d - 1); s.oy += dy * (room / d - 1); }
      } else {  // a miscellaneous skill: outside every category
        for (const c of Object.values(cats)) {
          const [x, y] = at(s), dx = x - (c.cx + c.ox), dy = y - (c.cy + c.oy), d = Math.hypot(dx, dy) || 1;
          const need = c.r + s.r + 6;
          if (d < need) { s.ox += dx / d * (need - d); s.oy += dy / d * (need - d); }
        }
      }
    }

    function render() {
      Object.values(cats).forEach((c) => c.el.setAttribute("transform", "translate(" + c.ox.toFixed(1) + "," + c.oy.toFixed(1) + ")"));
      Object.values(skills).forEach((s) => {
        const dx = s.ox + (s.cat ? s.cat.ox : 0), dy = s.oy + (s.cat ? s.cat.oy : 0);
        s.el.setAttribute("transform", "translate(" + dx.toFixed(1) + "," + dy.toFixed(1) + ")");
        s.el.dataset.px = (s.x + dx).toFixed(1); s.el.dataset.py = (s.y + dy).toFixed(1);
      });
      mains.forEach((line) => {
        const a = cats[line.dataset.sgFrom], b = cats[line.dataset.sgTo];
        line.setAttribute("x1", a.cx + a.r + a.ox); line.setAttribute("y1", a.cy + a.oy);
        line.setAttribute("x2", b.cx - b.r + b.ox); line.setAttribute("y2", b.cy + b.oy);
      });
      lanes.forEach((lane) => {
        const c = cats[lane.dataset.sgLane];
        const points = [[c.cx - c.r + c.ox, c.cy + c.oy]];
        lane.dataset.sgIds.split(",").forEach((id) => skills[id] && points.push(at(skills[id])));
        points.push([c.cx + c.r + c.ox, c.cy + c.oy]);
        lane.setAttribute("d", smooth(points));
      });
      miscLines.forEach((line) => {
        const a = skills[line.dataset.sgA], b = skills[line.dataset.sgB];
        if (!a || !b) return;
        const [x1, y1] = at(a), [x2, y2] = at(b);
        line.setAttribute("x1", x1); line.setAttribute("y1", y1); line.setAttribute("x2", x2); line.setAttribute("y2", y2);
      });
      root.dispatchEvent(new CustomEvent("sg-moved"));
    }

    // The spring: anything further than its slack from where it was drawn is pulled back.
    let running = false, held = null;
    function tick() {
      let moving = false;
      for (const body of [...Object.values(cats), ...Object.values(skills)]) {
        if (body === held) continue;
        const slack = SLACK[body.kind], d = Math.hypot(body.ox, body.oy);
        if (d > slack) {
          const pull = SPRING * (d - slack) / d;
          body.vx -= body.ox * pull * STEP; body.vy -= body.oy * pull * STEP;
        }
        body.vx *= DAMPING; body.vy *= DAMPING;
        body.ox += body.vx * STEP; body.oy += body.vy * STEP;
        if (body.kind === "skill") keepIn(body);
        if (Math.abs(body.vx) + Math.abs(body.vy) > 1 || d > slack + 0.5) moving = true;
      }
      render();
      if (moving || held) requestAnimationFrame(tick); else running = false;
    }
    function start() { if (!running) { running = true; requestAnimationFrame(tick); } }

    // Dragging (not while drawing a path, which uses the pointer itself).
    let drag = null;
    function svgPoint(event) {
      const point = svg.createSVGPoint();
      point.x = event.clientX; point.y = event.clientY;
      return point.matrixTransform(svg.getScreenCTM().inverse());
    }
    svg.addEventListener("pointerdown", (event) => {
      if (svg.classList.contains("sg-drawing") || event.button > 0) return;
      const node = event.target.closest("[data-sg-skill]");
      const cat = !node && event.target.closest(".sg-cat") && event.target.closest("[data-sg-cat]");
      const body = node ? skills[node.dataset.sgSkill] : cat ? cats[cat.dataset.sgCat] : null;
      if (!body) return;
      const p = svgPoint(event);
      drag = { body, startX: p.x, startY: p.y, ox: body.ox, oy: body.oy, moved: false, id: event.pointerId };
    });
    svg.addEventListener("pointermove", (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      const p = svgPoint(event), dx = p.x - drag.startX, dy = p.y - drag.startY;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;  // a click, so far
      if (!drag.moved) { drag.moved = true; held = drag.body; svg.setPointerCapture(event.pointerId); svg.classList.add("sg-dragging"); start(); }
      event.preventDefault();
      drag.body.ox = drag.ox + dx; drag.body.oy = drag.oy + dy; drag.body.vx = 0; drag.body.vy = 0;
      if (drag.body.kind === "skill") keepIn(drag.body);
    });
    function release() {
      if (!drag) return;
      if (drag.moved) { root.dataset.sgDragged = "1"; setTimeout(() => { root.dataset.sgDragged = ""; }, 0); }
      held = null; drag = null; svg.classList.remove("sg-dragging");
      start();
    }
    svg.addEventListener("pointerup", release);
    svg.addEventListener("pointercancel", release);
  }

  function setUpAll() { document.querySelectorAll("[data-sg-root]").forEach(setUp); }
  document.addEventListener("DOMContentLoaded", setUpAll);
  document.addEventListener("htmx:afterSettle", setUpAll);
})();


// The skill map's hover label: a skill node's name and creator (and agent,
// status and votes), right next to it. Shown on hover or keyboard focus,
// hidden while dragging, and kept beside the node as it moves.
(function () {
  function setUp(root) {
    if (root.dataset.sgTip) return;
    root.dataset.sgTip = "1";
    const tip = root.querySelector("[data-sg-tip]");
    const svg = root.querySelector("[data-skill-graph]");
    if (!tip || !svg) return;
    let shown = null;

    function place() {
      if (!shown) return;
      const box = shown.querySelector("circle").getBoundingClientRect();
      const frame = root.getBoundingClientRect();
      tip.style.left = (box.left + box.width / 2 - frame.left) + "px";
      tip.style.top = (box.top - frame.top - 8) + "px";
    }
    function show(node) {
      if (svg.classList.contains("sg-dragging")) return;
      shown = node;
      const d = node.dataset, score = +d.score || 0;
      const votes = score > 0 ? "▲+" + score : score < 0 ? "▼" + score : "±0 votes";
      tip.replaceChildren();
      const name = document.createElement("strong"); name.textContent = d.name;
      const by = document.createElement("span"); by.textContent = "by " + d.ownerName + (d.agent ? " · " + d.agent : "");
      const meta = document.createElement("span"); meta.className = "sg-tip-meta";
      meta.textContent = d.status + " · " + votes + (d.new ? " · " + d.new + ", not checked yet" : "") +
                         (d.category === "misc" ? " · no category" : "");
      tip.append(name, by, meta);
      tip.hidden = false;
      place();
    }
    function hide() { shown = null; tip.hidden = true; }

    svg.addEventListener("pointerover", (event) => {
      const node = event.target.closest("[data-sg-skill]");
      if (node) show(node);
    });
    svg.addEventListener("pointerout", (event) => {
      const node = event.target.closest("[data-sg-skill]");
      if (node && !node.contains(event.relatedTarget)) hide();
    });
    svg.addEventListener("focusin", (event) => { const node = event.target.closest("[data-sg-skill]"); if (node) show(node); });
    svg.addEventListener("focusout", hide);
    svg.addEventListener("pointerdown", hide);
    root.addEventListener("sg-moved", place);
    root.querySelector(".overflow-x-auto")?.addEventListener("scroll", place);
  }
  function setUpAll() { document.querySelectorAll("[data-sg-root]").forEach(setUp); }
  document.addEventListener("DOMContentLoaded", setUpAll);
  document.addEventListener("htmx:afterSettle", setUpAll);
})();
