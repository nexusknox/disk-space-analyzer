"""Core logic for Disk Space Analyzer.

Read-only: nothing here changes, moves or deletes anything.
No UI code lives here, so everything can be tested on temporary folders.
"""

import heapq
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# File types (for the "By type" totals)
# ---------------------------------------------------------------------------

CATEGORIES = {
    "Videos": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv"},
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".svg", ".raw"},
    "Audio": {".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".md",
                  ".xls", ".xlsx", ".csv", ".ppt", ".pptx"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".iso"},
    "Programs": {".exe", ".msi", ".dll"},
}
OTHER = "Other"


def category_of(name: str) -> str:
    extension = Path(name).suffix.lower()
    for category, extensions in CATEGORIES.items():
        if extension in extensions:
            return category
    return OTHER


# ---------------------------------------------------------------------------
# Windows attributes
# ---------------------------------------------------------------------------

# A "reparse point" is a link (symlink or junction). Following it could count
# files twice, or loop forever (e.g. "Application Data" points to its parent).
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# OneDrive "online-only" files: their size is reported, but they use no disk space.
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_ONLY_FLAGS = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS


def attributes(stat_result) -> int:
    # st_file_attributes only exists on Windows; elsewhere treat it as 0
    return getattr(stat_result, "st_file_attributes", 0)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class FolderNode:
    path: Path
    size: int = 0          # bytes, including everything below it
    file_count: int = 0    # files, including everything below it
    own_size: int = 0      # bytes of files directly in this folder (not in subfolders)
    own_files: int = 0
    children: list["FolderNode"] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name or str(self.path)  # a drive like C:\ has no name


@dataclass
class TypeTotal:
    size: int = 0
    count: int = 0


@dataclass
class ScanResult:
    root: FolderNode
    largest_files: list[tuple[int, Path]]   # (size, path), biggest first
    by_type: dict[str, TypeTotal]           # biggest first
    skipped_folders: int = 0                # couldn't open (no permission)
    cloud_only_count: int = 0
    cloud_only_size: int = 0                # not counted in any total
    cancelled: bool = False


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def scan(
    folder: Path,
    top_files: int = 100,
    cancel: threading.Event | None = None,
    progress: Callable[[int], None] | None = None,
    progress_every: int = 1000,
) -> ScanResult:
    """Measure everything under `folder`.

    - Walks the tree once, without recursion (very deep folders can't crash it).
    - Never follows links, so nothing is counted twice and it can't loop.
    - Folders it can't open are counted in `skipped_folders`, not an error.
    - Keeps only the `top_files` largest files (a heap), so memory stays small
      even on a drive with millions of files.
    - `cancel` (set from the window) stops the scan; `progress` reports the
      number of files seen so far.
    """
    root = FolderNode(path=folder)
    nodes_in_order: list[tuple[FolderNode, FolderNode | None]] = []  # (node, parent)
    to_visit: list[tuple[FolderNode, FolderNode | None]] = [(root, None)]

    largest: list[tuple[int, str]] = []  # min-heap of (size, path) - the smallest of the top N is on top
    by_type: dict[str, TypeTotal] = {}
    result = ScanResult(root=root, largest_files=[], by_type={})
    files_seen = 0

    while to_visit:
        node, parent = to_visit.pop()
        nodes_in_order.append((node, parent))

        try:
            entries = list(os.scandir(node.path))
        except OSError:
            result.skipped_folders += 1
            continue

        for entry in entries:
            if cancel is not None and cancel.is_set():
                result.cancelled = True
                return finish(result, nodes_in_order, largest, by_type)
            try:
                info = entry.stat(follow_symlinks=False)
                flags = attributes(info)
                if entry.is_symlink() or flags & FILE_ATTRIBUTE_REPARSE_POINT:
                    continue  # never follow links or junctions

                if entry.is_dir(follow_symlinks=False):
                    child = FolderNode(path=Path(entry.path))
                    node.children.append(child)
                    to_visit.append((child, node))
                    continue

                if flags & CLOUD_ONLY_FLAGS:
                    result.cloud_only_count += 1
                    result.cloud_only_size += info.st_size
                    continue

                size = info.st_size
                node.size += size
                node.file_count += 1
                node.own_size += size
                node.own_files += 1

                total = by_type.setdefault(category_of(entry.name), TypeTotal())
                total.size += size
                total.count += 1

                if len(largest) < top_files:
                    heapq.heappush(largest, (size, entry.path))
                elif size > largest[0][0]:
                    heapq.heapreplace(largest, (size, entry.path))

                files_seen += 1
                if progress and files_seen % progress_every == 0:
                    progress(files_seen)
            except OSError:
                continue  # vanished or unreadable mid-scan: skip it

    return finish(result, nodes_in_order, largest, by_type)


def finish(result, nodes_in_order, largest, by_type) -> ScanResult:
    """Add each folder's total to its parent, deepest first, then sort."""
    # nodes_in_order lists every parent before its children, so walking it
    # backwards guarantees a folder is complete before it's added to its parent
    for node, parent in reversed(nodes_in_order):
        node.children.sort(key=lambda c: (-c.size, c.name.lower()))
        if parent is not None:
            parent.size += node.size
            parent.file_count += node.file_count

    result.largest_files = [(size, Path(p)) for size, p in sorted(largest, reverse=True)]
    result.by_type = dict(sorted(by_type.items(), key=lambda item: -item[1].size))
    return result


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def human_size(size: int) -> str:
    """1536 -> '1.5 KB'"""
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{size} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def percent(part: int, whole: int) -> float:
    return 0.0 if whole == 0 else 100 * part / whole