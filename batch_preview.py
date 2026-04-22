"""Headless Blender STL preview batch renderer.

Usage:
    blender --background --python batch_preview.py -- <root_dir> [--force] [--force-groups]

Produces sidecar <stem>.preview.png next to each STL, and a
_GROUP_<key>.preview.png per folder grouping related files.

Resume-safe: per-file skip if sidecar exists. Per-folder group image
regenerated when any of its members change, or always when --force-groups.

Progress is logged to:
    C:\\projects\\blender_pics\\manifest.jsonl   (one JSON event per line)
    C:\\projects\\blender_pics\\batch.log        (human-readable)
"""

import sys, os, re, math, time, json, argparse
from pathlib import Path

HERE = Path(__file__).parent
VENDOR = HERE / "vendor"
sys.path.insert(0, str(VENDOR))

import bpy
import numpy as np
from mathutils import Vector
from PIL import Image, ImageDraw, ImageFont

# ---------- config ----------
TILE = 1024
LABEL_H = 160
INSET_MINI = 300       # per-mini resolution for inverse 4-view inset
INSET_GAP = 6          # gap between minis inside the inset block
BG_F = (0.91, 0.91, 0.92)
BG_8 = tuple(int(c * 255) for c in BG_F) + (255,)
CLAY = (0.78, 0.74, 0.68)
FONT_PATH = r"C:\Windows\Fonts\segoeui.ttf"
FONT_SIZE = 72
GROUP_GAP = 24                  # px gap between tiles in group image
GROUP_BORDER_PX = 4             # px border drawn around each tile
GROUP_BORDER_RGB = (70, 70, 80, 255)
GROUP_BG_RGB = (220, 220, 224, 255)   # slightly darker than tile bg so gaps read as "frame"
GROUP_MAX_COLS = 5
GROUP_MIN_SIZE = 2              # groups smaller than this don't get a stitched image
GROUP_MAX_ITEMS = 30            # split into parts above this
FILLER_TILE_PATH = HERE / "filler_tile.png"  # pasted into empty grid cells

# Skip any file whose path contains ANY of these substrings (case insensitive).
# Use for vendors that ship their own previews.
EXCLUDE_SUBSTRINGS = [
    "greytide studios",
    "greytide",
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
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kw}
    with open(MANIFEST_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

# ---------- scene / render setup ----------
def setup_scene():
    for obj in list(bpy.data.objects):
        bpy.data.objects.remove(obj, do_unlink=True)
    for m in list(bpy.data.meshes):
        bpy.data.meshes.remove(m)
    for c in list(bpy.data.cameras):
        bpy.data.cameras.remove(c)

    scene = bpy.context.scene
    scene.render.engine = 'BLENDER_WORKBENCH'
    sh = scene.display.shading
    sh.light = 'STUDIO'
    sh.color_type = 'SINGLE'
    sh.single_color = CLAY
    sh.show_cavity = True
    sh.cavity_type = 'BOTH'
    sh.cavity_ridge_factor = 1.0
    sh.cavity_valley_factor = 1.0
    sh.show_shadows = False
    if scene.world is None:
        scene.world = bpy.data.worlds.new("World")
    scene.world.color = BG_F
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = 'PNG'
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.resolution_x = TILE
    scene.render.resolution_y = TILE

    cam_data = bpy.data.cameras.new('BatchCam')
    cam = bpy.data.objects.new('BatchCam', cam_data)
    scene.collection.objects.link(cam)
    scene.camera = cam
    return scene, cam, cam_data

def look_at(c, t=Vector((0, 0, 0))):
    c.rotation_euler = (t - c.location).to_track_quat('-Z', 'Y').to_euler()

def clear_meshes():
    for obj in list(bpy.data.objects):
        if obj.type == 'MESH':
            bpy.data.objects.remove(obj, do_unlink=True)
    for m in list(bpy.data.meshes):
        if m.users == 0:
            bpy.data.meshes.remove(m)

# ---------- STL import (background-safe) ----------
def import_stl(path):
    # Blender 4.x/5.x wm.stl_import works in background without VIEW_3D context.
    # Fall back to legacy import_mesh.stl if needed.
    try:
        bpy.ops.wm.stl_import(filepath=str(path))
        return
    except RuntimeError:
        pass
    bpy.ops.import_mesh.stl(filepath=str(path))

def recenter_origin(obj):
    # Move origin to bbox center, then zero location. Avoid bpy.ops (context-free).
    bbox_local = [Vector(c) for c in obj.bound_box]
    center_local = sum(bbox_local, Vector()) / 8.0
    # shift mesh vertices so bbox center is at object origin
    mesh = obj.data
    for v in mesh.vertices:
        v.co -= center_local
    mesh.update()
    obj.location = (0, 0, 0)

# ---------- rendering ----------
_render_tmp = None
def render_view_u8(scene, cam, cam_data, cam_loc, ortho, max_dim):
    global _render_tmp
    if _render_tmp is None:
        _render_tmp = os.path.join(bpy.app.tempdir, "view_tmp.png")
    cam.location = Vector(cam_loc)
    look_at(cam)
    cam_data.type = 'ORTHO' if ortho else 'PERSP'
    if ortho:
        cam_data.ortho_scale = max_dim * 1.15
    else:
        cam_data.lens = 50
    scene.render.filepath = _render_tmp
    bpy.ops.render.render(write_still=True)
    # Skip the bpy.data.images round-trip — PIL decodes the PNG ~5x faster
    # and yields uint8 RGBA with origin already top-left (no flip needed).
    return np.asarray(Image.open(_render_tmp).convert('RGBA'))

def render_stl_preview(scene, cam, cam_data, stl_path, out_path):
    clear_meshes()
    import_stl(stl_path)
    obj = next((o for o in bpy.data.objects if o.type == 'MESH'), None)
    if obj is None:
        raise RuntimeError(f"no mesh imported from {stl_path}")
    recenter_origin(obj)
    # world-space size
    bbox = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    sx = max(v.x for v in bbox) - min(v.x for v in bbox)
    sy = max(v.y for v in bbox) - min(v.y for v in bbox)
    sz = max(v.z for v in bbox) - min(v.z for v in bbox)
    max_dim = max(sx, sy, sz) or 1.0
    dist = max_dim * 3.0

    v_front = render_view_u8(scene, cam, cam_data, (0, -dist, 0), True, max_dim)
    v_right = render_view_u8(scene, cam, cam_data, (dist, 0, 0), True, max_dim)
    v_top   = render_view_u8(scene, cam, cam_data, (0, 0, dist), True, max_dim)
    v_iso   = render_view_u8(scene, cam, cam_data,
                             (dist * 0.7, -dist * 0.7, dist * 0.55), False, max_dim)

    # 4 inverse mini-views to fill the dead space at the composite center.
    # Render at lower resolution to keep batch fast (each main view dominates cost).
    saved_x, saved_y = scene.render.resolution_x, scene.render.resolution_y
    scene.render.resolution_x = INSET_MINI; scene.render.resolution_y = INSET_MINI
    v_back    = render_view_u8(scene, cam, cam_data, (0,  dist, 0),  True,  max_dim)
    v_left    = render_view_u8(scene, cam, cam_data, (-dist, 0, 0), True,  max_dim)
    v_bottom  = render_view_u8(scene, cam, cam_data, (0, 0, -dist), True,  max_dim)
    v_antiiso = render_view_u8(scene, cam, cam_data,
                               (-dist*0.7, dist*0.7, -dist*0.55), False, max_dim)
    scene.render.resolution_x, scene.render.resolution_y = saved_x, saved_y

    W = TILE * 2
    H = TILE * 2 + LABEL_H
    canvas = Image.new('RGBA', (W, H), BG_8)
    canvas.paste(Image.fromarray(v_front, 'RGBA'), (0, 0))
    canvas.paste(Image.fromarray(v_right, 'RGBA'), (TILE, 0))
    canvas.paste(Image.fromarray(v_top,   'RGBA'), (0, TILE))
    canvas.paste(Image.fromarray(v_iso,   'RGBA'), (TILE, TILE))

    # paste the 2x2 inverse-inset dead-center
    INSET_W = INSET_MINI * 2 + INSET_GAP
    ix0 = W // 2 - INSET_W // 2
    iy0 = TILE - INSET_W // 2
    canvas.paste(Image.fromarray(v_back,    'RGBA'), (ix0, iy0))
    canvas.paste(Image.fromarray(v_left,    'RGBA'), (ix0 + INSET_MINI + INSET_GAP, iy0))
    canvas.paste(Image.fromarray(v_bottom,  'RGBA'), (ix0, iy0 + INSET_MINI + INSET_GAP))
    canvas.paste(Image.fromarray(v_antiiso, 'RGBA'),
                 (ix0 + INSET_MINI + INSET_GAP, iy0 + INSET_MINI + INSET_GAP))

    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    except OSError:
        font = ImageFont.load_default()
    label = os.path.basename(str(stl_path))
    tb = draw.textbbox((0, 0), label, font=font)
    tw = tb[2] - tb[0]
    th = tb[3] - tb[1]
    draw.text(((W - tw) // 2 - tb[0],
               TILE * 2 + (LABEL_H - th) // 2 - tb[1]),
              label, fill=(30, 30, 30, 255), font=font)
    # optimize=False trades a few hundred KB of file size for ~30% faster save
    canvas.save(out_path, 'PNG', optimize=False, compress_level=1)
    return (sx, sy, sz)

# ---------- grouping & stitching ----------
# Delegate to stitch_only so there's one implementation. Both scripts live in HERE.
sys.path.insert(0, str(HERE))
from stitch_only import group_key, make_filler_image  # noqa: E402

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
            continue  # no filler available; leave blank
        for b in range(GROUP_BORDER_PX):
            draw.rectangle(
                [x - 1 - b, y - 1 - b, x + w + b, y + h + b],
                outline=GROUP_BORDER_RGB
            )
    canvas.save(out_path, 'PNG', optimize=True)
    return (GW, GH)

def stitch_groups_for_folder(folder, force_groups):
    """For each folder, always emit _ALL.preview.png covering every preview.
    Additionally emit _GROUP_<key>.preview.png for natural sub-families
    identified by the stem-prefix heuristic (unless a sub-group is the whole
    folder, in which case _ALL already covers it)."""
    previews = sorted(p for p in folder.glob("*.preview.png")
                      if not p.name.startswith(("_GROUP_", "_ALL")))
    if not previews:
        return 0

    # per-prefix sub-groups
    sub_groups = {}
    for p in previews:
        stem = p.name.replace('.preview.png', '')
        sub_groups.setdefault(group_key(stem), []).append(p)

    # schedule work: always _ALL if >= GROUP_MIN_SIZE, plus sub-groups that
    # aren't identical to _ALL
    work = []  # list of (out_filename_prefix, members)
    if len(previews) >= GROUP_MIN_SIZE:
        work.append(("_ALL", previews))
    for key, members in sub_groups.items():
        if len(members) < GROUP_MIN_SIZE:
            continue
        if len(members) == len(previews):
            continue  # _ALL already covers the whole folder
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
                manifest("group_error", folder=str(folder), key=prefix,
                         error=str(e))
    return made

# ---------- main walk ----------
def _excluded(path):
    lower = str(path).lower()
    return any(sub.lower() in lower for sub in EXCLUDE_SUBSTRINGS)

def iter_stls(root):
    for p in sorted(root.rglob("*.stl")):
        if p.is_file() and not _excluded(p):
            yield p
    # Some stls have uppercase ext; rglob is case-insensitive on Windows for this.

def main():
    ap = argparse.ArgumentParser()
    # Blender passes args after " -- "; filter accordingly
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    ap.add_argument("root")
    ap.add_argument("--force", action="store_true", help="re-render existing previews")
    ap.add_argument("--force-groups", action="store_true", help="re-stitch group images")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")

    scene, cam, cam_data = setup_scene()
    log(f"=== batch start: root={root} force={args.force} force_groups={args.force_groups}")
    manifest("start", root=str(root), force=args.force, force_groups=args.force_groups)

    # Pass 1: render each STL
    stls = list(iter_stls(root))
    total = len(stls)
    log(f"discovered {total} .stl files")
    manifest("discovered", count=total)

    # Pipelined: as soon as we leave a folder, hand it to a stitch worker
    # thread. PIL releases the GIL for image ops, so this overlaps cleanly with
    # Blender's CPU-bound rendering. Crash-resilient too: each completed folder
    # is fully done (renders + group images) before the loop moves on.
    from concurrent.futures import ThreadPoolExecutor

    rendered = 0
    skipped = 0
    failed = 0
    groups_made = 0
    t_start = time.time()
    stitch_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stitch")
    pending = []  # in-flight stitch futures

    def submit_stitch(folder):
        nonlocal groups_made
        def _job():
            return stitch_groups_for_folder(folder, args.force_groups)
        fut = stitch_pool.submit(_job)
        pending.append((folder, fut))

    def reap_done():
        nonlocal groups_made
        still = []
        for folder, fut in pending:
            if fut.done():
                try:
                    groups_made += fut.result()
                except Exception as e:
                    log(f"  STITCH ERROR in {folder}: {e}")
                    manifest("stitch_error", folder=str(folder), error=str(e))
            else:
                still.append((folder, fut))
        pending[:] = still

    prev_folder = None
    try:
        for i, stl in enumerate(stls, 1):
            # folder transition: previous folder is now fully rendered
            if prev_folder is not None and stl.parent != prev_folder:
                submit_stitch(prev_folder)
                reap_done()
            prev_folder = stl.parent

            out = stl.parent / (stl.stem + ".preview.png")
            if out.exists() and not args.force:
                skipped += 1
                if skipped % 50 == 0:
                    log(f"  [{i}/{total}] skipped {skipped} (exists)")
                continue
            ts = time.time()
            try:
                dims = render_stl_preview(scene, cam, cam_data, stl, out)
                dt = time.time() - ts
                rendered += 1
                log(f"  [{i}/{total}] {stl.name} ({dt:.1f}s)")
                manifest("render", stl=str(stl), out=str(out), seconds=round(dt, 2),
                         bbox=[round(x, 3) for x in dims])
            except Exception as e:
                failed += 1
                log(f"  [{i}/{total}] FAILED {stl.name}: {e}")
                manifest("render_error", stl=str(stl), error=str(e))

        # last folder + drain
        if prev_folder is not None:
            submit_stitch(prev_folder)
    finally:
        log(f"pass1 done: rendered={rendered} skipped={skipped} failed={failed} "
            f"in {time.time()-t_start:.1f}s")
        if pending:
            log(f"draining {len(pending)} pending stitch jobs...")
        stitch_pool.shutdown(wait=True)
        # final reap to count completed jobs
        for folder, fut in pending:
            try:
                groups_made += fut.result()
            except Exception as e:
                log(f"  STITCH ERROR in {folder}: {e}")
                manifest("stitch_error", folder=str(folder), error=str(e))
        pending.clear()

    manifest("finish", rendered=rendered, skipped=skipped, failed=failed,
             groups=groups_made, seconds=round(time.time() - t_start, 2))
    log(f"=== batch done: rendered={rendered} skipped={skipped} failed={failed} "
        f"groups={groups_made} total={time.time()-t_start:.1f}s")

if __name__ == "__main__":
    main()
