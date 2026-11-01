"""Disk Space Analyzer - the window.

All the real work happens in analyzer.py. This file only shows results.
Scanning runs in a background thread so the window stays responsive, and
can be cancelled. Read-only: nothing is ever changed.

Run with:  python app.py
"""

import logging
import os
import subprocess
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from analyzer import FolderNode, ScanResult, human_size, percent, scan

POLL_MS = 150
BAR_WIDTH = 20  # characters in the text bar in the Folders tab

# Chart colours (single series: one hue, text in text colours)
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
BAR_COLOUR = "#2a78d6"


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    folder = Path(base) / "DiskSpaceAnalyzer" if base else Path.home() / ".disk-space-analyzer"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


DATA_DIR = app_data_dir()
logging.basicConfig(
    filename=DATA_DIR / "app.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def text_bar(share: float) -> str:
    """A bar made of characters: 50% -> ██████████"""
    return "█" * round(BAR_WIDTH * share / 100)


def open_in_explorer(path: Path) -> None:
    if sys.platform == "win32":
        subprocess.run(["explorer", "/select,", str(path)])
    else:
        messagebox.showinfo("Location", str(path))


class AnalyzerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Disk Space Analyzer")
        self.geometry("980x640")
        self.minsize(760, 480)

        self.folder: Path | None = None
        self.result: ScanResult | None = None
        self.nodes: dict[str, FolderNode] = {}  # tree row id -> folder
        self.files_seen = 0
        self.cancel_event = threading.Event()
        self.scan_output = None

        self.fix_row_height()
        self.build_top_row()
        self.build_bottom_row()
        self.build_tabs()
        self.refresh_buttons(scanning=False)

    # ---------------------------------------------------------------- layout

    def fix_row_height(self):
        """Make table rows tall enough for the font. Without this, rows are
        squashed on Windows when display scaling is above 100%."""
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        ttk.Style(self).configure("Treeview", rowheight=int(line * 1.5))
        # Column widths are in pixels, so grow them as much as the font grew
        self.scale = max(1.0, line / 16)

    def build_top_row(self):
        row = ttk.Frame(self, padding=(12, 12, 12, 6))
        row.pack(fill="x")
        ttk.Button(row, text="Choose folder or drive…", command=self.on_choose).pack(side="left")
        self.scan_button = ttk.Button(row, text="Scan", command=self.on_scan)
        self.scan_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(row, text="Cancel", command=self.on_cancel)
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.folder_label = ttk.Label(row, text="No folder chosen yet", padding=(10, 0))
        self.folder_label.pack(side="left")

    def build_bottom_row(self):
        row = ttk.Frame(self, padding=(12, 6, 12, 12))
        row.pack(side="bottom", fill="x")
        self.status = ttk.Label(row, text="Choose a folder or drive, then click Scan. Nothing is ever changed.")
        self.status.pack(side="left", fill="x", expand=True)
        self.open_button = ttk.Button(row, text="Open in Explorer", command=self.on_open)
        self.open_button.pack(side="right")

    def build_tabs(self):
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=12)
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self.refresh_buttons(scanning=False))

        # Folders: expandable tree, biggest first
        self.folder_tree = self.make_tree(
            ("size", "share", "bar", "files"),
            (("size", "Size", 100), ("share", "% of parent", 110), ("bar", "", 200), ("files", "Files", 80)),
            tree_label="Folder",
        )
        self.folder_tree.bind("<<TreeviewOpen>>", self.on_expand)

        # Largest files
        self.file_tree = self.make_tree(
            ("size", "folder"), (("size", "Size", 100), ("folder", "Folder", 420)), tree_label="File",
        )

        # By type: a bar chart drawn on a canvas
        frame = ttk.Frame(self.tabs, padding=8)
        self.chart = tk.Canvas(frame, background=SURFACE, highlightthickness=0)
        self.chart.pack(fill="both", expand=True)
        self.chart.bind("<Configure>", lambda e: self.draw_chart())
        self.tabs.add(frame, text="By type")

    def make_tree(self, columns, headings, tree_label) -> ttk.Treeview:
        frame = ttk.Frame(self.tabs, padding=8)
        tree = ttk.Treeview(frame, columns=columns, show="tree headings")
        tree.heading("#0", text=tree_label)
        tree.column("#0", width=int(340 * self.scale), stretch=True)
        for column, text, width in headings:
            tree.heading(column, text=text)
            tree.column(column, width=int(width * self.scale), minwidth=int(width * self.scale),
                        stretch=column == "folder",
                        anchor="e" if column in ("size", "share", "files") else "w")
        tree.tag_configure("loose", foreground=TEXT_SECONDARY)
        tree.bind("<Double-1>", lambda e: self.on_open())
        tree.bind("<<TreeviewSelect>>", lambda e: self.refresh_buttons(scanning=False))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tabs.add(frame, text="Folders" if tree_label == "Folder" else "Largest files")
        return tree

    def refresh_buttons(self, scanning: bool):
        def enable(button, on):
            button.state(["!disabled"] if on else ["disabled"])

        enable(self.scan_button, self.folder is not None and not scanning)
        enable(self.cancel_button, scanning)
        enable(self.open_button, self.selected_path() is not None)

    # -------------------------------------------------------------- scanning

    def on_choose(self):
        chosen = filedialog.askdirectory(title="Choose a folder or drive")
        if chosen:
            self.folder = Path(chosen)
            self.folder_label.config(text=str(self.folder))
            self.refresh_buttons(scanning=False)

    def on_scan(self):
        self.clear_results()
        self.cancel_event.clear()
        self.files_seen = 0
        self.scan_output = None
        self.status.config(text="Scanning…")
        self.refresh_buttons(scanning=True)
        threading.Thread(target=self.scan_in_background, args=(self.folder,), daemon=True).start()
        self.after(POLL_MS, self.check_scan)

    def scan_in_background(self, folder: Path):
        """Runs on a background thread: no window code allowed in here."""
        try:
            result = scan(folder, cancel=self.cancel_event, progress=self.record_progress)
            self.scan_output = ("ok", result)
        except Exception:
            logging.exception("Scan failed")
            self.scan_output = ("error", None)

    def record_progress(self, files_seen: int):
        self.files_seen = files_seen  # read by check_scan on the window's thread

    def on_cancel(self):
        self.cancel_event.set()
        self.status.config(text="Cancelling…")

    def check_scan(self):
        """Runs on the window's thread until the scan finishes."""
        if self.scan_output is None:
            if not self.cancel_event.is_set():
                self.status.config(text=f"Scanning… {self.files_seen:,} files so far")
            self.after(POLL_MS, self.check_scan)
            return

        outcome, result = self.scan_output
        self.refresh_buttons(scanning=False)
        if outcome == "error":
            self.status.config(text="The scan stopped unexpectedly.")
            messagebox.showerror("Something went wrong",
                                 f"Sorry, the scan stopped unexpectedly.\n\nDetails: {DATA_DIR / 'app.log'}")
            return

        self.result = result
        self.show_results()

    # --------------------------------------------------------------- results

    def clear_results(self):
        self.result = None
        self.nodes.clear()
        self.folder_tree.delete(*self.folder_tree.get_children())
        self.file_tree.delete(*self.file_tree.get_children())
        self.draw_chart()

    def show_results(self):
        result = self.result
        root = result.root

        # Folders tab: the root, open, with its children
        root_id = self.add_folder_row("", root, root.size)
        self.folder_tree.item(root_id, open=True)
        self.fill_children(root_id)

        # Largest files tab
        for size, path in result.largest_files:
            self.file_tree.insert("", "end", iid=str(path), text=path.name,
                                  values=(human_size(size), str(path.parent)))

        self.draw_chart()

        parts = [f"{human_size(root.size)} in {root.file_count:,} files"]
        if result.cancelled:
            parts.insert(0, "Cancelled - partial results:")
        if result.skipped_folders:
            parts.append(f"{result.skipped_folders:,} folder(s) couldn't be opened (no permission)")
        if result.cloud_only_count:
            parts.append(f"{result.cloud_only_count:,} online-only OneDrive file(s) "
                         f"({human_size(result.cloud_only_size)}, not using disk space)")
        self.status.config(text=" · ".join(parts))

    def add_folder_row(self, parent_id: str, node: FolderNode, parent_size: int) -> str:
        share = percent(node.size, parent_size)
        row_id = self.folder_tree.insert(
            parent_id, "end", text=node.name,
            values=(human_size(node.size), f"{share:.1f}%", text_bar(share), f"{node.file_count:,}"),
        )
        self.nodes[row_id] = node
        if node.children:
            self.folder_tree.insert(row_id, "end", text="loading…")  # placeholder so it can expand
        return row_id

    def fill_children(self, row_id: str):
        """Add a folder's subfolders only when it's opened (fast for huge drives).

        Files sitting directly in the folder get their own row, sorted in with
        the subfolders, so the rows always add up to the folder's total.
        """
        node = self.nodes[row_id]
        self.folder_tree.delete(*self.folder_tree.get_children(row_id))
        rows = [(child.size, child) for child in node.children]
        if node.own_files:
            rows.append((node.own_size, None))  # None = "files in this folder"
        rows.sort(key=lambda r: -r[0])
        for size, child in rows:
            if child is None:
                self.add_loose_files_row(row_id, node)
            else:
                self.add_folder_row(row_id, child, node.size)

    def add_loose_files_row(self, parent_id: str, node: FolderNode):
        share = percent(node.own_size, node.size)
        row_id = self.folder_tree.insert(
            parent_id, "end", text="(files directly in this folder)", tags=("loose",),
            values=(human_size(node.own_size), f"{share:.1f}%", text_bar(share), f"{node.own_files:,}"),
        )
        self.nodes[row_id] = node  # Open in Explorer opens the folder itself

    def on_expand(self, event):
        row_id = self.folder_tree.focus()
        children = self.folder_tree.get_children(row_id)
        if children and children[0] not in self.nodes:  # still the placeholder
            self.fill_children(row_id)

    def draw_chart(self):
        """Horizontal bars, one per file type, biggest first, labelled directly."""
        canvas = self.chart
        canvas.delete("all")
        if not self.result or not self.result.by_type:
            canvas.create_text(20, 20, anchor="nw", fill=TEXT_SECONDARY,
                               text="Scan a folder to see space used by file type.")
            return

        width = max(canvas.winfo_width(), 400)
        label_width, value_width, row_height, bar_height = 110, 220, 34, 14
        bar_space = width - label_width - value_width - 40
        total = self.result.root.size
        biggest = max(t.size for t in self.result.by_type.values()) or 1

        canvas.create_text(20, 16, anchor="w", fill=TEXT_PRIMARY, font=("TkDefaultFont", 10, "bold"),
                           text="Space used by file type")
        for i, (category, t) in enumerate(self.result.by_type.items()):
            y = 50 + i * row_height
            canvas.create_text(20, y, anchor="w", fill=TEXT_PRIMARY, text=category)
            length = max(2, bar_space * t.size / biggest)
            x0 = 20 + label_width
            canvas.create_rectangle(x0, y - bar_height / 2, x0 + length, y + bar_height / 2,
                                    fill=BAR_COLOUR, outline="")
            canvas.create_text(x0 + length + 8, y, anchor="w", fill=TEXT_SECONDARY,
                               text=f"{human_size(t.size)} · {percent(t.size, total):.0f}% · {t.count:,} {'file' if t.count == 1 else 'files'}")

    # --------------------------------------------------------------- actions

    def selected_path(self) -> Path | None:
        current = self.tabs.index(self.tabs.select()) if self.tabs.tabs() else 0
        if current == 0:
            selection = self.folder_tree.selection()
            node = self.nodes.get(selection[0]) if selection else None
            return node.path if node else None
        if current == 1:
            selection = self.file_tree.selection()
            return Path(selection[0]) if selection else None
        return None

    def on_open(self):
        path = self.selected_path()
        if path is not None:
            open_in_explorer(path)

    def report_callback_exception(self, exc_type, exc_value, exc_traceback):
        logging.error("Unexpected error", exc_info=(exc_type, exc_value, exc_traceback))
        messagebox.showerror("Something went wrong",
                             f"Sorry, something unexpected happened.\n\nDetails were saved to:\n{DATA_DIR / 'app.log'}")


def enable_sharp_text_on_windows():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


if __name__ == "__main__":
    enable_sharp_text_on_windows()
    AnalyzerApp().mainloop()