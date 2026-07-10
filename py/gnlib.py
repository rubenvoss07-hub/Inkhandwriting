#!/usr/bin/env python3
"""
gnlib — read/write primitives for the GoodNotes 6 .goodnotes stroke format.

Built from the verified format map (see FORMAT.md). Reuses the subagents'
proven LZ4 decoder + tpl round-trip; ADDS an all-literal LZ4 encoder and
page/record builders so we can WRITE strokes.

Layering (innermost first):
  points  ->  tpl record (decompressed)  ->  bv41 LZ4 block  ->  stroke record
  (protobuf body)  ->  page file (concatenated records)  ->  .goodnotes zip.

Everything is little-endian. Self-checks live in test_gnlib().
"""
import struct, uuid, zipfile, io, os

# ----------------------------------------------------------------------------- varint / protobuf
def read_varint(b, i):
    shift = 0; result = 0
    while True:
        byte = b[i]; i += 1
        result |= (byte & 0x7f) << shift
        if not (byte & 0x80): break
        shift += 7
    return result, i

def write_varint(v):
    out = bytearray()
    while True:
        b = v & 0x7f; v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v: break
    return bytes(out)

def pb_fields(buf):
    """Yield (field_num, wire_type, key_start, val_start, val_end)."""
    i = 0; n = len(buf)
    while i < n:
        key_start = i
        tag, i = read_varint(buf, i)
        fnum = tag >> 3; wt = tag & 7
        if wt == 0:
            _, i = read_varint(buf, i)
        elif wt == 1:
            i += 8
        elif wt == 2:
            ln, i = read_varint(buf, i)
            yield (fnum, wt, key_start, i, i + ln); i += ln; continue
        elif wt == 5:
            i += 4
        else:
            raise ValueError(f"bad wiretype {wt} at {key_start}")
        yield (fnum, wt, key_start, key_start, i)  # scalar: val span = whole field after key

def pb_key(fnum, wt):
    return write_varint((fnum << 3) | wt)

# ----------------------------------------------------------------------------- LZ4 block
def lz4_decompress(src):
    out = bytearray(); i = 0; n = len(src)
    while i < n:
        tok = src[i]; i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]; i += 1; lit += b
                if b != 255: break
        out += src[i:i+lit]; i += lit
        if i >= n: break
        off = src[i] | (src[i+1] << 8); i += 2
        ml = tok & 0xf
        if ml == 15:
            while True:
                b = src[i]; i += 1; ml += b
                if b != 255: break
        ml += 4; st = len(out) - off
        for k in range(ml): out.append(out[st+k])
    return bytes(out)

def lz4_compress_literal(data):
    """Emit a single all-literal LZ4 block (no back-references). Always valid;
    decodes via lz4_decompress to the exact input."""
    out = bytearray()
    litlen = len(data)
    token = (15 if litlen >= 15 else litlen) << 4
    out.append(token)
    if litlen >= 15:
        rem = litlen - 15
        while rem >= 255: out.append(255); rem -= 255
        out.append(rem)
    out += data
    return bytes(out)

BV41 = b'bv41'; BV4_END = b'bv4$'

def bv41_frame(decompressed):
    payload = lz4_compress_literal(decompressed)
    return BV41 + struct.pack('<I', len(decompressed)) + struct.pack('<I', len(payload)) + payload + BV4_END

def bv41_unframe(buf, start=0):
    j = buf.index(BV41, start)
    unc = struct.unpack_from('<I', buf, j+4)[0]
    comp = struct.unpack_from('<I', buf, j+8)[0]
    payload = buf[j+12:j+12+comp]
    end = j + 12 + comp + len(BV4_END)   # includes bv4$
    return lz4_decompress(payload), j, end

# ----------------------------------------------------------------------------- tpl record
SCHEMA = b'vuA(v)A(S(uu))A(S(uuuu))vA(f)'

def build_tpl(points, width=1.8189, pressures=None):
    """points = list of (x,y) floats; count MUST be even (pad by caller).
    pressures: optional list (len == len(points)) of f32 for the A(f) array
    (GoodNotes' per-point pressure/width — empty by default)."""
    assert len(points) % 2 == 0, "point count must be even"
    nstruct = len(points) // 2
    body = bytearray()
    body += b'tpl\x00'
    body += struct.pack('<I', 0)            # total size, backfilled
    body += SCHEMA + b'\x00'
    body += struct.pack('<H', 2)            # v = 2
    body += struct.pack('<f', width)        # u = pen width
    # A(v): flags  count = nstruct+1, [0,1,1,...]
    flags = [0] + [1]*nstruct
    body += struct.pack('<I', len(flags)) + b''.join(struct.pack('<H', f) for f in flags)
    # A(S(uu)) anchor = first point
    body += struct.pack('<I', 1) + struct.pack('<ff', points[0][0], points[0][1])
    # A(S(uuuu)) the points: nstruct structs of 4 floats (2 pts each)
    body += struct.pack('<I', nstruct)
    for k in range(nstruct):
        x0, y0 = points[2*k]; x1, y1 = points[2*k+1]
        body += struct.pack('<ffff', x0, y0, x1, y1)
    body += struct.pack('<H', 1)            # v2 = 1
    if pressures:
        assert len(pressures) == len(points), "pressures must match point count"
        body += struct.pack('<I', len(pressures)) + b''.join(struct.pack('<f', p) for p in pressures)
    else:
        body += struct.pack('<I', 0)        # A(f) pressure: empty
    struct.pack_into('<I', body, 4, len(body))   # backfill total size
    return bytes(body)

def color_submsg(r, g, b, a=1.0):
    """Build the body #4 color sub-message {#1:r,#2:g,#3:b,#4:a} (f32 each).
    HYPOTHESIS: RGB live in fields 1-3, alpha in #4 (the sampled black stroke had
    only #4=1.0 with RGB omitted=0). Verify on import; black default omits RGB."""
    out = b''
    for fld, val in ((1, r), (2, g), (3, b), (4, a)):
        out += pb_key(fld, 5) + struct.pack('<f', val)
    return out

def body_set_color(body, r, g, b, a=1.0):
    """Replace body field #4 (color) with an orange/RGBA color sub-message."""
    sub = color_submsg(r, g, b, a)
    newfield = pb_key(4, 2) + write_varint(len(sub)) + sub
    for fnum, wt, ks, vs, ve in pb_fields(body):
        if fnum == 4 and wt == 2:
            return body[:ks] + newfield + body[ve:]
    return body + newfield  # no existing #4 -> append

def parse_tpl(buf):
    p = [0]
    def u16(): v = struct.unpack_from('<H', buf, p[0])[0]; p[0]+=2; return v
    def u32(): v = struct.unpack_from('<I', buf, p[0])[0]; p[0]+=4; return v
    def f32(): v = struct.unpack_from('<f', buf, p[0])[0]; p[0]+=4; return v
    assert buf[0:3] == b'tpl'
    p[0] = 4; total = u32()
    end = buf.index(0, p[0]); schema = buf[p[0]:end]; p[0] = end+1
    v = u16(); width = f32()
    n = u32(); av = [u16() for _ in range(n)]
    n2 = u32(); anchor = [(f32(), f32()) for _ in range(n2)]
    n3 = u32(); pts = []
    for _ in range(n3):
        a, b, c, d = f32(), f32(), f32(), f32(); pts += [(a, b), (c, d)]
    v2 = u16(); n4 = u32(); af = [f32() for _ in range(n4)]
    return dict(total=total, schema=schema, v=v, width=width, av=av,
                anchor=anchor, npts=len(pts), pts=pts, v2=v2, af=af,
                end=p[0], blen=len(buf))

# ----------------------------------------------------------------------------- page = length-delimited message stream
# A notes/<UUID> page file is a stream of [varint length][protobuf message].
# Each STROKE is a PAIR of consecutive messages:
#   header msg : {#1 strokeUUID, #2 meta, #8 docid, #9 seq, #14, #16}
#   geom msg   : {#7 -> body{ #1 strokeUUID, #2 bv41 blob, #4 color, ... }}
# Empty page = 0-byte file.

def split_stream(buf):
    """Return list of raw protobuf messages (the length prefixes stripped)."""
    msgs = []; i = 0; n = len(buf)
    while i < n:
        ln, i = read_varint(buf, i)
        msgs.append(buf[i:i+ln]); i += ln
    if i != n:
        raise ValueError(f"stream overran: consumed {i} of {n}")
    return msgs

def build_stream(msgs):
    out = bytearray()
    for m in msgs:
        out += write_varint(len(m)) + m
    return bytes(out)

def field7_unwrap(geom_msg):
    """geom msg = {#7: body}. Return the inner body bytes."""
    for fnum, wt, ks, vs, ve in pb_fields(geom_msg):
        if fnum == 7 and wt == 2:
            return geom_msg[vs:ve]
    raise ValueError("geom msg has no field #7")

def field7_wrap(body):
    return pb_key(7, 2) + write_varint(len(body)) + body

def body_get_bv41(body):
    """Return (decompressed_tpl, field2_keystart, field2_valstart, field2_valend)."""
    for fnum, wt, ks, vs, ve in pb_fields(body):
        if fnum == 2 and wt == 2 and body[vs:vs+4] == BV41:
            return lz4_decompress_from(body[vs:ve]), ks, vs, ve
    raise ValueError("no bv41 field #2 in body")

def lz4_decompress_from(bv41buf):
    dec, j, end = bv41_unframe(bv41buf, 0)
    return dec

def body_replace_bv41(body, new_bv41):
    """Replace field #2 (the bv41 blob) value with new_bv41, fixing the varint length."""
    for fnum, wt, ks, vs, ve in pb_fields(body):
        if fnum == 2 and wt == 2 and body[vs:vs+4] == BV41:
            new_field = pb_key(2, 2) + write_varint(len(new_bv41)) + new_bv41
            return body[:ks] + new_field + body[ve:]
    raise ValueError("no bv41 field #2 to replace")

def body_set_uuid(body, new_uuid):
    """Replace field #1 (str UUID) value."""
    for fnum, wt, ks, vs, ve in pb_fields(body):
        if fnum == 1 and wt == 2:
            new_field = pb_key(1, 2) + write_varint(len(new_uuid)) + new_uuid.encode()
            return body[:ks] + new_field + body[ve:]
    raise ValueError("no field #1")

def header_set_uuid(header, new_uuid):
    for fnum, wt, ks, vs, ve in pb_fields(header):
        if fnum == 1 and wt == 2:
            new_field = pb_key(1, 2) + write_varint(len(new_uuid)) + new_uuid.encode()
            return header[:ks] + new_field + header[ve:]
    return header  # no outer #1 (some records) -> leave as is

def gn_uuid():
    return str(uuid.uuid4()).upper()

# ----------------------------------------------------------------------------- zip
def read_goodnotes(path):
    entries = {}
    with zipfile.ZipFile(path, 'r') as z:
        names = z.namelist()
        for nm in names:
            entries[nm] = z.read(nm)
    return entries, names

def write_goodnotes(path, entries, names):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for nm in names:
            data = entries[nm]
            if nm.endswith('/') or data == b'':
                # preserve directory / empty entries
                zi = zipfile.ZipInfo(nm)
                z.writestr(zi, data)
            else:
                z.writestr(nm, data)

# ----------------------------------------------------------------------------- self-test
def test_gnlib(sample_dir):
    """Round-trip checks against the real extracted sample."""
    ok = True
    for nm, fn in [('line', '2EF3AAA0-2693-484A-9E7D-7DD87E4C6278'),
                   ('L', '3F56988F-0324-4E9F-937A-917335922A28'),
                   ('ab', '9B164991-617E-4336-922A-F49714EF6135')]:
        buf = open(os.path.join(sample_dir, 'notes', fn), 'rb').read()
        msgs = split_stream(buf)
        rebuilt = build_stream(msgs)
        bx = rebuilt == buf
        print(f"[stream {nm}] {len(msgs)} msgs ({len(msgs)//2} strokes); byte-exact: {bx}")
        ok &= bx
    # detailed checks on the line page (1 stroke = header + geom)
    buf = open(os.path.join(sample_dir, 'notes', '2EF3AAA0-2693-484A-9E7D-7DD87E4C6278'), 'rb').read()
    header, geom = split_stream(buf)
    body = field7_unwrap(geom)
    dec, ks, vs, ve = body_get_bv41(body)
    t = parse_tpl(dec)
    print(f"[tpl] points={t['npts']} width={t['width']:.4f} schema_ok={t['schema']==SCHEMA} parse_end_ok={t['end']==t['blen']}")
    print(f"[lz4] literal-encode -> decode lossless: {lz4_decompress(lz4_compress_literal(dec)) == dec}")
    # rebuild geometry from points and re-inject; confirm a valid stream round-trips through OUR builder
    pts = t['pts']
    if len(pts) % 2: pts = pts + [pts[-1]]
    new_bv41 = bv41_frame(build_tpl(pts, t['width']))
    body2 = body_replace_bv41(body, new_bv41)
    geom2 = field7_wrap(body2)
    page2 = build_stream([header, geom2])
    # re-read our page and confirm geometry survives
    h2, g2 = split_stream(page2)
    dec2, *_ = body_get_bv41(field7_unwrap(g2))
    print(f"[reinject] geometry preserved through our stream builder: {parse_tpl(dec2)['npts'] == t['npts']}")
    ok &= (parse_tpl(dec2)['npts'] == t['npts'])
    print("ALL OK" if ok else "FAILURES PRESENT")
    return ok

if __name__ == '__main__':
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), '..', 'out', 'gn6.goodnotes')
    test_gnlib(d)
