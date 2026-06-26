"""
Pak Rat — automatic asset packager for Retro Rewind (UE 5.4).

Proof-of-concept GUI: an installer-style QWizard. All real injection/packaging
is stubbed in core.py behind clean seams; this file is pure UI + flow.

Flow:
  ModePage → AssetPage → ExtractPage → ImagePage → [MeshPage] → ProcessPage → FinishPage
  (MeshPage only when "Mesh + Texture" is chosen)
  Extract mode short-circuits: ModePage → AssetPage → ExtractPage → ExportPage
  (hands the user the decoded original texture; no packaging).

Run (Windows):  python.exe src/pak_rat.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QCompleter, QFileDialog,
    QHBoxLayout, QInputDialog, QLabel, QMessageBox, QProgressBar, QPushButton,
    QRadioButton, QScrollArea, QSplashScreen, QVBoxLayout, QWidget, QWizard,
    QWizardPage,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402

# Page ids
PAGE_MODE, PAGE_ASSET, PAGE_EXTRACT, PAGE_IMAGE, PAGE_REQUIRED, PAGE_PROCESS, \
    PAGE_FINISH, PAGE_EXPORT = range(8)

GREEN = "#2e9e44"


def resource_path(name: str) -> str:
    """Path to a bundled resource — frozen (onedir _internal) or source dir."""
    base = getattr(sys, "_MEIPASS", None) or str(Path(__file__).resolve().parent)
    return str(Path(base) / name)


def _basename(asset: str) -> str:
    """Leaf name of an asset path, e.g. .../textures/MI_Detail_01 -> MI_Detail_01."""
    return asset.rstrip("/").split("/")[-1]


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
        self.rb_extract = QRadioButton("Extract Texture")
        self.rb_regular.setChecked(True)

        self.group = QButtonGroup(self)
        self.group.addButton(self.rb_regular, 0)
        self.group.addButton(self.rb_mesh, 1)
        self.group.addButton(self.rb_extract, 2)

        lay = QVBoxLayout(self)
        lay.addWidget(self.rb_regular)
        lab1 = QLabel("    Swap a single texture (PNG/DDS) on an existing asset.")
        lab1.setStyleSheet("color:#888;")
        lay.addWidget(lab1)
        lay.addSpacing(12)
        lay.addWidget(self.rb_mesh)
        lab2 = QLabel("    Swap a mesh and its texture together.")
        lab2.setStyleSheet("color:#888;")
        lay.addWidget(lab2)
        lay.addSpacing(12)
        lay.addWidget(self.rb_extract)
        lab3 = QLabel("    Pull an original texture out of the game to edit (PNG/DDS).")
        lab3.setStyleSheet("color:#888;")
        lay.addWidget(lab3)
        lay.addStretch(1)

    def initializePage(self):
        self.wizard().mode = "regular"
        self.group.idToggled.connect(self._on_toggle)

    def _on_toggle(self, _id, checked):
        if self.rb_mesh.isChecked():
            self.wizard().mode = "mesh"
        elif self.rb_extract.isChecked():
            self.wizard().mode = "extract"
        else:
            self.wizard().mode = "regular"

    def nextId(self):
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

        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.setInsertPolicy(QComboBox.NoInsert)
        self.combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setMinimumContentsLength(24)
        self.combo.setMaximumWidth(560)
        self.combo.currentTextChanged.connect(self.completeChanged)
        self.combo.currentTextChanged.connect(self._on_primary_changed)

        # Second dropdown — mesh mode only: the auto-resolved overlay texture.
        self.tex_lbl = QLabel("Overlay texture:")
        self.combo2 = QComboBox()
        self.combo2.setMaximumWidth(560)
        self.combo2.currentTextChanged.connect(self.completeChanged)

        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Start typing to filter, or pick from the list:"))
        lay.addWidget(self.combo)
        lay.addSpacing(8)
        lay.addWidget(self.tex_lbl)
        lay.addWidget(self.combo2)
        lay.addStretch(1)

        self.registerField("asset", self.combo, "currentText",
                            self.combo.currentTextChanged)
        self.registerField("overlay_tex", self.combo2, "currentText",
                            self.combo2.currentTextChanged)

    def _is_mesh(self):
        return getattr(self.wizard(), "mode", "regular") == "mesh"

    def initializePage(self):
        mesh = self._is_mesh()
        items = core.load_meshes() if mesh else core.load_assets()
        self._resolved_for = None
        self._mesh_set = set(items) if mesh else set()
        self.combo.clear()
        self.combo.addItems(items)
        self.combo.setCurrentIndex(-1)
        self.combo.setEditText("")
        completer = QCompleter(items, self.combo)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self.combo.setCompleter(completer)

        self.combo2.clear()
        self.tex_lbl.setVisible(mesh)
        self.combo2.setVisible(mesh)
        if mesh:
            self.setTitle("Select the mesh")
            self.setSubTitle("Pick the mesh, then its overlay texture.")
        elif getattr(self.wizard(), "mode", "regular") == "extract":
            self.setTitle("Select the texture to extract")
            self.setSubTitle("Pick the texture you want to pull out of the game.")
        else:
            self.setTitle("Select the texture")
            self.setSubTitle("Pick the texture you want to replace.")
        self.completeChanged.emit()

    def _on_primary_changed(self, text):
        if not self._is_mesh():
            return
        text = text.strip()
        if text == self._resolved_for or text not in self._mesh_set:
            return
        self._resolved_for = text
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            texs = core.resolve_overlay_textures(text)
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
        return PAGE_EXTRACT


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
        mode = getattr(self.wizard(), "mode", "regular")
        if mode == "extract":
            return PAGE_EXPORT
        if mode == "mesh":
            return PAGE_REQUIRED
        return PAGE_IMAGE


# ---------------------------------------------------------------------------
# Page 3 — image (PNG/DDS) picker + validation
# ---------------------------------------------------------------------------
class ImagePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Choose replacement texture")
        self.setSubTitle("Select the PNG or DDS you want to swap in.")
        self._validated = False

        self.spec_lbl = QLabel("")
        self.spec_lbl.setWordWrap(True)

        # Two thumbnails side by side: the original ("before") and the user's.
        self.preview = QLabel("Original")
        self.new_preview = QLabel("Your image")
        for p in (self.preview, self.new_preview):
            p.setFixedSize(150, 150)
            p.setAlignment(Qt.AlignCenter)
            p.setStyleSheet("border:1px solid #444; color:#888;")

        self.btn = QPushButton("Choose PNG / DDS…")
        self.btn.clicked.connect(self.pick_file)
        self.status = QLabel("No file chosen.")
        self.status.setWordWrap(True)

        thumbs = QHBoxLayout()
        thumbs.addWidget(self.preview)
        thumbs.addWidget(self.new_preview)
        thumbs.addStretch(1)

        lay = QVBoxLayout(self)
        lay.addWidget(self.spec_lbl)
        lay.addLayout(thumbs)
        lay.addSpacing(8)
        lay.addWidget(self.btn)
        lay.addSpacing(6)
        lay.addWidget(self.status)
        lay.addStretch(1)

    def initializePage(self):
        self._validated = False
        self.status.setText("No file chosen.")
        self.status.setStyleSheet("")
        spec = getattr(self.wizard(), "target_spec", None)
        if spec is not None:
            self.spec_lbl.setText(
                f"Replacing:  {_basename(spec.asset)}\n"
                f"{spec.tex_type} · {spec.dxgi_format} · "
                f"{spec.width}×{spec.height} · {spec.mips} mips\n"
                f"Your image is resized to {spec.width}×{spec.height}.")
            self.preview.setText("(no preview)")
            if spec.preview_png and Path(spec.preview_png).exists():
                pm = QPixmap(spec.preview_png)
                if not pm.isNull():
                    self.preview.setPixmap(pm.scaled(
                        self.preview.size(), Qt.KeepAspectRatio,
                        Qt.SmoothTransformation))
        # reset the user's-image thumbnail each time the page is shown
        self.new_preview.clear()
        self.new_preview.setText("Your image")
        self.completeChanged.emit()

    def pick_file(self):
        # Default the picker's filename to the chosen asset's leaf name.
        default_name = _basename(self.field("asset") or "")
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose replacement texture", default_name,
            "Images (*.png *.dds);;All files (*)")
        if not path:
            return
        if not core.validate_image_ext(path):
            QMessageBox.warning(self, "Wrong file type",
                                "Please choose a PNG or DDS file.")
            self.pick_file()  # back to the picker
            return
        info = core.prepare_image(path, self.wizard().target_spec)
        self.wizard().image_info = info
        # Thumbnail of THEIR image. prepared_png is always a valid PNG (RGBA,
        # resized) — covers PNG and DDS inputs alike; fall back to a label.
        pm = QPixmap(info.prepared_png)
        if not pm.isNull():
            self.new_preview.setPixmap(pm.scaled(
                self.new_preview.size(), Qt.KeepAspectRatio,
                Qt.SmoothTransformation))
        else:
            self.new_preview.setText(
                "DDS (no preview)" if info.ext == ".dds" else "(no preview)")
        self._validated = True
        self.status.setText(
            f"✓ Validated  ·  {Path(path).name}\n"
            f"   {path}\n   encoding: {info.encoding}")
        self.status.setStyleSheet(f"color:{GREEN}; font-weight:600;")
        self.completeChanged.emit()

    def isComplete(self):
        return self._validated

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
# Process page — spinner while the (stubbed) pipeline runs
# ---------------------------------------------------------------------------
class PipelineWorker(QThread):
    status = Signal(str)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, mode, asset, image_info, mesh_plan, mesh_user_files):
        super().__init__()
        self.mode = mode
        self.asset = asset
        self.image_info = image_info
        self.mesh_plan = mesh_plan
        self.mesh_user_files = mesh_user_files

    def run(self):
        try:
            if self.mode == "mesh":
                pak = core.run_mesh_pipeline(self.mesh_plan, self.mesh_user_files,
                                             progress=self.status.emit)
            else:
                pak = core.run_pipeline(self.asset, self.image_info, None,
                                        progress=self.status.emit)
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
            getattr(w, "mode", "regular"), self.field("asset"),
            getattr(w, "image_info", None), getattr(w, "mesh_plan", None),
            getattr(w, "mesh_user_files", {}))
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


# ---------------------------------------------------------------------------
# Export page (extract mode) — hand the user the decoded original texture
# ---------------------------------------------------------------------------
class ExportPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Extract the original texture")
        self.setSubTitle("Save the game's original art so you can edit it.")

        self.spec_lbl = QLabel("")
        self.spec_lbl.setWordWrap(True)

        self.preview = QLabel("Original")
        self.preview.setFixedSize(150, 150)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("border:1px solid #444; color:#888;")

        self.fmt = QComboBox()
        # PNG first (default) — easiest to edit; DDS preserves exact format+mips.
        self.fmt.addItem("PNG — easy to edit (recommended)", "png")
        self.fmt.addItem("DDS — exact format + mips (for re-injection)", "dds")
        self.fmt.setMaximumWidth(360)

        lay = QVBoxLayout(self)
        lay.addWidget(self.spec_lbl)
        lay.addWidget(self.preview)
        lay.addSpacing(8)
        lay.addWidget(QLabel("Export format:"))
        lay.addWidget(self.fmt)
        lay.addStretch(1)

    def selected_format(self) -> str:
        return self.fmt.currentData() or "png"

    def initializePage(self):
        self.setFinalPage(True)
        self.wizard().setButtonText(QWizard.FinishButton, "Save…")
        spec = getattr(self.wizard(), "target_spec", None)
        self.preview.setText("(no preview)")
        if spec is not None:
            self.spec_lbl.setText(
                f"Extracting:  {_basename(spec.asset)}\n"
                f"{spec.tex_type} · {spec.dxgi_format} · "
                f"{spec.width}×{spec.height} · {spec.mips} mips")
            if spec.preview_png and Path(spec.preview_png).exists():
                pm = QPixmap(spec.preview_png)
                if not pm.isNull():
                    self.preview.setPixmap(pm.scaled(
                        self.preview.size(), Qt.KeepAspectRatio,
                        Qt.SmoothTransformation))
        self.completeChanged.emit()

    def isComplete(self):
        return getattr(self.wizard(), "target_spec", None) is not None

    def isFinalPage(self):
        return True

    def nextId(self):
        return -1


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------
class PakRatWizard(QWizard):
    def __init__(self):
        super().__init__()
        self.mode = "regular"
        self.image_info = None
        self.pak_path = None
        self.target_spec = None
        self.mesh_plan = None
        self.mesh_user_files = {}

        self.setWindowTitle("Pak Rat")
        self.setWindowIcon(QIcon(resource_path("Pak-Rat.ico")))
        self.setWizardStyle(QWizard.ModernStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        # Fixed size — long asset paths must never push the window off-screen.
        self.setFixedSize(640, 470)

        self.setPage(PAGE_MODE, ModePage())
        self.setPage(PAGE_ASSET, AssetPage())
        self.setPage(PAGE_EXTRACT, ExtractPage())
        self.setPage(PAGE_IMAGE, ImagePage())
        self.setPage(PAGE_REQUIRED, RequiredFilesPage())
        self.setPage(PAGE_PROCESS, ProcessPage())
        self.setPage(PAGE_FINISH, FinishPage())
        self.setPage(PAGE_EXPORT, ExportPage())
        self.setStartId(PAGE_MODE)

    def _ask_pak_name(self) -> str | None:
        """Prompt for the mod's file name. None if the user cancels."""
        leaf = _basename(self.target_spec.asset) if self.target_spec else ""
        suggestion = leaf or "MyMod"
        name, ok = QInputDialog.getText(
            self, "Name your pak",
            "Mod file name  (a _P.pak suffix is added if you omit it):",
            text=suggestion)
        return name if ok else None

    def _accept_extract(self):
        """Extract mode final action: decode the original to the user's chosen
        file + format, reveal it, then close."""
        spec = self.target_spec
        if spec is None:
            return
        fmt = self.page(PAGE_EXPORT).selected_format()
        leaf = _basename(spec.asset)
        default = str(Path(os.path.expanduser("~")) / "Documents" / f"{leaf}.{fmt}")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save extracted texture", default,
            f"{fmt.upper()} (*.{fmt});;All files (*)")
        if not path:
            return  # cancelled the save dialog — keep the wizard open
        if not path.lower().endswith("." + fmt):
            path += "." + fmt
        try:
            out = core.export_texture(spec, path, fmt)
        except Exception as e:  # surface; keep open so the user can retry
            QMessageBox.critical(self, "Pak Rat", f"Export failed:\n{e}")
            return
        core.reveal_in_explorer(out)
        QMessageBox.information(
            self, "Pak Rat", f"Extracted the original texture:\n{Path(out).name}")
        super().accept()

    def accept(self):
        """Final action: name the pak, then Deploy or Finish, then close."""
        if getattr(self, "mode", "regular") == "extract":
            self._accept_extract()
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
