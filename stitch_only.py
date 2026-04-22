"""Pure-Python group-image stitcher. No Blender required.

Usage:
    python stitch_only.py <root_dir> [--force]

Walks root_dir for all folders containing *.preview.png files (excluding
_ALL* and _GROUP_* themselves), emits:
    - _ALL.preview.png (or _ALL_part1/2/... for big folders) covering every
      preview in that folder
    - _GROUP_<key>.preview.png for natural sub-families identified by the
      stem-prefix heuristic (skipped when a sub-group covers the whole folder)

Empty cells in the grid are filled with filler_tile.png if present.
"""

import sys, os, re, math, time, json, argparse
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "vendor"))
from PIL import Image, ImageDraw, ImageFont

# ----- config (keep in sync with batch_preview.py) -----
GROUP_GAP = 24
GROUP_BORDER_PX = 4
GROUP_BORDER_RGB = (70, 70, 80, 255)
GROUP_BG_RGB = (220, 220, 224, 255)
GROUP_MAX_COLS = 5
GROUP_MIN_SIZE = 2
GROUP_MAX_ITEMS = 30
FILLER_TILE_PATH = HERE / "filler_tile.png"
FILLER_LOGO_SRC = HERE / "filler_logo_src.png"
FILLER_FONT = r"C:\Windows\Fonts\segoeuii.ttf"  # italic

# Adeptus Mechanicus phrases — one is chosen at random when filler is built.
MECHANICUS_PHRASES = [
    "Praise the Omnissiah",
    "The flesh is weak",
    "Ave Deus Mechanicus",
    "Knowledge is power, guard it well",
    "From Iron Cometh Strength",
    "Only Flesh Falters: The Machine Is Never Corrupted",
    "Seek not the words of the xenos, lest they infect us with blasphemy",
    "Better in ignorance than in heresy",
    "In Aeternem Ferra Mortis",
    "Thou shalt not suffer a machine to think",
    "The eyes of the Omnissiah are ever upon us",
    "Blessed is the mind too small for doubt",
]

MANIFEST_PATH = HERE / "manifest.jsonl"
LOG_PATH = HERE / "batch.log"

_log_fh = None
def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    global _log_fh
    if _log_fh is None:
        _log_fh = open(LOG_PATH, 'a', encoding='utf-8', buffering=1)
    _log_fh.write(line + "\n")

def manifest(event, **kw):
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kw}
    with open(MANIFEST_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

_GROUP_PATTERNS = [
    r'[_\-\s]+(?:v|ver|version)\s*\d+$',
    r'[_\-\s]+(?:left|right|l|r)$',
    r'(?<=[a-z])(Left|Right)$',
    r'[_\-\s]+\d+$',
    r'[_\-\s]+[A-Za-z]$',
]

def group_key(stem):
    s = stem.strip()
    changed = True
    while changed:
        changed = False
        for pat in _GROUP_PATTERNS:
            new = re.sub(pat, '', s, flags=re.IGNORECASE).rstrip(' _-')
            if new != s and new:
                s = new
                changed = True
    return s or stem

_logo_cache = {}
def _get_logo(w, h):
    if not FILLER_LOGO_SRC.exists():
        return None
    key = (w, h)
    if key in _logo_cache:
        return _logo_cache[key]
    logo = Image.open(FILLER_LOGO_SRC).convert('RGBA')
    lw, lh = logo.size
    scale = max(w / lw, h / lh)
    nw, nh = int(lw * scale + 0.5), int(lh * scale + 0.5)
    logo = logo.resize((nw, nh), Image.LANCZOS)
    cx = (nw - w) // 2; cy = (nh - h) // 2
    logo = logo.crop((cx, cy, cx + w, cy + h))
    _logo_cache[key] = logo
    return logo

def make_filler_image(tile_w, tile_h, label_h=160, phrase=None):
    """Build a fresh filler tile in memory: aspect-fill logo + italic phrase.
    Returns (image, phrase). If phrase is None, picks one at random."""
    import random
    if phrase is None:
        phrase = random.choice(MECHANICUS_PHRASES)
    view_h = tile_h - label_h
    logo = _get_logo(tile_w, view_h)
    canvas = Image.new('RGBA', (tile_w, tile_h), GROUP_BG_RGB)
    if logo is not None:
        canvas.paste(logo, (0, 0))
    draw = ImageDraw.Draw(canvas)
    fs = 84
    while fs >= 36:
        font = ImageFont.truetype(FILLER_FONT, fs)
        tb = draw.textbbox((0, 0), phrase, font=font)
        if tb[2] - tb[0] <= tile_w - 80:
            break
        fs -= 4
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    draw.text(((tile_w - tw) // 2 - tb[0],
               view_h + (label_h - th) // 2 - tb[1]),
              phrase, fill=(30, 30, 30, 255), font=font)
    return canvas, phrase

def build_filler_tile(tile_w=2048, tile_h=2208, label_h=160):
    """Write filler_tile.png to disk with a random phrase (for manual preview)."""
    im, phrase = make_filler_image(tile_w, tile_h, label_h)
    im.save(FILLER_TILE_PATH, 'PNG', optimize=True)
    return phrase, None

def stitch_group(paths, out_path):
    imgs = [Image.open(p) for p in paths]
    w, h = imgs[0].size
    n = len(imgs)
    cols = min(GROUP_MAX_COLS, max(1, math.ceil(math.sqrt(n))))
    rows = math.ceil(n / cols)
    GW = cols * w + (cols + 1) * GROUP_GAP
    GH = rows * h + (rows + 1) * GROUP_GAP
    canvas = Image.new('RGBA', (GW, GH), GROUP_BG_RGB)
    draw = ImageDraw.Draw(canvas)
    filler = None
    total_cells = cols * rows
    if total_cells > n:
        # fresh filler with a random phrase, same one for all empty cells in
        # this group image (different group images get different phrases)
        filler, _ = make_filler_image(w, h)
    for i in range(total_cells):
        r = i // cols
        c = i % cols
        x = GROUP_GAP + c * (w + GROUP_GAP)
        y = GROUP_GAP + r * (h + GROUP_GAP)
        if i < n:
            canvas.paste(imgs[i], (x, y))
        elif filler is not None:
            canvas.paste(filler, (x, y))
        else:
            continue
        for b in range(GROUP_BORDER_PX):
            draw.rectangle(
                [x - 1 - b, y - 1 - b, x + w + b, y + h + b],
                outline=GROUP_BORDER_RGB
            )
    # Huge group canvases (up to ~140MP for a 30-item _ALL) make optimize=True
    # dominate wallclock. Fast compression for ~5-10x faster save at the cost
    # of a few hundred KB per file.
    canvas.save(out_path, 'PNG', optimize=False, compress_level=1)
    return (GW, GH)

def stitch_groups_for_folder(folder, force_groups):
    previews = sorted(p for p in folder.glob("*.preview.png")
                      if not p.name.startswith(("_GROUP_", "_ALL")))
    if not previews:
        return 0
    sub_groups = {}
    for p in previews:
        stem = p.name.replace('.preview.png', '')
        sub_groups.setdefault(group_key(stem), []).append(p)
    work = []
    if len(previews) >= GROUP_MIN_SIZE:
        work.append(("_ALL", previews))
    for key, members in sub_groups.items():
        if len(members) < GROUP_MIN_SIZE:
            continue
        if len(members) == len(previews):
            continue
        work.append((f"_GROUP_{key}", members))
    made = 0
    for prefix, members in work:
        parts = [members[i:i+GROUP_MAX_ITEMS]
                 for i in range(0, len(members), GROUP_MAX_ITEMS)]
        for idx, part in enumerate(parts):
            suffix = "" if len(parts) == 1 else f"_part{idx+1}"
            out = folder / f"{prefix}{suffix}.preview.png"
            need = force_groups or not out.exists()
            if not need and out.exists():
                gmt = out.stat().st_mtime
                need = any(p.stat().st_mtime > gmt for p in part)
            if not need:
                continue
            try:
                dims = stitch_group(part, out)
                log(f"  {prefix}: {len(part)} items -> {out.name} {dims[0]}x{dims[1]}")
                manifest("group", folder=str(folder), key=prefix,
                         count=len(part), out=str(out), dims=list(dims))
                made += 1
            except Exception as e:
                log(f"  GROUP ERROR {prefix}: {e}")
                manifest("group_error", folder=str(folder), key=prefix, error=str(e))
    return made

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--rebuild-filler", action="store_true",
                    help="regenerate filler_tile.png with a random phrase before stitching")
    args = ap.parse_args()
    if args.rebuild_filler:
        phrase, fs = build_filler_tile()
        log(f"rebuilt filler with phrase: {phrase!r} (font size {fs})")
    root = Path(args.root)
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")

    log(f"=== stitch_only start: root={root} force={args.force}")
    manifest("stitch_start", root=str(root), force=args.force)

    # find all folders that contain any .preview.png
    folders = set()
    for p in root.rglob("*.preview.png"):
        folders.add(p.parent)
    log(f"found {len(folders)} folders with previews")

    t0 = time.time()
    total_made = 0
    for i, folder in enumerate(sorted(folders), 1):
        made = stitch_groups_for_folder(folder, args.force)
        total_made += made
        if i % 25 == 0:
            log(f"  [{i}/{len(folders)}] folders processed, groups made so far: {total_made}")
    log(f"=== stitch_only done: groups made/updated={total_made} in {time.time()-t0:.1f}s")
    manifest("stitch_finish", groups=total_made, seconds=round(time.time()-t0, 2))

if __name__ == "__main__":
    main()
