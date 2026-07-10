#!/usr/bin/env python3
"""
Strict protobuf parser that mimics SwiftProtobuf's binary decoder:
  - field number must be >= 1
  - wiretypes 3/4 (groups) and 6/7 are rejected
  - length-delimited fields MUST NOT overrun the message end
Raises StrictError(offset, reason) at the first violation. Used to find where
our written records diverge from a structure GoodNotes (SwiftProtobuf) accepts.
"""
import struct, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gnlib as G

class StrictError(Exception):
    def __init__(self, off, msg): super().__init__(f"@0x{off:x}: {msg}"); self.off=off

def read_varint(b, i, end):
    shift=0; result=0; start=i
    while True:
        if i>=end: raise StrictError(start, "truncated varint")
        byte=b[i]; i+=1
        result |= (byte&0x7f)<<shift
        if not (byte&0x80): break
        shift+=7
        if shift>63: raise StrictError(start, "varint too long")
    return result,i

def parse(b, start=0, end=None, depth=0, recurse=True):
    if end is None: end=len(b)
    i=start; fields=[]
    while i<end:
        koff=i
        tag,i=read_varint(b,i,end)
        fnum=tag>>3; wt=tag&7
        if fnum==0: raise StrictError(koff, f"field number 0 (tag {tag:#x})")
        if wt in (3,4,6,7): raise StrictError(koff, f"bad wiretype {wt} field#{fnum}")
        if wt==0:
            v,i=read_varint(b,i,end); fields.append((koff,fnum,'varint',v))
        elif wt==1:
            if i+8>end: raise StrictError(koff,"truncated 64bit")
            fields.append((koff,fnum,'i64',b[i:i+8])); i+=8
        elif wt==5:
            if i+4>end: raise StrictError(koff,"truncated 32bit")
            fields.append((koff,fnum,'i32',b[i:i+4])); i+=4
        elif wt==2:
            ln,i=read_varint(b,i,end)
            if i+ln>end: raise StrictError(koff, f"len-delim field#{fnum} len={ln} OVERRUNS end (have {end-i})")
            val=b[i:i+ln]
            sub=None
            if recurse and ln>0 and depth<6:
                try: sub=parse(val,0,ln,depth+1,recurse)
                except StrictError: sub=None
            fields.append((koff,fnum,'bytes',(ln,val,sub))); i+=ln
    return fields

def show(fields, ind=0):
    pad='  '*ind
    for koff,fnum,kind,val in fields:
        if kind=='bytes':
            ln,raw,sub=val
            if sub is not None:
                print(f"{pad}@0x{koff:x} #{fnum} msg({ln}) {{"); show(sub,ind+1); print(f"{pad}}}")
            else:
                txt=''
                if 0<ln<=40 and all(32<=c<127 for c in raw): txt=' = '+repr(raw.decode())
                print(f"{pad}@0x{koff:x} #{fnum} bytes({ln}){txt}  {raw[:16].hex()}")
        else:
            print(f"{pad}@0x{koff:x} #{fnum} {kind} {val if kind=='varint' else val.hex()}")

def try_parse(label, b, start=0, end=None):
    try:
        f=parse(b,start,end)
        print(f"[{label}] OK — {len(f)} top-level fields, consumed to end")
        return f
    except StrictError as e:
        print(f"[{label}] STRICT FAIL {e}")
        return None

if __name__=='__main__':
    orig=open('out/gn6.goodnotes/notes/2EF3AAA0-2693-484A-9E7D-7DD87E4C6278','rb').read()
    test1=open('out/tests/__t1page.bin','rb').read() if os.path.exists('out/tests/__t1page.bin') else None
    print("=== original line page ===")
    try_parse('orig whole @0', orig)
    try_parse('orig @1 (skip framing)', orig, 1)
    ob=G.split_page(orig)[0].body
    print(f"\n original record body ({len(ob)}B):")
    f=try_parse('orig body', ob)
    if f: show(f)
