import os
import threading
from pathlib import Path

from analyzer import category_of, human_size, percent, scan


def write(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def child(node, name):
    return next(c for c in node.children if c.name == name)


def test_folder_sizes_include_everything_below(tmp_path):
    write(tmp_path / "a.txt", 100)
    write(tmp_path / "Photos" / "p1.jpg", 1000)
    write(tmp_path / "Photos" / "2026" / "p2.jpg", 2000)

    result = scan(tmp_path)

    assert result.root.size == 3100 and result.root.file_count == 3
    photos = child(result.root, "Photos")
    assert photos.size == 3000 and photos.file_count == 2
    assert child(photos, "2026").size == 2000


def test_children_sorted_biggest_first(tmp_path):
    write(tmp_path / "Small" / "s.txt", 10)
    write(tmp_path / "Big" / "b.txt", 500)
    write(tmp_path / "Medium" / "m.txt", 100)

    names = [c.name for c in scan(tmp_path).root.children]
    assert names == ["Big", "Medium", "Small"]


def test_empty_folder(tmp_path):
    (tmp_path / "Empty").mkdir()
    result = scan(tmp_path)
    assert result.root.size == 0
    assert child(result.root, "Empty").size == 0


def test_largest_files_keeps_only_the_top_n(tmp_path):
    for i in range(1, 11):
        write(tmp_path / f"f{i}.bin", i * 100)

    result = scan(tmp_path, top_files=3)

    assert [(s, p.name) for s, p in result.largest_files] == [
        (1000, "f10.bin"), (900, "f9.bin"), (800, "f8.bin"),
    ]


def test_totals_by_type_biggest_first(tmp_path):
    write(tmp_path / "movie.mp4", 5000)
    write(tmp_path / "a.jpg", 300)
    write(tmp_path / "b.PNG", 200)
    write(tmp_path / "data.xyz", 50)

    by_type = scan(tmp_path).by_type

    assert list(by_type) == ["Videos", "Images", "Other"]
    assert by_type["Images"].size == 500 and by_type["Images"].count == 2


def test_links_are_never_followed(tmp_path):
    write(tmp_path / "real" / "big.bin", 1000)
    os.symlink(tmp_path / "real", tmp_path / "link", target_is_directory=True)
    os.symlink(tmp_path, tmp_path / "real" / "loop", target_is_directory=True)  # points to its own parent

    result = scan(tmp_path)

    assert result.root.size == 1000  # counted once, and no infinite loop
    assert [c.name for c in result.root.children] == ["real"]


def test_unreadable_folder_is_skipped_not_crashed(tmp_path):
    write(tmp_path / "ok.txt", 10)
    locked = tmp_path / "Locked"
    write(locked / "secret.txt", 999)
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):  # running as admin/root: can't simulate
            return
        result = scan(tmp_path)
        assert result.skipped_folders == 1
        assert result.root.size == 10
    finally:
        locked.chmod(0o755)


def test_cancel_stops_the_scan(tmp_path):
    for i in range(50):
        write(tmp_path / f"f{i}.txt", 1)
    cancel = threading.Event()
    cancel.set()

    result = scan(tmp_path, cancel=cancel)

    assert result.cancelled
    assert result.root.file_count < 50


def test_progress_is_reported(tmp_path):
    for i in range(25):
        write(tmp_path / f"f{i}.txt", 1)
    seen = []

    scan(tmp_path, progress=seen.append, progress_every=10)

    assert seen == [10, 20]


def test_very_deep_folders_do_not_crash(tmp_path):
    deep = tmp_path
    for i in range(60):
        deep = deep / f"d{i}"
    write(deep / "bottom.txt", 7)
    assert scan(tmp_path).root.size == 7


def test_category_of():
    assert category_of("Movie.MKV") == "Videos"
    assert category_of("report.pdf") == "Documents"
    assert category_of("README") == "Other"


def test_human_size_and_percent():
    assert human_size(512) == "512 bytes"
    assert human_size(1536) == "1.5 KB"
    assert human_size(3 * 1024 ** 3) == "3.0 GB"
    assert percent(25, 100) == 25.0
    assert percent(5, 0) == 0.0


def test_online_only_files_are_counted_separately(tmp_path, monkeypatch):
    import analyzer

    write(tmp_path / "local.jpg", 100)
    write(tmp_path / "cloud.mp4", 777)  # pretend this one is OneDrive "online-only"
    # Simulate the Windows flag: files of 777 bytes are marked as cloud-only
    monkeypatch.setattr(
        analyzer, "attributes",
        lambda info: analyzer.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS if info.st_size == 777 else 0,
    )

    result = scan(tmp_path)

    assert result.root.size == 100
    assert result.cloud_only_count == 1 and result.cloud_only_size == 777
    assert "Videos" not in result.by_type


def test_files_directly_in_a_folder_are_tracked_separately(tmp_path):
    # Downloads-style: big loose files next to small subfolders
    write(tmp_path / "big.iso", 5000)
    write(tmp_path / "Sub" / "small.txt", 100)

    result = scan(tmp_path)

    root = result.root
    assert root.size == 5100
    assert root.own_size == 5000 and root.own_files == 1
    sub = child(root, "Sub")
    # subfolders + loose files always add up to the folder's total
    assert sum(c.size for c in root.children) + root.own_size == root.size
    assert sub.own_size == 100