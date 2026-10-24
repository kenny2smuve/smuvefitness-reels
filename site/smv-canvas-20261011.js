/* SMUVE fast figure renderer (Oct 11 2026).
   The figure engine keeps building and animating its SVG exactly as before; this module paints that SVG
   onto a <canvas> each frame instead of letting the browser re-run style, layout and paint for hundreds
   of SVG nodes. Same shapes, gradients, glows and shadows; far less work per frame on phones.
   Static layers (gym or room backdrop, still equipment) are painted once and reused.
   Opt out for comparison with ?svg=1. Any error puts that figure back on plain SVG rendering. */
(function () {
  "use strict";
  if (window.SMVCanvas) return;
  var off = false, forced = false;
  try { off = /[?&]svg=1(?:&|$)/.test(location.search) || localStorage.getItem("smv-svg") === "1"; forced = /[?&](?:cv=1|render=canvas)(?:&|$)/.test(location.search) ? "canvas" : /[?&]render=svg(?:&|$)/.test(location.search) ? "svg" : false; } catch (e) {}
  var probe = document.createElement("canvas"), pctx = probe.getContext && probe.getContext("2d");
  if (!pctx || typeof Path2D !== "function") off = true;
  var CAN_FILTER = false;
  try { if (pctx && "filter" in pctx) { pctx.filter = "blur(2px)"; CAN_FILTER = pctx.filter === "blur(2px)"; pctx.filter = "none"; } } catch (e) {}
  var MAXDPR = 2.5, DIRECT = true;   /* 2.5x is visually identical to 3x for these soft illustrations and 30% fewer pixels */
  var POOL = [];
  var SHAPE = { path: 1, rect: 1, circle: 1, ellipse: 1, line: 1 };
  var GEOM = { d: 1, x: 1, y: 1, width: 1, height: 1, rx: 1, ry: 1, cx: 1, cy: 1, r: 1, x1: 1, y1: 1, x2: 1, y2: 1 };
  var SIG = /^smv\d+bl2$/;
  var css = document.createElement("style");
  css.textContent = "svg[data-smvcv]>g{display:none!important}canvas.smv-cv{pointer-events:none;display:block;position:absolute;margin:0;padding:0;border:0}canvas.smv-cv[hidden]{display:none!important}svg[data-smvsvg]{will-change:transform}";
  (document.head || document.documentElement).appendChild(css);

  /* ---------- small math ---------- */
  function mul(a, b) { /* a then b  =>  a x b (2D affine [a b c d e f]) */
    return [a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1], a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3], a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5]];
  }
  var I = [1, 0, 0, 1, 0, 0];
  function parseTransform(s) {
    if (!s) return null;
    var m = I, re = /(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)/g, r;
    while ((r = re.exec(s))) {
      var a = r[2].trim().split(/[\s,]+/).map(Number), t;
      if (r[1] === "translate") t = [1, 0, 0, 1, a[0] || 0, a[1] || 0];
      else if (r[1] === "scale") t = [a[0], 0, 0, a.length > 1 ? a[1] : a[0], 0, 0];
      else if (r[1] === "rotate") {
        var k = (a[0] || 0) * Math.PI / 180, c = Math.cos(k), s2 = Math.sin(k);
        t = [c, s2, -s2, c, 0, 0];
        if (a.length > 2) t = mul(mul([1, 0, 0, 1, a[1], a[2]], t), [1, 0, 0, 1, -a[1], -a[2]]);
      }
      else if (r[1] === "matrix") t = a.slice(0, 6);
      else if (r[1] === "skewX") t = [1, 0, Math.tan(a[0] * Math.PI / 180), 1, 0, 0];
      else t = [1, Math.tan(a[0] * Math.PI / 180), 0, 1, 0, 0];
      m = mul(m, t);
    }
    return m;
  }
  function apply(m, x, y) { return [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]]; }
  function boxT(m, b) { /* device-space AABB of a local bbox [x0,y0,x1,y1] */
    var p = [apply(m, b[0], b[1]), apply(m, b[2], b[1]), apply(m, b[0], b[3]), apply(m, b[2], b[3])];
    return [Math.min(p[0][0], p[1][0], p[2][0], p[3][0]), Math.min(p[0][1], p[1][1], p[2][1], p[3][1]), Math.max(p[0][0], p[1][0], p[2][0], p[3][0]), Math.max(p[0][1], p[1][1], p[2][1], p[3][1])];
  }
  function scaleOf(m) { return Math.sqrt(Math.abs(m[0] * m[3] - m[1] * m[2])); }
  function num(v, d) { var n = parseFloat(v); return isNaN(n) ? d : n; }

  /* ---------- tight bounding box of SVG path data (curve extrema, not control points) ---------- */
  function pathBox(d) {
    var tok = d.match(/[a-zA-Z]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?/g) || [];
    var pend = null, i = 0, cmd = "", x = 0, y = 0, sx = 0, sy = 0, cx = 0, cy = 0, qx = 0, qy = 0, b = [Infinity, Infinity, -Infinity, -Infinity], prev = "";
    function P(px, py) { if (px < b[0]) b[0] = px; if (py < b[1]) b[1] = py; if (px > b[2]) b[2] = px; if (py > b[3]) b[3] = py; }
    function n() { return parseFloat(tok[i++]); }
    function cubic(x0, y0, x1, y1, x2, y2, x3, y3) {
      P(x3, y3);
      [[x0, x1, x2, x3], [y0, y1, y2, y3]].forEach(function (c, ax) {
        var a = -c[0] + 3 * c[1] - 3 * c[2] + c[3], bb = 2 * (c[0] - 2 * c[1] + c[2]), cc = c[1] - c[0], ts = [];
        if (Math.abs(a) < 1e-12) { if (Math.abs(bb) > 1e-12) ts.push(-cc / bb); }
        else { var disc = bb * bb - 4 * a * cc; if (disc >= 0) { var sq = Math.sqrt(disc); ts.push((-bb + sq) / (2 * a), (-bb - sq) / (2 * a)); } }
        ts.forEach(function (t) { if (t > 0 && t < 1) { var mt = 1 - t, v = mt * mt * mt * c[0] + 3 * mt * mt * t * c[1] + 3 * mt * t * t * c[2] + t * t * t * c[3]; if (ax) P(cubicAt([x0, x1, x2, x3], t), v); else P(v, cubicAt([y0, y1, y2, y3], t)); } });
      });
    }
    function cubicAt(c, t) { var mt = 1 - t; return mt * mt * mt * c[0] + 3 * mt * mt * t * c[1] + 3 * mt * t * t * c[2] + t * t * t * c[3]; }
    function quad(x0, y0, x1, y1, x2, y2) { cubic(x0, y0, x0 + 2 / 3 * (x1 - x0), y0 + 2 / 3 * (y1 - y0), x2 + 2 / 3 * (x1 - x2), y2 + 2 / 3 * (y1 - y2), x2, y2); }
    while (i < tok.length) {
      if (/[a-zA-Z]/.test(tok[i])) cmd = tok[i++];
      else if (!cmd) { i++; continue; }
      var rel = cmd === cmd.toLowerCase(), C = cmd.toUpperCase(), ox = rel ? x : 0, oy = rel ? y : 0;
      if (C === "Z") { x = sx; y = sy; prev = "Z"; continue; }
      if (C === "M") { x = ox + n(); y = oy + n(); sx = x; sy = y; pend = [x, y]; cmd = rel ? "l" : "L"; prev = "M"; continue; }
      if (pend) { P(pend[0], pend[1]); pend = null; }
      if (C === "L") { x = ox + n(); y = oy + n(); P(x, y); }
      else if (C === "H") { x = ox + n(); P(x, y); }
      else if (C === "V") { y = oy + n(); P(x, y); }
      else if (C === "C") { var x1 = ox + n(), y1 = oy + n(), x2 = ox + n(), y2 = oy + n(), x3 = ox + n(), y3 = oy + n(); cubic(x, y, x1, y1, x2, y2, x3, y3); cx = x2; cy = y2; x = x3; y = y3; }
      else if (C === "S") { var rx1 = prev === "C" || prev === "S" ? 2 * x - cx : x, ry1 = prev === "C" || prev === "S" ? 2 * y - cy : y, sx2 = ox + n(), sy2 = oy + n(), sx3 = ox + n(), sy3 = oy + n(); cubic(x, y, rx1, ry1, sx2, sy2, sx3, sy3); cx = sx2; cy = sy2; x = sx3; y = sy3; }
      else if (C === "Q") { var qx1 = ox + n(), qy1 = oy + n(), qx2 = ox + n(), qy2 = oy + n(); quad(x, y, qx1, qy1, qx2, qy2); qx = qx1; qy = qy1; x = qx2; y = qy2; }
      else if (C === "T") { var tx1 = prev === "Q" || prev === "T" ? 2 * x - qx : x, ty1 = prev === "Q" || prev === "T" ? 2 * y - qy : y, tx2 = ox + n(), ty2 = oy + n(); quad(x, y, tx1, ty1, tx2, ty2); qx = tx1; qy = ty1; x = tx2; y = ty2; }
      else if (C === "A") { var arx = Math.abs(n()), ary = Math.abs(n()); n(); n(); n(); var ax2 = ox + n(), ay2 = oy + n(); P(x, y); P(ax2, ay2); var R = Math.max(arx, ary); P(Math.min(x, ax2) - R, Math.min(y, ay2) - R); P(Math.max(x, ax2) + R, Math.max(y, ay2) + R); x = ax2; y = ay2; }
      else { i++; }
      prev = C;
    }
    return b[0] === Infinity ? null : b;
  }

  /* ---------- colours ---------- */
  var colorCache = {};
  function rgba(c, alpha) { /* any CSS colour + extra alpha -> rgba() string */
    var key = c + "|" + alpha; if (colorCache[key]) return colorCache[key];
    pctx.fillStyle = "#000"; pctx.fillStyle = c; var v = pctx.fillStyle, r, g, b, a = 1, m;
    if (v[0] === "#") { r = parseInt(v.slice(1, 3), 16); g = parseInt(v.slice(3, 5), 16); b = parseInt(v.slice(5, 7), 16); }
    else if ((m = v.match(/rgba?\(([^)]+)\)/))) { var p = m[1].split(/[\s,\/]+/).filter(Boolean).map(Number); r = p[0]; g = p[1]; b = p[2]; if (p.length > 3) a = p[3]; }
    else { r = g = b = 0; }
    return (colorCache[key] = "rgba(" + r + "," + g + "," + b + "," + (a * alpha) + ")");
  }
  function styleProps(s) { var o = {}; if (!s) return o; s.split(";").forEach(function (kv) { var i = kv.indexOf(":"); if (i > 0) o[kv.slice(0, i).trim()] = kv.slice(i + 1).trim(); }); return o; }
  function urlId(v) { var m = v && /url\(\s*['"]?#([^'")\s]+)/.exec(v); return m ? m[1] : null; }

  /* ---------- a mirror bound to one engine SVG ---------- */
  function Mirror(svg) {
    var self = this;
    this.svg = svg; svg.__smvcvTry = this; this.recs = new WeakMap(); this.layerVer = []; this.cacheN = 0; this.cacheVer = []; this.refs = [];
    this.byId = {}; this.pool = POOL;
    var cv = this.cv = document.createElement("canvas");
    cv.className = "smv-cv"; cv.setAttribute("aria-hidden", "true");
    svg.parentNode.insertBefore(cv, svg.nextSibling);
    this.ctx = cv.getContext("2d");
    svg.setAttribute("data-smvcv", "");
    this.mo = new MutationObserver(function (rs) { probe.active = performance.now(); kick(); if (self.svgMode || self.dead) return; try { self.records(rs); } catch (e) { self.detach(); return; } self.render(); });
    this.mo.observe(svg, { attributes: true, childList: true, subtree: true, characterData: false });
    this.ro = window.ResizeObserver ? new ResizeObserver(function () { if (self.dead) return; if (!svg.isConnected) { self.detach(); return; } try { self.place(); } catch (e) {} self.render(true); }) : null;
    if (this.ro) { this.ro.observe(svg); if (svg.parentElement) this.ro.observe(svg.parentElement); } else window.addEventListener("resize", function () { if (self.dead) return; try { self.place(); } catch (e) {} self.render(true); });
    this.frames = 0;
    /* memory: only figures on or near the screen keep a full-size bitmap (a session can hold 20+ figures) */
    var r0 = svg.getBoundingClientRect(), vh = window.innerHeight || 800, vw = window.innerWidth || 400;
    this.vis = r0.width > 0 && r0.bottom > -300 && r0.top < vh + 300 && r0.right > -300 && r0.left < vw + 300;
    if (window.IntersectionObserver) {
      this.io = new IntersectionObserver(function (es) { self.setVis(es[es.length - 1].isIntersecting); }, { rootMargin: "300px" });
      this.io.observe(svg);
    } else this.vis = true;
    this.index(); this.place(); this.render(true);
  }
  Mirror.prototype.setVis = function (v) {
    if (this.dead || v === this.vis) return;
    this.vis = v;
    if (!v) { this.cv.width = 0; this.cv.height = 0; this.freeCache(); }
    else { this.place(); this.render(true); }
  };
  Mirror.prototype.freeCache = function () { if (this.cache) { this.cache.width = 0; this.cache.height = 0; } this.cacheN = 0; this.prevVer = null; };
  Mirror.prototype.place = function () {
    if (this.dead || !this.svg.isConnected || !this.cv.parentNode) return;
    var svg = this.svg, cv = this.cv, par = cv.offsetParent || svg.parentNode, sr = svg.getBoundingClientRect(), pr = par.getBoundingClientRect ? par.getBoundingClientRect() : { left: 0, top: 0 };
    var cs = getComputedStyle(svg);
    cv.style.left = (sr.left - pr.left - (par.clientLeft || 0) + (par.scrollLeft || 0)) + "px";
    cv.style.top = (sr.top - pr.top - (par.clientTop || 0) + (par.scrollTop || 0)) + "px";
    cv.style.width = sr.width + "px"; cv.style.height = sr.height + "px";
    cv.style.zIndex = cs.zIndex === "auto" ? "" : cs.zIndex;
    var dpr = Math.min(window.devicePixelRatio || 1, MAXDPR), W = this.vis ? Math.max(1, Math.round(sr.width * dpr)) : 0, H = this.vis ? Math.max(1, Math.round(sr.height * dpr)) : 0;
    this.cssW = sr.width; this.cssH = sr.height; this.dpr = dpr;
    if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; this.cacheN = 0; }
  };
  Mirror.prototype.index = function () { /* id -> element, and the scene's top-level layers */
    var by = {}, all = this.svg.querySelectorAll("[id]");
    for (var i = 0; i < all.length; i++) by[all[i].id] = all[i];
    this.byId = by;
    var scene = null, ch = this.svg.children;
    for (i = 0; i < ch.length; i++) if (ch[i].tagName === "g") { scene = ch[i]; break; }
    this.scene = scene;
    this.layers = scene ? Array.prototype.slice.call(scene.children) : [];
    this.layerVer = this.layers.map(function () { return 0; });
    this.cacheN = 0;
  };
  Mirror.prototype.layerOf = function (n) {
    while (n && n.parentNode !== this.scene) { if (n === this.svg || !n.parentNode) return -1; n = n.parentNode; }
    return n ? this.layers.indexOf(n) : -1;
  };
  Mirror.prototype.records = function (rs) {
    var reindex = false, dirtyIds = {};
    for (var i = 0; i < rs.length; i++) {
      var r = rs[i], t = r.target;
      if (t === this.svg && r.type === "attributes") { if (r.attributeName === "data-smvcv" || r.attributeName === "data-smvsvg") continue; this.cacheN = 0; continue; }
      if (r.type === "childList") { reindex = true; }
      var ex = r.type === "attributes" && this.recs.get(t);
      if (ex && GEOM[r.attributeName] && SHAPE[ex.tag]) ex.geo = true; else this.recs.delete(t);
      if (r.type === "childList") { for (var k = 0; k < r.addedNodes.length; k++) this.recs.delete(r.addedNodes[k]); }
      /* which top-level layer changed */
      var li = this.layerOf(t);
      if (li >= 0) this.layerVer[li]++;
      else { /* defs: a gradient, stop, filter or clip changed -> every layer that used that id */
        var n = t; while (n && n !== this.svg && !n.id) n = n.parentNode;
        if (n && n.id) dirtyIds[n.id] = 1; else { this.cacheN = 0; }
        if (t.parentNode && t.parentNode.tagName && /Gradient$/.test(t.parentNode.tagName)) this.recs.delete(t.parentNode);
      }
      if (t.id) dirtyIds[t.id] = 1;
    }
    if (reindex) this.index();
    for (var id in dirtyIds) for (var L = 0; L < this.refs.length; L++) if (this.refs[L] && this.refs[L][id]) this.layerVer[L]++;
  };

  /* parsed, cached view of one element (rebuilt only when that element changes) */
  Mirror.prototype.rec = function (el) {
    var r = this.recs.get(el); if (r) { if (r.geo) this.geom(el, r); return r; }
    var tag = el.tagName, a = function (k) { return el.getAttribute(k); }, st = styleProps(a("style"));
    r = { tag: tag, tf: parseTransform(a("transform")), op: num(a("opacity"), 1), hide: a("display") === "none" || st.display === "none" || a("visibility") === "hidden" || st.visibility === "hidden",
      filter: urlId(a("filter") || st.filter), clip: urlId(a("clip-path") || st["clip-path"]), blend: st["mix-blend-mode"] || a("mix-blend-mode") || null };
    if (r.blend === "normal") r.blend = null;
    if (tag === "use") r.href = (a("href") || a("xlink:href") || "").replace(/^#/, "");
    var fill = a("fill"), stroke = a("stroke");
    r.fill = fill == null ? "#000" : fill; r.stroke = stroke == null ? "none" : stroke;
    r.fillId = urlId(r.fill); r.strokeId = urlId(r.stroke);
    r.fop = num(a("fill-opacity"), 1); r.sop = num(a("stroke-opacity"), 1);
    r.sw = num(a("stroke-width"), 1); r.cap = a("stroke-linecap") || "butt"; r.join = a("stroke-linejoin") || "miter"; r.miter = num(a("stroke-miterlimit"), 4);
    var dash = a("stroke-dasharray"); r.dash = dash && dash !== "none" ? dash.split(/[\s,]+/).map(Number).filter(function (v) { return !isNaN(v); }) : null;
    if (r.dash && r.dash.length % 2) r.dash = r.dash.concat(r.dash);
    this.geom(el, r);
    this.recs.set(el, r);
    return r;
  };
  /* path geometry (and its bbox, computed lazily) - rebuilt alone when only coordinates change */
  Mirror.prototype.geom = function (el, r) {
    var tag = r.tag, a = function (k) { return el.getAttribute(k); };
    var p = null, b = null;
    if (tag === "path") { var d = a("d") || ""; p = new Path2D(d); b = undefined; r.d = d; }
    else if (tag === "rect") {
      var x = num(a("x"), 0), y = num(a("y"), 0), w = num(a("width"), 0), h = num(a("height"), 0), rx = a("rx"), ry = a("ry");
      rx = rx == null ? (ry == null ? 0 : num(ry, 0)) : num(rx, 0); ry = ry == null ? rx : num(ry, 0);
      rx = Math.min(Math.max(rx, 0), w / 2); ry = Math.min(Math.max(ry, 0), h / 2);
      if (w > 0 && h > 0) {
        p = new Path2D();
        if (rx > 0 && ry > 0) {
          p.moveTo(x + rx, y); p.lineTo(x + w - rx, y); p.ellipse(x + w - rx, y + ry, rx, ry, 0, -Math.PI / 2, 0);
          p.lineTo(x + w, y + h - ry); p.ellipse(x + w - rx, y + h - ry, rx, ry, 0, 0, Math.PI / 2);
          p.lineTo(x + rx, y + h); p.ellipse(x + rx, y + h - ry, rx, ry, 0, Math.PI / 2, Math.PI);
          p.lineTo(x, y + ry); p.ellipse(x + rx, y + ry, rx, ry, 0, Math.PI, Math.PI * 1.5); p.closePath();
        } else p.rect(x, y, w, h);
        b = [x, y, x + w, y + h];
      }
    }
    else if (tag === "circle") { var ccx = num(a("cx"), 0), ccy = num(a("cy"), 0), cr = num(a("r"), 0); if (cr > 0) { p = new Path2D(); p.arc(ccx, ccy, cr, 0, Math.PI * 2); p.closePath(); b = [ccx - cr, ccy - cr, ccx + cr, ccy + cr]; } }
    else if (tag === "ellipse") { var ex = num(a("cx"), 0), ey = num(a("cy"), 0), erx = num(a("rx"), 0), ery = num(a("ry"), 0); if (erx > 0 && ery > 0) { p = new Path2D(); p.ellipse(ex, ey, erx, ery, 0, 0, Math.PI * 2); p.closePath(); b = [ex - erx, ey - ery, ex + erx, ey + ery]; } }
    else if (tag === "line") { var x1 = num(a("x1"), 0), y1 = num(a("y1"), 0), x2 = num(a("x2"), 0), y2 = num(a("y2"), 0); p = new Path2D(); p.moveTo(x1, y1); p.lineTo(x2, y2); b = [Math.min(x1, x2), Math.min(y1, y2), Math.max(x1, x2), Math.max(y1, y2)]; r.fill = "none"; }
    r.path = p; r.box = b; r.geo = false;
  };
  function bx(r) { if (r.box === undefined) r.box = r.d != null ? pathBox(r.d) : null; return r.box; }

  /* ---------- paint servers ---------- */
  Mirror.prototype.grad = function (id, ctx) {
    var g = this.byId[id]; if (!g) return null;
    var r = this.recs.get(g);
    if (!r) {
      var stops = [], ch = g.children, user = g.getAttribute("gradientUnits") === "userSpaceOnUse";
      for (var i = 0; i < ch.length; i++) if (ch[i].tagName === "stop") {
        var s = ch[i], st = styleProps(s.getAttribute("style")), off = s.getAttribute("offset") || "0";
        off = /%$/.test(off) ? parseFloat(off) / 100 : num(off, 0);
        var col = st["stop-color"] || s.getAttribute("stop-color") || "#000", op = num(st["stop-opacity"] != null ? st["stop-opacity"] : s.getAttribute("stop-opacity"), 1);
        stops.push([Math.min(1, Math.max(off, stops.length ? stops[stops.length - 1][0] : 0)), rgba(col, op)]);
      }
      var A = function (k, d) { var v = g.getAttribute(k); if (v == null) return d; return /%$/.test(v) ? parseFloat(v) / 100 * (user ? (k === "x1" || k === "x2" || k === "cx" || k === "fx" ? 100 : 100) : 1) : num(v, d); };
      r = { linear: g.tagName === "linearGradient", user: user, stops: stops,
        x1: A("x1", 0), y1: A("y1", 0), x2: A("x2", 1), y2: A("y2", 0), cx: A("cx", 0.5), cy: A("cy", 0.5), rr: A("r", 0.5), tf: parseTransform(g.getAttribute("gradientTransform")) };
      if (user && g.getAttribute("x2") == null) r.x2 = 100;
      this.recs.set(g, r);
    }
    var G = r.linear ? ctx.createLinearGradient(r.x1, r.y1, r.x2, r.y2) : ctx.createRadialGradient(r.cx, r.cy, 0, r.cx, r.cy, r.rr);
    if (!r.stops.length) return "rgba(0,0,0,0)";
    for (var k = 0; k < r.stops.length; k++) G.addColorStop(r.stops[k][0], r.stops[k][1]);
    return { g: G, user: r.user, tf: r.tf, single: r.stops.length === 1 ? r.stops[0][1] : null };
  };

  /* ---------- bounds of what a subtree paints, in device pixels ---------- */
  Mirror.prototype.bounds = function (el, m, deep) {
    var r = this.rec(el); if (r.hide) return null;
    if (r.tf) m = mul(m, r.tf);
    var out = null;
    if (el.tagName === "g") {
      var ch = el.children;
      for (var i = 0; i < ch.length; i++) { var b = this.bounds(ch[i], m, deep); if (b) out = out ? [Math.min(out[0], b[0]), Math.min(out[1], b[1]), Math.max(out[2], b[2]), Math.max(out[3], b[3])] : b; }
    } else if (r.path && bx(r)) {
      var hasF = r.fill !== "none", hasS = r.stroke !== "none" && r.sw > 0;
      if (!hasF && !hasS) return null;
      var pad = hasS ? r.sw / 2 * (r.join === "miter" ? Math.max(r.miter, 1.5) : 1.5) : 0;
      out = boxT(m, [r.box[0] - pad, r.box[1] - pad, r.box[2] + pad, r.box[3] + pad]);
    }
    if (out && r.filter) { var f = this.byId[r.filter], blur = f && f.querySelector("feGaussianBlur"), sd = blur ? num(blur.getAttribute("stdDeviation"), 0) * scaleOf(m) : 0; out = [out[0] - 3 * sd, out[1] - 3 * sd, out[2] + 3 * sd, out[3] + 3 * sd]; }
    return out;
  };
  /* geometry bbox of a subtree in its own user space (for objectBoundingBox units) */
  Mirror.prototype.gbox = function (el) {
    var r = this.rec(el);
    if (el.tagName !== "g") return bx(r);
    var out = null, ch = el.children;
    for (var i = 0; i < ch.length; i++) {
      var b = this.gbox(ch[i]); if (!b) continue;
      var cr = this.rec(ch[i]); if (cr.tf) b = boxT(cr.tf, b);
      out = out ? [Math.min(out[0], b[0]), Math.min(out[1], b[1]), Math.max(out[2], b[2]), Math.max(out[3], b[3])] : b.slice();
    }
    return out;
  };

  /* ---------- layers ---------- */
  Mirror.prototype.layer = function (w, h) {
    var c = this.pool.pop() || document.createElement("canvas");
    if (c.width < w || c.height < h) { c.width = Math.max(c.width, w); c.height = Math.max(c.height, h); }
    var x = c.getContext("2d"); x.setTransform(1, 0, 0, 1, 0, 0); x.globalAlpha = 1; x.globalCompositeOperation = "source-over"; x.filter && (x.filter = "none");
    x.clearRect(0, 0, w, h);
    return { c: c, x: x };
  };
  Mirror.prototype.free = function (l) { if (this.pool.length < 8) this.pool.push(l.c); };

  /* draw one element (and its subtree) into target t = {x: ctx, ox, oy}; m = user -> device matrix; gco = blend in effect */
  Mirror.prototype.draw = function (el, t, m, alpha, refs, gco) {
    gco = gco || "source-over";
    var r = this.rec(el);
    if (r.hide || r.tag === "defs" || r.tag === "clipPath" || r.tag === "linearGradient" || r.tag === "radialGradient" || r.tag === "filter" || r.tag === "title" || r.tag === "desc") return;
    var mp = m; if (r.tf) m = mul(m, r.tf);
    var isG = r.tag === "g", op = r.op, ch, i;
    if (op <= 0) return;
    if (r.filter) refs[r.filter] = 1; if (r.clip) refs[r.clip] = 1;
    var multi = op < 1 && (isG ? this.leafCount(el) > 1 : (r.fill !== "none" && r.stroke !== "none" && r.sw > 0));
    var blend = r.blend === "screen" ? "screen" : null;
    if (DIRECT && (!r.blend || blend)) {
      /* a screen-blended group: screen is associative, so its children can be screened straight onto the backdrop */
      if (blend && !r.filter && !r.clip && !multi) { ch = el.children; for (i = 0; i < ch.length; i++) this.draw(ch[i], t, m, alpha * op, refs, "screen"); return; }
      var g2 = blend || gco;
      /* blurred single-colour content (muscle glow, cast shadows): canvas shadows do the Gaussian blur, no offscreen buffer */
      if (r.filter) {
        var fi = this.filterInfo(el, r, m);
        if (fi === false) return;
        var col = fi && this.soleColor(el);
        if (col) {
          var x = t.x, mm = mul([1, 0, 0, 1, -t.ox, -t.oy], m);
          x.save();
          x.setTransform(mm[0], mm[1], mm[2], mm[3], mm[4], mm[5]);
          if (fi.region) { x.beginPath(); x.rect(fi.region[0], fi.region[1], fi.region[2] - fi.region[0], fi.region[3] - fi.region[1]); x.clip(); }
          var okc = true;
          if (r.clip) { var cpp = this.clipUnion(r.clip, refs); if (cpp) x.clip(cpp); else okc = false; }
          if (okc) { this.shadowed(el, t, m, alpha * op, g2, fi.sd, col, true); x.restore(); return; }
          x.restore();
        }
      }
      /* plain clip-path: clip the canvas and draw normally */
      else if (r.clip && !multi) {
        var cp2 = this.clipUnion(r.clip, refs);
        if (cp2) {
          var x2 = t.x, m2 = mul([1, 0, 0, 1, -t.ox, -t.oy], m);
          x2.save(); x2.setTransform(m2[0], m2[1], m2[2], m2[3], m2[4], m2[5]); x2.clip(cp2);
          if (isG) { ch = el.children; for (i = 0; i < ch.length; i++) this.draw(ch[i], t, m, alpha * op, refs, g2); }
          else this.paint(el, r, t, m, alpha * op, refs, g2);
          x2.restore(); return;
        }
      }
    }
    var needLayer = !!(r.filter || r.clip || r.blend) || multi;
    if (!needLayer) {
      if (isG) { ch = el.children; for (i = 0; i < ch.length; i++) this.draw(ch[i], t, m, alpha * op, refs, gco); }
      else this.paint(el, r, t, m, alpha * op, refs, gco);
      return;
    }
    this.fallbacks = (this.fallbacks || 0) + 1;
    /* offscreen: paint the subtree, then filter -> clip -> opacity/blend, like SVG */
    var b = this.bounds(el, mp, false);
    if (!b) return;
    if (r.filter) {
      var fi2 = this.filterInfo(el, r, m); if (fi2 === false) return;
      if (fi2 && fi2.region) { var fr = boxT(m, fi2.region); b = [Math.max(b[0], fr[0]), Math.max(b[1], fr[1]), Math.min(b[2], fr[2]), Math.min(b[3], fr[3])]; }
    }
    var x0 = Math.max(Math.floor(b[0]), t.ox), y0 = Math.max(Math.floor(b[1]), t.oy), x1 = Math.min(Math.ceil(b[2]), t.ox + t.w), y1 = Math.min(Math.ceil(b[3]), t.oy + t.h);
    if (x1 <= x0 || y1 <= y0) return;
    var W = x1 - x0, H = y1 - y0, L = this.layer(W, H), sub = { x: L.x, ox: x0, oy: y0, w: W, h: H };
    if (isG) { var kids = el.children; for (var j = 0; j < kids.length; j++) this.draw(kids[j], sub, m, 1, refs, "source-over"); }
    else this.paint(el, r, sub, m, 1, refs, "source-over");
    var src = L;
    if (r.filter) {
      var sd = fi2 ? fi2.sd : 0;
      if (sd > 0.05) { src = this.blur(L, W, H, sd, el); }
    }
    if (r.clip) {
      var cp = this.byId[r.clip];
      if (cp) {
        var M = this.layer(W, H), mm = mul([1, 0, 0, 1, -x0, -y0], m), cpt = parseTransform(cp.getAttribute("transform"));
        if (cpt) mm = mul(mm, cpt);
        var cch = cp.children;
        for (var q = 0; q < cch.length; q++) {
          var ce = cch[q], cr = this.rec(ce), tgt = ce, tm = mm;
          if (cr.tag === "use") { tgt = this.byId[cr.href]; if (!tgt) continue; refs[cr.href] = 1; var tr = this.rec(tgt); if (tr.tf) tm = mul(tm, tr.tf); cr = tr; }
          else if (cr.tf) tm = mul(tm, cr.tf);
          if (!cr.path || cr.hide) continue;
          M.x.setTransform(tm[0], tm[1], tm[2], tm[3], tm[4], tm[5]); M.x.fillStyle = "#000"; M.x.fill(cr.path, tgt.getAttribute("clip-rule") === "evenodd" ? "evenodd" : "nonzero");
        }
        M.x.setTransform(1, 0, 0, 1, 0, 0);
        src.x.setTransform(1, 0, 0, 1, 0, 0); src.x.globalCompositeOperation = "destination-in"; src.x.drawImage(M.c, 0, 0, W, H, 0, 0, W, H); src.x.globalCompositeOperation = "source-over";
        this.free(M);
      }
    }
    var tx = t.x; tx.setTransform(1, 0, 0, 1, 0, 0);
    tx.globalAlpha = alpha * op; tx.globalCompositeOperation = r.blend === "screen" || r.blend === "multiply" || r.blend === "overlay" || r.blend === "lighten" || r.blend === "darken" ? r.blend : gco;
    tx.drawImage(src.c, 0, 0, W, H, x0 - t.ox, y0 - t.oy, W, H);
    tx.globalAlpha = 1; tx.globalCompositeOperation = "source-over";
    this.free(src);
  };
  /* blur radius (device px) and filter region (element user space) of a feGaussianBlur filter; false = SVG draws nothing */
  Mirror.prototype.filterInfo = function (el, r, m) {
    var f = this.byId[r.filter]; if (!f) return null;
    var fe = f.querySelector("feGaussianBlur"); if (!fe || f.children.length !== 1) return null;
    var gb = this.gbox(el); if (!gb || gb[2] - gb[0] <= 0 || gb[3] - gb[1] <= 0) return false; /* zero-size bbox: SVG renders nothing */
    var pc = function (v, d) { if (v == null) return d; return /%$/.test(v) ? parseFloat(v) / 100 : num(v, d); }, bw = gb[2] - gb[0], bh = gb[3] - gb[1];
    var x0 = gb[0] + pc(f.getAttribute("x"), -0.1) * bw, y0 = gb[1] + pc(f.getAttribute("y"), -0.1) * bh;
    return { sd: num(fe.getAttribute("stdDeviation"), 0) * scaleOf(m), region: [x0, y0, x0 + pc(f.getAttribute("width"), 1.2) * bw, y0 + pc(f.getAttribute("height"), 1.2) * bh] };
  };
  /* the union of a clipPath's shapes as one Path2D in the referencing element's user space */
  Mirror.prototype.clipUnion = function (id, refs) {
    var cp = this.byId[id]; if (!cp || !Path2D.prototype.addPath) return null;
    if (cp.getAttribute("transform") || (cp.getAttribute("clipPathUnits") || "userSpaceOnUse") !== "userSpaceOnUse") return null;
    var P = new Path2D(), ch = cp.children;
    for (var q = 0; q < ch.length; q++) {
      var cr = this.rec(ch[q]);
      if (cr.tag === "use") { var tg = this.byId[cr.href]; if (!tg) continue; refs[cr.href] = 1; cr = this.rec(tg); if (tg.getAttribute("clip-rule") === "evenodd") return null; }
      if (cr.tf) return null;
      if (!cr.path || cr.hide) continue;
      P.addPath(cr.path);
    }
    return P;
  };
  /* paint a single-colour subtree only as its blurred shadow (shape kept off-canvas) */
  Mirror.prototype.shadowed = function (el, t, m, alpha, gco, sd, col, top) {
    var r = this.rec(el); if (r.hide) return;
    if (!top && r.tf) m = mul(m, r.tf);
    var op = top ? 1 : r.op;
    if (el.tagName === "g") { var ch = el.children; for (var i = 0; i < ch.length; i++) this.shadowed(ch[i], t, m, alpha * op, gco, sd, col, false); return; }
    if (!r.path) return;
    var x = t.x, D = t.w + t.h + sd * 8 + 400, mm = mul([1, 0, 0, 1, -t.ox - D, -t.oy], m);
    x.setTransform(mm[0], mm[1], mm[2], mm[3], mm[4], mm[5]);
    x.shadowColor = col; x.shadowBlur = sd * 2; x.shadowOffsetX = D; x.shadowOffsetY = 0;
    x.globalCompositeOperation = gco;
    if (r.fill !== "none") { x.globalAlpha = alpha * op * r.fop; x.fillStyle = r.fill; x.fill(r.path); }
    if (r.stroke !== "none" && r.sw > 0) { x.globalAlpha = alpha * op * r.sop; x.strokeStyle = r.stroke; x.lineWidth = r.sw; x.lineCap = r.cap; x.lineJoin = r.join; x.miterLimit = r.miter; x.setLineDash(r.dash || []); x.stroke(r.path); }
    x.shadowColor = "rgba(0,0,0,0)"; x.shadowBlur = 0; x.shadowOffsetX = 0; x.globalAlpha = 1; x.globalCompositeOperation = "source-over";
  };
  /* Gaussian blur of a layer. Soft blurs are computed at reduced resolution (a 10px blur loses nothing at
     half or quarter size) and scaled back up, which is many times cheaper on a phone. */
  Mirror.prototype.blur = function (L, W, H, sd, el) {
    var k = sd >= 7 ? 4 : sd >= 3.5 ? 2 : 1, w = Math.ceil(W / k), h = Math.ceil(H / k), S = L;
    if (k > 1) {
      S = this.layer(w, h); S.x.imageSmoothingEnabled = true; S.x.imageSmoothingQuality = "medium";
      if (k === 4) { var Hh = this.layer(Math.ceil(W / 2), Math.ceil(H / 2)); Hh.x.imageSmoothingEnabled = true; Hh.x.drawImage(L.c, 0, 0, W, H, 0, 0, W / 2, H / 2); S.x.drawImage(Hh.c, 0, 0, W / 2, H / 2, 0, 0, w * (W / k) / w, h * (H / k) / h); this.free(Hh); }
      else S.x.drawImage(L.c, 0, 0, W, H, 0, 0, W / k, H / k);
      this.free(L);
    }
    var B = this.layer(w, h), s2 = sd / k;
    if (CAN_FILTER) { B.x.filter = "blur(" + s2 + "px)"; B.x.drawImage(S.c, 0, 0, w, h, 0, 0, w, h); B.x.filter = "none"; }
    else {
      var col = this.soleColor(el);
      if (col) { var o = w + h + 50; B.x.shadowColor = col; B.x.shadowBlur = s2 * 2; B.x.shadowOffsetX = o; B.x.drawImage(S.c, 0, 0, w, h, -o, 0, w, h); B.x.shadowColor = "rgba(0,0,0,0)"; B.x.shadowBlur = 0; B.x.shadowOffsetX = 0; }
      else B.x.drawImage(S.c, 0, 0, w, h, 0, 0, w, h);
    }
    this.free(S);
    if (k === 1) return B;
    var U = this.layer(W, H); U.x.imageSmoothingEnabled = true; U.x.imageSmoothingQuality = "medium"; U.x.drawImage(B.c, 0, 0, w, h, 0, 0, w * k, h * k); this.free(B);
    return U;
  };
  function invert(m) { var det = m[0] * m[3] - m[1] * m[2]; if (!det) return I; return [m[3] / det, -m[1] / det, -m[2] / det, m[0] / det, (m[2] * m[5] - m[3] * m[4]) / det, (m[1] * m[4] - m[0] * m[5]) / det]; }
  Mirror.prototype.leafCount = function (el) { var n = 0, ch = el.children; for (var i = 0; i < ch.length && n < 2; i++) { var r = this.rec(ch[i]); if (r.hide) continue; n += ch[i].tagName === "g" ? this.leafCount(ch[i]) : (r.path && (r.fill !== "none" || r.stroke !== "none") ? 1 : 0); } return n; };
  Mirror.prototype.soleColor = function (el) {
    var col = null, ok = true, self = this;
    (function walk(e) { var r = self.rec(e); if (r.hide || !ok) return; if (e.tagName === "g") { for (var i = 0; i < e.children.length; i++) walk(e.children[i]); return; } if (!r.path) return;
      [r.fill, r.stroke].forEach(function (p, k) { if (p === "none" || (k && !(r.sw > 0))) return; if (urlId(p)) { ok = false; return; } var c = rgba(p, 1).replace(/,[^,]*\)$/, ",1)"); if (col && col !== c) ok = false; col = c; }); })(el);
    return ok ? col : null;
  };
  /* fill and stroke one shape */
  Mirror.prototype.paint = function (el, r, t, m, alpha, refs, gco) {
    if (!r.path) return;
    var x = t.x, mm = mul([1, 0, 0, 1, -t.ox, -t.oy], m);
    x.globalCompositeOperation = gco || "source-over";
    x.setTransform(mm[0], mm[1], mm[2], mm[3], mm[4], mm[5]);
    if (r.fill !== "none") {
      x.globalAlpha = alpha * r.fop;
      if (r.fillId) {
        refs[r.fillId] = 1;
        var G = this.grad(r.fillId, x);
        if (G && G.g) {
          if (G.user && !G.tf) { x.fillStyle = G.g; x.fill(r.path); }
          else if (G.user) { x.save(); x.clip(r.path); x.transform(G.tf[0], G.tf[1], G.tf[2], G.tf[3], G.tf[4], G.tf[5]); x.fillStyle = G.g; var inv = invert(G.tf), bb = boxT(inv, bx(r) || [0, 0, 0, 0]); x.fillRect(bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1]); x.restore(); }
          else if (bx(r) && r.box[2] > r.box[0] && r.box[3] > r.box[1]) {
            x.save(); x.clip(r.path); x.transform(r.box[2] - r.box[0], 0, 0, r.box[3] - r.box[1], r.box[0], r.box[1]); if (G.tf) x.transform(G.tf[0], G.tf[1], G.tf[2], G.tf[3], G.tf[4], G.tf[5]);
            x.fillStyle = G.g; x.fillRect(-2, -2, 5, 5); x.restore();
          }
        } else if (G) { x.fillStyle = G; x.fill(r.path); }
      } else { x.fillStyle = r.fill; x.fill(r.path); }
    }
    if (r.stroke !== "none" && r.sw > 0) {
      x.globalAlpha = alpha * r.sop;
      if (r.strokeId) { refs[r.strokeId] = 1; var S2 = this.grad(r.strokeId, x); x.strokeStyle = S2 && S2.g ? S2.g : "rgba(0,0,0,0)"; }
      else x.strokeStyle = r.stroke;
      x.lineWidth = r.sw; x.lineCap = r.cap; x.lineJoin = r.join; x.miterLimit = r.miter;
      x.setLineDash(r.dash || []);
      x.stroke(r.path);
    }
    x.globalAlpha = 1; x.globalCompositeOperation = "source-over";
  };

  /* ---------- one frame ---------- */
  Mirror.prototype.setSvg = function (on) {
    if (this.dead || !!on === !!this.svgMode) return;
    this.svgMode = !!on;
    if (on) { this.svg.removeAttribute("data-smvcv"); this.svg.setAttribute("data-smvsvg", ""); this.cv.hidden = true; this.cv.width = 0; this.cv.height = 0; this.freeCache(); }
    else { this.svg.removeAttribute("data-smvsvg"); this.svg.setAttribute("data-smvcv", ""); this.cv.hidden = false; this.mo.takeRecords(); this.recs = new WeakMap(); this.index(); this.place(); this.render(true); }
  };
  Mirror.prototype.render = function (force) {
    if (this.dead || this.svgMode || !this.vis) return;
    try {
      var svg = this.svg, ctx = this.ctx, cv = this.cv;
      if (!svg.isConnected) { this.detach(); return; }
      var pend = this.mo.takeRecords(); if (pend.length) this.records(pend);
      var nowT = performance.now(), idle = nowT - (this.lastT || 0) > 400; this.lastT = nowT;
      if (force || idle || (++this.frames % 45) === 0) {
        var sr = svg.getBoundingClientRect(), cr = cv.getBoundingClientRect();
        if (Math.abs(sr.width - cr.width) > 0.5 || Math.abs(sr.height - cr.height) > 0.5 || Math.abs(cr.left - sr.left) > 0.5 || Math.abs(cr.top - sr.top) > 0.5) this.place();
      }
      if (!this.scene) { ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, cv.width, cv.height); return; }
      /* viewBox + preserveAspectRatio (xMidYMid meet, the engine's setting) */
      var vb = (svg.getAttribute("viewBox") || "0 0 " + this.cssW + " " + this.cssH).split(/[\s,]+/).map(Number);
      var W = cv.width, H = cv.height, s = Math.min(W / vb[2], H / vb[3]), par = svg.getAttribute("preserveAspectRatio") || "xMidYMid meet";
      var tx = /xMin/.test(par) ? 0 : /xMax/.test(par) ? W - vb[2] * s : (W - vb[2] * s) / 2, ty = /YMin/.test(par) ? 0 : /YMax/.test(par) ? H - vb[3] * s : (H - vb[3] * s) / 2;
      var base = [s, 0, 0, s, tx - vb[0] * s, ty - vb[1] * s];
      var sceneRec = this.rec(this.scene), M = sceneRec.tf ? mul(base, sceneRec.tf) : base;
      var n = this.layers.length, k;
      /* longest run of untouched bottom layers can come from the cache */
      var valid = 0; if (this.cache) while (valid < this.cacheN && this.cacheVer[valid] === this.layerVer[valid]) valid++;
      var stable = 0; if (this.prevVer && this.prevVer.length === n) while (stable < n && this.prevVer[stable] === this.layerVer[stable]) stable++;
      var want = Math.min(stable, n);
      if (!this.cache || valid < this.cacheN || want !== this.cacheN) {
        if (want >= 1) {
          if (!this.cache) this.cache = document.createElement("canvas");
          if (this.cache.width !== W || this.cache.height !== H) { this.cache.width = W; this.cache.height = H; }
          var cx = this.cache.getContext("2d"); cx.setTransform(1, 0, 0, 1, 0, 0); cx.clearRect(0, 0, W, H);
          var ct = { x: cx, ox: 0, oy: 0, w: W, h: H };
          for (k = 0; k < want; k++) { var rf = {}; this.draw(this.layers[k], ct, M, 1, rf); this.refs[k] = rf; this.cacheVer[k] = this.layerVer[k]; }
          this.cacheN = want;
        } else { this.cacheN = 0; }
      }
      this.prevVer = this.layerVer.slice();
      ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.globalAlpha = 1; ctx.globalCompositeOperation = "source-over";
      ctx.clearRect(0, 0, W, H);
      if (this.cacheN) ctx.drawImage(this.cache, 0, 0);
      var t = { x: ctx, ox: 0, oy: 0, w: W, h: H };
      for (k = this.cacheN; k < n; k++) { var rr = {}; this.draw(this.layers[k], t, M, 1, rr); this.refs[k] = rr; }
    } catch (e) {
      try { console.warn("SMV canvas fallback:", e && e.message); } catch (e2) {}
      this.detach();
    }
  };
  Mirror.prototype.detach = function () {
    if (this.dead) return; this.dead = true;
    try { this.mo.disconnect(); } catch (e) {}
    try { this.ro && this.ro.disconnect(); } catch (e) {}
    try { this.io && this.io.disconnect(); } catch (e) {}
    try { this.svg.removeAttribute("data-smvcv"); this.svg.removeAttribute("data-smvsvg"); } catch (e) {}
    var k = live.indexOf(this); if (k >= 0) live.splice(k, 1);
    try { this.freeCache(); this.cv.width = 0; this.cv.height = 0; this.cv.remove(); } catch (e) {}
    this.svg.__smvcv = null; this.svg.__smvcvTry = null;
  };

  /* ---------- find engine figures as they mount ---------- */
  function isFigure(svg) {
    if (!svg || svg.tagName !== "svg" && svg.tagName !== "SVG") return false;
    var d = svg.firstElementChild; if (!d || d.tagName !== "defs") return false;
    for (var c = d.firstElementChild; c; c = c.nextElementSibling) if (c.tagName === "filter" && SIG.test(c.id)) return true;
    return false;
  }
  function attach(svg) {
    if (off || svg.__smvcv || !svg.parentNode || !isFigure(svg)) return null;
    if (probe.mode === "svg" && probe.phase === "done") { svg.setAttribute("data-smvsvg", ""); return null; }   /* this device draws SVG faster: no mirror, no observers */
    try { var mm = svg.__smvcv = new Mirror(svg); if (mm.dead) { svg.__smvcv = null; return null; } live.push(mm); if (probe.mode === "svg") mm.setSvg(true); startProbe(); return mm; }
    catch (e) { var t = svg.__smvcvTry; svg.__smvcv = null; try { if (t) { t.dead = false; t.detach(); } else svg.removeAttribute("data-smvcv"); } catch (e2) {} return null; }
  }
  function scan(root) {
    if (off || !root || !root.querySelectorAll) return;
    if (root.tagName === "svg") attach(root);
    var list = root.querySelectorAll("svg");
    for (var i = 0; i < list.length; i++) if (!list[i].__smvcv) attach(list[i]);
  }
  /* ---------- per-device check: keep whichever renderer is actually smoother on this phone ----------
     Canvas is tried first. If it cannot hold ~50 fps here, SVG gets the same test and the faster one is kept
     (both draw the same picture, so switching is invisible). The result is remembered for two weeks. */
  var KEY = "smv-render-v1", live = [], probe = { mode: "canvas", phase: "done", samples: [], active: 0, prev: 0, since: 0, ticking: false };
  var startProbe = function () {}, kick = function () {};
  (function () {
    if (off) return;
    var saved = null; try { saved = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) {}
    if (forced) { probe.mode = forced; return; }
    if (saved && saved.t && Date.now() - saved.t < 14 * 864e5 && (saved.mode === "svg" || saved.mode === "canvas")) { probe.mode = saved.mode; return; }
    probe.phase = "pending";
    function med(a) { var b = a.slice().sort(function (x, y) { return x - y; }); return b[b.length >> 1]; }
    function setMode(m) { probe.mode = m; for (var i = 0; i < live.length; i++) live[i].setSvg(m === "svg"); probe.samples = []; probe.prev = 0; probe.since = performance.now(); }
    function decide(m) {
      probe.phase = "done"; if (probe.mode !== m) setMode(m);
      if (m === "svg") { var all = live.slice(); for (var i = 0; i < all.length; i++) { var sv = all[i].svg; all[i].detach(); try { sv.setAttribute("data-smvsvg", ""); } catch (e) {} } }
      try { localStorage.setItem(KEY, JSON.stringify({ mode: m, c: probe.c, s: probe.s, t: Date.now() })); } catch (e) {}
    }
    function tick(ts) {
      probe.ticking = false;
      if (probe.phase === "done" || probe.phase === "pending") return;
      var now = performance.now();
      if (!live.length || now - probe.active > 1000) { probe.prev = 0; return; }   /* nothing animating: sleep until a figure moves (kick) */
      if (document.visibilityState === "visible" && now - probe.active < 100 && now - probe.since > 600) {
        if (probe.prev && ts - probe.prev < 250) probe.samples.push(ts - probe.prev);
        probe.prev = ts;
      } else probe.prev = 0;
      if (probe.samples.length >= 60) {
        if (probe.phase === "canvas") { probe.c = med(probe.samples); if (probe.c <= 20) decide("canvas"); else { probe.phase = "svg"; setMode("svg"); } }
        else { probe.s = med(probe.samples); decide(probe.s < probe.c * 0.85 ? "svg" : "canvas"); }
      }
      if (probe.phase !== "done") { probe.ticking = true; requestAnimationFrame(tick); }
    }
    kick = function () { if (!probe.ticking && (probe.phase === "canvas" || probe.phase === "svg")) { probe.ticking = true; requestAnimationFrame(tick); } };
    startProbe = function () { if (probe.phase === "pending") { probe.phase = "canvas"; probe.since = performance.now(); } kick(); };
  })();
  /* zooming or moving the window to another screen changes devicePixelRatio without a resize */
  (function watchDpr() {
    if (off || !window.matchMedia) return;
    try { var mq = matchMedia("(resolution: " + (window.devicePixelRatio || 1) + "dppx)"), fn = function () { for (var i = 0; i < live.length; i++) { try { live[i].place(); live[i].render(true); } catch (e) {} } if (mq.removeEventListener) mq.removeEventListener("change", fn); watchDpr(); };
      if (mq.addEventListener) mq.addEventListener("change", fn); } catch (e) {}
  })();
  window.SMVCanvas = { attach: attach, scan: scan, enabled: !off, canFilter: CAN_FILTER, setMaxDpr: function (v) { MAXDPR = v; }, setDirect: function (v) { DIRECT = !!v; }, probe: probe, live: live, Mirror: Mirror };
  if (off) return;
  var docMo = new MutationObserver(function (rs) {
    for (var i = 0; i < rs.length; i++) {
      var r = rs[i];
      if (r.target && r.target.tagName === "svg" && !r.target.__smvcv) attach(r.target);
      for (var q = 0; q < r.removedNodes.length; q++) { var x = r.removedNodes[q]; if (x.nodeType !== 1 || x.isConnected) continue; if (x.__smvcv) x.__smvcv.detach(); else if (x.querySelectorAll) { var ss = x.querySelectorAll("svg[data-smvcv]"); for (var z = 0; z < ss.length; z++) if (ss[z].__smvcv) ss[z].__smvcv.detach(); } }
      for (var k = 0; k < r.addedNodes.length; k++) { var n = r.addedNodes[k]; if (n.nodeType === 1) { if (n.tagName === "svg") attach(n); else if (n.firstElementChild) scan(n); } }
    }
  });
  function start() { if (window.SMV_CANVAS_MANUAL) return; docMo.observe(document.documentElement, { childList: true, subtree: true }); scan(document.documentElement); }
  if (document.documentElement) start(); else document.addEventListener("DOMContentLoaded", start);
})();
