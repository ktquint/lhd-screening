// Ported from backend/lhd_processor/hydraulics.py (Simplified-mode formulas
// used by the screening pipeline + the flip-depth formula used by
// analysis_classes.plot_flip_sequent). Inputs/outputs are SI throughout:
// Q in cms, L and P and depths in m. The display layer converts.
(function (global) {
    'use strict';

    const G = 9.81;     // m/s^2
    const C_W = 0.62;
    const M_TO_FT = 3.28084;
    const CMS_TO_CFS = 35.3147;

    // Real roots of ax^3 + bx^2 + cx + d via Cardano. Returns 1 or 3 reals.
    function cubicRealRoots(a, b, c, d) {
        if (a === 0) return [];
        const p = (3 * a * c - b * b) / (3 * a * a);
        const q = (2 * b * b * b - 9 * a * b * c + 27 * a * a * d) / (27 * a * a * a);
        const shift = -b / (3 * a);
        const disc = q * q / 4 + p * p * p / 27;
        const roots = [];
        if (disc > 0) {
            const sqrtD = Math.sqrt(disc);
            const u = Math.cbrt(-q / 2 + sqrtD);
            const v = Math.cbrt(-q / 2 - sqrtD);
            roots.push(u + v + shift);
        } else if (Math.abs(disc) < 1e-15) {
            const u = Math.cbrt(-q / 2);
            roots.push(2 * u + shift);
            roots.push(-u + shift);
        } else {
            const r = Math.sqrt(-p * p * p / 27);
            const phi = Math.acos(Math.max(-1, Math.min(1, -q / (2 * r))));
            const m = 2 * Math.cbrt(r);
            for (let k = 0; k < 3; k++) {
                roots.push(m * Math.cos((phi + 2 * Math.PI * k) / 3) + shift);
            }
        }
        return roots;
    }

    // Bisection root-finder. Mirrors scipy.optimize.brentq's "ValueError if
    // bracket doesn't bracket a root" contract; returns null in that case.
    function bisect(f, lo, hi, tol = 1e-8, maxIter = 100) {
        let flo = f(lo);
        let fhi = f(hi);
        if (!isFinite(flo) || !isFinite(fhi) || flo * fhi > 0) return null;
        if (flo === 0) return lo;
        if (fhi === 0) return hi;
        for (let i = 0; i < maxIter; i++) {
            const mid = 0.5 * (lo + hi);
            const fmid = f(mid);
            if (!isFinite(fmid)) return null;
            if (Math.abs(fmid) < tol || (hi - lo) * 0.5 < tol) return mid;
            if (fmid * flo < 0) { hi = mid; fhi = fmid; }
            else { lo = mid; flo = fmid; }
        }
        return 0.5 * (lo + hi);
    }

    // H = ((3/2)(Q/L) / C_W / sqrt(2g))^(2/3)  -- weir_H_simp
    function weirHSimp(Q, L) {
        if (Q <= 0 || L <= 0) return NaN;
        return Math.pow((3 / 2) * (Q / L) / C_W / Math.sqrt(2 * G), 2 / 3);
    }

    // Smallest positive real root of (Y1/H)^3 - (1+P/H)(Y1/H)^2 + (4/9)C_W^2(1+C_L) = 0
    // C_L = 0.1 * P/H. Returns Y1 in m, or null.
    function solveY1(H, P) {
        if (!(H > 0) || !(P > 0)) return null;
        const CL = 0.1 * P / H;
        const roots = cubicRealRoots(1, -(1 + P / H), 0, (4 / 9) * C_W * C_W * (1 + CL));
        const pos = roots.filter(r => r > 0).sort((x, y) => x - y);
        if (!pos.length) return null;
        return pos[0] * H;
    }

    // solve_Fr_simp: bracket [1, 20]; on failure mirror Python and return 1.0.
    function solveFrSimp(H, P) {
        if (!(H > 0) || !(P > 0)) return null;
        const CL = 0.1 * P / H;
        const ratio = 1 + P / H;
        const f = (Fr) => {
            const t1 = Math.pow((9 / (4 * C_W * C_W)) * 0.5 * Fr * Fr, 1 / 3) * ratio;
            const t2 = 0.5 * Fr * Fr * (1 + CL);
            return 1 - t1 + t2;
        };
        const root = bisect(f, 1.0, 20.0);
        return root === null ? 1.0 : root;
    }

    // Bélanger: Y2 = (Y1/2) * (-1 + sqrt(1 + 8 Fr1^2)). Returns m, or null.
    function calcY2Simp(H, P) {
        const y1 = solveY1(H, P);
        if (y1 === null) return null;
        const Fr1 = solveFrSimp(H, P);
        if (Fr1 === null) return null;
        return (y1 / 2) * (-1 + Math.sqrt(1 + 8 * Fr1 * Fr1));
    }

    // compute_y_flip_adv: solves a 1-D residual for H_flip, then returns
    // s_final*H_flip + P where s_final = max(0.48, (1 - 0.1*P/H)/1.1).
    function computeYFlipAdv(Q, L, P) {
        if (!(P > 0) || !(Q > 0) || !(L > 0)) return null;
        const Hguess = weirHSimp(Q, L);
        if (!isFinite(Hguess) || Hguess <= 0) return null;
        const sqrt2g = Math.sqrt(2 * G);
        const sqrt2_5 = Math.sqrt(2 / 5);
        const f = (H) => {
            const sLeut = (1 - 0.1 * (P / H)) / 1.1;
            const sLim = Math.max(0.48, sLeut);
            const Cw = (sLim * sLim >= 1) ? sqrt2_5
                                          : sqrt2_5 * Math.sqrt(1 - sLim * sLim);
            return (2 / 3) * Cw * L * sqrt2g * Math.pow(H, 1.5) - Q;
        };
        const Hflip = bisect(f, 0.01 * Hguess, 50.0 * Hguess);
        if (Hflip === null) return null;
        const sFinal = Math.max(0.48, (1 - 0.1 * (P / Hflip)) / 1.1);
        return sFinal * Hflip + P;
    }

    // Linear interpolation on a sorted xs array. Clamps at the edges.
    function interpLinear(x, xs, ys) {
        if (x <= xs[0]) return ys[0];
        if (x >= xs[xs.length - 1]) return ys[ys.length - 1];
        let lo = 0, hi = xs.length - 1;
        while (hi - lo > 1) {
            const mid = (lo + hi) >> 1;
            if (xs[mid] <= x) lo = mid; else hi = mid;
        }
        const t = (x - xs[lo]) / (xs[hi] - xs[lo]);
        return ys[lo] + t * (ys[hi] - ys[lo]);
    }

    // Build the three curves for plotting. Returns ft on y, cfs on x.
    //   P_ft, L_ft : dam height + crest length (display units; converted to m)
    //   twFn       : function (Q_cms) → tailwater depth (m)
    //   qMaxCms    : right edge of x range; left edge is qMaxCms/200
    //   nPoints    : sample count (default 80)
    function buildRatingCurvesFt(P_ft, L_ft, twFn, qMaxCms, nPoints = 80) {
        const P = P_ft / M_TO_FT;
        const L = L_ft / M_TO_FT;
        const qMin = Math.max(qMaxCms / 200, 0.05);
        const qs = [];
        const logLo = Math.log(qMin);
        const logHi = Math.log(qMaxCms);
        for (let i = 0; i < nPoints; i++) {
            qs.push(Math.exp(logLo + (logHi - logLo) * i / (nPoints - 1)));
        }
        const tailwater = [];
        const conjugate = [];
        const flip = [];
        const dangerConj = [];
        const dangerFlip = [];
        for (const Q of qs) {
            const Qcfs = Q * CMS_TO_CFS;
            const yt = twFn(Q);
            const H  = weirHSimp(Q, L);
            const y2 = calcY2Simp(H, P);
            const yf = computeYFlipAdv(Q, L, P);

            const ytFt = (yt !== null && isFinite(yt)) ? yt * M_TO_FT : null;
            const y2Ft = y2 !== null ? y2 * M_TO_FT : null;
            const yfFt = yf !== null ? yf * M_TO_FT : null;

            tailwater.push({ x: Qcfs, y: ytFt });
            conjugate.push({ x: Qcfs, y: y2Ft });
            flip.push     ({ x: Qcfs, y: yfFt });

            const isDanger = (ytFt !== null && y2Ft !== null && yfFt !== null) && (ytFt >= y2Ft && ytFt <= yfFt);
            dangerConj.push({ x: Qcfs, y: isDanger ? y2Ft : null });
            dangerFlip.push({ x: Qcfs, y: isDanger ? yfFt : null });
        }
        return { tailwater, conjugate, flip, dangerConj, dangerFlip };
    }

    global.LHDHydraulics = {
        weirHSimp, solveY1, solveFrSimp, calcY2Simp, computeYFlipAdv,
        interpLinear, buildRatingCurvesFt, bisect,
        constants: { G, C_W, M_TO_FT, CMS_TO_CFS },
    };
})(window);
