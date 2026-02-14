# -*- coding: utf-8 -*-
"""
Asset Browser (3ds Max friendly, with CORT section + grouped thumbnails)
- Prefers PySide2 (qtmax) in Max; falls back to PyQt5
- Parents window only when bindings match (no mixups)
- Lazy tree (expand-on-demand)
- Background file scan + background thumbnail loading (both cancellable)
- Safe QLabel creation (no overload ambiguity)
- Defensive filesystem IO; caps and batching for big/messy folders
- EXCLUDES CORT from Library-root scans so CORT doesn't mix in
- Grouped thumbnail view with thin separators between groups
"""

import os
import sys
import time
import threading
import pymxs
import csv
import datetime
from collections import defaultdict

# ------------------------- Qt selection & parenting --------------------------
QT_LIB = None
USING_PYSIDE2 = False
QtWidgets = QtGui = QtCore = None
QtMultimedia = None
MAIN_WINDOW = None

# Prefer PySide2 when qtmax (3ds Max) is present
try:
    import qtmax  # 3ds Max helper (PySide2-based)
    from PySide2 import QtWidgets, QtGui, QtCore
    try:
        from PySide2 import QtMultimedia
    except Exception:
        QtMultimedia = None
    QT_LIB = "PySide2"
    USING_PYSIDE2 = True
    MAIN_WINDOW = qtmax.GetQMaxMainWindow()
except Exception:
    # Fallback to PyQt5 (unparented top-level)
    try:
        from PyQt5 import QtWidgets, QtGui, QtCore, QtMultimedia
        QT_LIB = "PyQt5"
        USING_PYSIDE2 = False
        MAIN_WINDOW = None
    except Exception as e:
        raise RuntimeError("Neither PySide2 nor PyQt5 is available.") from e

rt = pymxs.runtime

def log(msg):
    try:
        print(msg)
        rt.print(msg)
    except Exception:
        try:
            print(msg)
        except Exception:
            pass

# ------------------------------ Configuration --------------------------------
LIBRARY_ROOT = r"L:\3D_Library\VRay\HargroveAssestsForMaxScript"
CORT_ROOT    = os.path.join(LIBRARY_ROOT, "CORT")
THUMB_ROOT   = os.path.join(LIBRARY_ROOT, "thumbnails")
NOTES_ROOT   = os.path.join(LIBRARY_ROOT, "Notes")
IMPORT_SFX   = os.path.join(LIBRARY_ROOT, "1447_magic-wand-01.wav")

THUMB_SIZE       = 120
GRID_COLS        = 6
MAX_ASSETS_PER_FOLDER = 2000   # cap results to keep UI responsive
SCAN_BATCH_EMIT  = 24          # UI update granularity during scan
THUMB_BATCH_EMIT = 32          # thumbnail load batch size

# Optional: skip common junk dirs
ENABLE_DIR_EXCLUDES = True
DIR_EXCLUDES = {"_old", "_backup", "backup", "temp", "tmp", "textures", "maps", "__macosx"}

# When scanning the Library root, exclude the CORT dir so assets don't mix
EXCLUDE_CORT_FROM_LIBRARY_SCAN = True

# ------------------------------- Workers -------------------------------------
class FileScanner(QtCore.QThread):
    if QT_LIB == "PySide2":
        fileFound   = QtCore.Signal(str)
        batchDone   = QtCore.Signal()
        finishedSafe= QtCore.Signal()
        error       = QtCore.Signal(str)
    else:
        fileFound   = QtCore.pyqtSignal(str)
        batchDone   = QtCore.pyqtSignal()
        finishedSafe= QtCore.pyqtSignal()
        error       = QtCore.pyqtSignal(str)

    def __init__(self, root_path, recursive=True, max_items=MAX_ASSETS_PER_FOLDER, dir_exclude_names=None, parent=None):
        super(FileScanner, self).__init__(parent)
        self.root_path = root_path
        self.recursive = recursive
        self.max_items = max_items
        self._stop = threading.Event()
        self.dir_exclude_names = set((dir_exclude_names or []))

    def stop(self):
        self._stop.set()

    def run(self):
        count = 0
        try:
            if not os.path.isdir(self.root_path):
                self.finishedSafe.emit()
                return

            if self.recursive:
                for root, dirs, files in os.walk(self.root_path, followlinks=False):
                    if self._stop.is_set():
                        break

                    # prune excluded dirs in-place to speed up
                    try:
                        if ENABLE_DIR_EXCLUDES:
                            dirs[:] = [d for d in dirs if d.lower() not in DIR_EXCLUDES]
                        if self.dir_exclude_names:
                            dirs[:] = [d for d in dirs if d not in self.dir_exclude_names]
                    except Exception:
                        pass

                    for f in files:
                        if self._stop.is_set():
                            break
                        try:
                            if f.lower().endswith(".max"):
                                self.fileFound.emit(os.path.join(root, f))
                                count += 1
                                if count % SCAN_BATCH_EMIT == 0:
                                    self.batchDone.emit()
                                if count >= self.max_items:
                                    self.error.emit("Asset limit reached ({}). Showing first {} items."
                                                    .format(self.max_items, self.max_items))
                                    self.finishedSafe.emit()
                                    return
                        except Exception:
                            continue
            else:
                try:
                    files = os.listdir(self.root_path)
                except Exception:
                    files = []
                for f in files:
                    if self._stop.is_set():
                        break
                    p = os.path.join(self.root_path, f)
                    try:
                        if os.path.isfile(p) and f.lower().endswith(".max"):
                            self.fileFound.emit(p)
                            count += 1
                            if count % SCAN_BATCH_EMIT == 0:
                                self.batchDone.emit()
                            if count >= self.max_items:
                                self.error.emit("Asset limit reached ({}). Showing first {} items."
                                                .format(self.max_items, self.max_items))
                                self.finishedSafe.emit()
                                return
                    except Exception:
                        continue

            self.finishedSafe.emit()
        except Exception as e:
            self.error.emit("Scan error: {}".format(e))
            self.finishedSafe.emit()

class ThumbLoader(QtCore.QThread):
    if QT_LIB == "PySide2":
        thumbReady   = QtCore.Signal(str, QtGui.QPixmap)
        finishedSafe = QtCore.Signal()
    else:
        thumbReady   = QtCore.pyqtSignal(str, QtGui.QPixmap)
        finishedSafe = QtCore.pyqtSignal()

    def __init__(self, thumb_size, thumbnail_root, parent=None):
        super(ThumbLoader, self).__init__(parent)
        self.thumb_size = thumb_size
        self.thumbnail_root = thumbnail_root
        self._stop = threading.Event()
        self._queue = []
        self._lock = threading.Lock()

    def stop(self):
        self._stop.set()

    def clearQueue(self):
        with self._lock:
            self._queue = []

    def addPaths(self, paths):
        with self._lock:
            self._queue.extend(paths)

    def run(self):
        try:
            while not self._stop.is_set():
                with self._lock:
                    if not self._queue:
                        break
                    batch = self._queue[:THUMB_BATCH_EMIT]
                    self._queue = self._queue[THUMB_BATCH_EMIT:]
                for asset_path in batch:
                    if self._stop.is_set():
                        break
                    try:
                        pm = self._make_thumb(asset_path, self.thumb_size)
                    except Exception:
                        pm = QtGui.QPixmap(self.thumb_size, self.thumb_size)
                        pm.fill(QtGui.QColor("gray"))
                    self.thumbReady.emit(asset_path, pm)
                time.sleep(0.01)
        finally:
            self.finishedSafe.emit()

    def _make_thumb(self, asset_path, size):
        base = os.path.splitext(os.path.basename(asset_path))[0]
        local_dir = os.path.dirname(asset_path)
        candidates = [
            os.path.join(local_dir, base + ".jpg"),
            os.path.join(local_dir, base + ".png"),
            os.path.join(self.thumbnail_root, base + ".jpg"),
            os.path.join(self.thumbnail_root, base + ".png"),
        ]
        pm = QtGui.QPixmap()
        for c in candidates:
            try:
                if os.path.exists(c):
                    pm = QtGui.QPixmap(c)
                    if not pm.isNull():
                        break
            except Exception:
                continue

        if pm.isNull():
            pm = QtGui.QPixmap(size, size)
            pm.fill(QtGui.QColor("gray"))

        pm = pm.scaled(size, size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)

        rounded = QtGui.QPixmap(size, size)
        rounded.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(rounded)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        path = QtGui.QPainterPath()
        path.addRoundedRect(0, 0, size, size, 10, 10)
        painter.setClipPath(path)
        x = (size - pm.width()) // 2
        y = (size - pm.height()) // 2
        painter.drawPixmap(x, y, pm)
        pen = QtGui.QPen(QtGui.QColor("white"), 1)
        painter.setPen(pen)
        painter.drawRoundedRect(0, 0, size, size, 10, 10)
        painter.end()
        return rounded

# ------------------------------ Main Widget ----------------------------------
class AssetBrowser(QtWidgets.QWidget):
    def __init__(self, library_root, cort_root, thumbnail_folder, notes_folder, parent=None):
        # Parent only when using the same binding as Max (PySide2)
        if USING_PYSIDE2 and parent is not None:
            super(AssetBrowser, self).__init__(parent, QtCore.Qt.Window)
        else:
            super(AssetBrowser, self).__init__(None, QtCore.Qt.Window)

        self.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)

        self.library_root = library_root
        self.cort_root = cort_root
        self.thumbnail_folder = thumbnail_folder
        self.notes_folder = notes_folder

        self.setWindowTitle("Asset Browser")
        self.resize(1400, 780)

        # data holders
        self.assets = {}          # path -> QLabel
        self.group_map = {}       # group_name -> [paths]
        self.current_folder = self.library_root

        self.scanner = None
        self.thumb_loader = None

        main_h = QtWidgets.QHBoxLayout(self)

        # Left splitter (sidebar + notes)
        self.splitter_left = QtWidgets.QSplitter(QtCore.Qt.Vertical)

        # Sidebar
        sidebar = QtWidgets.QWidget(self)
        side_v = QtWidgets.QVBoxLayout(sidebar)
        side_v.setContentsMargins(6, 6, 6, 6)
        side_v.setSpacing(6)

        # Controls
        controls = QtWidgets.QHBoxLayout()
        self.search_bar = QtWidgets.QLineEdit()
        self.search_bar.setPlaceholderText("Search in current folder...")
        self.search_bar.textChanged.connect(self._on_search_changed)

        self.recursive_chk = QtWidgets.QCheckBox("Recursive")
        self.recursive_chk.setChecked(True)
        
        self.mute_chk = QtWidgets.QCheckBox("Mute")
        self.mute_chk.setChecked(True)  # checked by default
        self.mute_chk.setEnabled(True)  # let the user toggle it
        controls.addWidget(self.mute_chk)
        
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop_workers)
        
        self.bom_btn = QtWidgets.QPushButton("Export BOM")
        self.bom_btn.clicked.connect(self._export_bom)
        controls.addWidget(self.bom_btn)

        controls.addWidget(self.search_bar)
        controls.addWidget(self.recursive_chk)
        controls.addWidget(self.mute_chk)
        controls.addWidget(self.stop_btn)
        side_v.addLayout(controls)

        # Tree
        self.file_tree = QtWidgets.QTreeWidget()
        self.file_tree.setHeaderLabel("Assets")
        self.file_tree.itemExpanded.connect(self._on_item_expanded)
        self.file_tree.itemClicked.connect(self._on_tree_item_click)
        side_v.addWidget(self.file_tree)

        self._build_lazy_tree()
        self.splitter_left.addWidget(sidebar)

        # Notes
        self.notes_box = QtWidgets.QTextEdit()
        self.notes_box.setReadOnly(True)
        self.notes_box.setPlaceholderText("Select an asset to see notes...")
        self.splitter_left.addWidget(self.notes_box)

        # Right content area
        self.splitter_main = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        self.splitter_main.addWidget(self.splitter_left)

        right = QtWidgets.QWidget(self)
        right_v = QtWidgets.QVBoxLayout(right)
        right_v.setContentsMargins(6, 6, 6, 6)
        right_v.setSpacing(6)

        self.breadcrumb = QtWidgets.QLabel("")
        self.breadcrumb.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.breadcrumb.setStyleSheet("color:#888; padding:2px;")
        right_v.addWidget(self.breadcrumb)

        self.status = QtWidgets.QLabel("")
        self.status.setStyleSheet("color:#888; padding:2px;")
        right_v.addWidget(self.status)

        self.scroll_area = QtWidgets.QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        right_v.addWidget(self.scroll_area)

        self.content = QtWidgets.QWidget()
        self.grid_layout = QtWidgets.QGridLayout(self.content)
        self.grid_layout.setContentsMargins(8, 8, 8, 8)
        self.grid_layout.setHorizontalSpacing(8)
        self.grid_layout.setVerticalSpacing(8)
        self.scroll_area.setWidget(self.content)

        self.splitter_main.addWidget(right)
        main_h.addWidget(self.splitter_main)

        self._setup_sound()
        self._start_thumb_loader()
        self._load_folder(self.current_folder)

        # Debounced search
        self._search_timer = QtCore.QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._apply_search_filter)

        # Bring to front after construction
        QtCore.QTimer.singleShot(0, self._bring_to_front)

    # ---------- show/raise ----------
    def _bring_to_front(self):
        try:
            self.show()
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

    # --------------------------- Tree (lazy) ---------------------------------
    def _build_lazy_tree(self):
        self.file_tree.clear()

        def add_root(label, path):
            item = QtWidgets.QTreeWidgetItem(self.file_tree, [label])
            item.setData(0, QtCore.Qt.UserRole, path)
            self._add_dummy_child(item)
            return item

        # Put Library first, CORT second (CORT appears at bottom)
        lib_item = add_root("Library", self.library_root)
        if os.path.isdir(self.cort_root):
            add_root("CORT", self.cort_root)
        self.file_tree.expandItem(lib_item)

    def _add_dummy_child(self, item):
        dummy = QtWidgets.QTreeWidgetItem(item, ["..."])
        dummy.setData(0, QtCore.Qt.UserRole, None)

    def _on_item_expanded(self, item):
        if item.childCount() == 1 and item.child(0).data(0, QtCore.Qt.UserRole) is None:
            item.removeChild(item.child(0))
            path = item.data(0, QtCore.Qt.UserRole)
            if not path or not os.path.isdir(path):
                return
            try:
                entries = sorted(os.listdir(path))
            except Exception:
                entries = []
            # dirs
            for e in entries:
                p = os.path.join(path, e)
                if os.path.isdir(p):
                    if ENABLE_DIR_EXCLUDES and e.lower() in DIR_EXCLUDES:
                        continue
                    child = QtWidgets.QTreeWidgetItem(item, [e])
                    child.setData(0, QtCore.Qt.UserRole, p)
                    self._add_dummy_child(child)
            # files at this level
            for e in entries:
                p = os.path.join(path, e)
                if os.path.isfile(p) and e.lower().endswith(".max"):
                    fitem = QtWidgets.QTreeWidgetItem(item, [e])
                    fitem.setData(0, QtCore.Qt.UserRole, p)

    # -------------------------- Loading / Scanning ---------------------------
    def _load_folder(self, folder):
        self._stop_workers()
        self.current_folder = folder
        self.breadcrumb.setText(folder)
        self.status.setText("Scanning...")
        self._clear_grid()
        self.assets.clear()
        self.group_map.clear()

        recursive = self.recursive_chk.isChecked()

        # If we're at the Library root and EXCLUDE_CORT is on, exclude CORT dir
        dir_exclude = set()
        if EXCLUDE_CORT_FROM_LIBRARY_SCAN:
            if os.path.abspath(folder) == os.path.abspath(self.library_root) and os.path.isdir(self.cort_root):
                dir_exclude.add(os.path.basename(self.cort_root))

        self.scanner = FileScanner(folder,
                                   recursive=recursive,
                                   max_items=MAX_ASSETS_PER_FOLDER,
                                   dir_exclude_names=dir_exclude)
        self.scanner.fileFound.connect(self._on_file_found)
        self.scanner.batchDone.connect(self._on_scan_batch_done)
        self.scanner.error.connect(self._on_scan_error)
        self.scanner.finishedSafe.connect(self._on_scan_finished)
        self.scanner.start()

    def _group_key(self, asset_path):
        """Group by first-level folder under current folder; files at root -> '(root)'."""
        try:
            rel_dir = os.path.relpath(os.path.dirname(asset_path), self.current_folder)
            if rel_dir.startswith(".."):
                return "(other)"
            parts = [p for p in rel_dir.split(os.sep) if p and p != "."]
            return parts[0] if parts else "(root)"
        except Exception:
            return "(root)"

    def _on_file_found(self, asset_path):
        # Build/remember label (parent set after construction to avoid overload issues)
        lbl = self._make_thumb_label(asset_path)
        if lbl is None:
            return
        self.assets[asset_path] = lbl

        # Group it
        g = self._group_key(asset_path)
        self.group_map.setdefault(g, []).append(asset_path)

        # Queue thumbs
        if self.thumb_loader is not None:
            self.thumb_loader.addPaths([asset_path])

    def _on_scan_batch_done(self):
        total = sum(len(v) for v in self.group_map.values())
        self.status.setText("Scanning... {} items".format(total))

    def _on_scan_error(self, msg):
        self.status.setText(msg)

    def _on_scan_finished(self):
        total = sum(len(v) for v in self.group_map.values())
        self.status.setText("Found {} item(s)".format(total))
        # Build grouped grid now
        self._render_grouped_grid()
        if self.thumb_loader and not self.thumb_loader.isRunning():
            self.thumb_loader.start()

    def _render_grouped_grid(self):
        self._clear_grid()
        row = 0
        first_group = True
        # Sort groups alphabetically; "(root)" first
        groups = sorted(self.group_map.keys(), key=lambda k: (k != "(root)", k.lower()))
        for g in groups:
            paths = self.group_map[g]
            # Separator line between groups
            if not first_group:
                line = QtWidgets.QFrame()
                line.setFrameShape(QtWidgets.QFrame.HLine)
                line.setFrameShadow(QtWidgets.QFrame.Sunken)
                line.setStyleSheet("color: #444;")
                self.grid_layout.addWidget(line, row, 0, 1, GRID_COLS)
                row += 1
            first_group = False

            # Group header
            header = QtWidgets.QLabel("{} ({})".format(g, len(paths)))
            header.setStyleSheet("font-weight: bold; padding: 4px 2px;")
            self.grid_layout.addWidget(header, row, 0, 1, GRID_COLS)
            row += 1

            # Thumbnails for this group
            col = 0
            for p in sorted(paths, key=lambda x: os.path.basename(x).lower()):
                lbl = self.assets.get(p)
                if lbl is None:
                    continue
                self.grid_layout.addWidget(lbl, row, col)
                col += 1
                if col >= GRID_COLS:
                    col = 0
                    row += 1
            if col != 0:
                row += 1  # move to next line after partial row

    def _start_thumb_loader(self):
        if self.thumb_loader:
            try:
                self.thumb_loader.stop()
            except Exception:
                pass
        self.thumb_loader = ThumbLoader(THUMB_SIZE, THUMB_ROOT)
        self.thumb_loader.thumbReady.connect(self._on_thumb_ready)

    def _stop_workers(self):
        if self.scanner and self.scanner.isRunning():
            self.scanner.stop()
            self.scanner.wait(1000)
        if self.thumb_loader and self.thumb_loader.isRunning():
            self.thumb_loader.stop()
            self.thumb_loader.wait(1000)
        if self.thumb_loader:
            self.thumb_loader.clearQueue()
        self.status.setText("Stopped.")

    # ------------------------------ UI helpers -------------------------------
    def _clear_grid(self):
        for i in reversed(range(self.grid_layout.count())):
            w = self.grid_layout.itemAt(i).widget()
            if w is not None:
                w.setParent(None)

    def _on_thumb_ready(self, asset_path, pixmap):
        lbl = self.assets.get(asset_path)
        if lbl:
            lbl.setPixmap(pixmap)

    # ------------------------------ Tree click -------------------------------
    def _on_tree_item_click(self, item, column):
        """Open folders in the grid or merge a clicked .max file."""
        try:
            data_path = item.data(0, QtCore.Qt.UserRole)
            if not data_path:
                return
            if os.path.isdir(data_path):
                self._load_folder(data_path)
            else:
                self._merge_asset(data_path)
                self._display_notes(data_path)
        except Exception as e:
            try:
                QtWidgets.QMessageBox.critical(self, "Asset Browser", "Error:\n{}".format(e))
            except Exception:
                pass

    # ------------------------------ Search -----------------------------------
    def _on_search_changed(self, _txt):
        self._search_timer.start()

    def _apply_search_filter(self):
        text = self.search_bar.text().lower().strip()
        for pth, lbl in self.assets.items():
            name = os.path.basename(pth).lower()
            lbl.setVisible((text in name) if text else True)

    # ------------------------------ Notes ------------------------------------
    def _display_notes(self, asset_path):
        base = os.path.splitext(os.path.basename(asset_path))[0]
        p = os.path.join(self.notes_folder, base + ".txt")
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    self.notes_box.setText(f.read())
            except Exception as e:
                self.notes_box.setText("Error reading notes: {}".format(e))
        else:
            self.notes_box.setText("No notes available for this asset.")

    # ------------------------------ Merge ------------------------------------
    def _merge_asset(self, asset_path):
        try:
            rt.mergeMaxFile(asset_path, quiet=True)
            self._play_sound()
            log("Merged: {}".format(asset_path))
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Import Error",
                "Failed to merge:\n{}\n\nError: {}".format(asset_path, e))

    def _on_thumb_click(self, event, asset_path):
        self._merge_asset(asset_path)
        self._display_notes(asset_path)

    # ------------------------------ BOM ------------------------------------
    def _export_bom(self):
        """
        Exports BOM based on GROUP HEADS that have BOMRoot=1.
        Reads PartName/PartNo from User Defined Properties on the group head.
        Writes CSV to Desktop (Excel will open it fine).
        """
        try:
            def get_prop(node, key):
                try:
                    buf = rt.getUserPropBuffer(node) or ""
                    # simple parse: Key=Value per line
                    for line in buf.splitlines():
                        if "=" in line:
                            k, v = line.split("=", 1)
                            if k.strip() == key:
                                return v.strip()
                    return ""
                except Exception:
                    return ""

            def iter_desc(node):
                yield node
                try:
                    for c in node.Children:  # MaxScript-style property
                        for d in iter_desc(c):
                            yield d
                except Exception:
                    # PyMXS sometimes prefers .children
                    try:
                        for c in node.children:
                            for d in iter_desc(c):
                                yield d
                    except Exception:
                        return

            def is_geom(n):
                try:
                    return rt.superClassOf(n) == rt.GeometryClass
                except Exception:
                    return False

            def world_bbox(n):
                try:
                    bb = rt.nodeGetBoundingBox(n, n.transform)
                    return bb[0], bb[1]
                except Exception:
                    p = rt.point3(0, 0, 0)
                    return p, p

            def bbox_union(bbs):
                if not bbs:
                    p = rt.point3(0, 0, 0)
                    return p, p
                minx = min(b[0].x for b in bbs); miny = min(b[0].y for b in bbs); minz = min(b[0].z for b in bbs)
                maxx = max(b[1].x for b in bbs); maxy = max(b[1].y for b in bbs); maxz = max(b[1].z for b in bbs)
                return rt.point3(minx, miny, minz), rt.point3(maxx, maxy, maxz)

            def dims(a, b):
                return float(b.x - a.x), float(b.y - a.y), float(b.z - a.z)

            # Collect BOM items: ONLY group heads marked BOMRoot=1
            roots = []
            for n in list(rt.rootNode.children):
                try:
                    if rt.isGroupHead(n) and get_prop(n, "BOMRoot") == "1":
                        roots.append(n)
                except Exception:
                    continue

            if not roots:
                QtWidgets.QMessageBox.information(self, "BOM Export", "No BOM items found.\n\nTip: metadata must be on GROUP HEAD with BOMRoot=1.")
                return

            bom = defaultdict(lambda: {"PartName": "", "PartNo": "", "Qty": 0, "W": 0.0, "D": 0.0, "H": 0.0})

            for gh in roots:
                part_name = get_prop(gh, "PartName") or gh.name
                part_no = get_prop(gh, "PartNo")
                key = part_no if part_no else part_name

                bbs = []
                for d in iter_desc(gh):
                    if is_geom(d):
                        bbs.append(world_bbox(d))

                mn, mx = bbox_union(bbs)
                w, d, h = dims(mn, mx)

                row = bom[key]
                if not row["PartName"]:
                    row["PartName"] = part_name
                if not row["PartNo"]:
                    row["PartNo"] = part_no
                row["Qty"] += 1
                row["W"] = max(row["W"], w)
                row["D"] = max(row["D"], d)
                row["H"] = max(row["H"], h)

            # Output CSV to Desktop
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            scene = rt.maxFileName
            scene = os.path.splitext(scene)[0] if scene else "Untitled"
            desktop = os.path.join(os.path.expanduser("~"), "Desktop")
            out_path = os.path.join(desktop, f"{scene}_BOM_{ts}.csv")

            headers = ["PartName", "PartNo", "Qty", "Width", "Depth", "Height"]
            rows = []
            for _, v in sorted(bom.items(), key=lambda kv: (kv[1]["PartNo"] or kv[1]["PartName"]).lower()):
                rows.append([
                    v["PartName"],
                    v["PartNo"],
                    v["Qty"],
                    round(v["W"], 3),
                    round(v["D"], 3),
                    round(v["H"], 3),
                ])

            with open(out_path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(headers)
                w.writerows(rows)

            QtWidgets.QMessageBox.information(self, "BOM Export", f"Exported {len(rows)} line(s).\n\nSaved to:\n{out_path}")

        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "BOM Export Error", f"Failed to export BOM:\n\n{e}")

    # ------------------------------ Sound ------------------------------------
    def _setup_sound(self):
        self._sound_ready = False
        self._use_qsound = False
        self._use_winsound = False
        self._sfx_path = IMPORT_SFX if os.path.exists(IMPORT_SFX) else None
        if QtMultimedia and hasattr(QtMultimedia, "QSound") and self._sfx_path:
            try:
                self.import_sound = QtMultimedia.QSound(self._sfx_path)
                self._use_qsound = True
                self._sound_ready = True
                return
            except Exception:
                pass
        try:
            import winsound
            self._winsound = winsound
            if self._sfx_path:
                self._use_winsound = True
                self._sound_ready = True
        except Exception:
            self._winsound = None

    def _play_sound(self):
        # Don’t play if muted
        if hasattr(self, "mute_chk") and self.mute_chk.isChecked():
            return

        if not self._sound_ready:
            return
        try:
            if self._use_qsound:
                self.import_sound.play()
            elif self._use_winsound:
                self._winsound.PlaySound(self._sfx_path, 0x00020000)
        except Exception:
            pass


    # -------------------------- Safe label creation --------------------------
    def _make_thumb_label(self, asset_path):
        # Create QLabel WITHOUT a parent first, then setParent to avoid overload ambiguity
        try:
            pm = self._placeholder_thumb()
            lbl = ThumbnailLabel(pm, asset_path, THUMB_SIZE, None)
            lbl.setParent(self)  # parent after construction
            lbl.mousePressEvent = (lambda ev, p=asset_path: self._on_thumb_click(ev, p))
            return lbl
        except Exception:
            return None

    def _placeholder_thumb(self):
        pm = QtGui.QPixmap(THUMB_SIZE, THUMB_SIZE)
        pm.fill(QtGui.QColor("gray"))
        rounded = QtGui.QPixmap(THUMB_SIZE, THUMB_SIZE)
        rounded.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(rounded)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        path = QtGui.QPainterPath()
        path.addRoundedRect(0, 0, THUMB_SIZE, THUMB_SIZE, 10, 10)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, pm)
        pen = QtGui.QPen(QtGui.QColor("white"), 1)
        painter.setPen(pen)
        painter.drawRoundedRect(0, 0, THUMB_SIZE, THUMB_SIZE, 10, 10)
        painter.end()
        return rounded

# ------------------------------ Thumbnail label ------------------------------
class ThumbnailLabel(QtWidgets.QLabel):
    def __init__(self, pixmap, asset_path, thumbnail_size, parent=None):
        # Important: init WITHOUT a parent to avoid PySide2 overload confusion
        QtWidgets.QLabel.__init__(self)
        if parent is not None:
            self.setParent(parent)

        self.setPixmap(pixmap)
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setFixedSize(thumbnail_size, thumbnail_size)
        self.asset_path = asset_path
        self.setToolTip(os.path.basename(asset_path))

    def enterEvent(self, event):
        self.setStyleSheet("border: 2px solid white;")
        # Walk up to the AssetBrowser instance
        parent = self.parent()
        while parent and not isinstance(parent, AssetBrowser):
            parent = parent.parent()
        if parent:
            parent._display_notes(self.asset_path)

    def leaveEvent(self, event):
        self.setStyleSheet("border: none;")

# --------------------------- App lifecycle helpers ---------------------------
def close_existing_asset_browsers():
    app = QtWidgets.QApplication.instance()
    if app:
        for w in app.topLevelWidgets():
            try:
                if isinstance(w, AssetBrowser):
                    w.close()
                    w.deleteLater()
            except Exception:
                continue

def main():
    try:
        close_existing_asset_browsers()
        app = QtWidgets.QApplication.instance()
        if app is None:
            app = QtWidgets.QApplication(sys.argv)

        parent = MAIN_WINDOW if USING_PYSIDE2 else None
        w = AssetBrowser(
            library_root=LIBRARY_ROOT,
            cort_root=CORT_ROOT,
            thumbnail_folder=THUMB_ROOT,
            notes_folder=NOTES_ROOT,
            parent=parent
        )
        w.show()
        w.raise_()
        w.activateWindow()

        log("Asset Browser initialized (binding: {}).".format(QT_LIB))
        # Do not exec_() in Max; event loop is already running
    except Exception as e:
        log("Launcher error: {}".format(e))
        try:
            QtWidgets.QMessageBox.critical(MAIN_WINDOW if USING_PYSIDE2 else None,
                                           "Asset Browser",
                                           "Error:\n{}".format(e))
        except Exception:
            pass

if __name__ == "__main__":
    main()
