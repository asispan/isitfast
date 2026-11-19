(async function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const MIN_ZOOM = 13;          // below this no places load ("Zoom in to see places")
  const ALL_KINDS_ZOOM = 14.5;  // below this only cafes and coworking spaces are drawn as dots
  const DEFAULT = { center: { lat: 12.9716, lng: 77.5946 }, zoom: 13.5 }; // Bengaluru fallback
  // Speed tiers (Mbps): graphite → ultramarine. Slow is pale, never red (Figma v2 "Site Plan").
  const TIERS = [
    { max: 5, color: "#CBC6BA" },
    { max: 20, color: "#A2A0B2" },
    { max: 50, color: "#7884CA" },
    { max: 100, color: "#4A5CD8" },
    { max: Infinity, color: "#2234C8" },
  ];
  const NODATA = "#8E887B", ULTRA = "#2A3BD4", PANEL = "#FBFAF7";
  const state = { mode: "mobile", features: new Map(), selected: null, me: null };

  // ------------------------------------------------------------------ icons (1.7 stroke, 24 grid)
  const P = {
    wifi: '<path d="M2 8.8a15 15 0 0 1 20 0M5 12.6a10 10 0 0 1 14 0M8.5 16.4a5 5 0 0 1 7 0M12 20h.01"/>',
    phone: '<rect x="6" y="2.5" width="12" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
    home: '<path d="M3 10.5L12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    flag: '<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>',
    pin: '<path d="M12 21s-7-6.2-7-11.5A7 7 0 0 1 19 9.5C19 14.8 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.5"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    shield: '<path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/>',
    ext: '<path d="M7 17L17 7M9 7h8v8"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    chev: '<path d="M6 9l6 6 6-6"/>',
    alert: '<path d="M12 3.5l9.5 16.5h-19z"/><path d="M12 10v4.5M12 17.4v.1"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.6v.4"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    wifiOff: '<path d="M2 8.8a15 15 0 0 1 20 0M5 12.6a10 10 0 0 1 14 0"/><path d="M3 3l18 18"/>',
  };
  const ic = (n, cls = "") => `<svg class="ic ${cls}" viewBox="0 0 24 24" aria-hidden="true">${P[n]}</svg>`;
  const SPEED_IC = '<img src="/icons/speed-white.png?v=1" alt="" width="18" height="18" />';
  // Same Icons8 speed glyph (Figma Icon/speed) as an alpha mask, so it takes the text colour.
  const SPEED_MASK = '<span class="ic ic-speed" aria-hidden="true"></span>';

  // ------------------------------------------------------------------ map
  // Basemap: Google Maps (vector map, Map ID; its colours are set in the Cloud console style). Google's own
  // POIs open as venues; our own places (OSM cafes, coworking, restaurants, offices, apartments, plus any
  // tested place) are drawn as dots, and the tested ones as labelled pins.
  let GMap, OverlayView, AdvancedMarkerElement, Place, AutocompleteSuggestion, AutocompleteSessionToken;
  try {
    ({ Map: GMap, OverlayView } = await google.maps.importLibrary("maps"));
    ({ AdvancedMarkerElement } = await google.maps.importLibrary("marker"));
    ({ Place, AutocompleteSuggestion, AutocompleteSessionToken } = await google.maps.importLibrary("places"));
  } catch {
    $("#map").innerHTML = `<p class="map-err">Couldn't load the map — check your connection and reload.</p>`;
    return;
  }
  const map = new GMap($("#map"), {
    center: DEFAULT.center,
    zoom: DEFAULT.zoom,
    mapId: $("#map").dataset.mapId || "DEMO_MAP_ID",
    disableDefaultUI: true, // our own zoom + locate buttons (style.css)
    clickableIcons: true,
    gestureHandling: "greedy",
    isFractionalZoomEnabled: true,
  });
  window.isitfastMap = map; // handle for QA scripts

  const ll = (lat, lon) => ({ lat, lng: lon });
  const zoom = () => map.getZoom() ?? DEFAULT.zoom;
  function flyTo(lat, lon, z) {
    map.panTo(ll(lat, lon));
    if (z != null && Math.abs(z - zoom()) > 0.01) map.setZoom(z);
  }
  $("#zoom-in").addEventListener("click", () => map.setZoom(Math.round(zoom()) + 1));
  $("#zoom-out").addEventListener("click", () => map.setZoom(Math.round(zoom()) - 1));
  map.addListener("zoom_changed", () => $("#map").classList.toggle("labels", zoom() >= 15.5));

  // A tap on a Google POI opens it; a tap on one of our dots opens that place; anywhere else closes.
  map.addListener("click", (e) => {
    if (e.placeId) { e.stop(); openPlace(`g${e.placeId}`); return; }
    const hit = e.pixel ? dots.hit(e.pixel.x, e.pixel.y) : null;
    if (hit) { openPlace(hit); return; }
    if (sidebar.classList.contains("open")) closePanel();
  });
  map.addListener("idle", debounce(loadPlaces, 350));

  // ------------------------------------------------------------------ dots (all places) + the 50 m test range
  // Thousands of places per tile, so untested ones are drawn on one canvas instead of DOM markers.
  // The selected place's 50 m test range (a dimensioned circle) is drawn on the same canvas.
  const tierColor = (v) => (v == null ? NODATA : TIERS.find((t) => v < t.max).color);
  class Dots extends OverlayView {
    constructor() { super(); this.canvas = document.createElement("canvas"); this.canvas.style.cssText = "position:absolute;pointer-events:none"; this.drawn = []; }
    onAdd() { this.getPanes().overlayLayer.appendChild(this.canvas); }
    onRemove() { this.canvas.remove(); }
    draw() {
      const proj = this.getProjection(), b = map.getBounds(), el = $("#map");
      if (!proj || !b) return;
      const W = el.clientWidth, H = el.clientHeight, dpr = window.devicePixelRatio || 1;
      const ne = b.getNorthEast(), sw = b.getSouthWest();
      const tl = proj.fromLatLngToDivPixel(new google.maps.LatLng(ne.lat(), sw.lng()));
      const c = this.canvas;
      if (c.width !== W * dpr || c.height !== H * dpr) { c.width = W * dpr; c.height = H * dpr; c.style.width = W + "px"; c.style.height = H + "px"; }
      c.style.left = tl.x + "px"; c.style.top = tl.y + "px";
      const g = c.getContext("2d");
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, W, H);
      this.drawn = [];
      const z = zoom();
      if (z >= MIN_ZOOM) {
        const all = z >= ALL_KINDS_ZOOM, r = z >= 16 ? 5 : z >= 15 ? 4.5 : 3.5;
        for (const f of state.features.values()) {
          const p = f.properties;
          if (p.t || p.id === state.selected) continue; // tested places are pins
          if (!all && p.kind !== "cafe" && p.kind !== "coworking") continue;
          const [lon, lat] = f.geometry.coordinates;
          const pt = proj.fromLatLngToDivPixel(new google.maps.LatLng(lat, lon));
          const x = pt.x - tl.x, y = pt.y - tl.y;
          if (x < -10 || y < -10 || x > W + 10 || y > H + 10) continue;
          g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2);
          g.fillStyle = PANEL; g.fill();
          if (state.mode === "mobile" && p.mobile != null) { g.lineWidth = r > 4 ? 2.6 : 2.1; g.strokeStyle = tierColor(p.mobile); }
          else { g.lineWidth = 1.5; g.strokeStyle = NODATA; }
          g.stroke();
          this.drawn.push([x, y, p.id]);
        }
      }
      const cur = state.current;
      if (cur && cur.lat != null) this.ring(g, proj, tl, cur);
    }
    ring(g, proj, tl, p) {
      const m = radiusFor(p), inside = inRange();
      const c = proj.fromLatLngToDivPixel(new google.maps.LatLng(p.lat, p.lon));
      const e = proj.fromLatLngToDivPixel(new google.maps.LatLng(p.lat, p.lon + m / (111320 * Math.cos((p.lat * Math.PI) / 180))));
      const x = c.x - tl.x, y = c.y - tl.y, r = Math.abs(e.x - c.x);
      if (r < 8) return;
      g.save();
      g.beginPath(); g.arc(x, y, r, 0, Math.PI * 2);
      g.fillStyle = inside ? "rgba(42,59,212,.10)" : "rgba(42,59,212,.06)"; g.fill();
      g.setLineDash(inside ? [] : [5, 4]); g.lineWidth = 1.5; g.strokeStyle = ULTRA; g.stroke();
      g.setLineDash([]); g.lineWidth = 1.25;
      g.beginPath(); g.moveTo(x, y); g.lineTo(x + r, y); g.moveTo(x + r, y - 6); g.lineTo(x + r, y + 6); g.stroke();
      if (r > 34) {
        const label = `${m} m`; g.font = "500 11px Geist, system-ui, sans-serif";
        const w = g.measureText(label).width + 14, lx = x + r / 2 - w / 2 + 4, ly = y - 26;
        g.fillStyle = ULTRA; g.beginPath(); g.roundRect(lx, ly, w, 20, 6); g.fill();
        g.fillStyle = "#fff"; g.textBaseline = "middle"; g.fillText(label, lx + 7, ly + 10.5);
      }
      g.restore();
    }
    hit(px, py) {
      let best = null, bd = 14 * 14;
      for (const [x, y, id] of this.drawn) { const d = (x - px) ** 2 + (y - py) ** 2; if (d < bd) { bd = d; best = id; } }
      return best;
    }
  }
  const dots = new Dots();
  dots.setMap(map);
  let raf = 0;
  const redraw = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => dots.draw()); };
  map.addListener("bounds_changed", redraw);

  // ------------------------------------------------------------------ tested-place pins
  const pins = new Map(); // id -> { marker, el, f }
  function paintPin(pin) {
    const p = pin.f.properties, sel = state.selected === p.id;
    const v = state.mode === "wifi" ? p.wifi : p.mobile;
    pin.el.style.setProperty("--c", tierColor(v));
    pin.el.classList.toggle("area", state.mode === "mobile" && v != null); // area average: ring
    pin.el.classList.toggle("none", v == null);                             // nothing measured for this mode
    pin.el.classList.toggle("selected", sel);
    pin.marker.zIndex = sel ? 30 : 10;
  }
  function makePin(f) {
    const p = f.properties, [lon, lat] = f.geometry.coordinates;
    const el = document.createElement("div");
    el.className = "pin";
    // OSM/visitor places carry their own name; Google places get none (the basemap already labels them).
    el.innerHTML = `<span class="dot"></span>${p.name ? `<span class="lbl">${esc(p.name)}</span>` : ""}`;
    const marker = new AdvancedMarkerElement({ map, position: ll(lat, lon), content: el, title: p.name || "Tested place", gmpClickable: true });
    marker.addEventListener("gmp-click", () => openPlace(p.id));
    return { marker, el, f };
  }
  function syncPins() {
    for (const [id, pin] of pins) {
      const f = state.features.get(id);
      if (!f || !(f.properties.t || id === state.selected)) { pin.marker.map = null; pins.delete(id); }
    }
    for (const [id, f] of state.features) {
      if (!(f.properties.t || id === state.selected)) continue;
      let pin = pins.get(id);
      if (!pin) pins.set(id, (pin = makePin(f)));
      else pin.f = f;
      paintPin(pin);
    }
    if (state.selected && pins.has(state.selected)) markSpot(null); // its real pin has arrived
    redraw();
  }
  function select(id) {
    state.selected = id;
    syncPins(); // an untested place gets a pin while it is open
    if (!id || pins.has(id)) markSpot(null);
  }
  // An open place without a pin of its own (an untested Google place) gets a temporary selected pin.
  let spotMarker = null;
  function markSpot(p) {
    if (!p) { if (spotMarker) spotMarker.map = null; return; }
    if (!spotMarker) {
      const el = document.createElement("div");
      el.className = "pin selected none";
      el.innerHTML = '<span class="dot"></span>';
      spotMarker = new AdvancedMarkerElement({ content: el, zIndex: 40 });
    }
    spotMarker.position = ll(p.lat, p.lon);
    spotMarker.map = map;
  }
  // Pixel position of a point in the map container (null before the first render).
  function pxOf(lat, lon) {
    const proj = map.getProjection(), b = map.getBounds();
    if (!proj || !b) return null;
    const scale = 2 ** zoom();
    const nw = proj.fromLatLngToPoint(new google.maps.LatLng(b.getNorthEast().lat(), b.getSouthWest().lng()));
    const pt = proj.fromLatLngToPoint(new google.maps.LatLng(lat, lon));
    return { x: (pt.x - nw.x) * scale, y: (pt.y - nw.y) * scale };
  }
  // Bring a place into the part of the map the panel doesn't cover (desktop: left column; mobile: the
  // half-height sheet at the bottom); zoom in if it was off-screen.
  // Zooms to at least 17, where the 50 m test range reads as a ring (~45 px) around the place.
  function reveal(lat, lon) {
    if (!map.getProjection()) { google.maps.event.addListenerOnce(map, "idle", () => reveal(lat, lon)); return; } // first render: panBy is ignored before it
    const el = $("#map"), W = el.clientWidth, H = el.clientHeight, pt = pxOf(lat, lon), close = zoom() >= 16.5;
    if (isMobile()) {
      // the clear band between the colour toggle and the sheet; keep the place and its 50 m ring inside it
      const top = $(".controls").getBoundingClientRect().bottom + 60, bottom = H - sheetHalfPx() - 50;
      if (close && pt && pt.x > 50 && pt.x < W - 50 && pt.y > top && pt.y < bottom) return;
      map.moveCamera({ center: ll(lat, lon), zoom: Math.max(zoom(), 17) });
      map.panBy(0, H / 2 - (top + bottom) / 2);
      return;
    }
    const left = sidebar.getBoundingClientRect().right;
    if (close && pt && pt.x > left + 40 && pt.x < W - 80 && pt.y > 70 && pt.y < H - 100) return;
    map.moveCamera({ center: ll(lat, lon), zoom: Math.max(zoom(), 17) });
    map.panBy(-left / 2, 0);
  }

  // ------------------------------------------------------------------ Google place details
  // Google Maps Platform terms allow storing only a place_id. So a Google place's name, type and address
  // are fetched live in the browser on every open and never sent to or stored by our server; our server
  // keeps the place_id, the tests, and the position of the visitor who first tested there.
  const gInfo = new Map(); // this page view only
  const googleKind = (t) => !t ? "place"
    : /^(cafe|coffee_shop|tea_house|cafeteria|internet_cafe)$/.test(t) ? "cafe"
    : /cowork/.test(t) ? "coworking" : t;
  async function googleInfo(placeId, place) {
    if (gInfo.has(placeId)) return gInfo.get(placeId);
    const pl = place || new Place({ id: placeId });
    await pl.fetchFields({ fields: ["displayName", "primaryType", "shortFormattedAddress", "location", "googleMapsURI"] });
    const info = {
      name: pl.displayName || "Unnamed place", kind: googleKind(pl.primaryType), addr: pl.shortFormattedAddress || "",
      lat: pl.location ? pl.location.lat() : null, lon: pl.location ? pl.location.lng() : null, url: pl.googleMapsURI || null,
    };
    gInfo.set(placeId, info);
    return info;
  }
  // A Google POI that is one of our OSM/visitor places (same name, same spot) opens that place, so its
  // tests aren't split across two records.
  // Names match when one contains the other or they share a long prefix ("Starbucks" ~ Google's
  // "Starbucks - MG Road"; "Third Wave Coffee Roasters" ~ "Third Wave Coffee - Indira Nagar 12th Main").
  const norm = (s) => String(s || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  function sameName(a, b) {
    a = norm(a); b = norm(b);
    if (a.length < 4 || b.length < 4) return false;
    if (a.includes(b) || b.includes(a)) return true;
    let k = 0;
    while (k < a.length && a[k] === b[k]) k++;
    return k >= 10;
  }
  function ourTwin(info) { // within 100 m: OSM and Google positions for one venue often differ by tens of metres
    if (info.lat == null) return null;
    let best = null, bestD = 101;
    for (const f of state.features.values()) {
      const [lon, lat] = f.geometry.coordinates, d = distM({ lat, lon }, info);
      if (d < bestD && f.properties.id[0] !== "g" && sameName(f.properties.name, info.name)) { best = f.properties.id; bestD = d; }
    }
    return best;
  }

  function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

  // Places come in fixed 0.25° snapshot tiles (/api/places/tile/{i}/{j}): identical URLs for every visitor,
  // so browsers and the CDN cache them. Tiles already loaded are reused for TILE_TTL_MS.
  const TILE = 0.25, TILE_TTL_MS = 120000, MAX_TILES = 12;
  const tiles = new Map(); // "i/j" -> { at, features }
  const tileKey = (lat, lon) => `${Math.floor(lat / TILE)}/${Math.floor(lon / TILE)}`;
  function visibleTiles() {
    const b = map.getBounds(), keys = [];
    if (!b) return keys;
    const sw = b.getSouthWest(), ne = b.getNorthEast();
    for (let i = Math.floor(sw.lat() / TILE); i <= Math.floor(ne.lat() / TILE); i++)
      for (let j = Math.floor(sw.lng() / TILE); j <= Math.floor(ne.lng() / TILE); j++) keys.push(`${i}/${j}`);
    return keys;
  }
  async function fetchTile(k, fresh) {
    // ?f= is the tile format (TILE_FORMAT on the server): a new URL, so CDN/browser copies of the old format are skipped
    const r = await fetch(`/api/places/tile/${k}?f=g2${fresh ? `&fresh=${Date.now()}` : ""}`);
    if (!r.ok) throw new Error(r.status);
    tiles.set(k, { at: Date.now(), features: (await r.json()).features });
  }
  function publishFeatures() {
    state.features.clear();
    for (const t of tiles.values()) for (const f of t.features) state.features.set(f.properties.id, f);
    syncPins();
  }
  let hinted = false;
  async function loadPlaces(freshKey) {
    if (zoom() < MIN_ZOOM) { toast("Zoom in to see places"); redraw(); return; }
    const keys = visibleTiles();
    if (keys.length > MAX_TILES) { toast("Zoom in a little to load places"); return; }
    const todo = keys.filter((k) => k === freshKey || !tiles.has(k) || Date.now() - tiles.get(k).at > TILE_TTL_MS);
    if (!todo.length) return;
    const slow = setTimeout(() => toast("Loading cafes & coworking spots…", 0, true), 600);
    try {
      await Promise.all(todo.map((k) => fetchTile(k, k === freshKey)));
      for (const k of [...tiles.keys()]) if (tiles.size > MAX_TILES * 2 && !keys.includes(k)) tiles.delete(k); // bound memory
      publishFeatures();
      const shown = keys.reduce((n, k) => n + (tiles.get(k)?.features.length || 0), 0);
      if (!shown && !hinted) { hinted = true; toast("No places mapped here yet. Tap any cafe, restaurant or office on the map to test it.", 6000); }
      else hideToast();
    } catch {
      toast("Couldn't load places — try again");
    } finally { clearTimeout(slow); }
  }

  // ------------------------------------------------------------------ mode
  document.querySelectorAll(".seg button").forEach((b) =>
    b.addEventListener("click", () => {
      state.mode = b.dataset.mode;
      document.querySelectorAll(".seg button").forEach((x) => x.setAttribute("aria-checked", x === b));
      for (const pin of pins.values()) paintPin(pin);
      redraw();
    })
  );

  // ------------------------------------------------------------------ geolocation
  let meMarker = null, watchId = null;
  function setMe(p) {
    state.me = { lat: p.coords.latitude, lon: p.coords.longitude, acc: p.coords.accuracy };
    if (!meMarker) {
      const el = document.createElement("div");
      el.className = "me";
      meMarker = new AdvancedMarkerElement({ map, position: ll(state.me.lat, state.me.lon), content: el, zIndex: 5 });
    } else meMarker.position = ll(state.me.lat, state.me.lon);
    updateCta();
  }
  // Keep the position fresh so the speed-test CTA appears/disappears as the user walks in or out of range.
  // No timeout: a stationary device may not report for a while, and a TIMEOUT must not kill the watch.
  function trackMe() {
    if (!navigator.geolocation || watchId !== null) return;
    watchId = navigator.geolocation.watchPosition(setMe, (err) => {
      if (err.code === err.PERMISSION_DENIED) { navigator.geolocation.clearWatch(watchId); watchId = null; }
    }, { enableHighAccuracy: true, maximumAge: 15000 });
  }
  function locate(explicit) {
    if (!navigator.geolocation) return ipFallback();
    navigator.geolocation.getCurrentPosition(
      (p) => {
        setMe(p);
        trackMe();
        flyTo(state.me.lat, state.me.lon, Math.max(zoom(), 16));
      },
      () => (explicit ? toast("Location permission denied — search for a place instead") : ipFallback()),
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
    );
  }
  async function ipFallback() {
    try {
      const r = await (await fetch("/api/whereami")).json();
      if (r.lat != null && !state.me) { map.setCenter(ll(r.lat, r.lon)); map.setZoom(14); }
    } catch {}
  }
  $("#locate").addEventListener("click", () => locate(true));

  // ------------------------------------------------------------------ search
  // Google Places Autocomplete (New). One session token per search; picking a result ends the session with a
  // single details call, so the keystrokes themselves aren't billed per request.
  const q = $("#q"), results = $("#results");
  let sessionToken = null, preds = [], seq = 0;
  const suggest = debounce(async () => {
    const term = q.value.trim(), my = ++seq;
    if (term.length < 2) { results.hidden = true; preds = []; return; }
    sessionToken ||= new AutocompleteSessionToken();
    try {
      const { suggestions } = await AutocompleteSuggestion.fetchAutocompleteSuggestions({
        input: term, sessionToken, includedRegionCodes: ["in"], locationBias: map.getBounds() || map.getCenter(), language: "en",
      });
      if (my !== seq) return;
      preds = suggestions.map((x) => x.placePrediction).filter(Boolean);
      results.innerHTML = preds.length
        ? preds.map((x, i) => `<li><button type="button" data-i="${i}">${ic("pin")}<b>${esc(x.mainText?.text || x.text.text)}</b>${x.secondaryText ? `<span class="sub">${esc(x.secondaryText.text)}</span>` : ""}</button></li>`).join("")
        : `<li class="none"><b>No matches</b><span>Try a neighbourhood or city name, like Indiranagar or Pune.</span></li>`;
      results.hidden = false;
    } catch { if (my === seq) toast("Search failed"); }
  }, 250);
  q.addEventListener("input", suggest);
  q.addEventListener("focus", () => { if (preds.length) results.hidden = false; });
  results.addEventListener("click", (e) => { const b = e.target.closest("button[data-i]"); if (b) pick(preds[+b.dataset.i]); });
  $("#search").addEventListener("submit", (e) => {
    e.preventDefault();
    if (preds.length && !results.hidden) pick(preds[0]); else suggest();
  });
  async function pick(pred) {
    results.hidden = true;
    q.value = pred.mainText?.text || pred.text.text;
    q.blur();
    const place = pred.toPlace(); // carries the session token: its details call closes the session
    sessionToken = null; preds = [];
    const types = pred.types || [];
    if (types.includes("establishment") || types.includes("point_of_interest")) return openPlace(`g${pred.placeId}`, false, place);
    try {
      await place.fetchFields({ fields: ["location", "viewport"] });
      if (isMobile() && sidebar.classList.contains("open")) closePanel();
      if (place.viewport) {
        map.fitBounds(place.viewport, 40);
        google.maps.event.addListenerOnce(map, "idle", () => { if (zoom() > 15.5) map.setZoom(15.5); });
      } else flyTo(place.location.lat(), place.location.lng(), 15);
    } catch { toast("Search failed"); }
  }
  document.addEventListener("click", (e) => { if (!e.target.closest("#search")) results.hidden = true; });

  // ------------------------------------------------------------------ panel / mobile sheet
  const sidebar = $("#sidebar"), panel = $("#panel"), scroller = $("#sheet-scroll");
  const isMobile = () => matchMedia("(max-width: 760px)").matches;
  const sheetHalfPx = () => Math.round(innerHeight * 0.54);
  // Mobile: an open place is a half-height sheet over the map; pull it up (or scroll it) and it becomes a
  // full-screen page with the logo and search on top. "Map" (or a pull down at the top) brings it back.
  function setSheet(full) {
    document.body.classList.toggle("sheet-full", !!full);
    if (!full) scroller.scrollTop = 0;
  }
  $("#back").addEventListener("click", () => setSheet(false));
  (function sheetGestures() {
    let y0 = null, dy = 0, fromScroll = false, top0 = 0;
    const start = (y, scroll) => { if (!isMobile() || !sidebar.classList.contains("open")) return; y0 = y; dy = 0; fromScroll = scroll; top0 = sidebar.getBoundingClientRect().top; };
    const move = (y, ev) => {
      if (y0 == null) return;
      dy = y - y0;
      const full = document.body.classList.contains("sheet-full");
      if (fromScroll && full && (scroller.scrollTop > 0 || dy < 0)) { y0 = null; return; } // normal scrolling
      if (!full && dy < 0 && ev.cancelable) ev.preventDefault();
      sidebar.classList.add("dragging");
      sidebar.style.top = `${Math.max(0, top0 + dy)}px`;
    };
    const end = () => {
      if (y0 == null) return;
      sidebar.classList.remove("dragging"); sidebar.style.top = "";
      const full = document.body.classList.contains("sheet-full");
      if (!full && dy < -40) setSheet(true);
      else if (full && dy > 80) setSheet(false);
      else if (!full && dy > 120) closePanel();
      y0 = null;
    };
    const grab = $("#grab");
    grab.addEventListener("pointerdown", (e) => { start(e.clientY, false); grab.setPointerCapture(e.pointerId); });
    grab.addEventListener("pointermove", (e) => move(e.clientY, e));
    grab.addEventListener("pointerup", end);
    grab.addEventListener("pointercancel", end);
    scroller.addEventListener("touchstart", (e) => start(e.touches[0].clientY, true), { passive: true });
    scroller.addEventListener("touchmove", (e) => move(e.touches[0].clientY, e), { passive: false });
    scroller.addEventListener("touchend", end);
  })();

  // An open place gets its own history entry, so the phone's Back gesture returns to the map.
  // Switching places while open replaces that entry.
  function markOpen(hash) {
    if (sidebar.classList.contains("open") && history.state && history.state.panel) history.replaceState({ panel: 1 }, "", hash);
    else history.pushState({ panel: 1 }, "", hash);
  }
  addEventListener("popstate", () => { if (sidebar.classList.contains("open")) closePanel(true); });
  // The "Test internet speed" action is pinned to the bottom of the panel, and only shown while a place is
  // open AND the device is within its test radius — you have to be there to test it.
  const TEST_RADIUS_M = 50;
  const distM = (a, b) => {
    const R = 6371000, rad = Math.PI / 180;
    const dLat = (b.lat - a.lat) * rad, dLon = (b.lon - a.lon) * rad;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * rad) * Math.cos(b.lat * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(h));
  };
  const radiusFor = () => TEST_RADIUS_M; // one 50 m range for every place, apartments included (server: MAX_TEST_DISTANCE_M)
  const inRange = () => !!(state.current && state.me && distM(state.me, state.current) <= radiusFor(state.current));
  function updateCta() { document.body.classList.toggle("can-test", inRange()); redraw(); }
  function clearCta() { state.current = null; updateCta(); }
  $("#cta").addEventListener("click", () => { if (inRange()) openTest(state.current); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { if (testing) { if (!running) hideTest(); } else closePanel(); } });

  function closePanel(fromHistory = false) {
    if (!fromHistory && history.state && history.state.panel) { history.back(); return; } // popstate closes it
    if (running) return;
    hideTest();
    sidebar.classList.remove("open");
    sidebar.setAttribute("aria-hidden", "true");
    setSheet(false);
    clearCta();
    select(null);
    history.replaceState(null, "", location.pathname);
  }

  function parseHash() { const m = /^#p=([nwr]\d+|s[0-9a-f]{11}|g[A-Za-z0-9_-]{10,300})$/.exec(location.hash); return m ? m[1] : null; }

  // id: "n…/w…/r…" (OSM), "s…" (visitor-named), "g…" (Google place_id). gPlace: a Places API Place object
  // from search, so its details call ends the autocomplete session.
  async function openPlace(id, fresh = false, gPlace = null) {
    if (running) return;
    hideTest();
    clearCta();
    select(id);
    markOpen(`#p=${id}`);
    sidebar.classList.add("open");
    sidebar.setAttribute("aria-hidden", "false");
    panel.innerHTML = `<div class="skeleton"><div></div><div></div><div></div><div></div></div>`;
    try {
      let info = null;
      const qs = new URLSearchParams();
      if (id[0] === "g") {
        try { info = await googleInfo(id.slice(1), gPlace); } catch { info = null; }
        if (state.selected !== id) return;
        if (info && info.lat != null) {
          const k = tileKey(info.lat, info.lon); // its tile may be off-screen (search): load it for the twin check
          if (!tiles.has(k)) { try { await fetchTile(k); publishFeatures(); } catch {} }
          if (state.selected !== id) return;
        }
        const twin = info && ourTwin(info);
        if (twin && twin !== id) return openPlace(twin);
        // untested Google place: our server previews it at Google's position (used for this request only)
        if (info && info.lat != null) { qs.set("lat", info.lat.toFixed(6)); qs.set("lon", info.lon.toFixed(6)); }
      }
      if (fresh) qs.set("fresh", Date.now()); // place cards are CDN-cached for 30 s; right after your own test, skip the cache
      const r = await fetch(`/api/places/${id}${qs.size ? `?${qs}` : ""}`);
      if (!r.ok) throw new Error(r.status);
      const p = await r.json();
      if (state.selected !== id) return;
      if (p.google) Object.assign(p, info ? { name: info.name, kind: info.kind, addr: info.addr, gurl: info.url } : { name: "This place" });
      showPlace(p);
    } catch {
      panel.innerHTML = `<div class="ph"><span></span>${closeBtn()}</div><div class="state-err"><div class="badge">${ic("alert")}</div>
        <h2 style="margin:0 0 8px;font-size:22px;font-weight:600">Couldn’t load this place.</h2>
        <p class="muted">Check your connection and try again.</p>
        <button class="btn ghost wide" type="button" id="retry">Try again</button></div>`;
      panel.querySelector("#retry")?.addEventListener("click", () => openPlace(id, true));
      wireClose();
    }
  }
  const closeBtn = () => `<button class="x" type="button" data-close aria-label="Close">${ic("x")}</button>`;
  function wireClose() { panel.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => closePanel())); }

  function showPlace(p) {
    panel.innerHTML = renderPlace(p);
    wireClose();
    state.current = p;
    $("#cta-name").textContent = p.name;
    updateCta();
    if (!state.me) trackMe(); // no fix yet: ask once, the CTA appears if we turn out to be here
    panel.querySelector("#first-test")?.addEventListener("click", () => { if (inRange()) openTest(p); });
    if (!pins.has(p.id)) markSpot(p);
    reveal(p.lat, p.lon);
  }

  const fmt = (v) => (v == null ? "–" : v >= 100 ? Math.round(v) : v.toFixed(1));
  const tierIdx = (v) => TIERS.findIndex((t) => v < t.max);
  const verdict = (v) => v == null ? "" : v >= 50 ? "Great for video calls" : v >= 20 ? "Fine for calls & work" : v >= 5 ? "OK for browsing" : "Slow";
  const ago = (iso) => {
    const d = (Date.now() - Date.parse(iso)) / 1000;
    if (d < 3600) return `${Math.max(1, Math.round(d / 60))} min ago`;
    if (d < 86400) return `${Math.round(d / 3600)} h ago`;
    if (d < 86400 * 60) return `${Math.round(d / 86400)} days ago`;
    return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  };
  const dateStr = (iso) => iso ? new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "unknown";
  const quarterRange = (period) => {
    const m = /(\d{4}) Q(\d)/.exec(period || "");
    if (!m) return "";
    const months = ["Jan–Mar", "Apr–Jun", "Jul–Sep", "Oct–Dec"][+m[2] - 1];
    return `${months} ${m[1]}`;
  };

  const stat = (label, value, unit) => `<div class="stat"><span class="s-label">${label}</span><span class="s-val">${value}</span><span class="s-unit">${unit}</span></div>`;
  const rampBar = (v) => { const k = tierIdx(v); return `<div class="rampbar">${TIERS.map((t, i) => `<i class="${i <= k ? "on" : ""}" style="--c:${t.color}"></i>`).join("")}</div>`; };

  // Collapsible section (closed by default).
  const accordion = (icon, title, body) =>
    `<details class="acc"><summary>${ic(icon)}${title}${ic("chev", "chev")}</summary><div class="acc-body">${body}</div></details>`;

  function ooklaBlock(o, title, icon) {
    if (!o) return "";
    const v = o.tile || o.area;
    const scope = o.tile ? "this ~600 m tile" : o.area ? `${o.area.tiles} nearby tiles (~1.8 km)` : "";
    if (!v) return accordion(icon, title, `<p class="muted">No published tests in this area for ${esc(o.period || "the latest quarter")}.</p>`);
    return accordion(icon, title,
      `<div class="stats">${stat("Download", fmt(v.down_mbps), "Mbps")}${stat("Upload", fmt(v.up_mbps), "Mbps")}</div>
      <p class="meta">Tested ${quarterRange(o.period)} · Published ${dateStr(o.published)}. Average of ${v.tests.toLocaleString()} tests from ${v.devices.toLocaleString()} devices in ${scope}, all carriers combined.</p>`);
  }

  const monthRange = (start, endMonth) => {
    if (!start || !endMonth) return "";
    const f = (iso, y) => new Date(iso + "T00:00:00Z").toLocaleDateString(undefined, { month: "short", ...(y ? { year: "numeric" } : {}), timeZone: "UTC" });
    return `${f(start)}–${f(endMonth, true)}`;
  };
  // Carrier logos (web/carriers/*.svg). A network without a logo shows just its name (no placeholder).
  const CARRIER_LOGO = { Jio: "jio", Airtel: "airtel", Vi: "vi", BSNL: "bsnl" };
  const clogo = (name, cls = "") => {
    const k = CARRIER_LOGO[name];
    return k ? `<img class="clogo ${cls}" src="/carriers/${k}.svg?v=1" alt="" width="18" height="18" decoding="async">` : "";
  };

  // Hover/tap tooltip (CSS, see [data-tip] in style.css). tabindex makes it reachable by tap on touch screens.
  const tip = (text, html, cls = "") => `<span class="tip ${cls}" tabindex="0" data-tip="${esc(text)}">${html}</span>`;
  const testedBadge = (v) => tip(`${ago(v.latest)} · ${v.tests} test${v.tests > 1 ? "s" : ""} here`, "Tested", "src here");
  // Broadband ISPs that are also mobile carriers (Airtel/Jio/Vi/BSNL) are shown as just "Broadband" with no
  // logo, so their WiFi rows can't be mistaken for mobile-data rows. Other ISPs show their name.
  const ispLogo = (isp) => (CARRIER_LOGO[isp] ? null : isp);
  const NET_HEAD = `<thead><tr><th>__NAME__</th><th class="num">${tip("Download (Mbps)", "↓ Mbps")}</th><th class="num">${tip("Upload (Mbps)", "↑ Mbps")}</th><th class="srccell">Source</th></tr></thead>`;
  const netRow = (logoName, label, v, src, area) => `<tr class="${area ? "area" : ""}">
        <td><span class="cname">${logoName === null ? "" : clogo(logoName)}<span>${esc(label)}</span></span></td>
        <td class="num down">${fmt(v.down_mbps)}</td>
        <td class="num">${fmt(v.up_mbps)}</td>
        <td class="srccell">${src}</td></tr>`;

  function mobileBlock(carriers) {
    const rows = carriers.map((c) => {
      const v = c.source === "here" ? c.here : c.city;
      const src = c.source === "here"
        ? testedBadge(v)
        : v.tip
        ? tip(v.tip, "Average", "src city")
        : tip(`${v.tests.toLocaleString()} M-Lab tests, IP-located to ${v.city}, ${monthRange(v.period_start, v.period_end_month)}`, "Average", "src city");
      return netRow(c.carrier, c.carrier, v, src, c.source !== "here");
    }).join("");
    return `<section class="sec">
      <h3 class="sec-h">${ic("phone")}Mobile data speeds</h3>
      ${carriers.length ? `<table class="carriers">${NET_HEAD.replace("__NAME__", "Carrier")}<tbody>${rows}</tbody></table>`
        : `<p class="muted">No per-carrier data for this area yet.</p>`}
    </section>`;
  }

  // Shown only once someone has tested this place's WiFi; same layout as the mobile table.
  const ispName = (isp, many) => (ispLogo(isp) === null || !isp ? (many && isp ? `${isp} broadband` : "Broadband") : isp);
  function wifiBlock(p) {
    const rows = p.wifi_isps?.length ? p.wifi_isps : p.wifi ? [{ isp: p.wifi.isp, here: p.wifi }] : [];
    if (!rows.length) return "";
    const many = rows.length > 1;
    return `<section class="sec">
      <h3 class="sec-h">${ic("wifi")}${many ? `WiFi speed · ${rows.length} providers` : "WiFi speed"}</h3>
      <table class="carriers">${NET_HEAD.replace("__NAME__", "Provider")}<tbody>${rows.map((r) => netRow(ispLogo(r.isp) === null ? null : "", ispName(r.isp, many), r.here, testedBadge(r.here))).join("")}</tbody></table>
    </section>`;
  }

  // Top section: the fastest connection actually measured at this place (WiFi or any carrier).
  // Untested places get a "be the first" call to action instead of published averages.
  const venueNoun = (k) => k === "cafe" ? "cafe" : k === "coworking" ? "coworking space" : k === "restaurant" ? "restaurant" : ["office", "company"].includes(k) ? "office" : "business";
  function fastestBlock(p) {
    const cands = [];
    const isps = p.wifi_isps?.length ? p.wifi_isps : p.wifi ? [{ isp: p.wifi.isp, here: p.wifi }] : [];
    for (const r of isps) cands.push({ v: r.here, label: ispName(r.isp, true), wifi: true });
    for (const c of p.carriers || []) if (c.source === "here") cands.push({ v: c.here, label: `${c.carrier} mobile data`, logo: c.carrier });
    if (!cands.length) return `<section class="sec">
        <h3 class="sec-h">${ic("flag")}No one has tested here yet</h3>
        <p class="muted">${p.residential
          ? `Test your WiFi and mobile data at ${esc(p.name)} — it helps neighbours and anyone moving in compare providers.`
          : `Run the first speed test and put ${esc(p.name)} on the map for everyone looking for fast internet.`}</p>
        <button class="btn primary wide in-range-only" id="first-test" type="button">${SPEED_IC}Run the first speed test</button>
        <p class="range-note out-range-only">${ic("pin")}You need to be here (within ${radiusFor(p)} m) to run a test.</p>
      </section>`;
    const best = cands.reduce((a, b) => (b.v.down_mbps > a.v.down_mbps ? b : a));
    const w = best.v;
    const advice = p.residential
      ? (best.wifi ? `Fastest broadband here: ${esc(best.label)}` : `Fastest here: ${esc(best.label)}`)
      : best.wifi ? `Use WiFi of the ${venueNoun(p.kind)} here` : "Use mobile hotspot here";
    return `<section class="sec">
        <h3 class="sec-h">${SPEED_MASK}Fastest here <span class="right src here">${ic("shield")}Tested here</span></h3>
        <p class="advice">${advice}${best.wifi ? "" : clogo(best.logo)}</p>
        <div class="stats">${stat("Download", fmt(w.down_mbps), "Mbps")}${stat("Upload", fmt(w.up_mbps), "Mbps")}</div>
        ${rampBar(w.down_mbps)}
        <p class="verdict">${verdict(w.down_mbps)}</p>
        <p class="meta">Fastest of ${cands.length} network${cands.length > 1 ? "s" : ""} ${tip(`Last tested ${ago(w.latest)}`, "tested at this place", "dotted")} · ${ago(w.latest)}</p>
      </section>`;
  }

  const kindLabel = (k) => ({ coworking: "Coworking", cafe: "Cafe", restaurant: "Restaurant", office: "Office", apartments: "Apartments" })[k]
    || String(k || "place").replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

  function renderPlace(p) {
    const t = p.tags || {};
    const addr = p.addr || [t["addr:housenumber"], t["addr:street"], t["addr:suburb"], t["addr:city"]].filter(Boolean).join(", ");
    const web = t.website || t["contact:website"];
    const osmWifi = t.internet_access;
    const wifiTag = osmWifi ? (["wlan", "yes", "wifi"].includes(osmWifi) ? `WiFi listed${t["internet_access:fee"] === "no" ? " · free" : t["internet_access:fee"] === "yes" ? " · paid" : ""}` : osmWifi === "no" ? "No WiFi listed" : `Internet: ${esc(osmWifi)}`) : null;
    const fixed = p.ookla?.fixed;

    return `
      <div class="ph">
        <h2>${esc(p.name)}</h2>${closeBtn()}
        <p class="meta-line">${esc(kindLabel(p.kind))}${addr ? ` · ${esc(addr)}` : ""}</p>
        <p class="chips">
          ${wifiTag ? `<span class="chip">${ic(osmWifi === "no" ? "wifiOff" : "wifi")}${wifiTag}</span>` : ""}
          ${t.opening_hours ? `<span class="chip" title="Opening hours">${ic("clock")}${esc(t.opening_hours)}</span>` : ""}
          ${web ? `<a class="chip link" href="${esc(web)}" target="_blank" rel="noopener nofollow">Website${ic("ext")}</a>` : ""}
          <a class="chip link" href="${esc(p.gurl || `https://www.google.com/maps/search/?api=1&query=${p.lat},${p.lon}`)}" target="_blank" rel="noopener">${p.gurl ? "Google Maps" : "Directions"}${ic("ext")}</a>
        </p>
      </div>

      ${fastestBlock(p)}

      ${mobileBlock(p.carriers || [])}

      ${wifiBlock(p)}

      ${ooklaBlock(fixed, "Home/office broadband nearby", "home")}

      ${p.recent_tests?.length ? accordion("clock", "Recent tests", `<ul class="recent">
        ${p.recent_tests.map((x) => `<li><span>${x.network === "wifi" ? ic("wifi") + "WiFi" : clogo(x.carrier, "sm") + esc(x.carrier)}</span><span class="d">↓ ${fmt(x.down_mbps)}</span><span class="u">↑ ${fmt(x.up_mbps)}</span><span class="when">${ago(x.at)}</span></li>`).join("")}
      </ul>`) : ""}

      <p class="foot">${p.google
        ? `Place details from Google Maps. Speeds are from visitors who tested here.`
        : p.osm_url
        ? `Place data © <a href="${p.osm_url}" target="_blank" rel="noopener">OpenStreetMap</a> contributors. Wrong or missing? <a href="${p.osm_url}" target="_blank" rel="noopener">Edit on OSM</a>.`
        : `Named by a visitor — not on OpenStreetMap yet. <a href="https://www.openstreetmap.org/#map=19/${p.lat}/${p.lon}" target="_blank" rel="noopener">Add it on OSM</a>.`}
        Area speeds: <a href="https://www.ookla.com/ookla-for-good/open-data" target="_blank" rel="noopener">Speedtest® by Ookla® Open Data</a> (CC BY-NC-SA 4.0). <a href="/about.html">About & sources</a></p>
    `;
  }

  // ------------------------------------------------------------------ speed test (runs inside the panel)
  const tv = $("#testview"), status = $("#test-status"), runBtn = $("#test-run"), cancelBtn = $("#test-cancel");
  let testPlace = null, running = false, testing = false;
  const netVal = () => tv.querySelector("input[name=network]:checked").value;
  function setStatus(kind, html) {
    status.className = `tstatus ${kind}`;
    const lead = kind === "run" ? '<span class="spin"></span>' : kind === "ok" ? ic("check") : kind === "bad" ? ic("alert") : ic("info");
    status.innerHTML = `${lead}<span>${html}</span>`;
    $("#test-receipt").hidden = !(kind === "run" || kind === "ok");
  }
  function gauge(id, v, st) {
    const el = $(`#g-${id}`); el.textContent = v; el.classList.toggle("idle", st === "idle");
    $(`#gg-${id}`).classList.toggle("live", st === "live");
  }
  function setRun(label, disabled, icon) { runBtn.disabled = disabled; runBtn.innerHTML = `${icon ? SPEED_IC : ""}${label}`; }
  function openTest(p, preset) {
    testPlace = p;
    testing = true;
    document.body.classList.add("testing");
    $("#wifi-label").textContent = p.residential ? "This building's WiFi / broadband" : "This place's WiFi";
    $("#test-place").textContent = inRange() ? `${p.name} · you’re inside the ${radiusFor(p)} m range` : p.name;
    ["ping", "down", "up"].forEach((k) => gauge(k, "–", "idle"));
    setRun("Start test", false, true);
    cancelBtn.disabled = false; cancelBtn.hidden = false;
    $("#netset").disabled = false;
    tv.querySelector(".actions").classList.remove("one");
    const c = navigator.connection;
    const net = c && c.type ? (c.type === "cellular" ? "cellular" : "wifi") : preset || "wifi";
    tv.querySelector(`input[value="${net}"]`).checked = true;
    const setNote = () => setStatus("", dataNote(netVal()));
    setNote();
    tv.querySelectorAll("input[name=network]").forEach((r) => (r.onchange = () => { if (!running) setNote(); }));
    panel.hidden = true; tv.hidden = false; scroller.scrollTop = 0;
  }
  function hideTest() {
    if (!testing) return;
    testing = false;
    document.body.classList.remove("testing");
    tv.hidden = true; panel.hidden = false;
  }
  // Mirrors the transfer caps in speedtest.js (mobile data / Data Saver get the frugal profile).
  function dataNote(network) {
    const frugal = network === "cellular" || navigator.connection?.saveData;
    return (frugal
      ? "Takes about 20 s and uses up to ~100 MB of data (less on slower connections)."
      : "Takes about 25 s and uses data in proportion to your speed (about 50 MB at 20 Mbps, up to ~550 MB on very fast links).")
      + " Your location is checked once to confirm you’re at the place and is not stored.";
  }
  cancelBtn.addEventListener("click", () => { if (!running) hideTest(); });
  $("#test-back").addEventListener("click", () => { if (!running) hideTest(); });
  $("#test-x").addEventListener("click", () => { if (!running) closePanel(); });

  function getPosition() {
    return new Promise((res, rej) =>
      navigator.geolocation
        ? navigator.geolocation.getCurrentPosition(res, rej, { enableHighAccuracy: true, timeout: 15000, maximumAge: 30000 })
        : rej(new Error("no geolocation"))
    );
  }

  // Anonymous beacon so we can see which speed endpoint ran tests and how much data they used.
  function beacon(result, outcome, network) {
    const m = result?.meta;
    if (!m || !navigator.sendBeacon || result._beaconed) return;
    result._beaconed = true;
    const body = { endpoint: m.endpoint, own_failed: m.own_failed, outcome, network, down_mb: m.down_mb, up_mb: m.up_mb };
    navigator.sendBeacon("/api/telemetry", new Blob([JSON.stringify(body)], { type: "application/json" }));
  }

  runBtn.addEventListener("click", async () => {
    if (running || !testPlace) return;
    if (runBtn.dataset.done) { delete runBtn.dataset.done; hideTest(); openPlace(testPlace.id, true); refreshFeature(testPlace); return; }
    let result = null;
    running = true; setRun("Testing…", true); cancelBtn.disabled = true; $("#netset").disabled = true;
    try {
      setStatus("run", "Checking you’re at the place…");
      let pos;
      try { pos = await getPosition(); } catch { throw new Error("Location is needed to confirm you’re at the place. Allow location for isitfast.cloud, then try again."); }
      const here = { lat: pos.coords.latitude, lon: pos.coords.longitude, accuracy_m: pos.coords.accuracy };
      // An untested Google place has no stored location yet: send where Google puts it (checked against
      // your position, not stored — the place is saved at your position).
      const spot = testPlace.google && testPlace.preview ? { lat: testPlace.lat, lon: testPlace.lon } : null;
      const sr = await fetch(`/api/places/${testPlace.id}/test-start`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ ...here, spot }),
      });
      const start = await sr.json().catch(() => ({}));
      if (!sr.ok) throw new Error(typeof start.detail === "string" ? start.detail : "Couldn't start the test.");
      testPlace.id = start.id; // a previewed place is stored now that someone is testing it
      testPlace.preview = false;
      let phase = "ping";
      result = await SpeedTest.run({
        network: netVal(),
        sid: start.sid,
        onPhase: (s) => { setStatus("run", esc(s)); if (/download/i.test(s)) phase = "down"; if (/upload/i.test(s)) phase = "up"; },
        onPing: (v) => gauge("ping", Math.round(v), "live"),
        onDown: (v) => { gauge("ping", $("#g-ping").textContent, "done"); gauge("down", fmt(v), "live"); },
        onUp: (v) => { gauge("down", $("#g-down").textContent, "done"); gauge("up", fmt(v), "live"); },
      });
      gauge("ping", Math.round(result.ping_ms), "done");
      gauge("down", fmt(result.down_mbps), "done");
      gauge("up", fmt(result.up_mbps), "done");
      setStatus("run", "Saving…");
      const network = netVal();
      const r = await fetch(`/api/places/${testPlace.id}/tests`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ network, ...result, meta: undefined, ...here, token: start.token }),
      });
      const body = await r.json().catch(() => ({}));
      beacon(result, r.ok ? "saved" : "rejected", network);
      if (!r.ok) throw new Error(body.detail && typeof body.detail === "string" ? body.detail : "Couldn't save the result.");
      setStatus("ok", body.network === "wifi" && network === "cellular"
        ? `Saved as WiFi — ${clogo(body.carrier, "sm")}${esc(body.carrier)} is a broadband network, not mobile data. Thanks!`
        : `Saved. Thanks! Network: ${clogo(body.carrier, "sm")}${esc(body.carrier)}.`);
      cancelBtn.hidden = true; tv.querySelector(".actions").classList.add("one");
      runBtn.dataset.done = "1"; setRun("Done", false);
    } catch (e) {
      beacon(result, "failed", netVal());
      setStatus("bad", esc(e.message || "Test failed."));
      ["ping", "down", "up"].forEach((k) => { const el = $(`#g-${k}`); gauge(k, el.textContent, el.textContent === "–" ? "idle" : "done"); });
      setRun("Try again", false, true); cancelBtn.disabled = false; $("#netset").disabled = false;
    } finally { running = false; }
  });

  function refreshFeature(p) { loadPlaces(tileKey(p.lat, p.lon)); }

  // ------------------------------------------------------------------ toast
  let toastT;
  function toast(msg, ms = 3500, spin = false) {
    const t = $("#toast");
    t.innerHTML = `${spin ? '<span class="spin"></span>' : ""}<span>${esc(msg)}</span>`; t.hidden = false;
    clearTimeout(toastT);
    if (ms) toastT = setTimeout(hideToast, ms);
  }
  function hideToast() { $("#toast").hidden = true; }

  // ------------------------------------------------------------------ boot
  const fromHash = parseHash();
  if (fromHash) { history.replaceState(null, "", location.pathname); openPlace(fromHash); } // Back → the map, not the same place
  else locate(false);
})();
