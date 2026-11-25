# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

import hou

from qtpy.QtWidgets import (
    QComboBox,
    QLabel,
    QLineEdit,
    QWidget,
    QGridLayout,
    QVBoxLayout,
)
from qtpy.QtCore import Qt, QCoreApplication  # type: ignore
from deadline.client.exceptions import DeadlineOperationCanceled
from deadline.client.ui.dialogs.submit_job_to_deadline_dialog import SubmitJobToDeadlineDialog
from deadline.client.ui.pre_gui_hooks import (
    PreGuiHookContext,
    apply_pre_gui_output,
    run_pre_gui_hooks,
)
from deadline.client.dataclasses import SubmitterInfo
from deadline_cloud_for_houdini._version import version as houdini_submitter_version
from deadline_cloud_for_houdini._assets import (
    _get_evaluated_asset_references,
    _get_scene_asset_references,
)
from deadline_cloud_for_houdini.constants import FrameRange
from deadline_cloud_for_houdini.houdini_submitter_widget import SceneSettingsWidget
from deadline_cloud_for_houdini.queue_parameters import (
    get_shared_parameter_values_for_dialog,
)
from deadline_cloud_for_houdini.submitter import (
    _is_deadline_cloud_rop,
    _is_hou_node_alive,
    _pre_gui_hook_confirm_callback,
    _make_create_bundle_callback,
    _install_pre_submission_gate,
    _install_queue_parameter_sync,
    _resolve_live_rop_node,
    _safe_node_path,
    get_saved_host_requirements,
    read_ui_settings_from_node,
)


class SubmitterPanel(QWidget):
    # Ordered to match the FrameRange enum's integer values (index == trange parm value).
    _FRAME_RANGE_LABELS = (
        "Render Current Frame",
        "Render Frame Range",
        "Render Frame Range Only (Strict)",
    )
    # A ROP's ``take`` parm uses an empty string to mean "use the current take"; some Houdini
    # versions have historically used "_current_" -- treat both as the "Current" menu entry.
    _CURRENT_TAKE_TOKENS = ("", "_current_")

    def __init__(self, parent, deadline_cloud_widget, node, pane_tab=None):
        super().__init__(parent=parent)
        self.deadline_cloud_widget = deadline_cloud_widget
        self.node = node
        self.pane_tab = pane_tab
        self._last_node_path = _safe_node_path(node)
        self._signals_connected = False
        if self.deadline_cloud_widget is not None:
            self.deadline_cloud_widget._deadline_active_rop_node = node
            if pane_tab is not None:
                self.deadline_cloud_widget._deadline_pane_tab = pane_tab

        self._build_ui()
        self._load_from_node()

    def _build_ui(self):
        parent_layout = QVBoxLayout(self)
        frame_info_layout = QGridLayout(self)

        grid_row = 0
        frame_info_layout.addWidget(QLabel("Valid Frame Range"), grid_row, 0)
        self.frame_range_combo = QComboBox(self)
        self.frame_range_combo.addItems(self._FRAME_RANGE_LABELS)
        frame_info_layout.addWidget(self.frame_range_combo, grid_row, 1)

        grid_row += 1
        frame_info_layout.addWidget(QLabel("Start/End/Inc"), grid_row, 0)
        self.frame_start_edit = QLineEdit(self)
        self.frame_end_edit = QLineEdit(self)
        self.frame_inc_edit = QLineEdit(self)
        self._frame_edits = (self.frame_start_edit, self.frame_end_edit, self.frame_inc_edit)
        frame_info_layout.addWidget(self.frame_start_edit, grid_row, 1)
        frame_info_layout.addWidget(self.frame_end_edit, grid_row, 2)
        frame_info_layout.addWidget(self.frame_inc_edit, grid_row, 3)

        grid_row += 1
        frame_info_layout.addWidget(QLabel("Render with Take"), grid_row, 0)
        self.take_combo = QComboBox(self)
        frame_info_layout.addWidget(self.take_combo, grid_row, 1)

        parent_layout.addLayout(frame_info_layout)
        parent_layout.addWidget(self.deadline_cloud_widget)

    def _load_from_node(self):
        """Populate the controls from the node's trange/f/take parms and wire write-back.

        Signals are connected only after the initial values are set so that populating the
        widgets does not itself trigger write-back callbacks.
        """
        node = self._current_node()
        if not _is_hou_node_alive(node):
            return

        widgets = (self.frame_range_combo, *self._frame_edits, self.take_combo)
        for w in widgets:
            w.blockSignals(True)
        try:
            # Valid Frame Range
            trange = int(node.parm("trange").eval())
            if 0 <= trange < self.frame_range_combo.count():
                self.frame_range_combo.setCurrentIndex(trange)

            # Start / End / Inc
            for edit, parm_name in zip(self._frame_edits, ("f1", "f2", "f3")):
                edit.setText(self._format_frame_value(node.parm(parm_name).eval()))

            # Render with Take: "Current" (empty token) followed by every named take.
            self.take_combo.clear()
            self.take_combo.addItem("Current", "")
            current_take_value = node.parm("take").evalAsString()
            for take in hou.takes.takes():
                self.take_combo.addItem(take.name(), take.name())
            if current_take_value in self._CURRENT_TAKE_TOKENS:
                self.take_combo.setCurrentIndex(0)
            else:
                match_index = self.take_combo.findData(current_take_value)
                if match_index >= 0:
                    self.take_combo.setCurrentIndex(match_index)
        finally:
            for w in widgets:
                w.blockSignals(False)

        self._update_frame_edit_enabled_state()

        # Wire write-back once after initial population.
        if not self._signals_connected:
            self.frame_range_combo.currentIndexChanged.connect(self._on_frame_range_changed)
            self.frame_start_edit.editingFinished.connect(
                lambda: self._on_frame_value_changed("f1")
            )
            self.frame_end_edit.editingFinished.connect(lambda: self._on_frame_value_changed("f2"))
            self.frame_inc_edit.editingFinished.connect(lambda: self._on_frame_value_changed("f3"))
            self.take_combo.currentIndexChanged.connect(self._on_take_changed)
            self._signals_connected = True

    def _current_node(self):
        live = _resolve_live_rop_node(
            self.node,
            last_known_path=self._last_node_path,
            dialog=self.deadline_cloud_widget,
        )
        if live is not None:
            self.node = live
            self._last_node_path = _safe_node_path(live) or self._last_node_path
            if self.deadline_cloud_widget is not None:
                self.deadline_cloud_widget._deadline_active_rop_node = live
        return self.node

    def set_node(self, node) -> None:
        """Rebind the panel and its embedded submit dialog when the active ROP node changes."""
        if not _is_deadline_cloud_rop(node):
            return
        same_node = False
        try:
            same_node = self.node is not None and self.node == node
        except Exception:
            same_node = False

        self.node = node
        self._last_node_path = _safe_node_path(node) or self._last_node_path
        if self.deadline_cloud_widget is not None:
            self.deadline_cloud_widget._deadline_active_rop_node = node

        if same_node:
            return

        self._load_from_node()
        if self.deadline_cloud_widget is None:
            return
        try:
            ui_settings = read_ui_settings_from_node(node)
            shared_values = get_shared_parameter_values_for_dialog(node)
            shared_settings = getattr(self.deadline_cloud_widget, "shared_job_settings", None)
            if shared_settings is not None:
                shared_settings.initial_shared_parameter_values.clear()
                shared_settings.initial_shared_parameter_values.update(shared_values)
                for name, value in shared_values.items():
                    if not name.startswith("deadline:"):
                        try:
                            shared_settings.set_parameter_value({"name": name, "value": value})
                        except Exception:
                            pass
            self.deadline_cloud_widget.refresh(
                job_settings=ui_settings,
                auto_detected_attachments=_get_scene_asset_references(node, evaluate=True),
                attachments=_get_evaluated_asset_references(node, manual_only=True),
            )
            host_reqs = get_saved_host_requirements(node)
            host_widget = getattr(self.deadline_cloud_widget, "host_requirements", None)
            if host_widget is not None and host_reqs is not None:
                host_widget.set_requirements(host_reqs)
        except Exception:
            pass

    @staticmethod
    def _format_frame_value(value) -> str:
        """Render a frame parm value without a trailing ".0" for whole numbers."""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    def _update_frame_edit_enabled_state(self):
        """Start/End/Inc only apply when rendering a range (trange != Render Current Frame)."""
        enabled = self.frame_range_combo.currentIndex() != FrameRange.RENDER_CURRENT_FRAME.value
        for edit in self._frame_edits:
            edit.setEnabled(enabled)

    def _on_frame_range_changed(self, index: int):
        node = self._current_node()
        if _is_hou_node_alive(node):
            node.parm("trange").set(index)
        self._update_frame_edit_enabled_state()

    def _on_frame_value_changed(self, parm_name: str):
        node = self._current_node()
        if not _is_hou_node_alive(node):
            return
        edit = {"f1": self.frame_start_edit, "f2": self.frame_end_edit, "f3": self.frame_inc_edit}[
            parm_name
        ]
        parm = node.parm(parm_name)
        try:
            parm.set(float(edit.text()))
        except ValueError:
            # Reject non-numeric input by restoring the parm's current value.
            edit.setText(self._format_frame_value(parm.eval()))

    def _on_take_changed(self, index: int):
        node = self._current_node()
        if _is_hou_node_alive(node):
            node.parm("take").set(self.take_combo.itemData(index))


def _pump_qt_events() -> None:
    """Persistent event-loop callback: delivers Qt cross-thread signals on each Houdini tick."""
    QCoreApplication.sendPostedEvents(None, 0)


# Module-level flag prevents duplicate registration across panel open/close cycles.
_event_pump_installed: bool = False


def _install_login_event_pump(dialog) -> None:
    """Work around Houdini's event loop not delivering Qt cross-thread signals.

    deadline-cloud's modal dialogs (``DeadlineLoginDialog``, ``SubmitJobProgressDialog``) run
    background tasks on worker threads and deliver results via ``Qt.QueuedConnection`` (posted
    ``QMetaCallEvent``). Houdini's native event loop does not dispatch posted Qt events unless a
    real Qt ``QEventLoop`` is spinning. When these dialogs are shown with ``.show()`` +
    ``.setModal(True)`` (not ``.exec_()``), the signals are posted but never delivered, causing
    the dialogs to hang.

    We install a **persistent** ``hou.ui.addEventLoopCallback`` that runs on every Houdini
    native-loop tick and force-drains Qt's posted-event queue with
    ``QCoreApplication.sendPostedEvents(None, 0)``. This ensures cross-thread signals are
    delivered for ALL deadline-cloud dialogs without needing per-dialog workarounds.

    The login dialog is additionally rewired to show non-modally so Houdini's loop keeps ticking
    (a modal dialog would block the very callback that delivers its success signal).

    The pump is registered at most once per Houdini session (guarded by a module-level flag) so
    repeated panel open/close cycles do not accumulate duplicate callbacks.

    TODO: upstream a proper fix to deadline-cloud so this workaround can be removed.
    """
    global _event_pump_installed  # noqa: PLW0603

    # Imported lazily: this module is imported by unit tests (which have no Houdini/Qt display),
    # and the login dialog is only needed at panel runtime inside Houdini.
    from deadline.client.ui.dialogs.deadline_login_dialog import DeadlineLoginDialog

    # --- Persistent event pump for all cross-thread signal delivery (register once) ---
    if not _event_pump_installed:
        hou.ui.addEventLoopCallback(_pump_qt_events)
        _event_pump_installed = True

    # --- Login-specific workaround: show non-modally so the pump can fire ---
    def _login_with_loop_callback() -> None:
        # Parent to Houdini's main window (SideFX-recommended for modal Qt dialogs) rather than the
        # embedded panel widget. Show NON-MODALLY so Houdini's native event loop keeps ticking --
        # a modal dialog would block that loop and starve the _pump_qt_events callback above,
        # which is exactly what must run to deliver the success signal and close it.
        login_dialog = DeadlineLoginDialog(parent=hou.qt.mainWindow(), close_on_success=True)
        login_dialog.setWindowModality(Qt.NonModal)
        login_dialog.show()

        def _on_close() -> None:
            # Poll until the dialog closes, then refresh auth status.
            if not login_dialog.isVisible():
                hou.ui.removeEventLoopCallback(_on_close)
                dialog.refresh_deadline_settings()
                dialog.deadline_authentication_status.refresh_status()

        hou.ui.addEventLoopCallback(_on_close)

    login_signal = dialog.auth_status_box.login_clicked
    try:
        login_signal.disconnect(dialog.on_login)
    except (TypeError, RuntimeError):
        # Fall back to clearing all slots if the bound-method match fails; on_login is the only
        # slot the shared dialog connects to this signal.
        login_signal.disconnect()
    login_signal.connect(_login_with_loop_callback)


_active_panel = None


def onNodePathChanged(node):
    """Houdini Python Panel callback invoked when the Parameter Pane's target node changes."""
    if _active_panel is not None and node is not None:
        _active_panel.set_node(node)


def onActivateInterface():
    """Houdini Python Panel callback invoked when the cached interface becomes active again."""
    if _active_panel is None:
        return
    try:
        pane_tab = kwargs.get("paneTab")  # type: ignore # noqa:F821
    except NameError:
        pane_tab = None
    if pane_tab is not None:
        _active_panel.pane_tab = pane_tab
        if _active_panel.deadline_cloud_widget is not None:
            _active_panel.deadline_cloud_widget._deadline_pane_tab = pane_tab
        try:
            current = pane_tab.currentNode()
        except Exception:
            current = None
        if current is not None:
            _active_panel.set_node(current)


def onDestroyInterface():
    """Houdini Python Panel callback invoked when the panel tab is closed."""
    global _active_panel  # noqa: PLW0603
    _active_panel = None


def onCreateInterface():
    global _active_panel  # noqa: PLW0603

    # Convention for how Houdini references nodes in its Python panels; we can't include **kwargs in the function signature.
    # See https://www.sidefx.com/docs/houdini/ref/windows/pythonpaneleditor.html#interfaces-tab for more information.
    # Ignore this as to not set off the linter.
    pane_tab = kwargs["paneTab"]  # type: ignore # noqa:F821
    n = pane_tab.currentNode()

    ui_settings = read_ui_settings_from_node(n)
    # Seed the shared queue-parameter values with Conda/Rez packages pinned to the running
    # Houdini + adaptor version. The shared dialog applies these as overrides on top of the
    # queue's own (non-version-pinned) defaults. Pre-GUI hooks below can still override them.
    # ODS G6: seed from the node's saved queue parameters, defaults only where unset.
    shared_parameter_values: dict = get_shared_parameter_values_for_dialog(n)

    # Run pre-GUI hooks so studios can pre-populate dialog fields before it opens. Houdini has no
    # on-disk job bundle at this point, so hooks are sourced from DEADLINE_HOOKS_DIR only
    # (bundle_dir=None), gated by settings.allow_environment_hooks. The confirmation prompt is
    # skipped when auto_accept is set; otherwise the standard dialog is shown.
    try:
        pre_gui_output = run_pre_gui_hooks(
            PreGuiHookContext(
                bundle_dir=None,
                job_name=ui_settings.name,
                submitter_name="houdini",
                parameters=dict(shared_parameter_values),
            ),
            confirm_callback=_pre_gui_hook_confirm_callback(hou.qt.mainWindow()),
        )
    except DeadlineOperationCanceled:
        # The user declined the hook confirmation prompt. That is a deliberate cancellation, not
        # an error: skip the hooks and open the submitter with its default settings. onCreateInterface
        # must still return a panel (Houdini calls it to build the Python panel), and there is no
        # outer gui_error_handler here, so without this the exception would propagate as a raw
        # traceback and the panel would fail to open.
        pre_gui_output = {}
    # run_pre_gui_hooks returns {} when no hooks run; `or {}` is defensive against any future
    # contract change so the common no-hooks path can never pass a falsy value into
    # apply_pre_gui_output.
    apply_pre_gui_output(pre_gui_output or {}, ui_settings, shared_parameter_values)

    widget = SubmitJobToDeadlineDialog(
        job_setup_widget_type=SceneSettingsWidget,
        initial_job_settings=ui_settings,
        initial_shared_parameter_values=shared_parameter_values,
        auto_detected_attachments=_get_scene_asset_references(n, evaluate=True),
        attachments=_get_evaluated_asset_references(n, manual_only=True),
        on_create_job_bundle_callback=_make_create_bundle_callback(n),
        # submitter_info replaces submitter_name as of deadline-cloud 0.54.0:
        # https://github.com/aws-deadline/deadline-cloud/releases/tag/0.54.0
        submitter_info=SubmitterInfo(
            submitter_name="Houdini",
            submitter_package_name="deadline-cloud-for-houdini",
            submitter_package_version=houdini_submitter_version,
            host_application_name="Houdini",
            host_application_version=hou.applicationVersionString(),
        ),
        f=Qt.Tool,
        show_host_requirements_tab=True,
        # ODS: restore the Host requirements tab from the node (persisted on submit/export).
        host_requirements=get_saved_host_requirements(n),
        use_deadline_cloud_v2_channel=True,
    )

    # The shared dialog closes itself after a successful submission
    # (SubmitJobToDeadlineDialog._close_event_receiver). That's correct for a standalone modal
    # dialog, but here it is embedded in a persistent Houdini Python Panel, so self-closing blanks
    # the panel. Neutralize the receiver on this instance so the panel stays open for repeat
    # submissions. This depends on a semi-private method; TODO: upstream a proper "stay open" option.
    widget._close_event_receiver = lambda: None  # type: ignore[method-assign]

    # Run the pre-submission checks (locked ROPs / file parse / unsaved-scene save) when Submit is
    # clicked, before the dialog opens its modal progress dialog. Without this the panel would
    # submit without ever prompting the user (the shared dialog only invokes the job-bundle callback
    # after the progress dialog is already shown, which suppresses the prompts).
    _install_pre_submission_gate(widget, n)
    _install_queue_parameter_sync(widget, n)

    # Work around the Houdini login modal that never auto-closes on success (see helper docstring).
    _install_login_event_pump(widget)

    panel = SubmitterPanel(
        parent=None, deadline_cloud_widget=widget, node=n, pane_tab=pane_tab
    )
    _active_panel = panel

    return panel
