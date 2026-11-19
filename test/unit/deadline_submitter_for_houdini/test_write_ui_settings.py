# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from unittest.mock import Mock, patch

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.hip_settings import (
    HoudiniSubmitterUISettings,
)
from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    write_ui_settings_to_node,
    _make_create_bundle_callback,
)


def _mock_node_with_parms():
    """Return a node whose .parm(name) yields a distinct, cached Mock per parameter name."""
    node = Mock()
    parms: dict = {}

    def get_parm(name):
        if name not in parms:
            parms[name] = Mock()
        return parms[name]

    node.parm = get_parm
    return node, parms


def test_write_ui_settings_to_node_writes_expected_parms():
    node, parms = _mock_node_with_parms()
    settings = HoudiniSubmitterUISettings(
        name="MyJob",
        description="a description",
        priority=75,
        initial_status="SUSPENDED",
        max_failed_tasks_count=3,
        max_retries_per_task=2,
        separate_steps=True,
        include_adaptor_wheels=True,
        adaptor_wheels_dir="/wheels",
        auto_unlock_rops=True,
        auto_parse_hip=False,
        auto_save_hip=True,
    )

    write_ui_settings_to_node(node, settings)

    parms["name"].set.assert_called_once_with("MyJob")
    parms["description"].set.assert_called_once_with("a description")
    parms["priority"].set.assert_called_once_with(75)
    # max_failed_tasks_count / max_retries_per_task map to the node's differently-named parms.
    parms["initial_status"].set.assert_called_once_with("SUSPENDED")
    parms["failed_tasks_limit"].set.assert_called_once_with(3)
    parms["task_retry_limit"].set.assert_called_once_with(2)
    # Toggles are written as ints.
    parms["separate_steps"].set.assert_called_once_with(1)
    parms["auto_unlock_rops"].set.assert_called_once_with(1)
    parms["auto_parse_hip"].set.assert_called_once_with(0)
    parms["auto_save_hip"].set.assert_called_once_with(1)
    parms["include_adaptor_wheels"].set.assert_called_once_with(1)
    parms["adaptor_wheels"].set.assert_called_once_with("/wheels")


def test_write_ui_settings_to_node_does_not_write_frame_or_take_parms():
    """Frame range/take are owned by the panel's frame controls and must not be clobbered."""
    node, parms = _mock_node_with_parms()

    write_ui_settings_to_node(node, HoudiniSubmitterUISettings())

    assert "trange" not in parms
    assert "f1" not in parms
    assert "f2" not in parms
    assert "f3" not in parms
    assert "take" not in parms


def test_write_ui_settings_to_node_defaults_empty_adaptor_wheels_dir():
    node, parms = _mock_node_with_parms()

    write_ui_settings_to_node(
        node, HoudiniSubmitterUISettings(include_adaptor_wheels=False, adaptor_wheels_dir=None)
    )

    parms["adaptor_wheels"].set.assert_called_once_with("")


@patch("deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._create_job_bundle")
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.set_queue_parameter_values_from_openjd"
)
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.write_ui_settings_to_node"
)
def test_create_bundle_callback_persists_before_bundling(
    mock_write, mock_set_queue_params, mock_create_bundle
):
    node = Mock()
    callback = _make_create_bundle_callback(node)

    settings = Mock()
    queue_parameters = [{"name": "CondaPackages", "value": "houdini=21.0.*"}]
    asset_references = Mock()

    result = callback(
        Mock(),  # widget
        "/tmp/bundle",
        settings,
        queue_parameters,
        asset_references,
        None,  # host_requirements
        purpose=Mock(),
    )

    mock_write.assert_called_once_with(node, settings)
    mock_set_queue_params.assert_called_once_with(node, queue_parameters)
    mock_create_bundle.assert_called_once_with(node, "/tmp/bundle", asset_references, None)
    assert result is None


@patch("deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._create_job_bundle")
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.set_queue_parameter_values_from_openjd"
)
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.write_ui_settings_to_node"
)
def test_create_bundle_callback_forwards_host_requirements(
    mock_write, mock_set_queue_params, mock_create_bundle
):
    node = Mock()
    callback = _make_create_bundle_callback(node)

    settings = Mock()
    queue_parameters = [{"name": "CondaPackages", "value": "houdini=21.0.*"}]
    asset_references = Mock()
    host_requirements = {"amounts": [{"name": "amount.worker.vcpu", "min": 8}]}

    result = callback(
        Mock(),  # widget
        "/tmp/bundle",
        settings,
        queue_parameters,
        asset_references,
        host_requirements,
        purpose=Mock(),
    )

    # Host requirements from the dialog must be forwarded to the bundle builder so they
    # end up in the generated template rather than being silently dropped.
    mock_create_bundle.assert_called_once_with(
        node, "/tmp/bundle", asset_references, host_requirements
    )
    assert result is None


@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._run_pre_submission_checks"
)
def test_pre_submission_gate_runs_checks_before_on_submit(mock_checks):
    """The gate must run the checks (with the dialog's current toggles) then call on_submit."""
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
        _install_pre_submission_gate,
    )

    order = []
    mock_checks.side_effect = lambda node, **kwargs: order.append("checks")

    dialog = Mock()
    dialog.on_submit.side_effect = lambda: order.append("submit")
    # The Scene Settings tab's current (unwritten) checkbox state.
    dialog.job_settings.auto_unlock_rops_check.isChecked.return_value = False
    dialog.job_settings.auto_parse_hip_check.isChecked.return_value = True
    dialog.job_settings.auto_save_hip_check.isChecked.return_value = False
    node = Mock()

    _install_pre_submission_gate(dialog, node)

    # The default Submit->on_submit wiring is removed and replaced with the guarded wrapper.
    dialog.submit_button.clicked.disconnect.assert_called_once_with(dialog.on_submit)
    guarded_submit = dialog.submit_button.clicked.connect.call_args.args[0]

    # Simulate the user clicking Submit.
    guarded_submit()

    # Checks must be run with the dialog's CURRENT toggle values (not read from the node).
    mock_checks.assert_called_once_with(
        node, auto_unlock_rops=False, auto_parse_hip=True, auto_save_hip=False
    )
    dialog.on_submit.assert_called_once_with()
    # Checks must happen strictly before the submission proceeds.
    assert order == ["checks", "submit"]


@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._run_pre_submission_checks"
)
def test_pre_submission_gate_aborts_submit_on_cancel(mock_checks):
    """If the checks raise UserInitiatedCancel, on_submit must NOT be called."""
    from deadline.client.exceptions import UserInitiatedCancel
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
        _install_pre_submission_gate,
    )

    mock_checks.side_effect = UserInitiatedCancel("Submission canceled.")

    dialog = Mock()
    dialog.job_settings.auto_unlock_rops_check.isChecked.return_value = False
    dialog.job_settings.auto_parse_hip_check.isChecked.return_value = False
    dialog.job_settings.auto_save_hip_check.isChecked.return_value = False
    node = Mock()

    _install_pre_submission_gate(dialog, node)
    guarded_submit = dialog.submit_button.clicked.connect.call_args.args[0]

    # Clicking Submit runs the checks; the cancel must abort without submitting or raising.
    guarded_submit()

    mock_checks.assert_called_once()
    dialog.on_submit.assert_not_called()


def test_pre_submission_checks_honor_auto_save_override_over_node_parm():
    """The unsaved-changes prompt must respect the dialog's current auto_save choice.

    Regression test: the node's ``auto_save_hip`` parm is 1 (would save silently), but the user
    unchecked "Automatically save scene" in the dialog. Passing ``auto_save_hip=False`` must make
    the check prompt instead of silently saving.
    """
    from .mock_hou import hou_module as hou
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini import submitter

    node = Mock()
    node.inputAncestors.return_value = [Mock()]
    # Node parm still says "auto save on" (stale).
    node.parm.return_value.eval.return_value = 1

    hou.ui.displayMessage.reset_mock()
    hou.ui.displayMessage.return_value = 2  # "Ignore"
    hou.hipFile.hasUnsavedChanges.return_value = True

    asset_refs = Mock()
    asset_refs.input_filenames = {"/scene.hip"}

    with (
        patch.object(submitter, "_get_evaluated_asset_references", return_value=asset_refs),
        patch.object(submitter, "_is_node_locked", return_value=False),
        patch.object(submitter, "_follow_fetch_nodes", side_effect=lambda n: n),
        patch.object(submitter, "_get_hip_file", return_value="/scene.hip"),
    ):
        submitter._run_pre_submission_checks(node, auto_save_hip=False)

    messages = [c.args[0] for c in hou.ui.displayMessage.call_args_list]
    assert any("unsaved changes" in str(m) for m in messages), messages


def test_queue_parameter_sync_resets_to_new_queue_defaults_on_switch():
    """Switching farm/queue in the dialog must clear old overrides and apply the new queue defaults."""
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini import (
        queue_parameters,
        submitter,
    )

    node, parms = _mock_node_with_parms()
    node.userData.return_value = None

    dialog = Mock()
    shared_settings = Mock()
    shared_settings.initial_shared_parameter_values = {
        "CondaPackages": "houdini=21.0.* houdini-openjd=0.7.*",
        "CondaChannels": "deadline-cloud",
    }
    observed_defs = []

    def fake_orig_handler(defs):
        for p in defs:
            if p["name"] in shared_settings.initial_shared_parameter_values:
                p["value"] = shared_settings.initial_shared_parameter_values[p["name"]]
        observed_defs.extend(defs)

    shared_settings._handle_queue_parameters_update = fake_orig_handler
    shared_settings.get_parameters.side_effect = lambda: [
        {"name": p["name"], "value": p.get("value", p.get("default", ""))} for p in observed_defs
    ]
    shared_settings.deadline_cloud_settings_box.farm_box.box.currentText.return_value = (
        "(eu-central-1) staging-farm"
    )
    shared_settings.deadline_cloud_settings_box.queue_box.box.currentText.return_value = (
        "staging-queue"
    )
    dialog.shared_job_settings = shared_settings

    active_settings = {
        "defaults.farm_id": "farm-old",
        "defaults.queue_id": "queue-old",
    }

    with (
        patch.object(submitter, "get_setting", side_effect=lambda k: active_settings.get(k, "")),
        patch.object(queue_parameters.hou, "applicationVersionString", return_value="21.0.729"),
        patch.object(queue_parameters, "adaptor_version_tuple", (0, 7, 14)),
        patch.object(submitter, "apply_queue_parameter_definitions_to_node") as mock_apply_defs,
        patch.object(submitter, "set_queue_parameter_values_from_openjd") as mock_set_vals,
    ):
        submitter._install_queue_parameter_sync(dialog, node)

        # Simulate switching to a new farm/queue whose Conda environment defines explicit defaults.
        active_settings["defaults.farm_id"] = "farm-staging"
        active_settings["defaults.queue_id"] = "queue-staging"
        new_queue_defs = [
            {
                "name": "CondaPackages",
                "type": "STRING",
                "default": "houdini=21.0.729 houdini-husk-arnold=6.5.3.1 custom-usd-resolver=21.0.729 houdini-openjd=0.7.14=ods2*",
            },
            {
                "name": "CondaChannels",
                "type": "STRING",
                "default": "s3://staging-bucket/Conda/Default",
            },
        ]
        shared_settings._handle_queue_parameters_update(new_queue_defs)

    assert shared_settings.initial_shared_parameter_values == {}
    by_name = {p["name"]: p for p in observed_defs}
    assert "value" not in by_name["CondaPackages"]
    assert (
        by_name["CondaPackages"]["default"]
        == "houdini=21.0.729 houdini-husk-arnold=6.5.3.1 custom-usd-resolver=21.0.729 houdini-openjd=0.7.14=ods2*"
    )
    assert by_name["CondaChannels"]["default"] == "s3://staging-bucket/Conda/Default"
    mock_apply_defs.assert_called_once()
    assert mock_apply_defs.call_args.kwargs["farm_id"] == "farm-staging"
    assert mock_apply_defs.call_args.kwargs["queue_id"] == "queue-staging"
    assert mock_apply_defs.call_args.kwargs["preserve_existing"] is False
    mock_set_vals.assert_called_once()
    parms["farm"].set.assert_called_once_with("(eu-central-1) staging-farm")
    parms["queue"].set.assert_called_once_with("staging-queue")


@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._run_pre_submission_checks"
)
def test_pre_submission_gate_recovers_when_original_node_was_deleted(mock_checks):
    """If the original ROP node was deleted and recreated, _guarded_submit must resolve the new live node."""
    from .mock_hou import hou_module as hou
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini import submitter

    dead_node = Mock()
    dead_node.path.return_value = "/stage/ropnet1/deadline_cloud1"

    live_node = Mock()
    live_node.path.return_value = "/stage/ropnet1/deadline_cloud1"
    live_node.type.return_value.name.return_value = "deadline_cloud"

    dialog = Mock()
    dialog.job_settings.auto_unlock_rops_check.isChecked.return_value = False
    dialog.job_settings.auto_parse_hip_check.isChecked.return_value = False
    dialog.job_settings.auto_save_hip_check.isChecked.return_value = False

    submitter._install_pre_submission_gate(dialog, dead_node)
    guarded_submit = dialog.submit_button.clicked.connect.call_args.args[0]

    # Simulate deleting dead_node after the panel was created, and dropping live_node at the same path.
    dead_node.path.side_effect = RuntimeError("ObjectWasDeleted")
    with patch.object(
        hou,
        "node",
        side_effect=lambda p: live_node if p == "/stage/ropnet1/deadline_cloud1" else None,
    ):
        guarded_submit()

    mock_checks.assert_called_once_with(
        live_node, auto_unlock_rops=False, auto_parse_hip=False, auto_save_hip=False
    )
    dialog.on_submit.assert_called_once_with()


@patch("deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._create_job_bundle")
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.set_queue_parameter_values_from_openjd"
)
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.write_ui_settings_to_node"
)
def test_create_bundle_callback_uses_pane_tab_current_node_when_original_deleted(
    mock_write, mock_set_queue_params, mock_create_bundle
):
    """When the Python Panel's paneTab points to a newly dropped ROP node, bundle creation must use it."""
    dead_node = Mock()
    dead_node.path.return_value = "/stage/ropnet1/deadline_cloud1"

    new_node = Mock()
    new_node.path.return_value = "/stage/ropnet1/deadline_cloud2"
    new_node.type.return_value.name.return_value = "deadline_cloud"

    callback = _make_create_bundle_callback(dead_node)
    dead_node.path.side_effect = RuntimeError("ObjectWasDeleted")

    pane_tab = Mock()
    pane_tab.currentNode.return_value = new_node
    widget = Mock()
    widget._deadline_pane_tab = pane_tab

    settings = Mock()
    queue_parameters = [{"name": "CondaPackages", "value": "houdini=21.0.729"}]
    asset_references = Mock()

    callback(
        widget,
        "/tmp/bundle",
        settings,
        queue_parameters,
        asset_references,
        None,
        purpose=Mock(),
    )

    mock_write.assert_called_once_with(new_node, settings)
    mock_set_queue_params.assert_called_once_with(new_node, queue_parameters)
    mock_create_bundle.assert_called_once_with(new_node, "/tmp/bundle", asset_references, None)


@patch("deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._create_job_bundle")
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.set_queue_parameter_values_from_openjd"
)
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.write_ui_settings_to_node"
)
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._get_scene_asset_references"
)
def test_create_bundle_callback_refreshes_stale_auto_attachments_and_keeps_manual(
    mock_get_scene_refs, mock_write, mock_set_queue_params, mock_create_bundle
):
    """After File -> Save As while the panel stays open, bundle creation must drop stale
    auto-detected files (e.g. old v019.hip) and keep user-added manual attachments."""
    from types import SimpleNamespace
    from deadline.client.job_bundle.submission import AssetReferences

    rop_node = Mock()
    rop_node.path.return_value = "/stage/ropnet1/deadline_cloud1"
    rop_node.type.return_value.name.return_value = "deadline_cloud"

    stale_auto = AssetReferences(
        input_filenames={"/proj/scene_v019.hip", "/proj/galleries/scene_v019.db"},
        output_directories=set(),
    )
    user_manual = AssetReferences(
        input_filenames={"/proj/manual_extra.exr"},
        input_directories={"/proj/manual_dir"},
    )
    fresh_auto = AssetReferences(
        input_filenames={"/proj/scene_v022.hip", "/proj/usd/asset_v002.usd"},
        output_directories={"/proj/render/beauty"},
    )
    mock_get_scene_refs.return_value = fresh_auto

    refreshed_calls = []

    class FakeJobAttachmentsWidget:
        def __init__(self):
            self.auto_detected_attachments = stale_auto
            self.attachments = user_manual

        def refresh_ui(self, auto_detected, manual):
            refreshed_calls.append((auto_detected, manual))
            if auto_detected:
                self.auto_detected_attachments = auto_detected

    widget = SimpleNamespace(job_attachments=FakeJobAttachmentsWidget())
    passed_refs = stale_auto.union(user_manual)

    callback = _make_create_bundle_callback(rop_node)
    callback(widget, "/tmp/bundle", Mock(), [], passed_refs, None, purpose=Mock())

    mock_get_scene_refs.assert_called_once_with(rop_node, evaluate=True)
    assert passed_refs.input_filenames == {
        "/proj/scene_v022.hip",
        "/proj/usd/asset_v002.usd",
        "/proj/manual_extra.exr",
    }
    assert "/proj/scene_v019.hip" not in passed_refs.input_filenames
    assert "/proj/galleries/scene_v019.db" not in passed_refs.input_filenames
    assert passed_refs.input_directories == {"/proj/manual_dir"}
    assert passed_refs.output_directories == {"/proj/render/beauty"}
    assert widget.job_attachments.auto_detected_attachments == fresh_auto
    assert refreshed_calls == [(fresh_auto, None)]
    mock_create_bundle.assert_called_once_with(rop_node, "/tmp/bundle", passed_refs, None)


@patch("deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._create_job_bundle")
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.set_queue_parameter_values_from_openjd"
)
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter.write_ui_settings_to_node"
)
@patch(
    "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._get_scene_asset_references",
    side_effect=RuntimeError("stage cook failed"),
)
def test_create_bundle_callback_keeps_existing_attachments_if_refresh_fails(
    mock_get_scene_refs, mock_write, mock_set_queue_params, mock_create_bundle
):
    """If re-scanning the scene raises, keep the existing attachments and still build the bundle."""
    from types import SimpleNamespace
    from deadline.client.job_bundle.submission import AssetReferences

    rop_node = Mock()
    rop_node.path.return_value = "/stage/ropnet1/deadline_cloud1"
    rop_node.type.return_value.name.return_value = "deadline_cloud"

    existing = AssetReferences(
        input_filenames={"/proj/scene_v021.hip"},
        output_directories={"/proj/render"},
    )
    widget = SimpleNamespace(
        job_attachments=SimpleNamespace(
            auto_detected_attachments=existing,
            attachments=AssetReferences(),
        )
    )

    callback = _make_create_bundle_callback(rop_node)
    callback(widget, "/tmp/bundle", Mock(), [], existing, None, purpose=Mock())

    assert existing.input_filenames == {"/proj/scene_v021.hip"}
    assert existing.output_directories == {"/proj/render"}
    mock_create_bundle.assert_called_once_with(rop_node, "/tmp/bundle", existing, None)
