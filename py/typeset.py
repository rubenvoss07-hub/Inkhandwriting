#!/usr/bin/env python3
"""
Phase 2: turn text into placed ink strokes using the captured handwriting library.

Pipeline:
  load samples.json -> normalize each glyph (baseline=0, y-up, x-height=1 unit,
  left-aligned, advance width) -> lay out text (word wrap) -> place glyph strokes
  with light jitter + sample variety -> derive per-point WIDTH from pen velocity
  -> emit (a) an SVG preview (variable-width ribbons) and (b) doc.json, a neutral
  document model pages[]->strokes[]->points[]{x,y,w} for the GoodNotes writer.

Usage:
  voice_python.exe tools/typeset.py "Your text here"  [--size 14] [--out preview]
"""
import sys, json, math, os, hashlib

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(DIR, 'assets', 'glyphs.json')

# ---- page + layout params (preview coordinate space; mapped to GoodNotes units in Phase 3) ----
PAGE_W, PAGE_H = 768.0, 1024.0     # arbitrary preview page (points-ish)
MARGIN_L, MARGIN_R, MARGIN_T = 60.0, 50.0, 90.0
MARGIN_B = 60.0                    # bottom reserve (keeps last line's descenders on the page)
XH_PX = 15.0                        # how many px one x-height unit maps to (sets text size)
LINE_SPACING = 2.4                 # in x-height units
BASELINES = None                   # if set (list of page-y per line), text sits on THESE
                                   # exact baselines (the paper's ruled lines) instead of a
                                   # computed/justified pitch. One text line per entry.
SPACE_ADV = 1.15                   # word space width in x-height units
SIDE_BEARING = 0.18                # per-side glyph spacing in x-height units
JITTER_ROT = 0.04                  # per-glyph rotation sd, RADIANS
JITTER_SCALE = 0.04                # per-glyph size variation
JITTER_Y = 0.04                    # per-glyph vertical wobble (fraction of x-height)
# velocity->width mapping (in x-height units)
W_MIN, W_MAX = 0.05, 0.14
SMOOTH_PASSES = 3                  # moving-average passes to de-jagged captured strokes

MATH = set("0123456789~°±²³·¼½×Ø÷ø–‰′″√∛∞≈≝≠≡≤≥")

# ---------- deterministic RNG (mulberry32-ish) ----------
class RNG:
    def __init__(self, seed): self.s = seed & 0xffffffff
    def next(self):
        self.s = (self.s + 0x6D2B79F5) & 0xffffffff
        t = self.s
        t = (t ^ (t >> 15)) * (1 | t) & 0xffffffff
        t = (t + ((t ^ (t >> 7)) * (61 | t) & 0xffffffff)) & 0xffffffff
        return ((t ^ (t >> 14)) & 0xffffffff) / 4294967296
    def gauss(self, sd):
        u = self.next() or 1e-9; v = self.next() or 1e-9
        return sd * math.sqrt(-2*math.log(u)) * math.cos(2*math.pi*v)

# ---------- load + normalize ----------
def load_lib():
    d = json.load(open(LIB, encoding='utf-8'))
    g = d['glyphs']; cell = d['meta']['cell']; guide = d['meta']['guide']
    H = cell['h']
    y_base = guide['baseline'] * H
    xheight_px = (guide['baseline'] - guide['xheight']) * H   # px per x-height
    scale = 1.0 / xheight_px
    norm = {}
    for ch, samples in g.items():
        glyphs = []
        for s in samples:
            strokes = []
            minx = 1e9; maxx = -1e9
            for st in s['strokes']:
                pts = []
                for p in st:
                    nx = p[0] * scale
                    ny = (y_base - p[1]) * scale     # baseline -> 0, y up
                    t = p[3]
                    pts.append([nx, ny, t])
                    minx = min(minx, nx); maxx = max(maxx, nx)
                strokes.append(pts)
            if maxx < minx:   # no points
                continue
            # left-align inked content to x=0
            for st in strokes:
                for p in st: p[0] -= minx
            adv = (maxx - minx) + 2*SIDE_BEARING
            glyphs.append({'strokes': strokes, 'adv': adv})
        if glyphs:
            norm[ch] = glyphs
    return norm

def lib_extents(lib):
    """Return (max_ascent, max_descent) over all glyphs, in x-height units —
    how far the tallest ascender rises above and the deepest descender drops
    below the baseline. Used to size the top/bottom margins exactly."""
    asc = 1.0; desc = 0.3
    for samples in lib.values():
        for s in samples:
            for st in s['strokes']:
                for p in st:
                    if p[1] > asc: asc = p[1]
                    if -p[1] > desc: desc = -p[1]
    return asc, desc

def glyph_for(ch, lib):
    if ch in lib: return lib[ch]
    if ch == '-' and '–' in lib: return lib['–']
    if ch.lower() in lib: return lib[ch.lower()]
    return None

def advance(ch, lib):
    if ch == ' ': return SPACE_ADV
    g = glyph_for(ch, lib)
    return g[0]['adv'] if g else SPACE_ADV*0.8

# ---------- velocity -> per-point width ----------
def widths_for_stroke(pts):
    n = len(pts)
    if n == 1: return [W_MAX]
    sp = [0.0]*n
    for i in range(n):
        a = pts[max(0, i-1)]; b = pts[min(n-1, i+1)]
        d = math.hypot(b[0]-a[0], b[1]-a[1])
        dt = max(1.0, (b[2]-a[2]))           # ms
        sp[i] = d/dt
    ref = sorted(sp)[int(n*0.6)] or 1e-6     # ~60th percentile speed
    w = []
    for s in sp:
        f = min(1.0, s/(ref*1.6))
        w.append(W_MAX - (W_MAX-W_MIN)*f)
    # smooth
    out = w[:]
    for i in range(1, n-1):
        out[i] = (w[i-1]+2*w[i]+w[i+1])/4.0
    return out

# ---------- layout ----------
def layout(text, lib):
    words = []
    # split into tokens preserving explicit newlines
    paragraphs = text.split('\n')
    usable = (PAGE_W - MARGIN_L - MARGIN_R) / XH_PX   # in x-height units
    lines = []
    for para in paragraphs:
        if para.strip() == '':
            lines.append([]); continue
        cur = []; curw = 0.0
        for word in para.split(' '):
            ww = sum(advance(c, lib) for c in word)
            extra = SPACE_ADV if cur else 0.0
            if curw + extra + ww <= usable or not cur:
                if cur: curw += SPACE_ADV
                cur.append(word); curw += ww
            else:
                lines.append(cur); cur = [word]; curw = ww
        lines.append(cur)
    return lines

def _smooth(pts, passes=None):
    """Light moving-average smoothing of a stroke's (x,y) to remove hand tremor /
    polygonal jaggedness. Endpoints fixed so glyph extents/joins don't shift."""
    passes = SMOOTH_PASSES if passes is None else passes
    if passes <= 0 or len(pts) < 3:
        return pts
    for _ in range(passes):
        new = [pts[0]]
        for i in range(1, len(pts) - 1):
            x = 0.25*pts[i-1][0] + 0.5*pts[i][0] + 0.25*pts[i+1][0]
            y = 0.25*pts[i-1][1] + 0.5*pts[i][1] + 0.25*pts[i+1][1]
            w = pts[i][2] if len(pts[i]) > 2 else 1.0
            new.append([x, y, w])
        new.append(pts[-1])
        pts = new
    return pts

def _place_line(strokes_out, line, x0, y, size_px, rng, lib):
    x = x0
    for word in line:
        for ch in word:
            g = glyph_for(ch, lib)
            if g is None:
                x += SPACE_ADV*size_px; continue
            sample = g[rng.s % len(g)] if len(g) > 1 else g[0]
            rot = rng.gauss(JITTER_ROT)                    # radians
            sc = size_px * (1 + rng.gauss(JITTER_SCALE))
            yoff = rng.gauss(JITTER_Y) * size_px           # vertical wobble
            cosr, sinr = math.cos(rot), math.sin(rot)
            for st in sample['strokes']:
                w = widths_for_stroke(st)
                pts = []
                for k, p in enumerate(st):
                    gx, gy = p[0]*sc, p[1]*sc
                    rx = gx*cosr - gy*sinr
                    ry = gx*sinr + gy*cosr
                    pts.append([x + rx, y + yoff - ry, w[k]*size_px])
                strokes_out.append(_smooth(pts))
            x += sample['adv']*size_px
        x += SPACE_ADV*size_px
    return x

def build_pages(text, lib, size_px=XH_PX):
    """Paginate text. Full pages are vertically JUSTIFIED: the line pitch is
    stretched so the last line lands exactly at the bottom reserve, leaving one
    box free top and bottom and filling the rest. The final (partial) page is
    top-aligned at the natural pitch so it doesn't get odd gaps."""
    rng = RNG(int(hashlib.md5(text.encode()).hexdigest()[:8], 16))
    lines = layout(text, lib)

    # Lined-paper path: place each text line's baseline ON a ruled line of the
    # paper (BASELINES, in page units). No justification — the pitch is the
    # paper's, so writing always sits on the lines. Paginate by rules-per-page.
    if BASELINES:
        bl = BASELINES
        per_page = max(1, len(bl))
        pages = []
        for start in range(0, max(1, len(lines)), per_page):
            chunk = lines[start:start+per_page]
            cur = []
            for k, line in enumerate(chunk):
                _place_line(cur, line, MARGIN_L, bl[k], size_px, rng, lib)
            pages.append({'page': {'w': PAGE_W, 'h': PAGE_H}, 'strokes': cur})
        return pages

    row = LINE_SPACING * size_px
    y_first = MARGIN_T + row
    y_last_max = PAGE_H - MARGIN_B
    avail = y_last_max - y_first
    per_page = max(1, int(avail // row) + 1)            # lines that fit at the natural pitch
    pages = []
    for start in range(0, max(1, len(lines)), per_page):
        chunk = lines[start:start+per_page]
        n = len(chunk)
        pitch = avail / (n - 1) if (n == per_page and n > 1) else row   # justify only full pages
        cur = []
        for k, line in enumerate(chunk):
            _place_line(cur, line, MARGIN_L, y_first + k*pitch, size_px, rng, lib)
        pages.append({'page': {'w': PAGE_W, 'h': PAGE_H}, 'strokes': cur})
    return pages

def build_doc(text, lib, size_px=XH_PX):
    rng = RNG(int(hashlib.md5(text.encode()).hexdigest()[:8], 16))
    lines = layout(text, lib)
    strokes_out = []
    y = MARGIN_T + LINE_SPACING*size_px
    for line in lines:
        x = MARGIN_L
        for wi, word in enumerate(line):
            for ch in word:
                g = glyph_for(ch, lib)
                if g is None:
                    x += SPACE_ADV*size_px; continue
                sample = g[rng.s % len(g)] if len(g) > 1 else g[0]
                rot = math.radians(rng.gauss(JITTER_ROT))
                sc = size_px * (1 + rng.gauss(JITTER_SCALE))
                cosr, sinr = math.cos(rot), math.sin(rot)
                for st in sample['strokes']:
                    w = widths_for_stroke(st)
                    pts = []
                    for k, p in enumerate(st):
                        # scale, rotate, translate; y is up in glyph space -> down on page
                        gx, gy = p[0]*sc, p[1]*sc
                        rx = gx*cosr - gy*sinr
                        ry = gx*sinr + gy*cosr
                        px = x + rx
                        py = y - ry
                        pts.append([px, py, w[k]*size_px])
                    strokes_out.append(_smooth(pts))
                x += sample['adv']*size_px
            x += SPACE_ADV*size_px
        y += LINE_SPACING*size_px
        if y > PAGE_H - MARGIN_T:
            break  # single-page preview for now
    return {'page': {'w': PAGE_W, 'h': PAGE_H}, 'strokes': strokes_out}

# ---------- render ribbons to SVG ----------
def ribbon_path(pts):
    n = len(pts)
    if n < 2:
        x, y, w = pts[0]; r = max(0.4, w/2)
        return f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{r:.2f}"/>'
    left = []; right = []
    for i in range(n):
        a = pts[max(0, i-1)]; b = pts[min(n-1, i+1)]
        dx, dy = b[0]-a[0], b[1]-a[1]
        L = math.hypot(dx, dy) or 1e-6
        nx, ny = -dy/L, dx/L
        hw = max(0.3, pts[i][2]/2)
        left.append((pts[i][0]+nx*hw, pts[i][1]+ny*hw))
        right.append((pts[i][0]-nx*hw, pts[i][1]-ny*hw))
    pathpts = left + right[::-1]
    d = 'M ' + ' L '.join(f'{x:.2f},{y:.2f}' for x, y in pathpts) + ' Z'
    return f'<path d="{d}"/>'

def to_svg(doc, ink='#1a2238'):
    W, H = doc['page']['w'], doc['page']['h']
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}">',
             f'<rect width="{W}" height="{H}" fill="#fdfdfb"/>']
    # faint ruled lines
    yy = MARGIN_T + LINE_SPACING*XH_PX
    while yy < H - MARGIN_T:
        parts.append(f'<line x1="0" y1="{yy:.1f}" x2="{W}" y2="{yy:.1f}" stroke="#dfe7f0" stroke-width="0.8"/>')
        yy += LINE_SPACING*XH_PX
    parts.append(f'<g fill="{ink}" stroke="none">')
    for st in doc['strokes']:
        parts.append(ribbon_path(st))
    parts.append('</g></svg>')
    return '\n'.join(parts)

def main():
    args = sys.argv[1:]
    text = args[0] if args and not args[0].startswith('--') else \
        "Hallo! Das hier ist meine echte Handschrift,\nrekonstruiert aus den erfassten Strichen.\nabcdefg 0123456789 ä ö ü ß"
    size = XH_PX
    out = 'preview'
    for i, a in enumerate(args):
        if a == '--size': size = float(args[i+1])
        if a == '--out': out = args[i+1]
    lib = load_lib()
    doc = build_doc(text, lib, size)
    svg = to_svg(doc)
    outdir = os.path.join(DIR, 'out')
    os.makedirs(outdir, exist_ok=True)
    open(os.path.join(outdir, out+'.svg'), 'w', encoding='utf-8').write(svg)
    json.dump(doc, open(os.path.join(outdir, out+'.doc.json'), 'w'))
    nstroke = len(doc['strokes']); npts = sum(len(s) for s in doc['strokes'])
    print(f"glyphs in lib: {len(lib)} | text chars: {len(text)} | strokes placed: {nstroke} | points: {npts}")
    print(f"wrote out/{out}.svg  and  out/{out}.doc.json")

if __name__ == '__main__':
    main()
