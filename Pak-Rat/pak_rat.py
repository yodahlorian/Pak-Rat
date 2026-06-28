"""
Pak Rat — automatic asset packager + cooker for Retro Rewind (UE 5.4).

Installer-style QWizard. Real injection/packaging lives in core.py; the UE
cooking toolchain lives in cook.py. This file is pure UI + flow.

Four modes (chosen on ModePage):
  regular  ModePage → AssetPage → TextureListPage → ProcessPage → FinishPage
  mesh     ModePage → AssetPage → ExtractPage → RequiredFilesPage → ProcessPage → FinishPage
  cook     ModePage → SetupPage → AssetPage → CookListPage → ProcessPage → FinishPage
  extract  ModePage → AssetPage → ExtractListPage  (hands back originals; no packaging)
  combine  ModePage → CombineSourcePage → CombineSelectPage → ProcessPage → FinishPage
           (cherry-pick assets from existing paks, conflict-aware, into one pak)
  regular & extract pack MANY items into one pak. SetupPage is always step 1 of the
  cook path (short-circuits instantly if already set up). Cook mode is shown only when
  an Unreal install is found — otherwise ModePage shows an "install UE 5.4.4" note.

Run (Windows):  python.exe pak_rat.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QIcon, QPainter, QPalette,
                           QPen, QPixmap)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QCompleter, QDialog,
    QDialogButtonBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMenu, QMessageBox, QProgressBar, QPushButton, QRadioButton, QScrollArea,
    QSplashScreen, QToolTip, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
    QWizard, QWizardPage,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402
import cook  # noqa: E402  (v2 UE cooking toolchain)

# Page ids
PAGE_MODE, PAGE_ASSET, PAGE_EXTRACT, PAGE_TEXLIST, PAGE_REQUIRED, PAGE_PROCESS, \
    PAGE_FINISH, PAGE_SETUP, PAGE_COOKINPUT, PAGE_COOKTEX, \
    PAGE_EXTRACTLIST, PAGE_EXTRACTPROG, PAGE_EXTRACTDONE, \
    PAGE_COMBINESRC, PAGE_COMBINESEL = range(15)

APP_VERSION = "2.0.8"

# ---------------------------------------------------------------------------
# Synthwave theme — palette sampled straight from the app icon (neon rat badge):
# near-black violet ground, neon cyan + magenta accents.
# ---------------------------------------------------------------------------
BG      = "#0B0518"   # window ground (icon background)
PANEL   = "#160C2A"   # raised panels / input fields
TEXT    = "#ECE9F7"   # primary text
MUTED   = "#8C86A8"   # disabled / hints
CYAN    = "#10EBF7"   # primary neon accent
MAGENTA = "#F21CE0"   # secondary neon accent
BORDER  = "#2A2350"   # subtle outlines
GREEN   = "#27E0A0"   # success ✓ (neon, tuned to fit the palette)

_QSS = f"""
QPushButton {{
    background: {PANEL}; color: {CYAN};
    border: 1px solid {CYAN}; border-radius: 6px; padding: 6px 14px;
}}
QPushButton:hover    {{ background: {CYAN}; color: {BG}; }}
QPushButton:pressed  {{ background: {MAGENTA}; border-color: {MAGENTA}; color: {BG}; }}
QPushButton:disabled {{ color: {MUTED}; border-color: {BORDER}; background: {PANEL}; }}
QComboBox, QLineEdit, QAbstractItemView {{
    background: {PANEL}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 6px;
    selection-background-color: {CYAN}; selection-color: {BG};
}}
QComboBox:hover, QLineEdit:focus {{ border-color: {CYAN}; }}
QProgressBar {{
    border: 1px solid {BORDER}; border-radius: 6px;
    background: {PANEL}; text-align: center; color: {TEXT};
}}
QProgressBar::chunk {{
    border-radius: 5px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                               stop:0 {CYAN}, stop:1 {MAGENTA});
}}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: {BG}; width: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 6px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {CYAN}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QPushButton#rm {{
    padding: 0; min-width: 26px; max-width: 26px; min-height: 24px;
    color: {MAGENTA}; border: 1px solid {MAGENTA};
    font-weight: bold; font-size: 14px;
}}
QPushButton#rm:hover {{ background: {MAGENTA}; color: {BG}; }}
"""


def apply_theme(app):
    """Dark synthwave look drawn from the icon's palette (Fusion + accents)."""
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(BG))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Base, QColor(PANEL))
    pal.setColor(QPalette.AlternateBase, QColor(BG))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor(PANEL))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.ToolTipBase, QColor(PANEL))
    pal.setColor(QPalette.ToolTipText, QColor(TEXT))
    pal.setColor(QPalette.Highlight, QColor(CYAN))
    pal.setColor(QPalette.HighlightedText, QColor(BG))
    pal.setColor(QPalette.Link, QColor(CYAN))
    pal.setColor(QPalette.PlaceholderText, QColor(MUTED))
    for grp in (QPalette.Disabled,):
        pal.setColor(grp, QPalette.Text, QColor(MUTED))
        pal.setColor(grp, QPalette.ButtonText, QColor(MUTED))
        pal.setColor(grp, QPalette.WindowText, QColor(MUTED))
    app.setPalette(pal)
    app.setStyleSheet(_QSS)


def resource_path(name: str) -> str:
    """Path to a bundled resource — frozen (onedir _internal) or source dir."""
    base = getattr(sys, "_MEIPASS", None) or str(Path(__file__).resolve().parent)
    return str(Path(base) / name)


def _basename(asset: str) -> str:
    """Leaf name of an asset path, e.g. .../textures/MI_Detail_01 -> MI_Detail_01."""
    return asset.rstrip("/").split("/")[-1]


# ---------------------------------------------------------------------------
# Category grouping for asset pickers — split by type (Meshes / Textures), then
# by family (the first token of the name, e.g. BackAlley, Candy).
# ---------------------------------------------------------------------------
def _is_tex(mount: str) -> bool:
    return _basename(mount).startswith("T_")


def _is_mesh_asset(mount: str) -> bool:
    return _basename(mount).startswith(("LA_", "SM_", "SK_", "SKM_"))


def _family(mount: str) -> str:
    leaf = _basename(mount)
    for pre in ("SKM_", "SK_", "SM_", "LA_", "T_"):
        if leaf.startswith(pre):
            leaf = leaf[len(pre):]
            break
    out = []
    for ch in leaf:
        if ch.isalnum():
            out.append(ch)
        else:
            break
    return "".join(out) or "Other"


def _grouped_order(mounts):
    """Ordered [(level, text, mount)]: level 0 = type header, 1 = family header,
    2 = selectable item (mount set). Type headers only appear when both kinds
    are present."""
    tex = sorted(m for m in mounts if _is_tex(m))
    mesh = sorted(m for m in mounts if _is_mesh_asset(m))
    rest = sorted(set(mounts) - set(tex) - set(mesh))
    both = bool(tex) and bool(mesh)
    out = []

    def emit(items):
        fams = {}
        for m in items:
            fams.setdefault(_family(m), []).append(m)
        for fam in sorted(fams):
            out.append((1, fam, None))
            for m in fams[fam]:
                out.append((2, _basename(m), m))

    if mesh:
        if both:
            out.append((0, "Meshes", None))
        emit(mesh)
    if tex:
        if both:
            out.append((0, "Textures", None))
        emit(tex)
    if rest:
        out.append((0, "Other", None))
        emit(rest)
    return out


def populate_grouped_combo(combo, mounts):
    """Fill an editable combo with grouped, disabled headers + selectable items
    (leaf shown, full mount in data). Returns (labels, mount_by_label)."""
    combo.clear()
    labels, mbl = [], {}
    model = combo.model()
    for level, text, mount in _grouped_order(mounts):
        if mount is None:
            disp = f"── {text} ──" if level == 0 else f"  {text}"
            combo.addItem(disp)
            it = model.item(combo.count() - 1)
            it.setEnabled(False)
            if level == 0:
                f = it.font()
                f.setBold(True)
                it.setFont(f)
        else:
            label = text                       # leaf name
            if label in mbl:                   # disambiguate duplicate leaves
                parent = mount.rstrip("/").split("/")[-2] if "/" in mount else ""
                label = f"{text}  ·{parent}"
                n = 2
                while label in mbl:
                    label = f"{text}  ·{parent}{n}"
                    n += 1
            combo.addItem("      " + label, mount)
            mbl[label] = mount
            labels.append(label)
    return labels, mbl


class GroupedPickerDialog(QDialog):
    """Searchable, category-grouped asset picker (Meshes/Textures → family)."""
    def __init__(self, mounts, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(460, 460)
        self.selected = None
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.textChanged.connect(self._filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemDoubleClicked.connect(self._dbl)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.search)
        lay.addWidget(self.tree)
        lay.addWidget(bb)
        cur_type = cur_fam = None
        for level, text, mount in _grouped_order(mounts):
            if level == 0:
                cur_type = QTreeWidgetItem(self.tree, [text])
                cur_type.setExpanded(True)
                cur_fam = None
            elif level == 1:
                cur_fam = QTreeWidgetItem(cur_type or self.tree, [text])
            else:
                leaf = QTreeWidgetItem(cur_fam or cur_type or self.tree, [text])
                leaf.setData(0, Qt.UserRole, mount)

    def _filter(self, q):
        q = q.strip().lower()

        def visit(item):
            if item.data(0, Qt.UserRole) is not None:    # leaf
                vis = q in item.text(0).lower()
                item.setHidden(not vis)
                return vis
            any_vis = False
            for i in range(item.childCount()):
                if visit(item.child(i)):
                    any_vis = True
            item.setHidden(not any_vis)
            if any_vis and q:
                item.setExpanded(True)
            return any_vis
        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def _dbl(self, item, _col):
        if item.data(0, Qt.UserRole) is not None:
            self.selected = item.data(0, Qt.UserRole)
            self.accept()

    def _accept(self):
        it = self.tree.currentItem()
        if it and it.data(0, Qt.UserRole) is not None:
            self.selected = it.data(0, Qt.UserRole)
            self.accept()

    @staticmethod
    def pick(parent, mounts, title):
        d = GroupedPickerDialog(mounts, title, parent)
        return d.selected if d.exec() == QDialog.Accepted else None


HOVER_PX = 512   # size of the large on-hover preview (screen-safe; <1080p tall)


def _big_img_html(png: str) -> str:
    """HTML <img> for a large hover preview, or '' if the file is missing."""
    if png and os.path.exists(png):
        return f'<img src="{png.replace(chr(92), "/")}" width="{HOVER_PX}">'
    return ""


class Thumb(QLabel):
    """Fixed-size thumbnail. With spin=True it shows a neon spinning wheel until
    its image arrives (decoding is slow), then the scaled image + a big hover
    preview tooltip."""
    def __init__(self, size: int = 56, spin: bool = False, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(f"border:1px solid {BORDER}; color:{MUTED};")
        self._angle = 0
        self._loading = spin
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        if spin:
            self._timer.start(70)

    def _tick(self):
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._loading:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            r = min(self.width(), self.height()) // 4
            cx, cy = self.width() // 2, self.height() // 2
            pen = QPen(QColor(CYAN), 3)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(cx - r, cy - r, 2 * r, 2 * r, self._angle * 16, 110 * 16)
            p.end()

    def set_image(self, png: str):
        self._loading = False
        self._timer.stop()
        if png and os.path.exists(png):
            pm = QPixmap(png)
            if not pm.isNull():
                self.setPixmap(pm.scaled(self.size(), Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))
                self.setToolTip(_big_img_html(png))
                return
        self.setText("—")


def _preview_cell(thumb: "Thumb") -> QWidget:
    """A thumbnail with a tiny 'hover to enlarge' caption beneath it."""
    w = QWidget()
    v = QVBoxLayout(w)
    v.setContentsMargins(0, 0, 0, 0)
    v.setSpacing(1)
    cap = QLabel("hover to enlarge")
    cap.setAlignment(Qt.AlignCenter)
    cap.setStyleSheet(f"color:{MUTED}; font-size:8px;")
    v.addWidget(thumb, alignment=Qt.AlignCenter)
    v.addWidget(cap)
    return w


class PreviewWorker(QThread):
    """Decodes texture previews off the GUI thread; emits one signal per asset.

    Parented to its page so Qt keeps it alive (no manual ref-tracking).
    """
    ready = Signal(str, str)   # (mount, png path; "" = no preview)

    def __init__(self, mounts, parent=None):
        super().__init__(parent)
        self._mounts = list(mounts)

    def run(self):
        for m in self._mounts:
            try:
                p = core.decode_preview(m) or ""
            except Exception:
                p = ""
            self.ready.emit(m, p)


# ---------------------------------------------------------------------------
# Page 1 — mode select
# ---------------------------------------------------------------------------
class ModePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("What are you packaging?")
        self.setSubTitle("Choose the type of swap you want to build.")

        self.rb_regular = QRadioButton("Regular Texture")
        self.rb_mesh = QRadioButton("Mesh + Texture")
        self.rb_cook = QRadioButton("Cook Mesh from a 3D file  (FBX / OBJ / glTF / …)")
        self.rb_extract = QRadioButton("Extract Asset")
        self.rb_combine = QRadioButton("Combine Mods")
        self.rb_regular.setChecked(True)

        self.group = QButtonGroup(self)
        self.group.addButton(self.rb_regular, 0)
        self.group.addButton(self.rb_mesh, 1)
        self.group.addButton(self.rb_extract, 2)
        self.group.addButton(self.rb_cook, 3)
        self.group.addButton(self.rb_combine, 4)

        lay = QVBoxLayout(self)
        lay.addWidget(self.rb_regular)
        lab1 = QLabel("    Swap a single texture (PNG/DDS) on an existing asset.")
        lab1.setStyleSheet("color:#888;")
        lay.addWidget(lab1)
        lay.addSpacing(12)
        lay.addWidget(self.rb_mesh)
        lab2 = QLabel("    Swap an already-cooked mesh and its texture together.")
        lab2.setStyleSheet("color:#888;")
        lay.addWidget(lab2)
        lay.addSpacing(12)
        # Cooker — only meaningful when an Unreal Engine install is present.
        lay.addWidget(self.rb_cook)
        self.cook_lab = QLabel("    Bring your own model (any common 3D format) — "
                               "Pak Rat cooks it with Unreal for you.")
        self.cook_lab.setStyleSheet("color:#888;")
        lay.addWidget(self.cook_lab)
        lay.addSpacing(12)
        lay.addWidget(self.rb_extract)
        lab3 = QLabel("    Pull an original mesh or texture out of the game to edit "
                      "(textures as PNG/DDS, meshes as .uasset). Siblings auto-included.")
        lab3.setStyleSheet("color:#888;")
        lab3.setWordWrap(True)
        lay.addWidget(lab3)
        lay.addSpacing(12)
        lay.addWidget(self.rb_combine)
        lab4 = QLabel("    Cherry-pick assets from mods you already have and merge "
                      "them into one pak.")
        lab4.setStyleSheet("color:#888;")
        lab4.setWordWrap(True)
        lay.addWidget(lab4)
        lay.addStretch(1)

        # Shown only when no Unreal Engine is installed (cooker hidden then).
        self.cook_note = QLabel("NOTE: Install Unreal Engine 5.4.4 from the Epic "
                                "Games Launcher to unlock Mesh Cooking.")
        self.cook_note.setWordWrap(True)
        self.cook_note.setStyleSheet("color:#c08a2e; font-style:italic;")
        self.cook_note.setVisible(False)
        lay.addWidget(self.cook_note)

    def initializePage(self):
        self.wizard().mode = "regular"
        # The cooker needs an installed Unreal Engine; hide it (and show a hint)
        # otherwise.
        avail = getattr(self.wizard(), "cook_available", None)
        if avail is None:
            avail = cook.ue_available()
            self.wizard().cook_available = avail
        self.rb_cook.setVisible(avail)
        self.cook_lab.setVisible(avail)
        self.cook_note.setVisible(not avail)
        if not avail and self.rb_cook.isChecked():
            self.rb_regular.setChecked(True)
        self.group.idToggled.connect(self._on_toggle)

    def _on_toggle(self, _id, checked):
        if self.rb_mesh.isChecked():
            self.wizard().mode = "mesh"
        elif self.rb_cook.isChecked():
            self.wizard().mode = "cook"
        elif self.rb_extract.isChecked():
            self.wizard().mode = "extract"
        elif self.rb_combine.isChecked():
            self.wizard().mode = "combine"
        else:
            self.wizard().mode = "regular"

    def nextId(self):
        mode = getattr(self.wizard(), "mode", "regular")
        # Cooker path ALWAYS starts at the setup page (step 1). It short-circuits
        # instantly when the toolchain is already installed.
        if mode == "cook":
            return PAGE_SETUP
        # Combine has no single-asset picker — straight to choosing source paks.
        if mode == "combine":
            return PAGE_COMBINESRC
        return PAGE_ASSET


# ---------------------------------------------------------------------------
# Page 2 — texture (or texture+mesh) picker with autocomplete
# ---------------------------------------------------------------------------
class AssetPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Select the texture")
        self._resolved_for = None
        self._mesh_set = set()
        self._mount_by_label = {}   # leaf shown in combo -> full mount path

        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.NoInsert)
        self.combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setMinimumContentsLength(24)
        self.combo.setMaximumWidth(560)
        self.combo.currentTextChanged.connect(self.completeChanged)
        self.combo.currentTextChanged.connect(self._on_primary_changed)
        self.combo.currentTextChanged.connect(self._sync_asset)

        # Hidden field carrying the FULL mount path — the combo may show only the
        # leaf name (so long paths don't run off-screen), but downstream needs
        # the full path. registerField points here, not at the visible combo.
        self._asset = QLineEdit()
        self._asset.setVisible(False)

        # Second dropdown — mesh mode only: the auto-resolved overlay texture.
        self.tex_lbl = QLabel("Overlay texture:")
        self.combo2 = QComboBox()
        self.combo2.setMaximumWidth(560)
        self.combo2.currentTextChanged.connect(self.completeChanged)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Start typing to filter, or pick from the list:"))
        lay.addWidget(self.combo)
        lay.addWidget(self._asset)
        lay.addSpacing(8)
        lay.addWidget(self.tex_lbl)
        lay.addWidget(self.combo2)
        lay.addStretch(1)

        self.registerField("asset", self._asset)
        self.registerField("overlay_tex", self.combo2, "currentText",
                            self.combo2.currentTextChanged)

    def _is_mesh(self):
        return getattr(self.wizard(), "mode", "regular") == "mesh"

    def _sync_asset(self, text):
        t = text.strip()
        self._asset.setText(self._mount_by_label.get(t, t))

    def initializePage(self):
        mode = getattr(self.wizard(), "mode", "regular")
        mesh_like = mode in ("mesh", "cook")
        if mode == "extract":           # extract works on meshes AND textures
            items = sorted(set(core.load_meshes()) | set(core.load_assets()))
        else:
            items = core.load_meshes() if mesh_like else core.load_assets()
        self._resolved_for = None
        self.combo.blockSignals(True)
        labels, self._mount_by_label = populate_grouped_combo(self.combo, items)
        self.combo.setCurrentIndex(-1)
        self.combo.setEditText("")
        self.combo.blockSignals(False)
        self._asset.setText("")
        completer = QCompleter(labels, self.combo)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self.combo.setCompleter(completer)

        show_overlay = (mode == "mesh")  # cook mode reuses the game's own material
        self.combo2.clear()
        self.tex_lbl.setVisible(show_overlay)
        self.combo2.setVisible(show_overlay)
        if mode == "cook":
            self.setTitle("Select the mesh to replace")
            self.setSubTitle("Pick the game mesh your model will stand in for.")
        elif mode == "mesh":
            self.setTitle("Select the mesh")
            self.setSubTitle("Pick the mesh, then its overlay texture.")
        elif mode == "extract":
            self.setTitle("Select the asset to extract")
            self.setSubTitle("Pick a mesh or texture to pull out of the game.")
        else:
            self.setTitle("Select the texture")
            self.setSubTitle("Pick the texture you want to replace.")
        self.completeChanged.emit()

    def _on_primary_changed(self, text):
        if not self._is_mesh():
            return
        mount = self._mount_by_label.get(text.strip())
        if not mount or mount == self._resolved_for:
            return
        self._resolved_for = mount
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            texs = core.resolve_overlay_textures(mount)
        except Exception:
            texs = []
        finally:
            QApplication.restoreOverrideCursor()
        self.combo2.clear()
        self.combo2.addItems(texs)
        self.combo2.setCurrentIndex(0 if len(texs) == 1 else -1)
        self.completeChanged.emit()

    def isComplete(self):
        if not self.combo.currentText().strip():
            return False
        if self._is_mesh():
            return bool(self.combo2.currentText().strip())
        return True

    def nextId(self):
        mode = getattr(self.wizard(), "mode", "regular")
        if mode == "cook":
            return PAGE_COOKINPUT
        if mode == "extract":
            return PAGE_EXTRACTLIST
        if mode == "mesh":
            return PAGE_EXTRACT
        return PAGE_TEXLIST


# ---------------------------------------------------------------------------
# Extract page — prepare_target (live-extract + spec + preview) on a QThread
# ---------------------------------------------------------------------------
class ExtractWorker(QThread):
    done = Signal(object)    # core.TargetSpec or core.MeshPlan
    failed = Signal(str)

    def __init__(self, asset, mesh_mode):
        super().__init__()
        self.asset = asset
        self.mesh_mode = mesh_mode

    def run(self):
        try:
            res = (core.prepare_mesh(self.asset) if self.mesh_mode
                   else core.prepare_target(self.asset))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
        else:
            self.done.emit(res)


class ExtractPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Processing file…")
        self.setSubTitle("Extracting the original texture and reading its format.")
        self._done = False
        self.worker = None

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # indeterminate spinner
        self.status = QLabel("Extracting…")

        lay = QVBoxLayout(self)
        lay.addStretch(1)
        lay.addWidget(self.status, alignment=Qt.AlignCenter)
        lay.addWidget(self.bar)
        lay.addStretch(1)

    def initializePage(self):
        self._done = False
        self.completeChanged.emit()
        wiz = self.wizard()
        core.cleanup_target(getattr(wiz, "target_spec", None))
        core.cleanup_mesh(getattr(wiz, "mesh_plan", None))
        wiz.target_spec = None
        wiz.mesh_plan = None
        wiz.button(QWizard.BackButton).setEnabled(False)  # lock while working
        self._mesh = (getattr(wiz, "mode", "regular") == "mesh")
        self.setSubTitle("Walking the mesh's dependency tree…" if self._mesh
                         else "Extracting the original texture and reading its format.")
        self.worker = ExtractWorker(self.field("asset"), self._mesh)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_error)
        self.worker.start()

    def _on_done(self, res):
        wiz = self.wizard()
        if self._mesh:
            wiz.mesh_plan = res
        else:
            wiz.target_spec = res
        wiz.button(QWizard.BackButton).setEnabled(True)
        self._done = True
        self.completeChanged.emit()
        wiz.next()  # auto-advance (Image for texture, Required-files for mesh)

    def _on_error(self, msg):
        wiz = self.wizard()
        wiz.button(QWizard.BackButton).setEnabled(True)
        QMessageBox.critical(
            self, "Couldn't prepare that texture",
            f"Failed to extract / read the texture.\n\n{msg}")
        wiz.back()  # send the user back to AssetPage to choose another

    def isComplete(self):
        return self._done

    def nextId(self):
        return PAGE_REQUIRED  # ExtractPage is reached only by mesh mode now


# ---------------------------------------------------------------------------
# Texture list page (regular mode) — swap one or many textures into ONE pak.
# Mirrors the cook/extract list pages: the picked texture is seeded, you can add
# more, and you choose a replacement PNG/DDS per row. Each image is auto-resized
# to its target's exact size at pack time.
# ---------------------------------------------------------------------------
class TextureListPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Choose your replacement textures")
        self.setSubTitle("One pak can hold several texture swaps — "
                         "pick a PNG or DDS for each.")
        self._rows = []
        self._stretch_added = False

        self._container = QWidget()
        self._vbox = QVBoxLayout(self._container)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._container)

        self.add_btn = QPushButton("➕  Add another texture…")
        self.add_btn.clicked.connect(self._add_another)
        self.hint = QLabel("Each image is resized to its target's exact size "
                           "automatically. Fill at least one.")
        self.hint.setStyleSheet("color:#888;")
        self.hint.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.addWidget(scroll)
        lay.addWidget(self.add_btn)
        lay.addWidget(self.hint)

    def initializePage(self):
        while self._vbox.count():
            it = self._vbox.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._rows = []
        self._stretch_added = False
        wiz = self.wizard()
        wiz.tex_items = {}
        primary = (self.field("asset") or "").strip()
        if primary:
            self._add_row(primary, removable=False)
        self._vbox.addStretch(1)
        self._stretch_added = True
        self.completeChanged.emit()
        self._start_previews()

    def _add_row(self, mount, removable):
        if any(r["mount"] == mount for r in self._rows):
            return
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        thumb = Thumb(spin=True)                  # original (decoded async)
        name = QLabel(_basename(mount))
        name.setMinimumWidth(150)
        name.setWordWrap(True)
        status = QLabel("no image yet")
        status.setStyleSheet(f"color:{MUTED};")
        info = QWidget()
        iv = QVBoxLayout(info)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.addWidget(name)
        iv.addWidget(status)
        btn = QPushButton("Choose…")
        your = Thumb(spin=False)                  # the replacement they pick
        rec = {"mount": mount, "status": status, "thumb": thumb, "your": your}
        btn.clicked.connect(lambda _=False, rec=rec: self._pick(rec))
        h.addWidget(_preview_cell(thumb))
        h.addWidget(info, 1)
        h.addWidget(btn)
        h.addWidget(_preview_cell(your))
        if removable:
            rm = QPushButton("✕")
            rm.setObjectName("rm")
            rm.clicked.connect(lambda _=False, row=row, rec=rec: self._remove(row, rec))
            h.addWidget(rm)
        if self._stretch_added:
            self._vbox.insertWidget(self._vbox.count() - 1, row)
        else:
            self._vbox.addWidget(row)
        self._rows.append(rec)
        if self._stretch_added:                   # live add → preview just this one
            w = PreviewWorker([mount], self)
            w.ready.connect(self._on_preview)
            w.start()

    def _start_previews(self):
        w = PreviewWorker([r["mount"] for r in self._rows], self)
        w.ready.connect(self._on_preview)
        w.start()

    def _on_preview(self, mount, png):
        for r in self._rows:
            if r["mount"] == mount:
                r["thumb"].set_image(png)
                return

    def _pick(self, rec):
        path, _ = QFileDialog.getOpenFileName(
            self, f"Replacement for {_basename(rec['mount'])}",
            _basename(rec["mount"]), "Images (*.png *.dds);;All files (*)")
        if not path:
            return
        if not core.validate_image_ext(path):
            QMessageBox.warning(self, "Wrong file type",
                                "Please choose a PNG or DDS file.")
            return
        self.wizard().tex_items[rec["mount"]] = path
        rec["status"].setText("✓ " + Path(path).name)
        rec["status"].setStyleSheet(f"color:{GREEN}; font-weight:600;")
        if QPixmap(path).isNull():
            rec["your"].setText("DDS")
        else:
            rec["your"].set_image(path)
        self.completeChanged.emit()

    def _remove(self, row, rec):
        self.wizard().tex_items.pop(rec["mount"], None)
        if rec in self._rows:
            self._rows.remove(rec)
        row.deleteLater()
        self.completeChanged.emit()

    def _add_another(self):
        items = core.load_assets()
        mount = GroupedPickerDialog.pick(self, items, "Add a texture")
        if mount:
            self._add_row(mount.strip(), removable=True)
            self.completeChanged.emit()

    def isComplete(self):
        return bool(getattr(self.wizard(), "tex_items", {}))

    def nextId(self):
        return PAGE_PROCESS


# ---------------------------------------------------------------------------
# Required-files page (mesh mode) — one cooked-file picker per dependency slot
# ---------------------------------------------------------------------------
class RequiredFilesPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Provide your cooked files")
        self.setSubTitle("One cooked .uasset per required slot — sidecars auto-detected.")
        self._container = QWidget()
        self._vbox = QVBoxLayout(self._container)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._container)
        lay = QVBoxLayout(self)
        lay.addWidget(scroll)

    def initializePage(self):
        while self._vbox.count():  # clear previous rows
            it = self._vbox.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        wiz = self.wizard()
        wiz.mesh_user_files = {}
        plan = getattr(wiz, "mesh_plan", None)
        if plan is not None:
            for rf in plan.required:
                self._add_row(rf)
        self._vbox.addStretch(1)
        self.completeChanged.emit()

    def _add_row(self, rf):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        tag = {"mesh": "MESH", "material": "MAT", "texture": "TEX",
               "shader": "SHADER"}.get(rf.kind, rf.kind.upper())
        name = rf.asset.rsplit("/", 1)[-1]
        if not rf.swappable:
            lbl = QLabel(f"[{tag}] {name} — vanilla, not swappable")
            lbl.setStyleSheet("color:#888;")
            h.addWidget(lbl)
            self._vbox.addWidget(row)
            return
        shared = " · shared" if rf.shared else ""
        lbl = QLabel(f"[{tag}{shared}] {name}")
        lbl.setMinimumWidth(240)
        status = QLabel("—")
        status.setStyleSheet("color:#888;")
        btn = QPushButton("Choose .uasset…")
        btn.clicked.connect(lambda _=False, rf=rf, st=status: self._pick(rf, st))
        h.addWidget(lbl)
        h.addWidget(btn)
        h.addWidget(status, 1)
        self._vbox.addWidget(row)

    def _pick(self, rf, status):
        name = rf.asset.rsplit("/", 1)[-1]
        path, _ = QFileDialog.getOpenFileName(
            self, f"Cooked .uasset for {name}", name + ".uasset",
            "Unreal asset (*.uasset)")
        if not path:
            return
        if not core.validate_mesh_file(path, rf.asset):
            QMessageBox.warning(
                self, "Name mismatch",
                f"The file must be named {name}.uasset to fill this slot.")
            return
        self.wizard().mesh_user_files[rf.asset] = path
        status.setText("✓ " + Path(path).name)
        status.setStyleSheet(f"color:{GREEN}; font-weight:600;")
        self.completeChanged.emit()

    def isComplete(self):
        plan = getattr(self.wizard(), "mesh_plan", None)
        if plan is None:
            return False
        chosen = getattr(self.wizard(), "mesh_user_files", {})
        return all(rf.asset in chosen for rf in plan.swappable())

    def nextId(self):
        return PAGE_PROCESS


# ---------------------------------------------------------------------------
# First-run setup page (cook mode) — download Blender + build the cook project
# with a real progress bar so the user isn't left wondering.
# ---------------------------------------------------------------------------
class SetupWorker(QThread):
    progress = Signal(str, int)   # (message, percent)  percent<0 == indeterminate
    done = Signal(object)         # cook.CookEnv
    failed = Signal(str)

    def run(self):
        try:
            env = cook.setup(progress=lambda m, p=None:
                             self.progress.emit(m, -1 if p is None else int(p)))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
        else:
            self.done.emit(env)


class SetupPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("One-time cooking setup")
        self.setSubTitle("Getting the cooker ready — this happens only once.")
        self._done = False
        self.worker = None

        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.status = QLabel("Starting…")
        self.status.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.addWidget(self.info)
        lay.addStretch(1)
        lay.addWidget(self.status)
        lay.addWidget(self.bar)
        lay.addStretch(1)

    def initializePage(self):
        self._done = False
        self.completeChanged.emit()
        wiz = self.wizard()
        # Already installed? Setup stays step 1 of the cook path, it just
        # completes instantly and moves on.
        if cook.is_ready():
            self.info.setText("Cooker already set up — nothing to download.")
            self.status.setText("Ready.")
            self.bar.setRange(0, 100)
            self.bar.setValue(100)
            self._done = True
            self.completeChanged.emit()
            QTimer.singleShot(0, wiz.next)
            return
        ue = cook.pick_ue()
        ue_txt = (f"Found Unreal Engine {ue['version']}." if ue
                  else "No Unreal Engine found.")
        self.info.setText(
            f"{ue_txt}\n\nPak Rat will download a portable Blender (~370 MB) and "
            "build a small cooking project. Nothing is installed system-wide; it "
            "all lives in your user folder and is reused next time.\n\n"
            "(You may see an Unreal Engine window flash open during cooking — "
            "that's normal.)")
        # Next stays disabled via isComplete() until setup finishes.
        wiz.button(QWizard.BackButton).setEnabled(False)
        self.worker = SetupWorker()
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_error)
        self.worker.start()

    def _on_progress(self, msg, pct):
        self.status.setText(msg)
        if pct < 0:
            self.bar.setRange(0, 0)            # indeterminate
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(pct)

    def _on_done(self, _env):
        wiz = self.wizard()
        wiz.button(QWizard.BackButton).setEnabled(True)
        self.status.setText("Setup complete.")
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self._done = True
        self.completeChanged.emit()          # re-enables Next via isComplete()
        QTimer.singleShot(0, wiz.next)        # straight into mesh selection

    def _on_error(self, msg):
        wiz = self.wizard()
        wiz.button(QWizard.BackButton).setEnabled(True)
        self.status.setText("Setup failed.")
        QMessageBox.critical(self, "Pak Rat — setup", msg)
        wiz.back()  # back to mode select

    def isComplete(self):
        return self._done

    def nextId(self):
        return PAGE_ASSET


# ---------------------------------------------------------------------------
# Cook list page — one pak, many meshes. Seeds the picked mesh, smart-detects
# sibling parts (e.g. fishbowl → bowl/base/water), and lets you add more.
# ---------------------------------------------------------------------------
_MESH_FILTER = ("3D models (*.fbx *.obj *.gltf *.glb *.stl *.ply *.dae *.blend);;"
                "All files (*)")


class CookListPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Choose your 3D model(s)")
        self.setSubTitle("One pak can hold several meshes. "
                         "FBX / OBJ / glTF / GLB / STL / PLY / DAE / .blend.")
        self._rows = []
        self._stretch_added = False

        self._container = QWidget()
        self._vbox = QVBoxLayout(self._container)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._container)

        self.add_btn = QPushButton("➕  Add another game mesh…")
        self.add_btn.clicked.connect(self._add_another)
        self.hint = QLabel("Fill at least one. Detected sibling parts are "
                           "optional — leave them blank to skip.")
        self.hint.setStyleSheet("color:#888;")
        self.hint.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.addWidget(scroll)
        lay.addWidget(self.add_btn)
        lay.addWidget(self.hint)

    def initializePage(self):
        while self._vbox.count():
            it = self._vbox.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._rows = []
        self._stretch_added = False
        wiz = self.wizard()
        wiz.cook_items = {}

        primary = (self.field("asset") or "").strip()
        if primary:
            self._add_row(primary, "target")
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                related = cook.related_meshes(primary)
            except Exception:
                related = []
            finally:
                QApplication.restoreOverrideCursor()
            for m in related:
                self._add_row(m, "related")

        self._vbox.addStretch(1)
        self._stretch_added = True
        self.completeChanged.emit()

    def _add_row(self, mount, kind):
        if any(r["mount"] == mount for r in self._rows):
            return
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        name = mount.rsplit("/", 1)[-1]
        tag = {"target": "MESH", "related": "PART?", "added": "MESH"}.get(kind, "MESH")
        lbl = QLabel(f"[{tag}]  {name}")
        lbl.setMinimumWidth(230)
        if kind == "related":
            lbl.setToolTip("Auto-detected as part of this set — optional.")
        status = QLabel("optional" if kind == "related" else "—")
        status.setStyleSheet("color:#888;")
        btn = QPushButton("Choose model…")
        rec = {"mount": mount, "kind": kind, "status": status}
        btn.clicked.connect(lambda _=False, rec=rec: self._pick(rec))
        h.addWidget(lbl)
        h.addWidget(btn)
        h.addWidget(status, 1)
        if kind != "target":
            rm = QPushButton("✕")
            rm.setObjectName("rm")
            rm.clicked.connect(lambda _=False, row=row, rec=rec: self._remove(row, rec))
            h.addWidget(rm)

        if self._stretch_added:
            self._vbox.insertWidget(self._vbox.count() - 1, row)
        else:
            self._vbox.addWidget(row)
        self._rows.append(rec)

    def _pick(self, rec):
        name = rec["mount"].rsplit("/", 1)[-1]
        path, _ = QFileDialog.getOpenFileName(
            self, f"Model for {name}", "", _MESH_FILTER)
        if not path:
            return
        if not cook.valid_mesh_source(path):
            QMessageBox.warning(
                self, "Unsupported file",
                "Choose an FBX, OBJ, glTF/GLB, STL, PLY, DAE or .blend file.")
            return
        self.wizard().cook_items[rec["mount"]] = path
        rec["status"].setText("✓ " + Path(path).name)
        rec["status"].setStyleSheet(f"color:{GREEN}; font-weight:600;")
        self.completeChanged.emit()

    def _remove(self, row, rec):
        self.wizard().cook_items.pop(rec["mount"], None)
        if rec in self._rows:
            self._rows.remove(rec)
        row.deleteLater()
        self.completeChanged.emit()

    def _add_another(self):
        items = core.load_meshes()
        mount = GroupedPickerDialog.pick(self, items, "Add a mesh")
        if mount:
            self._add_row(mount.strip(), "added")
            self.completeChanged.emit()

    def isComplete(self):
        return bool(getattr(self.wizard(), "cook_items", {}))

    def nextId(self):
        return PAGE_COOKTEX


# ---------------------------------------------------------------------------
# Embedded-texture extraction worker (cook mode) — runs Blender headless to pull
# any textures baked into the user's model file(s). Slow (Blender spin-up), so
# off the UI thread. Emits the combined list of extracted PNG paths.
# ---------------------------------------------------------------------------
class EmbedTexWorker(QThread):
    done = Signal(object)      # list[str] of PNG paths

    def __init__(self, model_paths, parent=None):
        super().__init__(parent)
        self.model_paths = model_paths

    def run(self):
        env = cook.cook_env()
        out, seen = [], set()
        for src in self.model_paths:
            try:
                for png in cook.extract_embedded_textures(src, env):
                    if png not in seen:
                        seen.add(png)
                        out.append(png)
            except Exception:
                pass
        self.done.emit(out)


# ---------------------------------------------------------------------------
# Cook texture page (cook mode) — OPTIONAL textures for the freshly cooked mesh.
# Seeded from the base-colour textures the target mesh(es) use. Skippable.
# Textures baked into the user's model are extracted and offered per-slot via a
# "Use embedded" menu (the user assigns each to a real game texture slot).
# ---------------------------------------------------------------------------
class CookTexturePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Textures for your mesh  (optional)")
        self.setSubTitle("A new mesh usually wants new textures — swap any of "
                         "these, or just hit Next to skip.")
        self._rows = []
        self._embedded = []        # list[str] PNG paths from the model
        self._embed_worker = None

        self._container = QWidget()
        self._vbox = QVBoxLayout(self._container)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._container)

        # tray showing textures found embedded in the user's model
        self.embed_tray = QWidget()
        self._tray_h = QHBoxLayout(self.embed_tray)
        self._tray_h.setContentsMargins(0, 0, 0, 0)
        self.embed_tray.setVisible(False)
        self.embed_hint = QLabel("")
        self.embed_hint.setStyleSheet(f"color:{CYAN}; font-size:11px;")
        self.embed_hint.setWordWrap(True)
        self.embed_hint.setVisible(False)

        self.hint = QLabel("")
        self.hint.setStyleSheet(f"color:{MUTED};")
        self.hint.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.addWidget(scroll)
        lay.addWidget(self.embed_hint)
        lay.addWidget(self.embed_tray)
        lay.addWidget(self.hint)

    def initializePage(self):
        while self._vbox.count():
            it = self._vbox.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._rows = []
        self._embedded = []
        self._clear_tray()
        self.embed_tray.setVisible(False)
        self.embed_hint.setVisible(False)
        wiz = self.wizard()
        wiz.cook_tex_items = {}
        targets = list(getattr(wiz, "cook_items", {}).keys())
        QApplication.setOverrideCursor(Qt.WaitCursor)
        seen, mounts = set(), []
        try:
            for tgt in targets:
                try:
                    for t in core.resolve_overlay_textures(tgt):
                        if t not in seen:
                            seen.add(t)
                            mounts.append(t)
                except Exception:
                    pass
        finally:
            QApplication.restoreOverrideCursor()
        if mounts:
            self.hint.setText("These are the textures your target mesh uses. "
                              "Pick replacements for any you want changed.")
            for m in mounts:
                self._add_row(m)
        else:
            self.hint.setText("No textures auto-detected for this mesh — you can "
                              "skip and texture it later via Regular Texture.")
        self._vbox.addStretch(1)
        self.completeChanged.emit()
        self._start_previews()
        self._start_embed_scan(list(getattr(wiz, "cook_items", {}).values()))

    # --- embedded-texture scan ---------------------------------------------
    def _start_embed_scan(self, model_paths):
        if not model_paths or not cook.is_ready():
            return
        self.embed_hint.setVisible(True)
        self.embed_hint.setText("Scanning your model for embedded textures…")
        self._embed_worker = EmbedTexWorker(model_paths, self)
        self._embed_worker.done.connect(self._on_embedded)
        self._embed_worker.start()

    def _on_embedded(self, paths):
        self._embedded = paths or []
        if not self._embedded:
            self.embed_hint.setText("No textures were embedded in your model.")
            return
        self.embed_hint.setText(
            "Found %d texture(s) embedded in your model. Use “Use embedded” on "
            "any slot to assign one." % len(self._embedded))
        self._build_tray()
        self.embed_tray.setVisible(True)
        # light up the per-slot embedded buttons now that we have candidates
        for rec in self._rows:
            rec["embed_btn"].setVisible(True)

    def _clear_tray(self):
        while self._tray_h.count():
            it = self._tray_h.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

    def _build_tray(self):
        self._clear_tray()
        lbl = QLabel("Embedded:")
        lbl.setStyleSheet(f"color:{MUTED};")
        self._tray_h.addWidget(lbl)
        for png in self._embedded:
            cell = QWidget()
            v = QVBoxLayout(cell)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(1)
            t = Thumb(size=48, spin=False)
            t.set_image(png)
            cap = QLabel(Path(png).stem.split("_", 1)[-1][:14])
            cap.setAlignment(Qt.AlignCenter)
            cap.setStyleSheet(f"color:{MUTED}; font-size:8px;")
            v.addWidget(t, alignment=Qt.AlignCenter)
            v.addWidget(cap)
            self._tray_h.addWidget(cell)
        self._tray_h.addStretch(1)

    def _add_row(self, mount):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        thumb = Thumb(spin=True)
        name = QLabel(_basename(mount))
        name.setMinimumWidth(150)
        name.setWordWrap(True)
        status = QLabel("unchanged")
        status.setStyleSheet(f"color:{MUTED};")
        info = QWidget()
        iv = QVBoxLayout(info)
        iv.setContentsMargins(0, 0, 0, 0)
        iv.addWidget(name)
        iv.addWidget(status)
        embed_btn = QPushButton("Use embedded ▾")
        embed_btn.setVisible(False)        # shown once embedded textures arrive
        btn = QPushButton("Choose…")
        your = Thumb(spin=False)
        rec = {"mount": mount, "status": status, "thumb": thumb, "your": your,
               "embed_btn": embed_btn}
        btn.clicked.connect(lambda _=False, rec=rec: self._pick(rec))
        embed_btn.clicked.connect(lambda _=False, rec=rec: self._pick_embedded(rec))
        h.addWidget(_preview_cell(thumb))
        h.addWidget(info, 1)
        h.addWidget(embed_btn)
        h.addWidget(btn)
        h.addWidget(_preview_cell(your))
        self._vbox.addWidget(row)
        self._rows.append(rec)

    def _start_previews(self):
        w = PreviewWorker([r["mount"] for r in self._rows], self)
        w.ready.connect(self._on_preview)
        w.start()

    def _on_preview(self, mount, png):
        for r in self._rows:
            if r["mount"] == mount:
                r["thumb"].set_image(png)
                return

    def _pick_embedded(self, rec):
        """Menu of textures embedded in the model; selecting one assigns it to
        this slot (same path as a browsed file → rides tex_items)."""
        if not self._embedded:
            return
        menu = QMenu(self)
        for png in self._embedded:
            label = Path(png).stem.split("_", 1)[-1] or Path(png).name
            act = menu.addAction(label)
            act.triggered.connect(lambda _=False, p=png, rec=rec:
                                  self._assign(rec, p))
        menu.exec(QCursor.pos())

    def _assign(self, rec, path):
        self.wizard().cook_tex_items[rec["mount"]] = path
        rec["status"].setText("✓ " + Path(path).name)
        rec["status"].setStyleSheet(f"color:{GREEN}; font-weight:600;")
        if QPixmap(path).isNull():
            rec["your"].setText("DDS")
        else:
            rec["your"].set_image(path)

    def _pick(self, rec):
        path, _ = QFileDialog.getOpenFileName(
            self, f"Texture for {_basename(rec['mount'])}",
            _basename(rec["mount"]), "Images (*.png *.dds);;All files (*)")
        if not path:
            return
        if not core.validate_image_ext(path):
            QMessageBox.warning(self, "Wrong file type",
                                "Please choose a PNG or DDS file.")
            return
        self._assign(rec, path)

    def isComplete(self):
        return True            # textures are optional — Next always available

    def nextId(self):
        return PAGE_PROCESS


# ---------------------------------------------------------------------------
# Process page — spinner while the pipeline runs
# ---------------------------------------------------------------------------
class PipelineWorker(QThread):
    status = Signal(str)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, mode, mesh_plan, mesh_user_files,
                 cook_items=None, tex_items=None, combine_selected=None,
                 cook_tex_items=None):
        super().__init__()
        self.mode = mode
        self.mesh_plan = mesh_plan
        self.mesh_user_files = mesh_user_files
        self.cook_items = cook_items or {}
        self.tex_items = tex_items or {}
        self.combine_selected = combine_selected or []
        self.cook_tex_items = cook_tex_items or {}

    def run(self):
        try:
            if self.mode == "cook":
                items = [{"src": src, "target": tgt}
                         for tgt, src in self.cook_items.items()]
                pak = cook.run_cook_pipeline_multi(
                    items, tex_items=self.cook_tex_items,
                    progress=lambda m, p=None: self.status.emit(m))
            elif self.mode == "mesh":
                pak = core.run_mesh_pipeline(self.mesh_plan, self.mesh_user_files,
                                             progress=self.status.emit)
            elif self.mode == "combine":
                pak = core.combine_paks(self.combine_selected,
                                        progress=self.status.emit)
            else:  # regular texture mode — one or many textures into one pak
                items = [{"texture": tex, "image": img}
                         for tex, img in self.tex_items.items()]
                pak = core.run_pipeline_multi(items, progress=self.status.emit)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
        else:
            self.done.emit(pak)


class ProcessPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Building your mod")
        self.setSubTitle("Injecting and packaging — hang tight…")
        self._done = False
        self.worker = None

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)  # indeterminate "spinner"
        self.status = QLabel("Starting…")

        lay = QVBoxLayout(self)
        lay.addStretch(1)
        lay.addWidget(self.status, alignment=Qt.AlignCenter)
        lay.addWidget(self.bar)
        lay.addStretch(1)

    def initializePage(self):
        self._done = False
        self.completeChanged.emit()
        w = self.wizard()
        # lock navigation while working
        w.button(QWizard.BackButton).setEnabled(False)
        self.worker = PipelineWorker(
            getattr(w, "mode", "regular"), getattr(w, "mesh_plan", None),
            getattr(w, "mesh_user_files", {}), getattr(w, "cook_items", {}),
            getattr(w, "tex_items", {}), getattr(w, "combine_selected", []),
            getattr(w, "cook_tex_items", {}))
        self.worker.status.connect(self.status.setText)
        self.worker.done.connect(self.on_done)
        self.worker.failed.connect(self._on_fail)
        self.worker.start()

    def _on_fail(self, msg):
        self.status.setText("Failed.")
        self.wizard().button(QWizard.BackButton).setEnabled(True)
        QMessageBox.critical(self, "Pak Rat", f"Build failed:\n{msg}")

    def on_done(self, pak_path):
        self.wizard().pak_path = pak_path
        self._done = True
        self.completeChanged.emit()
        self.wizard().next()  # auto-advance to finish

    def isComplete(self):
        return self._done

    def nextId(self):
        return PAGE_FINISH


# ---------------------------------------------------------------------------
# Finish page — Deploy / Finish
# ---------------------------------------------------------------------------
class FinishPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Done — where should it go?")
        self.setSubTitle("Your .pak is built. Choose what to do with it.")

        self.rb_deploy = QRadioButton("Deploy  —  install into Retro Rewind ~mods")
        self.rb_finish = QRadioButton("Finish  —  save to Documents and reveal it")
        self.rb_deploy.setChecked(True)
        self.group = QButtonGroup(self)
        self.group.addButton(self.rb_deploy, 0)
        self.group.addButton(self.rb_finish, 1)
        self.group.idToggled.connect(lambda *_: self.completeChanged.emit())

        lay = QVBoxLayout(self)
        lay.addWidget(self.rb_deploy)
        lay.addSpacing(8)
        lay.addWidget(self.rb_finish)
        lay.addStretch(1)

    def initializePage(self):
        self.setFinalPage(True)
        self.wizard().setButtonText(QWizard.FinishButton, "Go")

    def isComplete(self):
        return self.rb_deploy.isChecked() or self.rb_finish.isChecked()

    def isFinalPage(self):
        return True

    def nextId(self):
        return -1  # truly the last page — no "Next", only the Go (Finish) button


# ---------------------------------------------------------------------------
# Extract-list page (extract mode) — pull one or many originals at once, with
# auto-detected sibling textures (_bc/_n/_ram). Saves to a chosen folder.
# ---------------------------------------------------------------------------
class ExtractListPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Extract assets")
        self.setSubTitle("Pull originals out of the game to edit — pick one or many.")
        self._rows = []
        self._stretch_added = False

        self._container = QWidget()
        self._vbox = QVBoxLayout(self._container)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._container)

        self.add_btn = QPushButton("➕  Add another asset…")
        self.add_btn.clicked.connect(self._add_another)
        self.fmt = QComboBox()
        self.fmt.addItem("PNG — easy to edit (recommended)", "png")
        self.fmt.addItem("DDS — exact format + mips (for re-injection)", "dds")
        self.fmt.setMaximumWidth(360)

        self.folder_btn = QPushButton("Save to…")
        self.folder_btn.clicked.connect(self._pick_folder)
        self.folder_lbl = QLabel("")
        self.folder_lbl.setStyleSheet(f"color:{MUTED};")
        self.folder_lbl.setWordWrap(True)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_btn)
        folder_row.addWidget(self.folder_lbl, 1)

        lay = QVBoxLayout(self)
        lay.addWidget(scroll)
        lay.addWidget(self.add_btn)
        lay.addWidget(QLabel("Texture export format  (meshes are handed back as .uasset):"))
        lay.addWidget(self.fmt)
        lay.addLayout(folder_row)

    def selected_format(self) -> str:
        return self.fmt.currentData() or "png"

    def selected_assets(self):
        return [r["mount"] for r in self._rows if r["cb"].isChecked()]

    def _pick_folder(self):
        default = getattr(self.wizard(), "extract_dest", "") or \
            str(Path(os.path.expanduser("~")) / "Documents")
        d = QFileDialog.getExistingDirectory(
            self, "Choose a folder to save the extracted assets", default)
        if d:
            self.wizard().extract_dest = d
            self.folder_lbl.setText(d)
            self.completeChanged.emit()

    def initializePage(self):
        wiz = self.wizard()
        if not getattr(wiz, "extract_dest", ""):
            wiz.extract_dest = str(Path(os.path.expanduser("~")) / "Documents")
        self.folder_lbl.setText(wiz.extract_dest)
        while self._vbox.count():
            it = self._vbox.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._rows = []
        self._stretch_added = False

        primary = (self.field("asset") or "").strip()
        if primary:
            self._add_row(primary, removable=False)
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                rel = core.related_assets(primary)
            except Exception:
                rel = []
            finally:
                QApplication.restoreOverrideCursor()
            for m in rel:
                self._add_row(m, removable=True)
        self._vbox.addStretch(1)
        self._stretch_added = True
        self.completeChanged.emit()
        self._start_previews()

    def _add_row(self, mount, removable):
        if any(r["mount"] == mount for r in self._rows):
            return
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        thumb = Thumb(48, spin=True)
        cb = QCheckBox(_basename(mount))
        cb.setChecked(True)
        cb.toggled.connect(lambda *_: self.completeChanged.emit())
        rec = {"mount": mount, "cb": cb, "thumb": thumb}
        h.addWidget(_preview_cell(thumb))
        h.addWidget(cb, 1)
        if removable:
            rm = QPushButton("✕")
            rm.setObjectName("rm")
            rm.clicked.connect(lambda _=False, row=row, rec=rec: self._remove(row, rec))
            h.addWidget(rm)
        if self._stretch_added:
            self._vbox.insertWidget(self._vbox.count() - 1, row)
        else:
            self._vbox.addWidget(row)
        self._rows.append(rec)
        if self._stretch_added:                   # live add → preview just this one
            w = PreviewWorker([mount], self)
            w.ready.connect(self._on_preview)
            w.start()

    def _start_previews(self):
        w = PreviewWorker([r["mount"] for r in self._rows], self)
        w.ready.connect(self._on_preview)
        w.start()

    def _on_preview(self, mount, png):
        for r in self._rows:
            if r["mount"] == mount:
                r["thumb"].set_image(png)
                return

    def _remove(self, row, rec):
        if rec in self._rows:
            self._rows.remove(rec)
        row.deleteLater()
        self.completeChanged.emit()

    def _add_another(self):
        items = sorted(set(core.load_meshes()) | set(core.load_assets()))
        mount = GroupedPickerDialog.pick(self, items, "Add an asset")
        if mount:
            self._add_row(mount.strip(), removable=True)
            self.completeChanged.emit()

    def isComplete(self):
        return bool(self.selected_assets()) and \
            bool(getattr(self.wizard(), "extract_dest", ""))

    def nextId(self):
        return PAGE_EXTRACTPROG


# ---------------------------------------------------------------------------
# Extract progress + done pages — same treatment as the installer/cooker.
# ---------------------------------------------------------------------------
class ExtractSaveWorker(QThread):
    status = Signal(str)
    done = Signal(list)        # written file paths
    failed = Signal(str)

    def __init__(self, assets, dest, fmt):
        super().__init__()
        self.assets = assets
        self.dest = dest
        self.fmt = fmt

    def run(self):
        try:
            written = core.export_assets(self.assets, self.dest, self.fmt,
                                         progress=self.status.emit)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
        else:
            self.done.emit(written)


class ExtractProgressPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Saving your assets")
        self.setSubTitle("Extracting — hang tight…")
        self._done = False
        self.worker = None
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)            # themed indeterminate bar
        self.status = QLabel("Starting…")
        lay = QVBoxLayout(self)
        lay.addStretch(1)
        lay.addWidget(self.status, alignment=Qt.AlignCenter)
        lay.addWidget(self.bar)
        lay.addStretch(1)

    def initializePage(self):
        self._done = False
        self.completeChanged.emit()
        wiz = self.wizard()
        wiz.button(QWizard.BackButton).setEnabled(False)
        page = wiz.page(PAGE_EXTRACTLIST)
        self.worker = ExtractSaveWorker(page.selected_assets(),
                                        getattr(wiz, "extract_dest", ""),
                                        page.selected_format())
        self.worker.status.connect(self.status.setText)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_fail)
        self.worker.start()

    def _on_done(self, written):
        wiz = self.wizard()
        wiz.extract_written = written
        wiz.button(QWizard.BackButton).setEnabled(True)
        self._done = True
        self.completeChanged.emit()
        QTimer.singleShot(0, wiz.next)

    def _on_fail(self, msg):
        self.wizard().button(QWizard.BackButton).setEnabled(True)
        self.status.setText("Failed.")
        QMessageBox.critical(self, "Pak Rat", f"Extract failed:\n{msg}")

    def isComplete(self):
        return self._done

    def nextId(self):
        return PAGE_EXTRACTDONE


class ExtractDonePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Done")
        self.setSubTitle("Your assets were extracted.")
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        self.reveal_btn = QPushButton("Open the folder")
        self.reveal_btn.clicked.connect(self._reveal)
        lay = QVBoxLayout(self)
        lay.addWidget(self.msg)
        lay.addSpacing(8)
        lay.addWidget(self.reveal_btn)
        lay.addStretch(1)

    def initializePage(self):
        self.setFinalPage(True)
        self.wizard().setButtonText(QWizard.FinishButton, "Close")
        written = getattr(self.wizard(), "extract_written", [])
        dest = getattr(self.wizard(), "extract_dest", "")
        self.msg.setText(f"Extracted {len(written)} file(s) to:\n{dest}")
        if written:
            try:
                core.reveal_in_explorer(written[0])
            except Exception:
                pass

    def _reveal(self):
        w = getattr(self.wizard(), "extract_written", [])
        if w:
            core.reveal_in_explorer(w[0])

    def isComplete(self):
        return True

    def isFinalPage(self):
        return True

    def nextId(self):
        return -1


# ---------------------------------------------------------------------------
# Combine mode — page 1: choose which paks to pull assets from
# ---------------------------------------------------------------------------
class CombineSourcePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Choose the mods to combine")
        self.setSubTitle("Pick the paks to pull assets from — your installed "
                         "mods are listed; add more from disk if you need to.")
        self._rows = []  # {path, cb}

        self._container = QWidget()
        self._vbox = QVBoxLayout(self._container)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._container)

        self.add_btn = QPushButton("➕  Add a pak from disk…")
        self.add_btn.clicked.connect(self._add_from_disk)
        self.hint = QLabel("Pick two or more to mix and match. If two mods touch "
                           "the same asset, you’ll choose the winner on the next "
                           "step.")
        self.hint.setStyleSheet("color:#888;")
        self.hint.setWordWrap(True)

        lay = QVBoxLayout(self)
        lay.addWidget(scroll)
        lay.addWidget(self.add_btn)
        lay.addWidget(self.hint)

    def initializePage(self):
        while self._vbox.count():
            it = self._vbox.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._rows = []
        installed = core.list_mod_paks()
        if installed:
            head = QLabel("Installed in Retro Rewind ~mods:")
            head.setStyleSheet("font-weight:600;")
            self._vbox.addWidget(head)
            for p in installed:
                self._add_row(p)
        else:
            none = QLabel("No installed mods found — use “Add a pak from disk…”.")
            none.setStyleSheet("color:#c08a2e;")
            self._vbox.addWidget(none)
        self._vbox.addStretch(1)
        self.completeChanged.emit()

    def _add_row(self, path, checked=False):
        if any(r["path"] == path for r in self._rows):
            return
        cb = QCheckBox(Path(path).name)
        cb.setToolTip(path)
        cb.setChecked(checked)
        cb.toggled.connect(lambda *_: self.completeChanged.emit())
        n = self._vbox.count()
        if n and self._vbox.itemAt(n - 1).spacerItem():
            self._vbox.insertWidget(n - 1, cb)   # before the trailing stretch
        else:
            self._vbox.addWidget(cb)
        self._rows.append({"path": path, "cb": cb})

    def _add_from_disk(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add pak(s)", str(Path(os.path.expanduser("~"))),
            "Unreal paks (*.pak);;All files (*)")
        for p in paths:
            self._add_row(p, checked=True)
        self.completeChanged.emit()

    def selected_paks(self):
        return [r["path"] for r in self._rows if r["cb"].isChecked()]

    def isComplete(self):
        return bool(self.selected_paks())

    def validatePage(self):
        paks = self.selected_paks()
        if not paks:
            return False
        self.wizard().combine_sources = paks
        return True

    def nextId(self):
        return PAGE_COMBINESEL


# ---------------------------------------------------------------------------
# Combine mode — page 2: cherry-pick assets (conflict-aware)
# ---------------------------------------------------------------------------
class _HoverCheckBox(QCheckBox):
    """A checkbox that emits when the pointer enters it (for hover previews)."""
    hovered = Signal()

    def enterEvent(self, e):
        self.hovered.emit()
        super().enterEvent(e)


class PakPreviewWorker(QThread):
    """Decodes one texture's preview from a specific pak (combine hover)."""
    ready = Signal(str, str)   # mount, png path ("" = none)

    def __init__(self, pak, mount, parent=None):
        super().__init__(parent)
        self._pak = pak
        self._mount = mount

    def run(self):
        try:
            p = core.decode_pak_preview(self._pak, self._mount) or ""
        except Exception:
            p = ""
        self.ready.emit(self._mount, p)


class CombineSelectPage(QWizardPage):
    _KIND_TAG = {"mesh": "MESH", "texture": "TEX", "material": "MAT",
                 "shader": "SHD", "other": "···"}

    def __init__(self):
        super().__init__()
        self.setTitle("Pick the assets to include")
        self.setSubTitle("Tick what you want from each mod. ⚠ marks an asset more "
                         "than one mod changes — you’ll pick the winner on Next. "
                         "Hover a texture to preview it.")
        self._rows = []   # {asset: PakAsset, cb}
        self._prev_cache = {}     # mount -> decoded png ("" = none)
        self._prev_pending = set()

        self._container = QWidget()
        self._vbox = QVBoxLayout(self._container)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._container)

        self.status = QLabel("")
        self.status.setStyleSheet("color:#888;")

        lay = QVBoxLayout(self)
        lay.addWidget(scroll)
        lay.addWidget(self.status)

    def initializePage(self):
        while self._vbox.count():
            it = self._vbox.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        self._rows = []
        self.status.setText("")
        paks = getattr(self.wizard(), "combine_sources", [])
        QApplication.setOverrideCursor(Qt.WaitCursor)
        per_pak, overlap = [], {}
        try:
            for p in paks:
                try:
                    assets = core.pak_assets(p)
                except Exception as e:  # noqa: BLE001
                    assets = []
                    self.status.setText(f"Couldn’t read {Path(p).name}: {e}")
                per_pak.append((p, assets))
                for a in assets:
                    overlap[a.mount] = overlap.get(a.mount, 0) + 1
        finally:
            QApplication.restoreOverrideCursor()

        for p, assets in per_pak:
            head = QLabel(Path(p).name)
            head.setStyleSheet("font-weight:600; margin-top:6px;")
            self._vbox.addWidget(head)
            sel_all = QCheckBox("    (select all from this mod)")
            sel_all.setStyleSheet("color:#888;")
            self._vbox.addWidget(sel_all)
            group_cbs = []
            for a in assets:
                tag = self._KIND_TAG.get(a.kind, "···")
                warn = "  ⚠" if overlap.get(a.mount, 0) > 1 else ""
                cb = _HoverCheckBox(f"    [{tag}]  {a.leaf}{warn}")
                if warn:
                    cb.setStyleSheet("color:#c08a2e;")
                cb.toggled.connect(lambda *_: self.completeChanged.emit())
                cb.hovered.connect(lambda a=a: self._hover(a))
                if not a.leaf.startswith("T_"):
                    cb.setToolTip("(no preview)")
                self._vbox.addWidget(cb)
                self._rows.append({"asset": a, "cb": cb})
                group_cbs.append(cb)
            sel_all.toggled.connect(
                lambda on, cbs=group_cbs: [c.setChecked(on) for c in cbs])
        self._vbox.addStretch(1)
        self.completeChanged.emit()

    def _hover(self, asset):
        png = self._prev_cache.get(asset.mount)
        if png is not None:
            self._show_tip(png)
            return
        if not asset.leaf.startswith("T_") or asset.mount in self._prev_pending:
            return
        self._prev_pending.add(asset.mount)
        QToolTip.showText(QCursor.pos(), "Loading preview…")
        w = PakPreviewWorker(asset.pak, asset.mount, self)
        w.ready.connect(self._prev_ready)
        w.start()

    def _prev_ready(self, mount, png):
        self._prev_pending.discard(mount)
        self._prev_cache[mount] = png
        self._show_tip(png)

    def _show_tip(self, png):
        html = _big_img_html(png)
        if html:
            QToolTip.showText(QCursor.pos(), html)

    def _checked(self):
        return [r["asset"] for r in self._rows if r["cb"].isChecked()]

    def isComplete(self):
        return bool(self._checked())

    def validatePage(self):
        selected = self._checked()
        if not selected:
            return False
        conflicts = core.find_conflicts(selected)
        winners = {}
        for mount, options in conflicts.items():
            leaf = mount.rsplit("/", 1)[-1]
            labels = [Path(a.pak).name for a in options]
            choice, ok = QInputDialog.getItem(
                self, "Resolve conflict",
                f"“{leaf}” is changed by more than one mod.\n"
                "Which one should win?", labels, 0, False)
            if not ok:
                return False  # cancelled — stay on the page
            winners[mount] = labels.index(choice)
        resolved, seen = [], set()
        for a in selected:
            if a.mount in conflicts:
                if a.mount in seen:
                    continue
                seen.add(a.mount)
                resolved.append(conflicts[a.mount][winners[a.mount]])
            else:
                resolved.append(a)
        self.wizard().combine_selected = resolved
        return True

    def nextId(self):
        return PAGE_PROCESS


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------
class PakRatWizard(QWizard):
    def __init__(self):
        super().__init__()
        self.mode = "regular"
        self.pak_path = None
        self.target_spec = None
        self.mesh_plan = None
        self.mesh_user_files = {}
        self.cook_items = {}
        self.cook_tex_items = {}
        self.extract_dest = ""
        self.extract_written = []
        self.tex_items = {}
        self.combine_sources = []
        self.combine_selected = []
        self.cook_available = None

        self.setWindowTitle(f"Pak Rat v{APP_VERSION}")
        self.setWindowIcon(QIcon(resource_path("Pak-Rat.ico")))
        self.setWizardStyle(QWizard.ModernStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        # Fixed size — long asset paths must never push the window off-screen.
        self.setFixedSize(640, 470)

        self.setPage(PAGE_MODE, ModePage())
        self.setPage(PAGE_ASSET, AssetPage())
        self.setPage(PAGE_EXTRACT, ExtractPage())
        self.setPage(PAGE_TEXLIST, TextureListPage())
        self.setPage(PAGE_REQUIRED, RequiredFilesPage())
        self.setPage(PAGE_PROCESS, ProcessPage())
        self.setPage(PAGE_FINISH, FinishPage())
        self.setPage(PAGE_SETUP, SetupPage())
        self.setPage(PAGE_COOKINPUT, CookListPage())
        self.setPage(PAGE_COOKTEX, CookTexturePage())
        self.setPage(PAGE_EXTRACTLIST, ExtractListPage())
        self.setPage(PAGE_EXTRACTPROG, ExtractProgressPage())
        self.setPage(PAGE_EXTRACTDONE, ExtractDonePage())
        self.setPage(PAGE_COMBINESRC, CombineSourcePage())
        self.setPage(PAGE_COMBINESEL, CombineSelectPage())
        self.setStartId(PAGE_MODE)

        # On the final "Done" page, Back should start the whole flow over at
        # page 1 (not re-run the pipeline). Mid-flow Back stays normal.
        back = self.button(QWizard.BackButton)
        try:
            back.clicked.disconnect()      # drop QWizard's built-in back()
        except (RuntimeError, TypeError):
            pass
        back.clicked.connect(self._on_back)

    def _on_back(self):
        if self.currentId() in (PAGE_FINISH, PAGE_EXTRACTDONE):
            self.restart()      # done page → start completely over at page 1
        else:
            self.back()         # normal step-back everywhere else

    def _ask_pak_name(self) -> str | None:
        """Prompt for the mod's file name. None if the user cancels."""
        if getattr(self, "mode", "") == "combine":
            leaf = "Combined"
        elif self.target_spec:
            leaf = _basename(self.target_spec.asset)
        elif getattr(self, "mesh_plan", None):
            leaf = _basename(self.mesh_plan.mesh)
        else:
            leaf = _basename(self.field("asset") or "")
        suggestion = leaf or "MyMod"
        name, ok = QInputDialog.getText(
            self, "Name your pak",
            "Mod file name  (a _P.pak suffix is added if you omit it):",
            text=suggestion)
        return name if ok else None

    def accept(self):
        """Final action: name the pak, then Deploy or Finish, then close."""
        if getattr(self, "mode", "regular") == "extract":
            super().accept()   # extract already saved on the progress page
            return
        page = self.page(PAGE_FINISH)
        name = self._ask_pak_name()
        if name is None:
            return  # cancelled the name prompt — keep the wizard open
        try:
            final_pak = core.rename_pak(self.pak_path, name)
            self.pak_path = final_pak
            if page.rb_deploy.isChecked():
                dest = core.deploy_to_rr(final_pak)
                QMessageBox.information(
                    self, "Pak Rat",
                    f"Installed to Retro Rewind ~mods:\n{Path(dest).name}")
            else:
                core.finish_to_documents(final_pak)
        except Exception as e:  # surface; keep open so the user can retry
            QMessageBox.critical(self, "Pak Rat", f"Final step failed:\n{e}")
            return
        super().accept()


def main():
    selftest = "--selftest" in sys.argv
    # Without an explicit AppUserModelID, Windows won't bind our window icon to
    # the taskbar button (it shows a blank/generic icon for frozen apps).
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "RetroRewind.PakRat")
        except Exception:
            pass
    app = QApplication([a for a in sys.argv if a != "--selftest"])
    app.setFont(QFont("Segoe UI", 10))
    apply_theme(app)  # synthwave dark theme from the icon palette
    app.setWindowIcon(QIcon(resource_path("Pak-Rat.ico")))

    # Splash while the wizard constructs (Pak-Rat.png scaled ~400px).
    splash = None
    splash_png = resource_path("Pak-Rat.png")
    if os.path.exists(splash_png):
        pm = QPixmap(splash_png)
        if not pm.isNull():
            splash = QSplashScreen(pm.scaled(
                400, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            splash.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            splash.show()
            splash.raise_()
            splash.activateWindow()
            app.processEvents()

    wiz = PakRatWizard()
    if splash is not None:
        splash.finish(wiz)
    wiz.show()
    # Force the window to the foreground even if the user alt-tabbed away during
    # load (Windows won't otherwise hand focus back to a freshly-shown window).
    if sys.platform == "win32":
        wiz.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        wiz.show()
        wiz.setWindowFlag(Qt.WindowStaysOnTopHint, False)
        wiz.show()
    wiz.raise_()
    wiz.activateWindow()
    if selftest:  # build verification: construct the GUI, then auto-quit
        from PySide6.QtCore import QTimer
        QTimer.singleShot(1200, app.quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
