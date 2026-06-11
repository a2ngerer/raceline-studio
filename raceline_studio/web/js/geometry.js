/* Geometry + color helpers. Pure functions, no DOM, no state. */

export function clamp(x, lo, hi) { return x < lo ? lo : x > hi ? hi : x; }

export function fmt(x, d = 2) {
  return (x === null || x === undefined || !isFinite(x)) ? "—" : x.toFixed(d);
}

/* ---- arc length ---- */
export function arcLengthData(pts, closed) {
  const n = pts.length;
  const s = new Float64Array(n);
  let acc = 0;
  for (let i = 1; i < n; i++) {
    acc += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    s[i] = acc;
  }
  let total = acc;
  if (closed && n > 1) {
    total += Math.hypot(pts[0][0] - pts[n - 1][0], pts[0][1] - pts[n - 1][1]);
  }
  return { s, total };
}

/* per-point spacing ds (mirror of the backend's _ds_closed) */
export function dsOf(pts, closed) {
  const n = pts.length;
  const ds = new Float64Array(n);
  for (let i = 0; i < n - 1; i++) {
    ds[i] = Math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]);
  }
  ds[n - 1] = closed && n > 1
    ? Math.hypot(pts[0][0] - pts[n - 1][0], pts[0][1] - pts[n - 1][1])
    : (n > 1 ? ds[n - 2] : 0.1);
  return ds;
}

/* ---- curvature radius (three-point fit, |1/kappa|) ---- */
export function radiiOf(pts, closed) {
  const n = pts.length;
  const out = new Float64Array(n);
  if (n < 3) { out.fill(1e4); return out; }
  for (let i = 0; i < n; i++) {
    const ip = closed ? (i - 1 + n) % n : Math.max(0, i - 1);
    const inx = closed ? (i + 1) % n : Math.min(n - 1, i + 1);
    const v1x = pts[i][0] - pts[ip][0], v1y = pts[i][1] - pts[ip][1];
    const v2x = pts[inx][0] - pts[i][0], v2y = pts[inx][1] - pts[i][1];
    const l1 = Math.hypot(v1x, v1y), l2 = Math.hypot(v2x, v2y);
    const cross = v1x * v2y - v1y * v2x;
    const dot = v1x * v2x + v1y * v2y;
    const dth = Math.atan2(cross, dot);
    const ds = 0.5 * (l1 + l2);
    const k = ds > 1e-9 ? dth / ds : 0;
    out[i] = Math.abs(k) > 1e-4 ? Math.abs(1 / k) : 1e4;
  }
  return out;
}

export function lapTimeOf(pts, V, closed) {
  if (!V || V.length !== pts.length || !pts.length) return null;
  const ds = dsOf(pts, closed);
  let t = 0;
  for (let i = 0; i < V.length; i++) t += ds[i] / Math.max(V[i], 1e-3);
  return t;
}

/* ---- picking (brute force is fine for ≤ a few thousand points) ---- */
export function nearestPoint(pts, x, y) {
  let best = -1, bd = Infinity;
  for (let i = 0; i < pts.length; i++) {
    const dx = pts[i][0] - x, dy = pts[i][1] - y;
    const d = dx * dx + dy * dy;
    if (d < bd) { bd = d; best = i; }
  }
  return { idx: best, dist: Math.sqrt(bd) };
}

/* Gaussian index-window brush. Calls fn(idx, weight) for |di| <= 3 sigma. */
export function gaussianApply(n, center, sigma, closed, fn) {
  const span = Math.max(1, Math.ceil(3 * sigma));
  const s2 = 2 * sigma * sigma;
  for (let di = -span; di <= span; di++) {
    let j = center + di;
    if (closed) j = ((j % n) + n) % n;
    else if (j < 0 || j >= n) continue;
    fn(j, Math.exp(-(di * di) / s2));
  }
}

/* one light Laplacian smoothing pass */
export function laplacian(pts, closed, factor = 0.25) {
  const n = pts.length;
  const out = new Array(n);
  for (let i = 0; i < n; i++) {
    const ip = closed ? (i - 1 + n) % n : Math.max(0, i - 1);
    const inx = closed ? (i + 1) % n : Math.min(n - 1, i + 1);
    out[i] = [
      pts[i][0] + factor * (0.5 * (pts[ip][0] + pts[inx][0]) - pts[i][0]),
      pts[i][1] + factor * (0.5 * (pts[ip][1] + pts[inx][1]) - pts[i][1]),
    ];
  }
  return out;
}

/* ---- polygons ---- */
export function pointInPoly(x, y, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
    if (((yi > y) !== (yj > y))
        && (x < (xj - xi) * (y - yi) / ((yj - yi) || 1e-12) + xi)) {
      inside = !inside;
    }
  }
  return inside;
}

export function polyCentroid(poly) {
  let cx = 0, cy = 0;
  for (const [x, y] of poly) { cx += x; cy += y; }
  return [cx / poly.length, cy / poly.length];
}

/* ---- semantic colors (meaning fixed by the spec) ---- */
export const COL = {
  bad: "#ff4d4d", warn: "#ffb340", ok: "#34e07e",
  acc: "#45e3ff", certPP: "#5b8cff", certRE: "#ff5b7a", carpet: "#8f7bff",
};

export function radiusColor(r, Rmin, Rsafe) {
  if (r < Rmin) return COL.bad;
  if (r < Rsafe) return COL.warn;
  return COL.ok;
}

/* green -> yellow -> red ramp, t in [0,1] */
export function speedColor(t) {
  t = clamp(t, 0, 1);
  let r, g;
  if (t < 0.5) { r = Math.round(2 * t * 255); g = 224; }
  else { r = 255; g = Math.round(224 * (1 - (t - 0.5) * 2) + 77 * (t - 0.5) * 2); }
  return `rgb(${r},${g},77)`;
}

/* certainty: red (0) -> grey (0.5) -> blue (1) */
export function certColor(score, alpha = 0.32) {
  const t = clamp(score, 0, 1);
  const mix = (a, b, u) => Math.round(a + (b - a) * u);
  let r, g, b;
  if (t < 0.5) {
    const u = t / 0.5;
    r = mix(255, 120, u); g = mix(91, 126, u); b = mix(122, 134, u);
  } else {
    const u = (t - 0.5) / 0.5;
    r = mix(120, 91, u); g = mix(126, 140, u); b = mix(134, 255, u);
  }
  return `rgba(${r},${g},${b},${alpha})`;
}
