"""Multiprocess dispatcher for batch_preview.

Walks the tree, partitions leaf folders across N workers (greedy bin-packing
on STL count), spawns N headless Blender processes that each render only
their assigned folders. Each worker has its own batch.log; the dispatcher
prints a combined summary at the end.

Usage:
    python batch_parallel.py <root> [--workers N] [--blender PATH] [--force] [--force-groups]

If --workers is omitted, defaults to max(1, cpu_count - 1) so the machine
stays responsive.
"""

import argparse, os, re, subprocess, sys, time
from pathlib import Path
import multiprocessing as mp
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = Path(__file__).parent

# keep in sync with batch_preview.py
EXCLUDE_SUBSTRINGS = ["greytide studios", "greytide"]

DEFAULT_BLENDER = r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe"

def _excluded(p):
    s = str(p).lower()
    return any(sub.lower() in s for sub in EXCLUDE_SUBSTRINGS)

def discover(root):
    """Return {folder: [stl_paths]} dict for all folders containing STLs."""
    folders = {}
    for p in root.rglob("*.stl"):
        if not p.is_file() or _excluded(p):
            continue
        folders.setdefault(p.parent, []).append(p)
    return folders

def partition(folder_counts, n_workers):
    """Greedy bin-packing: distribute folders across n_workers bins to balance
    total STL count per bin. Returns list of n_workers lists of folders."""
    bins = [[] for _ in range(n_workers)]
    weights = [0] * n_workers
    # sort folders by count descending so big folders go first
    items = sorted(folder_counts.items(), key=lambda kv: -kv[1])
    for folder, count in items:
        # assign to lightest bin
        idx = min(range(n_workers), key=lambda i: weights[i])
        bins[idx].append(folder)
        weights[idx] += count
    return bins, weights

def run_worker(worker_id, folder_list_path, blender, extra_args, log_path):
    """Spawn one Blender headless and stream its output to log_path."""
    cmd = [blender, "--background", "--python", str(HERE / "batch_preview.py"),
           "--", "--folders-file", str(folder_list_path)] + list(extra_args)
    t0 = time.time()
    with open(log_path, 'w', encoding='utf-8') as logf:
        proc = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT)
    return worker_id, proc.returncode, time.time() - t0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--workers", type=int,
                    default=max(1, (mp.cpu_count() or 4) - 1),
                    help=f"number of parallel Blender workers (default: cpu_count-1)")
    ap.add_argument("--blender", default=DEFAULT_BLENDER,
                    help=f"path to blender.exe (default: {DEFAULT_BLENDER})")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--force-groups", action="store_true")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")

    print(f"discovering STLs under {root}...", flush=True)
    folder_map = discover(root)
    if not folder_map:
        print("no STLs found", flush=True)
        return
    counts = {f: len(stls) for f, stls in folder_map.items()}
    total_stls = sum(counts.values())
    print(f"found {total_stls} STLs across {len(counts)} folders", flush=True)

    n = min(args.workers, len(counts))
    bins, weights = partition(counts, n)
    print(f"partitioning across {n} workers (weights: {weights})", flush=True)

    # write per-worker folder lists into a sibling 'workers/' dir
    workdir = HERE / "workers"
    workdir.mkdir(exist_ok=True)
    # clean prior chunk files
    for old in workdir.glob("worker_*.txt"):
        old.unlink()
    for old in workdir.glob("worker_*.log"):
        old.unlink()

    folder_list_paths = []
    log_paths = []
    for i, bucket in enumerate(bins):
        chunk_path = workdir / f"worker_{i:02d}.txt"
        with open(chunk_path, 'w', encoding='utf-8') as f:
            for folder in bucket:
                f.write(str(folder) + "\n")
        folder_list_paths.append(chunk_path)
        log_paths.append(workdir / f"worker_{i:02d}.log")

    extra_args = []
    if args.force: extra_args.append("--force")
    if args.force_groups: extra_args.append("--force-groups")

    print(f"\nlaunching {n} Blender workers...", flush=True)
    t0 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=n) as pool:
        futs = {pool.submit(run_worker, i, folder_list_paths[i], args.blender,
                            extra_args, log_paths[i]): i for i in range(n)}
        for fut in as_completed(futs):
            wid, rc, elapsed = fut.result()
            results.append((wid, rc, elapsed))
            status = "OK" if rc == 0 else f"FAIL(rc={rc})"
            print(f"  worker {wid:02d} done in {elapsed:.1f}s [{status}] "
                  f"({len(bins[wid])} folders, {weights[wid]} STLs)", flush=True)

    total_elapsed = time.time() - t0
    n_ok = sum(1 for _, rc, _ in results if rc == 0)
    n_fail = n - n_ok
    print(f"\n=== all workers done in {total_elapsed:.1f}s "
          f"({n_ok} ok, {n_fail} failed) — STLs/sec={total_stls/total_elapsed:.1f}",
          flush=True)
    print(f"per-worker logs: {workdir}\\worker_*.log")

if __name__ == "__main__":
    main()
