#!/usr/bin/env python3
"""
web.py — thin browser driver around the unmodified inkhandwriting skill.

Runs inside Pyodide. Exposes two entry points to JavaScript:

  preview_json(text, size, color, smooth) -> JSON string of placed stroke
      geometry in GoodNotes page coordinates + the paper's ruled-line y's, so the
      page can draw a live SVG preview WITHOUT building the whole file.

  build_b64(text, size, color, width, smooth, variable) -> base64 of the real
      .goodnotes file, produced by the skill's own handwrite.build() (byte-for-byte
      the same pipeline the CLI/skill uses).

Both mirror handwrite.build()'s setup exactly, so the preview matches the output.
Pure stdlib — nothing here needs pip.
"""
import os, sys, base64, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import handwrite as H
import gnlib as G
import typeset as T

# same colour names the skill's CLI accepts (handwrite.main)
COLORS = {
    'orange': H.ORANGE,
    'black':  (0.08, 0.08, 0.08),
    'blue':   (0.086, 0.137, 0.36),
    'red':    (0.70, 0.07, 0.16),
    'green':  (0.0, 0.45, 0.2),
}

def _color(c):
    if c in COLORS:
        return COLORS[c]
    return tuple(float(x) for x in str(c).split(','))

def _hex(rgb):
    return '#%02X%02X%02X' % tuple(max(0, min(255, int(round(v * 255)))) for v in rgb)

def _setup(text, size, smooth):
    """Reproduce the front half of handwrite.build(): configure the look, load the
    template + glyph library, align baselines to the paper's rules, and paginate.
    Returns (pages, baselines)."""
    size = float(size)
    H.configure_page(size)
    T.SMOOTH_PASSES = int(smooth)
    entries, names = G.read_goodnotes(H.SAMPLE)
    lib = T.load_lib()
    baselines = H.paper_baselines(entries, names)
    if baselines:
        T.BASELINES = baselines
    else:
        T.BASELINES = None
        H.apply_vertical_metrics(lib, size)
    pages = T.build_pages(text, lib, size_px=size)
    return pages, baselines

def preview_json(text, size=H.SIZE, color='orange', smooth=H.SMOOTH):
    pages, baselines = _setup(text, size, smooth)
    out_pages = []
    for pg in pages:
        strokes = [[[round(p[0], 2), round(p[1], 2)] for p in st] for st in pg['strokes']]
        out_pages.append({'w': round(T.PAGE_W, 2), 'h': round(T.PAGE_H, 2), 'strokes': strokes})
    return json.dumps({
        'pages': out_pages,
        'baselines': [round(b, 2) for b in (baselines or [])],
        'color': _hex(_color(color)),
        'npages': len(out_pages),
        'nstrokes': sum(len(p['strokes']) for p in out_pages),
    })

def build_b64(text, size=H.SIZE, color='orange', width=H.PEN_WIDTH,
              smooth=H.SMOOTH, variable=False):
    out = os.path.join(os.getcwd(), 'inkweb_out.goodnotes')
    H.build(text, out, size_px=float(size), color=_color(color), width=float(width),
            smooth=int(smooth), variable=bool(variable))
    with open(out, 'rb') as f:
        data = f.read()
    return base64.b64encode(data).decode('ascii')
