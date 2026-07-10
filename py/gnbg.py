#!/usr/bin/env python3
"""
Generate a ruled-paper background PDF matching the /handwriting skill's look
(white page + thin light-gray horizontal rules) at the GoodNotes page size,
with rule lines aligned to our typeset text baselines.

Padded to an EXACT target byte size so it can replace the template's attachment
without touching index.events.pb (which stores the attachment's byte length).
"""
import struct

# /handwriting skill rule colour (from its Form XObject): 0.8156863 0.8235294 0.827451
RULE_RGB = (0.8156863, 0.8235294, 0.827451)
PAGE_W_PT = 455.04
PAGE_H_PT = 588.45

def make_ruled_pdf(first_baseline_pt, spacing_pt, target_size=3209,
                   page_w=PAGE_W_PT, page_h=PAGE_H_PT, rule=RULE_RGB):
    """first_baseline_pt: distance from TOP of page to first rule (PDF pt).
       spacing_pt: gap between rules (PDF pt)."""
    r, g, b = rule
    # content stream: white fill, then a thin filled rect per rule line
    cs = []
    cs.append("1 1 1 rg 0 0 %.2f %.2f re f" % (page_w, page_h))
    cs.append("%.6f %.6f %.6f rg" % (r, g, b))
    y_top = first_baseline_pt
    while y_top < page_h - 8:
        y_pdf = page_h - y_top            # PDF y is bottom-up
        cs.append("0 %.2f %.2f 0.7 re f" % (y_pdf - 0.35, page_w))
        y_top += spacing_pt
    content = ("\n".join(cs)).encode('latin1')

    objs = []
    objs.append(b"<</Type/Catalog/Pages 2 0 R>>")
    objs.append(b"<</Type/Pages/Kids[3 0 R]/Count 1>>")
    objs.append(("<</Type/Page/Parent 2 0 R/MediaBox[0 0 %.2f %.2f]/Contents 4 0 R/Resources<<>>>>"
                 % (page_w, page_h)).encode('latin1'))
    objs.append(b"<</Length %d>>\nstream\n" % len(content) + content + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += ("%d 0 obj\n" % i).encode() + o + b"\nendobj\n"
    xref_start = len(out)
    out += ("xref\n0 %d\n" % (len(objs)+1)).encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode()
    out += ("trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF"
            % (len(objs)+1, xref_start)).encode()

    # pad to exact target with a trailing comment (ignored by PDF readers)
    if len(out) > target_size:
        raise ValueError(f"PDF {len(out)} exceeds target {target_size}")
    pad = target_size - len(out)
    if pad == 1:
        out += b"\n"
    elif pad >= 2:
        out += b"\n%" + b"P"*(pad-2)
    return bytes(out)


if __name__ == '__main__':
    pdf = make_ruled_pdf(first_baseline_pt=88.0, spacing_pt=46.0)
    open("out/ruled_bg.pdf", "wb").write(pdf)
    print("wrote out/ruled_bg.pdf", len(pdf), "bytes")
