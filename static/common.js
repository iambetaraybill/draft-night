/*
  Shared front-end plumbing for both views.

  The TV and the phone show completely different things, but they connect the
  same way, escape the same way and run the same clock. That all lives here
  so there is one copy of it rather than two that drift apart.
*/

const DN = (() => {
  const el = (id) => document.getElementById(id);

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* ---- network ---- */

  async function post(path, body, headers = {}) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "That did not work");
    return data;
  }

  /* ---- live state ---- */

  // Last snapshot, plus when it landed, so the countdown can run smoothly
  // between pushes instead of jumping once a second.
  const live = { snap: null, left: 0, total: 14, at: 0 };

  function connect(onSnapshot, onDrop) {
    const stream = new EventSource("/events");
    stream.onmessage = (ev) => {
      const snap = JSON.parse(ev.data);
      live.snap = snap;
      live.left = snap.seconds_left;
      live.total = snap.lot_seconds;
      live.at = performance.now();
      onSnapshot(snap);
    };
    if (onDrop) stream.onerror = onDrop;
    return stream;
  }

  function secondsLeft() {
    if (!live.snap || live.snap.phase !== "bidding") return null;
    return Math.max(0, live.left - (performance.now() - live.at) / 1000);
  }

  /*
    Drives anything carrying data-clock. The bar scales horizontally rather
    than animating width, which keeps it off the layout path and smooth on
    the kind of laptop that is also running a language model.
  */
  function runClock(onTick) {
    const frame = () => {
      const left = secondsLeft();
      if (left !== null) {
        const frac = Math.min(1, left / live.total);
        document.querySelectorAll("[data-clock-bar]").forEach((bar) => {
          bar.style.transform = `scaleX(${frac})`;
        });
        document.querySelectorAll("[data-clock]").forEach((node) => {
          node.classList.toggle("urgent", left <= 4);
        });
        if (onTick) onTick(left);
      }
      requestAnimationFrame(frame);
    };
    requestAnimationFrame(frame);
  }

  /* ---- transient messages ---- */

  let toastTimer = null;
  function toast(msg, tone = "bad") {
    let node = el("toast");
    if (!node) {
      node = document.createElement("div");
      node.id = "toast";
      document.body.appendChild(node);
    }
    node.className = `toast ${tone}`;
    node.textContent = msg;
    node.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 3200);
  }

  /* ---- shared rendering ---- */

  // One player card, used full size on the TV and small on a phone.
  function playerCard(lot, size = "big") {
    if (!lot) return "";
    return `<figure class="card-player ${size}" data-pos="${esc(lot.pos)}">
      <div class="card-corner">
        <span class="card-rating">${lot.rating}</span>
        <span class="card-pos">${esc(lot.pos)}</span>
      </div>
      <figcaption>
        <span class="card-name">${esc(lot.name)}</span>
        <span class="card-from">${[lot.club, lot.nation].filter(Boolean).map(esc).join(" · ")}</span>
      </figcaption>
    </figure>`;
  }

  function needList(needs) {
    const open = Object.entries(needs).filter(([, n]) => n);
    if (!open.length) return "squad complete";
    return open.map(([p, n]) => `${n}&nbsp;${p}`).join(" · ");
  }

  return { el, esc, post, connect, runClock, secondsLeft, toast, playerCard, needList, live };
})();
