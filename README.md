# Ink·Handwriting (web)

Type text → download a **`.goodnotes`** file written in a real captured handwriting as
**native, editable GoodNotes ink** (lasso-select, move, scale, recolor, Edit-Handwriting).
This is the browser version of the `inkhandwriting` Claude skill.

Runs **entirely client-side** — the skill's own Python pipeline executes in the browser via
[Pyodide](https://pyodide.org) (CPython compiled to WebAssembly). Nothing is uploaded; the
bytes it produces are identical to the skill.

**No third-party CDN at runtime:** Pyodide 0.27.2 (core) is *self-hosted* in `pyodide/`, so
everything — code, Python engine, assets — is served same-origin from this site with no external
requests. The only network calls are same-origin `fetch()`es for these pinned files.

## Use it
1. Open the page (GitHub Pages, or locally — see below).
2. Wait a few seconds for the Python engine to load.
3. Type your text, pick a colour/size, watch the live preview.
4. **Download .goodnotes** → AirDrop / Share to your iPad → *Open in GoodNotes* (GoodNotes 6).

> **Honest caveat:** whether GoodNotes 6 imports the file as native ink can only be finally
> confirmed on the iPad — exactly like the skill itself. Everything up to the import is verified.

## How it's wired
- `index.html` — the whole UI + glue (self-contained; loads Pyodide from a CDN).
- `py/` — the **unmodified** skill scripts (`handwrite.py`, `gnlib.py`, `gnwrite.py`,
  `typeset.py`, `strictpb.py`, `gnbg.py`) plus `web.py`, a thin driver exposing
  `preview_json()` and `build_b64()` to JavaScript.
- `assets/` — the captured glyph library, the blank lined GoodNotes template, and one
  prototype stroke (all from the skill, byte-for-byte).
- `pyodide/` — self-hosted Pyodide 0.27.2 core (`pyodide.js`, `pyodide.asm.js`,
  `pyodide.asm.wasm`, `python_stdlib.zip`, `pyodide-lock.json`; ~14 MB). Loaded via
  `loadPyodide({indexURL: "pyodide/"})` — no CDN.

The `py/ + assets/` layout matches the skill's own `dirname(dirname(__file__))/assets`
lookup, so the scripts run without edits.

## Local dev
Any static server works (Pyodide + `fetch()` need HTTP, not `file://`):

```bash
python3 -m http.server 8000    # then open http://localhost:8000
```

## Deploy (GitHub Pages)
Settings → Pages → Build and deployment → *Deploy from a branch* → `main` / `/ (root)`.
