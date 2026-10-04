/* ===== data visualizer (设计 18) — player (mockup). Identical block in every page that embeds the player.
   Fake data only: synthetic camera frames (canvas) and synthetic curves; the real player gets videos from
   presigned TOS URLs / the Daemon's episode media endpoint and curves from the Daemon's series endpoint. ===== */
window.Viz = (function () {
  'use strict';

  // ---------- small helpers ----------
  const PALETTE = ['#165DFF', '#14C9C9', '#FF7D00', '#722ED1', '#00B42A', '#F5319D', '#F7BA1E', '#9FDB1D'];
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const lerp = (a, b, k) => a + (b - a) * k;
  const ease = (k) => (k <= 0 ? 0 : k >= 1 ? 1 : k * k * (3 - 2 * k));
  function fmtTime(s) {
    s = Math.max(0, s || 0);
    const m = Math.floor(s / 60), r = s - m * 60;
    return `${String(m).padStart(2, '0')}:${r.toFixed(1).padStart(4, '0')}`;
  }
  function el(tag, attrs, ...children) {
    const n = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') n.className = v;
      else if (k === 'style') n.style.cssText = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else if (k === 'html') n.innerHTML = v;
      else if (v !== false && v != null) n.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) if (c != null && c !== false) n.append(c.nodeType ? c : document.createTextNode(String(c)));
    return n;
  }
  const svgNS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs, ...children) {
    const n = document.createElementNS(svgNS, tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) if (v != null && v !== false) n.setAttribute(k, v);
    for (const c of children.flat()) if (c != null && c !== false) n.append(c.nodeType ? c : document.createTextNode(String(c)));
    return n;
  }
  const toast = (m) => (window.toast ? window.toast(m) : console.log(m));
  function rng(seed) { let a = seed >>> 0; return () => { a |= 0; a = (a + 0x6D2B79F5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }

  // ---------- synthetic episode (the real page reads the dataset) ----------
  // Script of the left end effector in table coordinates: x left→right, y back→front, z height, g gripper opening (1 = open).
  function makeEpisode(ds, index, variant) {
    const fps = ds.fps, R = rng(1000 + index * 7919 + (variant || 0));
    const dur = 18 + R() * 7;                       // 18–25 s
    const frames = Math.round(dur * fps), duration = frames / fps;
    const k = dur / 21.4;                            // stretch the script to this episode's length
    const key = [
      { t: 0, x: .22, y: .70, z: .35, g: 1 }, { t: 4.2, x: .38, y: .62, z: .08, g: 1 }, { t: 6.0, x: .38, y: .62, z: .06, g: .2 },
      { t: 8.9, x: .38, y: .62, z: .30, g: .2 }, { t: 14.6, x: .68, y: .52, z: .30, g: .2 }, { t: 16.0, x: .68, y: .52, z: .14, g: .2 },
      { t: 17.3, x: .68, y: .52, z: .14, g: 1 }, { t: 21.4, x: .22, y: .70, z: .35, g: 1 },
    ].map((p) => ({ ...p, t: p.t * k }));
    const stepsRaw = [[0, 4.2, '伸向苹果'], [4.2, 8.9, '抓取苹果'], [8.9, 14.6, '移到碗上方'], [14.6, 17.3, '放入碗中'], [17.3, 21.4, '复位']];
    const steps = index % 5 === 3 ? [] : stepsRaw.map(([s, e, label], i) => ({ s: s * k, e: e * k, label, quality: index % 4 === 0 && i === 3 ? 'unqualified' : 'ok' }));
    const ee = (t) => {
      let i = 0; while (i < key.length - 2 && t > key[i + 1].t) i++;
      const a = key[i], b = key[i + 1], u = ease((t - a.t) / Math.max(1e-6, b.t - a.t));
      return { x: lerp(a.x, b.x, u), y: lerp(a.y, b.y, u), z: lerp(a.z, b.z, u), g: lerp(a.g, b.g, u) };
    };
    const D = ds.dims, names = ds.names;
    const state = new Float32Array(frames * D), action = new Float32Array(frames * D);
    const noise = []; for (let i = 0; i < D; i++) noise.push(R() * 6.28);
    const pose = (t) => {
      const p = ee(t), v = new Array(D).fill(0);
      v[0] = (p.x - .5) * 1.4; v[1] = .55 + (1 - p.y) * .9 - p.z * .5; v[2] = 1.25 - (1 - p.y) * .8 + p.z * .7;
      v[3] = -.35 + p.z * .9; v[4] = .12 * Math.sin(t * .9 + noise[4]); v[5] = .25 * Math.cos(t * .5 + noise[5]) + (p.x - .5) * .3; v[6] = p.g;
      // right arm idles with a slow sway; its gripper stays open
      const base = [.42, .9, 1.1, -.2, .05, -.3];
      for (let i = 0; i < 6; i++) v[7 + i] = base[i] + .04 * Math.sin(t * (.3 + i * .07) + noise[7 + i]);
      v[13] = 1;
      return v;
    };
    for (let f = 0; f < frames; f++) {
      const t = f / fps, s = pose(t), a = pose(Math.min(duration, t + 3 / fps));
      for (let i = 0; i < D; i++) {
        state[f * D + i] = s[i] + (R() - .5) * .006;
        action[f * D + i] = Math.round((a[i] + .004 * Math.sin(i)) / .004) * .004;
      }
    }
    const task = index % 7 === 6 ? '' : (ds.tasks[index % ds.tasks.length]);
    return { index, fps, frames, duration, task, steps, stepsSource: ds.stepsSource, dims: D, names, state, action, ee, apple: appleOf(key, k) };
  }
  function appleOf(key, k) {
    const grasp = 6.0 * k, release = 16.5 * k;
    return (t, ee) => (t < grasp ? { x: .38, y: .62, z: 0 } : t < release ? { x: ee.x, y: ee.y, z: Math.max(0, ee.z - .05) } : { x: .68, y: .52, z: .04 });
  }

  // ---------- synthetic camera frames ----------
  const CAM_DRAW = {
    head(ctx, w, h, ep, t) {
      const e = ep.ee(t), ap = ep.apple(t, e);
      const P = (x, y, z) => [w / 2 + (x - .5) * (.6 + .4 * y) * w, (.36 + y * .52) * h - z * .34 * h];
      const bg = ctx.createLinearGradient(0, 0, 0, h); bg.addColorStop(0, '#3B4654'); bg.addColorStop(1, '#1F2630');
      ctx.fillStyle = bg; ctx.fillRect(0, 0, w, h);
      // table
      ctx.beginPath(); const [ax, ay] = P(0, 0, 0), [bx, by] = P(1, 0, 0), [cx, cy] = P(1, 1, 0), [dx, dy] = P(0, 1, 0);
      ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.lineTo(cx, cy); ctx.lineTo(dx, dy); ctx.closePath();
      const tg = ctx.createLinearGradient(0, ay, 0, cy); tg.addColorStop(0, '#8E7250'); tg.addColorStop(1, '#B6956C'); ctx.fillStyle = tg; ctx.fill();
      ctx.strokeStyle = 'rgba(0,0,0,.18)'; ctx.lineWidth = 1;
      for (let i = 1; i < 8; i++) { ctx.beginPath(); const [x1, y1] = P(i / 8, 0, 0), [x2, y2] = P(i / 8, 1, 0); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke(); }
      // bowl
      const [bwx, bwy] = P(.68, .52, 0); const br = .085 * w;
      ctx.beginPath(); ctx.ellipse(bwx, bwy, br, br * .42, 0, 0, 6.3); ctx.fillStyle = '#D9DEE6'; ctx.fill(); ctx.strokeStyle = '#9AA3B2'; ctx.lineWidth = 2; ctx.stroke();
      ctx.beginPath(); ctx.ellipse(bwx, bwy, br * .78, br * .3, 0, 0, 6.3); ctx.fillStyle = '#B8C0CC'; ctx.fill();
      // right arm (idle)
      arm(ctx, [w * .9, h * .04], P(.78 + .01 * Math.sin(t * .4), .74, .32), 1, '#C9CDD4');
      // apple behind the left arm
      const [apx, apy] = P(ap.x, ap.y, ap.z); apple(ctx, apx, apy, .028 * w);
      // left arm
      arm(ctx, [w * .1, h * .04], P(e.x, e.y, e.z), e.g, '#E5E6EB');
      vignette(ctx, w, h);
    },
    left_wrist(ctx, w, h, ep, t) {
      const e = ep.ee(t), ap = ep.apple(t, e);
      const zoom = 1 + (.36 - e.z) * 2.2;
      wristBackdrop(ctx, w, h, e.x, e.y, zoom, 0);
      // bowl relative to the gripper
      const rel = (o) => [w / 2 + (o.x - e.x) * w * 2.2 * zoom, h * .55 + (o.y - e.y) * h * 2.6 * zoom];
      const [bx, by] = rel({ x: .68, y: .52 }); const br = .22 * w * zoom;
      if (bx > -br && bx < w + br) { ctx.beginPath(); ctx.ellipse(bx, by, br, br * .5, 0, 0, 6.3); ctx.fillStyle = '#D9DEE6'; ctx.fill(); ctx.strokeStyle = '#9AA3B2'; ctx.lineWidth = 3; ctx.stroke(); }
      const held = Math.abs(ap.x - e.x) < .01 && Math.abs(ap.y - e.y) < .01 && ap.z > 0;
      if (held) apple(ctx, w / 2, h * .78, .11 * w);
      else { const [x, y] = rel(ap); const r = .06 * w * zoom; if (x > -r && x < w + r && y > -r && y < h + r) apple(ctx, x, y, r); }
      fingers(ctx, w, h, e.g);
      vignette(ctx, w, h);
    },
    right_wrist(ctx, w, h, ep, t) {
      wristBackdrop(ctx, w, h, .78 + .01 * Math.sin(t * .4), .74, 1.05, 1);
      // the left arm's end effector passes through the far field when it travels to the bowl
      const e = ep.ee(t); const x = w * .15 + (e.x - .2) * w * .9, y = h * .25 - e.z * h * .3;
      if (e.x > .45) { ctx.fillStyle = '#C9CDD4'; ctx.beginPath(); ctx.roundRect(x - 14, y - 8, 28, 16, 3); ctx.fill(); }
      fingers(ctx, w, h, 1);
      vignette(ctx, w, h);
    },
  };
  function arm(ctx, from, to, g, color) {
    const mx = (from[0] + to[0]) / 2 + (to[0] > from[0] ? -40 : 40), my = Math.min(from[1], to[1]) - 30;
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.strokeStyle = 'rgba(0,0,0,.35)'; ctx.lineWidth = 14; ctx.beginPath(); ctx.moveTo(from[0], from[1]); ctx.lineTo(mx, my); ctx.lineTo(to[0], to[1]); ctx.stroke();
    ctx.strokeStyle = color; ctx.lineWidth = 10; ctx.beginPath(); ctx.moveTo(from[0], from[1]); ctx.lineTo(mx, my); ctx.lineTo(to[0], to[1]); ctx.stroke();
    ctx.fillStyle = '#4E5969'; ctx.beginPath(); ctx.arc(mx, my, 7, 0, 6.3); ctx.fill();
    const gap = 4 + g * 10; ctx.strokeStyle = '#4E5969'; ctx.lineWidth = 4;
    ctx.beginPath(); ctx.moveTo(to[0] - gap, to[1]); ctx.lineTo(to[0] - gap, to[1] + 14); ctx.moveTo(to[0] + gap, to[1]); ctx.lineTo(to[0] + gap, to[1] + 14); ctx.stroke();
  }
  function apple(ctx, x, y, r) {
    const g = ctx.createRadialGradient(x - r * .3, y - r * .3, r * .2, x, y, r); g.addColorStop(0, '#FF7875'); g.addColorStop(1, '#C0262A');
    ctx.fillStyle = g; ctx.beginPath(); ctx.arc(x, y, r, 0, 6.3); ctx.fill();
    ctx.strokeStyle = '#4F6B2A'; ctx.lineWidth = Math.max(1.5, r * .12); ctx.beginPath(); ctx.moveTo(x, y - r); ctx.lineTo(x + r * .25, y - r * 1.35); ctx.stroke();
  }
  function wristBackdrop(ctx, w, h, x, y, zoom, hue) {
    const g = ctx.createLinearGradient(0, 0, 0, h); g.addColorStop(0, hue ? '#8A7050' : '#9A7B58'); g.addColorStop(1, hue ? '#AE8E65' : '#BF9E73');
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = 'rgba(0,0,0,.14)'; ctx.lineWidth = 2;
    const sp = w * .19 * zoom, off = ((-x * w * 2.2 * zoom) % sp + sp) % sp;
    for (let px = off - sp; px < w + sp; px += sp) { ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke(); }
    const sp2 = h * .25 * zoom, off2 = ((-y * h * 2.6 * zoom) % sp2 + sp2) % sp2;
    ctx.strokeStyle = 'rgba(255,255,255,.08)';
    for (let py = off2 - sp2; py < h + sp2; py += sp2) { ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(w, py); ctx.stroke(); }
  }
  function fingers(ctx, w, h, g) {
    const gap = (.08 + g * .3) * w, fw = w * .09, top = h * .62;
    for (const sgn of [-1, 1]) {
      const x = w / 2 + sgn * gap - (sgn > 0 ? 0 : fw);
      ctx.fillStyle = '#2B313A'; ctx.beginPath(); ctx.roundRect(x, top, fw, h - top, 6); ctx.fill();
      ctx.fillStyle = '#4E5969'; ctx.fillRect(x + fw * .25, top + 10, fw * .5, h - top);
    }
    ctx.fillStyle = '#1D2129'; ctx.fillRect(w * .36, h * .92, w * .28, h * .08);
  }
  function vignette(ctx, w, h) {
    const g = ctx.createRadialGradient(w / 2, h / 2, h * .45, w / 2, h / 2, h * .95); g.addColorStop(0, 'rgba(0,0,0,0)'); g.addColorStop(1, 'rgba(0,0,0,.35)');
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
  }

  // ---------- curve cell ----------
  class Curve {
    constructor(host, group, player) {
      this.host = host; this.group = group; this.player = player; this.off = new Set();
      this.plot = el('div', { class: 'plot' }); this.legend = el('div', { class: 'legend' });
      host.append(this.plot, this.legend);
      this.plot.addEventListener('click', (ev) => { const r = this.plot.getBoundingClientRect(); const x = ev.clientX - r.left; if (x < this.m.l) return; player.seek(((x - this.m.l) / this.pw) * player.ep.duration); });
      this.buildLegend();
    }
    buildLegend() {
      this.legend.innerHTML = '';
      this.lis = this.group.dims.map((d, i) => {
        const c = PALETTE[i % PALETTE.length];
        const li = el('div', { class: 'li', title: '点击隐藏 / 显示这条序列', onclick: () => { this.off.has(d) ? this.off.delete(d) : this.off.add(d); li.classList.toggle('off'); this.renderStatic(); this.player.renderSide(); } },
          el('span', { class: 'sw', style: `color:${c}` }), el('span', { class: 'nm' }, this.group.names[i]),
          el('span', { class: 'val' }, '–'), el('span', { class: 'val a' }, '–'));
        this.legend.append(li); return li;
      });
    }
    layout() {
      const r = this.host.getBoundingClientRect(); this.lh = this.legend.offsetHeight || 0;
      this.w = Math.max(60, r.width); this.h = Math.max(40, r.height - this.lh); this.plot.style.height = this.h + 'px';
      this.m = { l: 44, r: 12, t: 32, b: 18 }; this.pw = this.w - this.m.l - this.m.r; this.ph = Math.max(10, this.h - this.m.t - this.m.b);
      this.renderStatic();
    }
    renderStatic() {
      const ep = this.player.ep, g = this.group, D = ep.dims;
      let lo = Infinity, hi = -Infinity;
      for (const d of g.dims) if (!this.off.has(d)) for (let f = 0; f < ep.frames; f += 2) { const a = ep.state[f * D + d], b = ep.action[f * D + d]; if (a < lo) lo = a; if (a > hi) hi = a; if (b < lo) lo = b; if (b > hi) hi = b; }
      if (!isFinite(lo)) { lo = 0; hi = 1; } if (hi - lo < 1e-6) { hi = lo + 1; } const pad = (hi - lo) * .08; lo -= pad; hi += pad;
      this.lo = lo; this.hi = hi;
      const X = (t) => this.m.l + (t / ep.duration) * this.pw, Y = (v) => this.m.t + (1 - (v - lo) / (hi - lo)) * this.ph;
      this.X = X; this.Y = Y;
      const root = svg('svg', { viewBox: `0 0 ${this.w} ${this.h}`, width: this.w, height: this.h });
      const grid = svg('g', { class: 'grid' }), axis = svg('g', { class: 'axis' });
      const yt = 4; for (let i = 0; i <= yt; i++) { const v = lo + (hi - lo) * i / yt, y = Y(v); grid.append(svg('line', { x1: this.m.l, x2: this.w - this.m.r, y1: y, y2: y })); axis.append(svg('text', { x: this.m.l - 6, y: y + 3, 'text-anchor': 'end' }, fmtNum(v))); }
      const xs = ep.duration > 40 ? 10 : ep.duration > 16 ? 5 : 2;
      for (let t = 0; t <= ep.duration + 1e-6; t += xs) { const x = X(t); grid.append(svg('line', { x1: x, x2: x, y1: this.m.t, y2: this.h - this.m.b })); axis.append(svg('text', { x, y: this.h - 5, 'text-anchor': 'middle' }, t + 's')); }
      root.append(grid, axis);
      // evidence bands (mini)
      for (const ev of this.player.evidence || []) { const [s, e] = evRange(ev, ep); if (e <= s) continue; root.append(svg('rect', { class: `band ${ev.level}${ev.id === this.player.focusEv ? ' focus' : ''}`, x: X(s), y: this.m.t, width: Math.max(2, X(e) - X(s)), height: this.ph })); }
      const step = Math.max(1, Math.floor(ep.frames / Math.max(120, this.pw)));
      g.dims.forEach((d, i) => {
        if (this.off.has(d)) return; const c = PALETTE[i % PALETTE.length];
        for (const [arr, dash] of [[ep.action, '4 3'], [ep.state, null]]) {
          let p = ''; for (let f = 0; f < ep.frames; f += step) p += (f ? 'L' : 'M') + X(f / ep.fps).toFixed(1) + ' ' + Y(arr[f * D + d]).toFixed(1);
          root.append(svg('path', { d: p, fill: 'none', stroke: c, 'stroke-width': dash ? 1.3 : 2, 'stroke-dasharray': dash, opacity: dash ? .8 : 1 }));
        }
      });
      this.cursor = svg('line', { class: 'cursor', x1: X(0), x2: X(0), y1: this.m.t - 6, y2: this.h - this.m.b });
      this.cursorLab = svg('text', { x: X(0), y: this.m.t - 9, 'text-anchor': 'middle', style: 'font-size:10px;fill:#1D2129' }, '');
      root.append(this.cursor, this.cursorLab);
      this.plot.innerHTML = ''; this.plot.append(root);
    }
    update(t) {
      if (!this.X) return; const ep = this.player.ep, f = this.player.frame(), D = ep.dims; const x = this.X(t);
      this.cursor.setAttribute('x1', x); this.cursor.setAttribute('x2', x); this.cursorLab.setAttribute('x', clamp(x, this.m.l + 16, this.w - 24)); this.cursorLab.textContent = fmtTime(t);
      this.group.dims.forEach((d, i) => { const li = this.lis[i]; li.children[2].textContent = fmtNum(ep.state[f * D + d]); li.children[3].textContent = fmtNum(ep.action[f * D + d]); });
    }
  }
  function fmtNum(v) { const a = Math.abs(v); return (a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : v.toFixed(2)); }
  function evRange(ev, ep) {
    if (ev.time) return [ev.time[0], ev.time[1] == null ? ev.time[0] : ev.time[1]];
    if (ev.frames) return [ev.frames[0] / ep.fps, (ev.frames[1] == null ? ev.frames[0] + 1 : ev.frames[1] + 1) / ep.fps];
    return [0, 0];
  }

  // ---------- the player ----------
  class Player {
    constructor(root, opts) {
      this.root = root; this.opts = opts; this.mode = opts.mode || 'full'; this.ds = opts.dataset; this.ep = opts.episode;
      this.t = 0; this.playing = false; this.speed = 1; this.loop = false; this.max = null; this.focus = -1; this.sideOpen = !!opts.sidebar;
      this.evidence = opts.evidence || []; this.focusEv = opts.focusEvidence || null; this.cells = []; this.cols = 2; this.rows = 2; this.template = opts.template || 'smart';
      this.build();
      if (opts.cells) this.setCells(opts.cells, opts.cols, opts.rows); else this.applyTemplate(this.template);
      if (this.focusEv) { const ev = this.evidence.find((e) => e.id === this.focusEv); if (ev) this.t = evRange(ev, this.ep)[0]; }
      this.ro = new ResizeObserver(() => this.relayout()); this.ro.observe(this.gridWrap);
      this.last = performance.now(); this.raf = requestAnimationFrame((n) => this.tick(n));
      document.addEventListener('keydown', this.onKey = (e) => this.key(e));
      document.addEventListener('pointerdown', this.onDocDown = (e) => { this.active = this.root.contains(e.target); if (!this.pop?.contains(e.target)) this.closePop(); });
      this.update();
    }
    destroy() { cancelAnimationFrame(this.raf); this.ro.disconnect(); document.removeEventListener('keydown', this.onKey); document.removeEventListener('pointerdown', this.onDocDown); this.root.innerHTML = ''; }

    // -- DOM
    build() {
      const r = this.root; r.classList.add('vz'); if (this.mode === 'mini') r.classList.add('is-mini'); r.innerHTML = '';
      // 顶栏
      this.hEp = el('span', { class: 'ep' }); this.hMeta = el('span', { class: 'meta' }); this.hTask = el('span', { class: 'task' });
      const ctl = el('div', { class: 'ctl' });
      if (this.mode === 'full') {
        this.tplSel = el('select', { class: 'select', title: '布局模版', onchange: (e) => this.applyTemplate(e.target.value) },
          el('option', { value: 'smart' }, '智能展示视频和运动曲线（默认）'), el('option', { value: 'video' }, '仅展示视频'), el('option', { value: 'curve' }, '仅展示运动曲线'), el('option', { value: 'custom' }, '自定义'));
        ctl.append(this.tplSel);
        this.gridSel = el('select', { class: 'select', style: 'width:84px', title: '格子数', onchange: (e) => { const [c, rr] = e.target.value.split('x').map(Number); this.resize(c, rr); } },
          ...['1x1', '2x1', '2x2', '3x2', '3x3'].map((v) => el('option', { value: v }, v.replace('x', ' × '))));
        ctl.append(this.gridSel);
      }
      this.sideBtn = el('button', { class: 'btn btn-outline', title: '显示 / 隐藏信息侧栏', onclick: () => this.toggleSidebar() }, icon('side'), '信息');
      ctl.append(this.sideBtn);
      r.append(el('div', { class: 'vz-head' }, this.hEp, this.hMeta, this.hTask, ctl));
      // 证据芯片（mini）
      if (this.mode === 'mini' && this.evidence.length) {
        this.evList = el('div', { class: 'vz-evlist' }); r.append(this.evList);
      }
      // 主体
      this.grid = el('div', { class: 'vz-grid' }); this.gridWrap = el('div', { class: 'vz-grid-wrap' }, this.grid);
      this.sideTitle = el('span'); this.sideBody = el('div', { class: 'vz-side-body' });
      this.side = el('aside', { class: 'vz-side' }, el('div', { class: 'vz-side-head' }, icon('info'), this.sideTitle, el('span', { class: 'spacer' }), el('button', { class: 'x', title: '收起', onclick: () => this.toggleSidebar(false) }, '×')), this.sideBody);
      r.append(el('div', { class: 'vz-body' }, this.gridWrap, this.side));
      // 走带
      this.playBtn = el('button', { class: 'tb play', title: '播放 / 暂停（空格）', onclick: () => this.toggle() }, icon('play'));
      this.speedSel = el('select', { class: 'select', title: '播放速度', onchange: (e) => { this.speed = Number(e.target.value); } }, el('option', { value: '1' }, '1x'), el('option', { value: '1.5' }, '1.5x'), el('option', { value: '2' }, '2x'));
      this.timeEl = el('span', { class: 'time' });
      this.frameIn = el('input', { class: 'input', title: '输入帧号后回车跳转', onkeydown: (e) => { if (e.key === 'Enter') { this.seekFrame(Number(e.target.value)); e.target.blur(); } }, onblur: (e) => { this.seekFrame(Number(e.target.value)); } });
      this.frameTot = el('span');
      this.loopBtn = el('button', { class: 'switch', title: '循环播放', onclick: () => { this.loop = !this.loop; this.loopBtn.classList.toggle('on', this.loop); } });
      this.prog = el('div', { class: 'vz-prog' }, this.stepsEl = el('div', { class: 'steps' }), el('div', { class: 'track' }, el('i', { class: 'buf', style: 'width:100%' }), this.fillEl = el('i', { class: 'fill' })), this.knob = el('i', { class: 'knob' }), this.evEl = el('div', { class: 'ev' }), this.tipEl = el('span', { class: 'tip' }));
      this.prog.addEventListener('pointerdown', (e) => { this.drag = true; this.prog.setPointerCapture(e.pointerId); this.seekAt(e); });
      this.prog.addEventListener('pointermove', (e) => { this.hoverAt(e); if (this.drag) this.seekAt(e); });
      this.prog.addEventListener('pointerup', () => { this.drag = false; });
      const tr = el('div', { class: 'vz-transport' },
        el('button', { class: 'tb', title: '上一帧（←）', onclick: () => this.step(-1) }, icon('prev')), this.playBtn, el('button', { class: 'tb', title: '下一帧（→）', onclick: () => this.step(1) }, icon('next')),
        this.speedSel, this.prog, this.timeEl,
        el('span', { class: 'frame' }, '帧', this.frameIn, '/', this.frameTot),
        el('label', { class: 'loop' }, this.loopBtn, '循环'));
      if (this.mode === 'full') {
        // 选 episode 在页面左侧的侧栏里；走带上只留上一条 / 下一条
        tr.append(el('span', { class: 'epnav' }, el('button', { class: 'btn btn-outline', title: '上一条 episode', onclick: () => this.gotoEpisode(this.prevIndex()) }, '‹ 上一条'), el('button', { class: 'btn btn-outline', title: '下一条 episode', onclick: () => this.gotoEpisode(this.nextIndex()) }, '下一条 ›')));
      }
      tr.append(el('span', { class: 'vz-keys', title: '键盘：空格 播放 / 暂停，← → 逐帧，Shift + ← → 跳 1 秒' }, el('kbd', null, '␣'), ' ', el('kbd', null, '←'), el('kbd', null, '→')));
      r.append(tr);
      // 字幕栏
      this.subIdx = el('span', { class: 'idx' }); this.subLab = el('span', { class: 'lab' }); this.subRng = el('span', { class: 'rng' }); this.subTag = el('span', { class: 'tag tag-orange', style: 'display:none' }, '不合格'); this.subSrc = el('span', { class: 'src' });
      this.sub = el('div', { class: 'vz-sub' }, this.subIdx, this.subLab, this.subRng, this.subTag, this.subSrc);
      r.append(this.sub);
      this.renderHead(); this.renderEvidence();
    }
    renderHead() {
      const ep = this.ds.episodes || [];
      this.hEp.innerHTML = ''; this.hEp.append(this.ds.name, el('small', null, `ep ${this.ep.index}`));
      this.hMeta.innerHTML = ''; this.hMeta.append(`${this.ds.format}`, el('i'), `${this.ep.fps} fps`, el('i'), `${this.ep.frames} 帧 · ${fmtTime(this.ep.duration)}`);
      this.hTask.classList.toggle('none', !this.ep.task); this.hTask.innerHTML = '';
      if (this.ep.task) { this.hTask.append(el('span', { class: 'k' }, '任务'), this.ep.task); this.hTask.title = this.ep.task; } else this.hTask.append('无任务描述');
      this.frameTot.textContent = this.ep.frames - 1;
      this.stepsEl.innerHTML = '';
      for (const s of this.ep.steps || []) this.stepsEl.append(el('span', { class: s.quality === 'unqualified' ? 'unq' : '', style: `left:${(s.s / this.ep.duration) * 100}%;width:${((s.e - s.s) / this.ep.duration) * 100}%`, title: `${s.label} · ${s.s.toFixed(1)}–${s.e.toFixed(1)} s${s.quality === 'unqualified' ? ' · 不合格' : ''}`, onpointerdown: (e) => { e.stopPropagation(); this.seek(s.s); } }));
      this.sub.classList.toggle('on', !!(this.ep.steps && this.ep.steps.length));
      this.subSrc.textContent = this.ep.stepsSource ? `来源：${this.ep.stepsSource}` : '';
    }
    renderEvidence() {
      this.evEl.innerHTML = '';
      for (const ev of this.evidence) {
        const [s, e] = evRange(ev, this.ep); const w = ((e - s) / this.ep.duration) * 100;
        this.evEl.append(el('span', { class: `${ev.level}${ev.id === this.focusEv ? ' focus' : ''}${w < .4 ? ' pt' : ''}`, style: `left:${(s / this.ep.duration) * 100}%;width:${Math.max(.4, w)}%`, title: `${ev.label} · ${fmtTime(s)}–${fmtTime(e)}`, onpointerdown: (x) => { x.stopPropagation(); this.setFocusEvidence(ev.id); } }));
      }
      if (this.evList) {
        this.evList.innerHTML = ''; this.evList.append(el('span', { class: 'k' }, '发现'));
        for (const ev of this.evidence) this.evList.append(el('span', { class: `chip ${ev.level}${ev.id === this.focusEv ? ' on' : ''}`, onclick: () => this.setFocusEvidence(ev.id) }, el('i', { class: 'dot' }), el('code', null, ev.item), ev.label));
      }
    }
    setFocusEvidence(id) {
      this.focusEv = id; const ev = this.evidence.find((e) => e.id === id); this.renderEvidence();
      if (ev) { this.pause(); this.seek(evRange(ev, this.ep)[0]); if (this.opts.smartCells) { const c = this.opts.smartCells(ev); if (c) this.setCells(c.cells, c.cols, c.rows); } }
      for (const c of this.curves()) c.renderStatic();
    }
    // -- layout
    applyTemplate(name) {
      this.template = name; if (this.tplSel) this.tplSel.value = name;
      const W = this.gridWrap.clientWidth || 1000, cams = this.ds.cameras, grps = this.ds.groups;
      let cells = [], cols, rows;
      if (name === 'video') { cols = Math.min(3, Math.max(1, cams.length)); cells = cams.map((c) => ({ kind: 'video', ref: c.key })); rows = Math.ceil(cells.length / cols); }
      else if (name === 'curve') { cols = grps.length > 1 ? 2 : 1; cells = grps.map((g) => ({ kind: 'curve', ref: g.key })); rows = Math.ceil(cells.length / cols); }
      else if (name === 'custom') { return; }
      else { cols = (W - 24) >= 1000 ? 3 : 2; cells = cams.map((c) => ({ kind: 'video', ref: c.key })).concat(grps.slice(0, 2).map((g) => ({ kind: 'curve', ref: g.key }))); rows = Math.min(3, Math.ceil(cells.length / cols)); }
      this.setCells(cells, cols, rows);
    }
    setCells(cells, cols, rows) {
      this.cols = cols; this.rows = rows; this.max = null; this.focus = -1;
      const n = cols * rows; this.cells = cells.slice(0, n); while (this.cells.length < n) this.cells.push({ kind: 'empty' });
      if (this.gridSel) this.gridSel.value = `${cols}x${rows}`;
      this.renderGrid();
    }
    resize(cols, rows) { this.template = 'custom'; if (this.tplSel) this.tplSel.value = 'custom'; this.setCells(this.cells.filter((c) => c.kind !== 'empty'), cols, rows); }
    setCell(i, content) { this.cells[i] = content; this.template = 'custom'; if (this.tplSel) this.tplSel.value = 'custom'; this.renderGrid(); this.focusCell(i); }
    renderGrid() {
      this.grid.innerHTML = ''; this.views = [];
      this.cells.forEach((c, i) => {
        const cell = el('div', { class: `vz-cell kind-${c.kind}`, onpointerdown: () => this.focusCell(i) });
        const tools = el('div', { class: 'vz-tools' });
        if (c.kind === 'video') {
          const cam = this.cam(c.ref); const cv = el('canvas');
          // 「平台转码」：浏览器播不了的编码由 Daemon 转码时在格子上标出来（D62）
          cell.append(cv, el('span', { class: 'vz-cap' }, el('i', { class: 'dot', style: `background:${cam.color}` }), cam.title, cam.transcoded ? el('span', { class: 'vz-tc', title: `原始编码 ${cam.codec}，浏览器不能直接播放，由平台转为 H.264（容器开关 CURATOR_VIZ_TRANSCODE，默认开）` }, '平台转码') : null), el('span', { class: 'vz-stamp' }));
          this.views.push({ kind: 'video', cam, cv, ctx: cv.getContext('2d'), stamp: cell.lastChild });
        } else if (c.kind === 'curve') {
          const g = this.grp(c.ref); cell.append(el('span', { class: 'vz-cap', title: '实线是状态，虚线是动作' }, g.title + (g.unit ? ` · ${g.unit}` : '')));
          this.views.push({ kind: 'curve', curve: new Curve(cell, g, this) });
        } else {
          cell.append(el('div', { class: 'plus', onclick: (e) => { e.stopPropagation(); this.openPop(i, cell); } }, el('span', { class: 'ring' }, icon('plus')), '选择要看的内容'));
          this.views.push({ kind: 'empty' });
        }
        if (c.kind !== 'empty') {
          if (this.mode === 'full' || this.opts.allowSwap !== false) tools.append(el('button', { title: '更换这个格子显示的内容', onclick: (e) => { e.stopPropagation(); this.openPop(i, cell); } }, icon('swap'), '更换'));
          tools.append(el('button', { title: this.max === i ? '还原' : '占满播放器', onclick: (e) => { e.stopPropagation(); this.maximize(this.max === i ? null : i); } }, icon(this.max === i ? 'shrink' : 'expand'), this.max === i ? '还原' : '放大'));
          if (this.mode === 'full') tools.append(el('button', { title: '清空这个格子', onclick: (e) => { e.stopPropagation(); this.setCell(i, { kind: 'empty' }); } }, '×'));
          cell.append(tools);
        }
        if (this.max != null && this.max !== i) cell.style.display = 'none';
        if (this.focus === i) cell.classList.add('is-focus');
        this.grid.append(cell);
      });
      this.relayout(); this.renderSide();
    }
    relayout() {
      const W = this.gridWrap.clientWidth - 24, gap = 10; if (W <= 0) return;
      // 智能布局跟着可用宽度变：侧栏挤压后不够放三列就降到两列（反之升回去）
      if (this.mode === 'full' && this.template === 'smart' && this.max == null) { const want = W >= 1000 ? 3 : 2; if (want !== this.cols) { this.applyTemplate('smart'); return; } }
      const cellH = clamp(((W - gap * (this.cols - 1)) / this.cols) * .66, 160, 420);
      if (this.max != null) { this.grid.style.gridTemplateColumns = '1fr'; this.grid.style.gridAutoRows = `${cellH * this.rows + gap * (this.rows - 1)}px`; }
      else { this.grid.style.gridTemplateColumns = `repeat(${this.cols}, minmax(0, 1fr))`; this.grid.style.gridAutoRows = `${cellH}px`; }
      for (const v of this.views) {
        if (v.kind === 'video') { const r = v.cv.getBoundingClientRect(); const dpr = window.devicePixelRatio || 1; v.cv.width = Math.max(2, r.width * dpr); v.cv.height = Math.max(2, r.height * dpr); v.dirty = true; }
        if (v.kind === 'curve') v.curve.layout();
      }
      this.update(true);
    }
    maximize(i) { this.max = i; this.renderGrid(); if (i != null) this.focusCell(i); }
    focusCell(i) { this.focus = i; [...this.grid.children].forEach((c, j) => c.classList.toggle('is-focus', j === i)); this.renderSide(); }
    toggleSidebar(force) { this.sideOpen = force == null ? !this.sideOpen : force; this.root.classList.toggle('side-open', this.sideOpen); this.sideBtn.classList.toggle('on', this.sideOpen); this.renderSide(); this.relayout(); }
    curves() { return this.views.filter((v) => v.kind === 'curve').map((v) => v.curve); }
    cam(key) { return this.ds.cameras.find((c) => c.key === key); }
    grp(key) { return this.ds.groups.find((g) => g.key === key); }

    openPop(i, cell) {
      this.closePop(); const cur = this.cells[i];
      const pop = el('div', { class: 'vz-pop' }, el('div', { class: 'grp' }, '相机'));
      for (const c of this.ds.cameras) pop.append(el('div', { class: `it${cur.kind === 'video' && cur.ref === c.key ? ' cur' : ''}`, onclick: () => { this.setCell(i, { kind: 'video', ref: c.key }); this.closePop(); } }, el('i', { class: 'dot', style: `color:${c.color}` }), c.title, el('span', { class: 'sub' }, `${c.w}×${c.h} · ${c.codec}`)));
      pop.append(el('div', { class: 'grp' }, '运动曲线'));
      for (const g of this.ds.groups) pop.append(el('div', { class: `it${cur.kind === 'curve' && cur.ref === g.key ? ' cur' : ''}`, onclick: () => { this.setCell(i, { kind: 'curve', ref: g.key }); this.closePop(); } }, icon('curve'), g.title, el('span', { class: 'sub' }, `${g.dims.length} 条`)));
      pop.append(el('div', { class: 'grp' }, '其他'));
      for (const [n, why] of this.ds.extras || []) pop.append(el('div', { class: 'it dis', title: why }, n, el('span', { class: 'sub' }, '本稿未实现')));
      const r = cell.getBoundingClientRect(), g = this.gridWrap.getBoundingClientRect();
      pop.style.left = `${Math.min(r.right - g.left - 236, g.width - 240)}px`; pop.style.top = `${r.top - g.top + 36}px`;
      this.gridWrap.style.position = 'relative'; this.gridWrap.append(pop); this.pop = pop;
    }
    closePop() { if (this.pop) { this.pop.remove(); this.pop = null; } }

    // -- sidebar
    renderSide() {
      if (!this.sideOpen) return; const c = this.cells[this.focus]; const b = this.sideBody; b.innerHTML = '';
      if (!c || c.kind === 'empty') { this.sideTitle.textContent = '信息'; b.append(el('div', { class: 'empty' }, c ? '空格子：点「+」选择要看的内容' : '点一个格子查看它的信息')); return; }
      if (c.kind === 'video') {
        const cam = this.cam(c.ref); this.sideTitle.textContent = `相机 · ${cam.title}`;
        const kv = (k, v) => el('div', { class: 'kv' }, el('span', { class: 'k' }, k), el('span', { class: 'v' }, v));
        b.append(el('div', { class: 'sec' }, '视频流'), kv('键名', cam.feature), kv('分辨率', `${cam.w} × ${cam.h}`), kv('编码', cam.transcoded ? `${cam.codec} · ${cam.pix} → 播放用 h264（平台转码）` : `${cam.codec} · ${cam.pix}`), kv('帧率', `${cam.fps} fps`), kv('帧数', `${this.ep.frames}`), kv('时长', fmtTime(this.ep.duration)),
          el('div', { class: 'sec' }, '来源'), kv('文件', el('span', { class: 'mono' }, cam.path(this.ep))), kv('时间范围', cam.range ? cam.range(this.ep) : '整个文件'), kv('读取方式', cam.transcoded ? el('span', null, el('span', { class: 'tag tag-orange', style: 'height:20px;margin-right:6px' }, '平台转码'), cam.access) : cam.access),
          el('div', { class: 'sec' }, '当前'), kv('帧号', el('span', { id: 'vzSideFrame' }, '')), kv('时间', el('span', { id: 'vzSideTime' }, '')),
          el('div', { style: 'margin-top:14px' }, el('button', { class: 'btn btn-outline', style: 'height:28px;font-size:12px', onclick: () => toast('真实页面：用预签名地址在新标签打开源文件') }, '在新标签打开源文件')));
      } else {
        const g = this.grp(c.ref), cv = this.curves().find((x) => x.group === g); this.sideTitle.textContent = `曲线 · ${g.title}`;
        const kv = (k, v) => el('div', { class: 'kv' }, el('span', { class: 'k' }, k), el('span', { class: 'v' }, v));
        b.append(el('div', { class: 'sec' }, '字段'), kv('状态', el('span', { class: 'mono' }, `${this.ds.stateKey}[${g.dims.join(',')}]`)), kv('动作', el('span', { class: 'mono' }, `${this.ds.actionKey}[${g.dims.join(',')}]`)), kv('单位', g.unit || '—'), kv('采样', `${this.ep.fps} Hz · ${this.ep.frames} 点`));
        b.append(el('div', { class: 'sec' }, '序列（勾选显示 · 当前值：状态 / 动作）'));
        this.sideSeries = g.dims.map((d, i) => {
          const c = PALETTE[i % PALETTE.length];
          const row = el('div', { class: 'sl' }, el('input', { type: 'checkbox', checked: !cv || !cv.off.has(d), onchange: () => cv && cv.lis[i].click() }), el('span', { class: 'sw', style: `color:${c}` }), el('span', { class: 'nm', title: g.names[i] }, g.names[i]), el('span', { class: 'val' }, ''));
          b.append(row); return { d, row };
        });
        b.append(el('div', { class: 'sec' }, '说明'), el('div', { style: 'color:var(--text-3);line-height:1.6' }, '实线是记录到的状态，虚线是下发的动作；两者同名维度叠在一起画。点图可以跳到那一刻。'));
      }
      this.updateSide();
    }
    updateSide() {
      if (!this.sideOpen) return; const f = this.frame(), D = this.ep.dims;
      const fr = document.getElementById('vzSideFrame'); if (fr) { fr.textContent = f; document.getElementById('vzSideTime').textContent = fmtTime(this.t); }
      if (this.sideSeries && this.cells[this.focus]?.kind === 'curve') for (const s of this.sideSeries) s.row.lastChild.textContent = `${fmtNum(this.ep.state[f * D + s.d])} / ${fmtNum(this.ep.action[f * D + s.d])}`;
    }

    // -- time
    frame() { return clamp(Math.round(this.t * this.ep.fps), 0, this.ep.frames - 1); }
    seek(t) { this.t = clamp(t, 0, this.ep.duration - 1 / this.ep.fps); this.update(true); }
    seekFrame(f) { if (Number.isFinite(f)) this.seek(clamp(Math.round(f), 0, this.ep.frames - 1) / this.ep.fps); else this.update(true); }
    step(n) { this.pause(); this.seek((this.frame() + n) / this.ep.fps); }
    play() { if (this.t >= this.ep.duration - 1 / this.ep.fps) this.t = 0; this.playing = true; this.last = performance.now(); this.playBtn.innerHTML = ''; this.playBtn.append(icon('pause')); }
    pause() { this.playing = false; this.playBtn.innerHTML = ''; this.playBtn.append(icon('play')); }
    toggle() { this.playing ? this.pause() : this.play(); }
    seekAt(e) { const r = this.prog.getBoundingClientRect(); this.seek(((e.clientX - r.left) / r.width) * this.ep.duration); }
    hoverAt(e) { const r = this.prog.getBoundingClientRect(); const k = clamp((e.clientX - r.left) / r.width, 0, 1); const t = k * this.ep.duration; this.tipEl.style.left = `${k * 100}%`; const st = (this.ep.steps || []).find((s) => t >= s.s && t < s.e); this.tipEl.textContent = `${fmtTime(t)} · 帧 ${Math.round(t * this.ep.fps)}${st ? ' · ' + st.label : ''}`; }
    key(e) {
      if (!this.active || /INPUT|SELECT|TEXTAREA/.test(e.target.tagName)) return;
      if (e.code === 'Space') { e.preventDefault(); this.toggle(); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); e.shiftKey ? (this.pause(), this.seek(this.t - 1)) : this.step(-1); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); e.shiftKey ? (this.pause(), this.seek(this.t + 1)) : this.step(1); }
    }
    tick(now) {
      const dt = (now - this.last) / 1000; this.last = now;
      if (this.playing) {
        this.t += dt * this.speed;
        if (this.t >= this.ep.duration) { if (this.loop) this.t = 0; else { this.t = this.ep.duration - 1 / this.ep.fps; this.pause(); } }
        this.update();
      } else if (this.needsDraw) this.update(true);
      this.raf = requestAnimationFrame((n) => this.tick(n));
    }
    update(force) {
      const ep = this.ep, t = this.t, f = this.frame(); const pct = (t / ep.duration) * 100;
      this.fillEl.style.width = `${pct}%`; this.knob.style.left = `${pct}%`;
      this.timeEl.textContent = `${fmtTime(t)} / ${fmtTime(ep.duration)}`;
      if (document.activeElement !== this.frameIn) this.frameIn.value = f;
      for (const v of this.views || []) {
        if (v.kind === 'video') {
          const cv = v.cv, W = cv.width, H = cv.height; const ctx = v.ctx; ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.fillStyle = '#0F141B'; ctx.fillRect(0, 0, W, H);
          let w = W, h = (W * v.cam.h) / v.cam.w; if (h > H) { h = H; w = (H * v.cam.w) / v.cam.h; } const ox = (W - w) / 2, oy = (H - h) / 2;
          ctx.save(); ctx.translate(ox, oy); ctx.beginPath(); ctx.rect(0, 0, w, h); ctx.clip(); (CAM_DRAW[v.cam.draw] || CAM_DRAW.head)(ctx, w, h, ep, t); ctx.restore();
          v.stamp.textContent = `${fmtTime(t)} · 帧 ${f}`;
        } else if (v.kind === 'curve') v.curve.update(t);
      }
      // 字幕
      if (ep.steps && ep.steps.length) {
        const i = ep.steps.findIndex((s) => t >= s.s && t < s.e); const s = ep.steps[i];
        if (s) { this.subIdx.textContent = `步骤 ${i + 1}/${ep.steps.length}`; this.subLab.textContent = s.label; this.subRng.textContent = `${s.s.toFixed(1)}–${s.e.toFixed(1)} s`; this.subTag.style.display = s.quality === 'unqualified' ? '' : 'none'; }
        else { this.subIdx.textContent = ''; this.subLab.textContent = '（无标注）'; this.subRng.textContent = ''; this.subTag.style.display = 'none'; }
      }
      this.updateSide(); this.needsDraw = false;
    }
    prevIndex() { const l = this.ds.episodes || []; const k = l.indexOf(this.ep.index); return k > 0 ? l[k - 1] : null; }
    nextIndex() { const l = this.ds.episodes || []; const k = l.indexOf(this.ep.index); return k >= 0 && k < l.length - 1 ? l[k + 1] : null; }
    gotoEpisode(i) {
      const list = this.ds.episodes || []; if (i == null || !list.includes(i)) { toast(i == null ? '已经是第一条 / 最后一条' : `没有 ep ${i}`); return; }
      this.pause(); const ep = this.opts.onEpisodeChange ? this.opts.onEpisodeChange(i) : null; if (!ep) return;
      this.setEpisode(ep);
    }
    setEpisode(ep) { this.ep = ep; this.t = 0; this.renderHead(); this.renderEvidence(); for (const c of this.curves()) { c.buildLegend(); c.layout(); } this.update(true); this.renderSide(); }
  }

  function icon(name) {
    const d = {
      play: '<path d="M5 3.5v9l7.5-4.5z" fill="currentColor"/>', pause: '<path d="M4.5 3.5h2.5v9H4.5zM9 3.5h2.5v9H9z" fill="currentColor"/>',
      prev: '<path d="M4 3.5v9M12 3.5L6 8l6 4.5z" fill="currentColor" stroke="currentColor" stroke-width="1.2"/>', next: '<path d="M12 3.5v9M4 3.5L10 8l-6 4.5z" fill="currentColor" stroke="currentColor" stroke-width="1.2"/>',
      plus: '<path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>', swap: '<path d="M3 5.5h8.5L9 3M13 10.5H4.5L7 13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>',
      expand: '<path d="M3 6.5V3h3.5M13 6.5V3H9.5M3 9.5V13h3.5M13 9.5V13H9.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>', shrink: '<path d="M6.5 3v3.5H3M9.5 3v3.5H13M6.5 13V9.5H3M9.5 13V9.5H13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
      side: '<rect x="2" y="3" width="12" height="10" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M10 3v10" stroke="currentColor" stroke-width="1.3"/>', info: '<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M8 7v4M8 5v.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
      curve: '<path d="M2 12c2-6 4-6 6-2s4 2 6-4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>',
    }[name] || '';
    const s = document.createElementNS(svgNS, 'svg'); s.setAttribute('width', 16); s.setAttribute('height', 16); s.setAttribute('viewBox', '0 0 16 16'); s.innerHTML = d; return s;
  }

  // ---------- the sample dataset used by the mockups ----------
  const SAMPLE = {
    id: 'ds-9f3k2m1qa', name: 'galaxea_open_world_kitchen', format: 'LeRobot v2.1', fps: 30, robot: 'Galaxea R1 Lite', region: '华北 2（北京）',
    uri: 'tos://pai-kit-datasets/lerobot/galaxea_open_world_kitchen/', episodes: Array.from({ length: 60 }, (_, i) => i), totalFrames: 38520, size: '2.1 GB',
    stateKey: 'observation.state', actionKey: 'action', dims: 14, stepsSource: 'meta/episodes.jsonl 的 subtasks（数据集自带分步）',
    names: ['left_joint_0', 'left_joint_1', 'left_joint_2', 'left_joint_3', 'left_joint_4', 'left_joint_5', 'left_gripper', 'right_joint_0', 'right_joint_1', 'right_joint_2', 'right_joint_3', 'right_joint_4', 'right_joint_5', 'right_gripper'],
    tasks: ['Pick up the apple and put it into the bowl / 把苹果放进碗里', '把杯子放到托盘上 / Place the cup on the tray', 'Open the drawer and take out the spoon / 打开抽屉拿出勺子'],
    cameras: [
      { key: 'head', title: 'head', feature: 'observation.images.head', w: 640, h: 480, codec: 'av1', pix: 'yuv420p', fps: 30, color: '#165DFF', draw: 'head', access: '预签名地址直连 TOS（D16、D55）', path: (ep) => `videos/chunk-000/observation.images.head/episode_${String(ep.index).padStart(6, '0')}.mp4` },
      { key: 'left_wrist', title: 'left_wrist', feature: 'observation.images.left_wrist', w: 640, h: 480, codec: 'av1', pix: 'yuv420p', fps: 30, color: '#0DA5AA', draw: 'left_wrist', access: '预签名地址直连 TOS（D16、D55）', path: (ep) => `videos/chunk-000/observation.images.left_wrist/episode_${String(ep.index).padStart(6, '0')}.mp4` },
      // 第三路故意写成 mpeg4（FastUMI 那种浏览器放不了的编码），用来演示「平台转码」标签
      { key: 'right_wrist', title: 'right_wrist', feature: 'observation.images.right_wrist', w: 640, h: 480, codec: 'mpeg4', pix: 'yuv420p', fps: 30, color: '#E8590C', draw: 'right_wrist', transcoded: true, access: 'Daemon 转码后的 fMP4（首次打开生成，按指纹缓存）', path: (ep) => `videos/chunk-000/observation.images.right_wrist/episode_${String(ep.index).padStart(6, '0')}.mp4` },
    ],
    groups: [
      { key: 'left_arm', title: '左臂关节位置', unit: 'rad', dims: [0, 1, 2, 3, 4, 5], names: ['left_joint_0', 'left_joint_1', 'left_joint_2', 'left_joint_3', 'left_joint_4', 'left_joint_5'] },
      { key: 'right_arm', title: '右臂关节位置', unit: 'rad', dims: [7, 8, 9, 10, 11, 12], names: ['right_joint_0', 'right_joint_1', 'right_joint_2', 'right_joint_3', 'right_joint_4', 'right_joint_5'] },
      { key: 'grippers', title: '夹爪开度', unit: '', dims: [6, 13], names: ['left_gripper', 'right_gripper'] },
    ],
    extras: [['深度图', '这个数据集没有深度流'], ['末端轨迹（3D）', '第二期'], ['同步曲线（质检任务产出）', '只在质检报告的可视化里提供']],
  };

  return { Player, makeEpisode, SAMPLE, PALETTE, fmtTime, el, icon };
})();
