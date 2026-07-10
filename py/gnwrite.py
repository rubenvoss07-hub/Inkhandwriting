#!/usr/bin/env python3
"""
Phase 3 writer (stream model): produce graduated .goodnotes test files.

A notes/<UUID> page = a varint-length-delimited stream of protobuf messages,
two per stroke: a header msg {#1 uuid,#2 meta,#8 docid,#9 seq,#14,#16} and a
geometry msg {#7 -> body{#1 uuid,#2 bv41,#4 color,...}}.

Emits into out/tests/:
  test0_control  - sample re-zipped unchanged (already confirmed: imports).
  test1_wave     - line page's single stroke replaced IN PLACE with a sine wave
                   (minimal edit: keep header, swap geometry, fix lengths).
  test2_hallo    - "Hallo" in the user's handwriting written from scratch onto
                   the previously-empty page (N fresh stroke pairs).

Run: voice_python.exe tools/gnwrite.py
"""
import os, sys, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gnlib as G
import typeset as T

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(DIR, 'assets', 'template.goodnotes')

PAGES = {
    'empty': 'notes/1D76856A-4A22-47F0-A5D0-EF19209AB702',
    'line':  'notes/2EF3AAA0-2693-484A-9E7D-7DD87E4C6278',
}

def even(points):
    return points if len(points) % 2 == 0 else points + [points[-1]]

def load_template():
    """Return (header_msg, body, raw_proto) for ONE prototype stroke. Read from
    `assets/stroke_proto.bin` (a saved real GoodNotes stroke = header + geom),
    so the template document itself can be a blank page with NO strokes. The
    prototype's #8 docid is a GoodNotes-wide constant (matches our blank lined
    template's events), so reusing it across documents is safe. Falls back to
    pulling a stroke out of the template's pages if the asset is missing."""
    proto = os.path.join(DIR, 'assets', 'stroke_proto.bin')
    if os.path.exists(proto):
        buf = open(proto, 'rb').read()
        header, geom = G.split_stream(buf)
        return header, G.field7_unwrap(geom), buf
    entries, _ = G.read_goodnotes(SAMPLE)
    for nm, data in entries.items():
        if nm.startswith('notes/') and data:
            header, geom = G.split_stream(data)[:2]
            return header, G.field7_unwrap(geom), data
    raise RuntimeError("no stroke prototype: add assets/stroke_proto.bin")

WMAX_REF = 0.14  # matches typeset.W_MAX; for normalizing per-point width -> pressure

def _pressures(points, size_px):
    """Derive a 0..1 pressure per point from the typeset per-point width
    (points may carry a 3rd element = absolute width)."""
    out = []
    ref = WMAX_REF * size_px
    for p in points:
        w = p[2] if len(p) > 2 else ref
        out.append(max(0.2, min(1.0, w / ref)))
    return out

def make_stroke_pair(tpl_header, tpl_body, points, width=1.8189, uid=None,
                     color=None, variable=False, size_px=40):
    uid = uid or G.gn_uuid()
    xy = even([(p[0], p[1]) for p in points])
    pressures = None
    if variable:
        pr = _pressures(points, size_px)
        if len(pr) < len(xy): pr = pr + [pr[-1]]   # match the even-padding
        pressures = pr
    bv41 = G.bv41_frame(G.build_tpl(xy, width, pressures))
    header = G.header_set_uuid(tpl_header, uid)
    body = G.body_replace_bv41(tpl_body, bv41)
    body = G.body_set_uuid(body, uid)
    if color:
        body = G.body_set_color(body, *color)
    return header, G.field7_wrap(body)

def make_page(tpl_header, tpl_body, strokes, width=1.8189, color=None,
              variable=False, size_px=40):
    msgs = []
    for s in strokes:
        h, g = make_stroke_pair(tpl_header, tpl_body, s, width, None, color, variable, size_px)
        msgs += [h, g]
    return G.build_stream(msgs)
