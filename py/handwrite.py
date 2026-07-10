#!/usr/bin/env python3
"""
handwrite.py — the production pipeline: TEXT -> a .goodnotes file containing the
user's real handwriting as NATIVE, EDITABLE GoodNotes ink.

  text -> typeset (your captured glyphs, GoodNotes page coords, paginated)
       -> per page: stream of (header, geometry) stroke-message pairs
       -> inject into a template .goodnotes (overwrite pages; add pages if needed)
       -> re-zip.

Page coords: GoodNotes stroke space = PDF MediaBox x 11/6 (~1.8333; page
834x1079 units), pen width 1.8189 (the sampled pen). Every written page is strict-validated
(SwiftProtobuf-style) before the file is saved.

Usage:
  voice_python.exe tools/handwrite.py "Your text" [--out FILE] [--size 40] [--trim]
  voice_python.exe tools/handwrite.py --file note.txt --out mynote.goodnotes
"""
import os, sys, argparse, math, re, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gnlib as G
import gnwrite as W
import typeset as T
import strictpb as S
import gnbg

DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(DIR, 'assets', 'template.goodnotes')

VERSION = "1.3.3"

# ============================================================================
#  TUNED DEFAULTS — the single source of truth for the skill's look.
#  Everything the output depends on is set here and applied in configure_page,
#  so running handwrite.py with NO style options already produces the tuned
#  result. (Don't rely on typeset.py's own constants — these override them.)
# ============================================================================
SIZE          = 11.0                 # x-height in page units (bigger = larger writing)
PEN_WIDTH     = 2.25                 # constant stroke thickness (pressure-off look)
ORANGE        = (0.902, 0.451, 0.0)  # ink colour #E67300
SPACE_ADV     = 2.325                # word spacing (x-height units)  [v1.3.2: 1.55 -> 2.325 (1.5x)]
SIDE_BEARING  = 0.10                 # letter spacing, per side (x-height units)  [v1.3.1: 0.18 -> 0.10]
JITTER_ROT    = 0.06                 # per-glyph rotation sd, RADIANS (more organic randomness)
JITTER_SCALE  = 0.06                 # per-glyph size variation
JITTER_Y      = 0.08                 # per-glyph vertical wobble (fraction of x-height)
SMOOTH        = 3                    # stroke smoothing passes
# GoodNotes stroke space = PDF point x 11/6 (~1.8333), NOT x2. Proof from the paper
# event: line spacing .2.7 = 32.0833 = 17.5pt x 11/6, page size .2.8 = (834.24,
# 1078.825) = MediaBox x 11/6. (The old x2 made text drift downward off the rules.)
SCALE         = 11.0 / 6.0
PAGE_W, PAGE_H = 455.04 * SCALE, 588.45 * SCALE   # = 834.24 x 1078.825
BOX           = 16.0 * SCALE         # ~16 PDF-pt side margin (= 29.33 stroke units)

def configure_page(size_px):
    """Apply the tuned look to the typeset module. Horizontal margins are exactly
    one BOX; the vertical metrics are set per-font in apply_vertical_metrics()
    (after the glyph library is loaded) so exactly one box stays free top & bottom."""
    T.XH_PX = size_px
    T.PAGE_W, T.PAGE_H = PAGE_W, PAGE_H
    T.MARGIN_L = T.MARGIN_R = BOX
    T.SPACE_ADV = SPACE_ADV
    T.SIDE_BEARING = SIDE_BEARING
    T.JITTER_ROT, T.JITTER_SCALE, T.JITTER_Y = JITTER_ROT, JITTER_SCALE, JITTER_Y

def apply_vertical_metrics(lib, size_px):
    """Leave exactly one BOX free top & bottom and fill the rest: the tallest
    ascender of the first line touches one box from the top edge, and pagination
    stops so the deepest descender of the last line touches one box from the
    bottom. Line pitch is a whole number of boxes. Call AFTER T.load_lib()."""
    asc, desc = T.lib_extents(lib)                     # x-height units (ascent, descent)
    rows = max(1, math.ceil((asc + desc) * size_px / BOX))
    row = rows * BOX                                   # baseline pitch in page units
    T.LINE_SPACING = row / size_px                     # build_pages multiplies back by size_px
    T.MARGIN_T = (BOX + asc * size_px) - row           # first baseline -> ascender top at one box
    T.MARGIN_B = BOX + desc * size_px                  # stop -> deepest descender one box from bottom

# ----------------------------------------------------------------------------
#  Read the paper's ruled lines straight out of the background PDF so the text
#  baselines land ON them. The paper (lined / grid / blank) is defined ENTIRELY
#  by the attachment PDF — same paper *name* renders differently per attachment,
#  so we trust the bytes, not the name. A rule is a full-width thin filled rect
#  "0 T m  W T l  W B l  0 B l" (T-B ~= 0.5 pt). Returns baselines in stroke
#  units (= (pageH - mid) x 2), top->bottom; [] if the paper has no h-rules.
# ----------------------------------------------------------------------------
_RULE = re.compile(r'0\s+([\d.\-]+)\s+m\s+([\d.\-]+)\s+\1\s+l\s+\2\s+([\d.\-]+)\s+l\s+0\s+\3\s+l')

def rules_from_pdf(pdf, scale=SCALE):
    chunks = []
    for m in re.finditer(rb'stream\r?\n', pdf):
        s = m.end(); e = pdf.find(b'endstream', s)
        if e < 0: continue
        raw = pdf[s:e]
        try: chunks.append(zlib.decompress(raw).decode('latin1'))
        except Exception: chunks.append(raw.decode('latin1', 'replace'))
    cs = "\n".join(chunks)
    mb = re.search(r'/MediaBox\s*\[\s*[\d.\-]+\s+[\d.\-]+\s+[\d.\-]+\s+([\d.\-]+)', cs)
    page_h = float(mb.group(1)) if mb else (PAGE_H / scale)
    ys = []
    for t_s, w_s, b_s in _RULE.findall(cs):
        t, w, b = float(t_s), float(w_s), float(b_s)
        if w < 100:            # not a full-width line
            continue
        if not (0.2 <= abs(t - b) <= 1.5):   # not a thin rule (skip page-fill rects)
            continue
        ys.append((page_h - (t + b) / 2.0) * scale)
    ys = sorted(set(round(y, 3) for y in ys))
    return ys

def paper_baselines(entries, names):
    att = next((nm for nm in names if nm.startswith('attachments/')), None)
    return rules_from_pdf(entries[att]) if att else []


# ----------------------------------------------------------------------------
#  Multi-page by cloning the template's per-page events.
#  The template is the user's blank LINED notebook = 1 page. GoodNotes renders
#  each page's paper from a paper-definition event + a page-create event that
#  references it; pages are NOT cross-referenced by content (page-entity and
#  page-content UUIDs are paired by content = entity+1). To add a lined page we
#  clone the existing page's events, remapping entity/content to a fresh pair
#  and giving each op its own fresh UUID, while KEEPING the shared doc + paper
#  UUIDs so the new page renders on the same lined paper. All UUIDs are 36 chars
#  so substitution preserves every protobuf length (stays strict-valid).
# ----------------------------------------------------------------------------
_UUID_RE = re.compile(rb'[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}')
_HEX = '0123456789ABCDEF'

def _uuids_in(b):
    return set(_UUID_RE.findall(b))

def _page_sibling(uuid, delta):
    """Step the last hex nibble (GoodNotes pairs page-content = page-entity + 1)."""
    i = _HEX.index(uuid[-1].upper())
    return uuid[:-1] + _HEX[(i + delta) % 16]

def _new_page_pair():
    """Mint (entity, content) = (base, base+1), matching GoodNotes' minting."""
    while True:
        u = G.gn_uuid()
        if u[-1].upper() != 'F':        # leave room for +1 without wrapping
            return u, _page_sibling(u, +1)

def _doc_uuid(ev_msgs):
    from collections import Counter
    c = Counter()
    for m in ev_msgs:
        for u in _uuids_in(m):
            c[u] += 1
    return c.most_common(1)[0][0].decode() if c else None

def _paper_uuid(ev_msgs):
    """The paper-definition event carries the paper name `…standard… - White`;
    its top-level #1 is the paper UUID that page-create events reference."""
    for m in ev_msgs:
        if b'_standard' in m or b'tandard' in m:
            for fnum, wt, ks, vs, ve in G.pb_fields(m):
                if fnum == 1 and wt == 2 and (ve - vs) == 36:
                    return m[vs:ve].decode()
    return None

def _clone_events(events, remap, keep):
    """Clone each raw event, substituting UUIDs: those in `remap` -> their target,
    those in `keep` -> unchanged, anything else -> a fresh UUID (per-op ids)."""
    out = []
    for m in events:
        b = m
        for u in _uuids_in(m):
            us = u.decode()
            if us in keep:
                continue
            rep = remap.get(us) or G.gn_uuid()
            b = b.replace(u, rep.encode())
        out.append(b)
    return out


def ordered_note_paths(entries):
    """Parse index.notes.pb -> ordered list of 'notes/<uuid>' (page order)."""
    idx = entries['index.notes.pb']
    paths = []
    for rec in G.split_stream(idx):
        for fnum, wt, ks, vs, ve in G.pb_fields(rec):
            if fnum == 2 and wt == 2:
                paths.append(rec[vs:ve].decode('ascii', 'replace'))
    return paths

def notes_index_record(page_uuid):
    path = ('notes/' + page_uuid).encode()
    body = G.pb_key(1, 2) + G.write_varint(36) + page_uuid.encode() \
         + G.pb_key(2, 2) + G.write_varint(len(path)) + path
    return body  # stream-prefixed by caller

def strict_check_page(data):
    for m in G.split_stream(data):
        S.parse(m)   # raises StrictError if malformed

def build(text, out_path, size_px=SIZE, trim=False, color=ORANGE, width=PEN_WIDTH,
          variable=False, ruled=False, smooth=SMOOTH):  # keep template's ruled bg; constant width
    print(f"inkhandwriting v{VERSION}: size={size_px} width={width} space={SPACE_ADV} "
          f"bearing={SIDE_BEARING} jitter_y={JITTER_Y} smooth={smooth}")
    configure_page(size_px)
    T.SMOOTH_PASSES = smooth
    entries, names = G.read_goodnotes(SAMPLE)
    tpl_header, tpl_body, _ = W.load_template()
    lib = T.load_lib()
    # Align text to the paper's ruled lines if it has any (lined paper); else
    # fall back to the computed one-box top/bottom margins (grid/blank paper).
    baselines = paper_baselines(entries, names)
    if baselines:
        T.BASELINES = baselines
        pitch = baselines[1] - baselines[0] if len(baselines) > 1 else 0.0
        print(f"  lined paper: {len(baselines)} ruled lines/page, baseline pitch "
              f"{pitch:.1f}u ({pitch/SCALE:.2f}pt), first @ {baselines[0]:.1f}u")
    else:
        T.BASELINES = None
        apply_vertical_metrics(lib, size_px)            # one-box top/bottom margins from the real glyph extents
    pages = T.build_pages(text, lib, size_px=size_px)   # use the CONFIGURED size, not the import-time default

    def mk(strokes):
        return W.make_page(tpl_header, tpl_body, strokes, width=width,
                           color=color, variable=variable, size_px=size_px)

    # The template is the user's blank LINED notebook = exactly ONE page. Locate
    # that page + its per-page events, the document UUID and the shared paper.
    note_paths = ordered_note_paths(entries)
    pg0_path    = note_paths[0]
    pg0_content = pg0_path.split('/', 1)[1]
    pg0_entity  = _page_sibling(pg0_content, -1)             # content = entity + 1
    ev_msgs     = G.split_stream(entries['index.events.pb'])
    doc_uuid    = _doc_uuid(ev_msgs)
    paper_uuid  = _paper_uuid(ev_msgs)
    page_events = [m for m in ev_msgs
                   if pg0_content.encode() in m or pg0_entity.encode() in m]
    print(f"text -> {len(pages)} page(s)  (1 base + {max(0, len(pages)-1)} cloned); "
          f"paper={paper_uuid[:8] if paper_uuid else '?'}  doc={doc_uuid[:8]}")

    # Page 0 -> overwrite the existing blank page. For a 1-page note THIS IS THE
    # ONLY CHANGE: the document identity, the event journal and index.notes.pb are
    # left byte-for-byte identical to the user's working lined notebook, so it
    # renders exactly like that notebook (the lined paper is tied to that identity)
    # — just with ink. (Nothing references a notes/ file's content, only that it
    # exists in index.notes.pb, so adding strokes needs no event change.)
    entries[pg0_path] = mk(pages[0]['strokes'])
    used_paths = [pg0_path]
    added = []

    # Pages 1..N-1 -> clone the per-page events (same doc + same lined paper),
    # add the note files. (Only touches the journal/index when >1 page.)
    if len(pages) > 1 and not page_events:
        print("  WARNING: no per-page events found in template — cannot clone pages; "
              "only page 1 written.")
        pages = pages[:1]
    elif len(pages) > 1:
        idx_records = G.split_stream(entries['index.notes.pb'])
        keep = {u for u in (doc_uuid, paper_uuid) if u}
        for j in range(1, len(pages)):
            new_entity, new_content = _new_page_pair()
            cloned = _clone_events(page_events,
                                   {pg0_entity: new_entity, pg0_content: new_content},
                                   keep)
            ev_msgs.extend(cloned)
            path = 'notes/' + new_content
            entries[path] = mk(pages[j]['strokes'])
            names.append(path)
            idx_records.append(notes_index_record(new_content))
            used_paths.append(path); added.append(path)
        entries['index.events.pb'] = G.build_stream(ev_msgs)
        entries['index.notes.pb']  = G.build_stream(idx_records)

    # strict-validate every written page; sanity-check that cloned events parse
    for path in used_paths:
        if entries.get(path):
            strict_check_page(entries[path])
    for m in ev_msgs:
        list(G.pb_fields(m))   # raises if a cloned event is malformed

    G.write_goodnotes(out_path, entries, names)
    npts = sum(len(s) for pg in pages for s in pg['strokes'])
    print(f"wrote {out_path}  ({os.path.getsize(out_path):,} bytes)")
    print(f"  pages: {len(used_paths)}  strokes: {sum(len(p['strokes']) for p in pages)}  points: {npts}")
    if added:
        print(f"  +{len(added)} page(s) cloned from the lined template (verify long docs on import).")

def main():
    ap = argparse.ArgumentParser(description="Convert text to a .goodnotes file of the user's real handwriting (native editable ink).")
    ap.add_argument('text', nargs='?', default=None, help='text to render (or use --file)')
    ap.add_argument('--file', help='read text from a UTF-8 file')
    ap.add_argument('--out', default='handwritten.goodnotes', help='output .goodnotes path (default: ./handwritten.goodnotes)')
    ap.add_argument('--size', type=float, default=SIZE, help=f'x-height in page units (default {SIZE}; larger = bigger writing)')
    ap.add_argument('--width', type=float, default=PEN_WIDTH, help=f'pen thickness (default {PEN_WIDTH})')
    ap.add_argument('--color', default='orange', help="ink color: 'orange' (default), 'black', 'blue', or 'r,g,b' floats 0..1")
    ap.add_argument('--variable', action='store_true', help='velocity-based variable width (default OFF — matches pressure-off writing)')
    ap.add_argument('--smooth', type=int, default=SMOOTH, help=f'stroke smoothing passes (default {SMOOTH}; 0 = raw captured points)')
    ap.add_argument('--trim', action='store_true', help='trim document to only the written pages')
    a = ap.parse_args()
    if a.file:
        text = open(a.file, encoding='utf-8').read()
    elif a.text:
        text = a.text
    else:
        text = ("Hallo! Das hier ist meine eigene Handschrift,\n"
                "aber als echte, bearbeitbare GoodNotes-Tinte.\n\n"
                "Ich kann jetzt jeden Text in meine Schrift verwandeln –\n"
                "und ihn dann im Lasso verschieben, skalieren und umfärben.")
    colors = {'orange': ORANGE, 'black': (0.08, 0.08, 0.08), 'blue': (0.086, 0.137, 0.36),
              'red': (0.70, 0.07, 0.16), 'green': (0.0, 0.45, 0.2)}
    if a.color in colors:
        color = colors[a.color]
    else:
        color = tuple(float(x) for x in a.color.split(','))
    outdir = os.path.dirname(os.path.abspath(a.out))
    os.makedirs(outdir, exist_ok=True)
    build(text, a.out, a.size, a.trim, color=color, width=a.width,
          variable=a.variable, smooth=a.smooth)   # ruled=False: keep the template's own ruled background

if __name__ == '__main__':
    main()
