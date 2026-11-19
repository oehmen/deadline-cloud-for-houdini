# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import pytest

from . import shared_callback_tests

from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    update_queue_parameters_callback,
)


@pytest.mark.parametrize("empty_farm_id", [None, ""])
def test_no_selected_farm_id(empty_farm_id, mock_api):
    shared_callback_tests.no_selected_farm_id_test(
        empty_farm_id, mock_api, update_queue_parameters_callback
    )


@pytest.mark.parametrize("empty_queue_id", [None, ""])
def test_no_selected_queue_id(empty_queue_id, mock_api):
    shared_callback_tests.no_selected_queue_id_test(
        empty_queue_id, mock_api, update_queue_parameters_callback
    )


def test_farm_and_queue_id_selected(mock_api):
    shared_callback_tests.farm_and_queue_id_selected_test(
        mock_api, update_queue_parameters_callback
    )


@pytest.mark.parametrize(
    ("saved_conda", "expected_conda"),
    [
        (
            "houdini=21.0.729 houdini-husk-arnold=6.5.3.1 custom-usd-resolver=21.0.729 houdini-openjd=0.7.14=ods2*",
            "houdini=21.0.729 houdini-husk-arnold=6.5.3.1 custom-usd-resolver=21.0.729 houdini-openjd=0.7.14=ods2*",
        ),
        (
            "houdini=21.0.729=hb0f4dca* houdini-openjd=0.7.*",
            "houdini=21.0.729=hb0f4dca* houdini-openjd=0.7.*",
        ),
        (
            "houdini=20.5.* houdini-openjd=0.7.*",
            "houdini=21.0.* houdini-openjd=0.7.*",
        ),
        (
            "houdini=21.0.* houdini-openjd=0.7.*",
            "houdini=21.0.* houdini-openjd=0.7.*",
        ),
    ],
)
def test_restore_queue_parameter_values_preserves_explicit_houdini_pin(
    saved_conda: str, expected_conda: str
):
    from unittest import mock
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini import queue_parameters

    node = mock.MagicMock()
    conda_parm = mock.MagicMock()
    node.parm.side_effect = (
        lambda name: conda_parm if name == "queue_env_do_not_use_CondaPackages" else None
    )
    with mock.patch.object(queue_parameters.hou, "applicationVersionString", return_value="21.0.729"):
        queue_parameters._restore_queue_parameter_values(node, {"CondaPackages": saved_conda})

    conda_parm.set.assert_called_once_with(expected_conda)


@pytest.mark.parametrize(
    ("param_def", "expected_default"),
    [
        (
            {"name": "CondaPackages", "type": "STRING", "default": ""},
            ("houdini=21.0.* houdini-openjd=0.7.*",),
        ),
        (
            {
                "name": "CondaPackages",
                "type": "STRING",
                "default": "houdini=21.0.729 houdini-husk-arnold=6.5.3.1 custom-usd-resolver=21.0.729 houdini-openjd=0.7.14=ods2*",
            },
            (
                "houdini=21.0.729 houdini-husk-arnold=6.5.3.1 custom-usd-resolver=21.0.729 houdini-openjd=0.7.14=ods2*",
            ),
        ),
        (
            {"name": "RezPackages", "type": "STRING", "default": "   "},
            ("houdini-21.0 deadline_cloud_for_houdini",),
        ),
        (
            {"name": "RezPackages", "type": "STRING", "default": "houdini-21.0.729 custom_plugin"},
            ("houdini-21.0.729 custom_plugin",),
        ),
        (
            {"name": "CondaChannels", "type": "STRING", "default": "s3://custom-bucket/Conda/Default"},
            ("s3://custom-bucket/Conda/Default",),
        ),
    ],
)
def test_get_default_value_honors_non_empty_queue_defaults(param_def, expected_default):
    from unittest import mock
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini import queue_parameters

    with (
        mock.patch.object(queue_parameters.hou, "applicationVersionString", return_value="21.0.729"),
        mock.patch.object(queue_parameters, "adaptor_version_tuple", (0, 7, 14)),
    ):
        assert queue_parameters._get_default_value(param_def) == expected_default


def test_update_queue_parameters_preserve_existing_false_skips_restore():
    from unittest import mock
    from deadline.houdini_submitter.python.deadline_cloud_for_houdini import queue_parameters

    node = mock.MagicMock()
    defs = [{"name": "CondaPackages", "type": "STRING", "default": "houdini=21.0.729"}]

    with (
        mock.patch.object(
            queue_parameters, "get_queue_parameter_definitions_from_service", return_value=defs
        ),
        mock.patch.object(queue_parameters, "remove_queue_parameters_from_node"),
        mock.patch.object(queue_parameters, "_rebuild_queue_parameters_ui"),
        mock.patch.object(queue_parameters, "_restore_queue_parameter_values") as mock_restore,
    ):
        queue_parameters.update_queue_parameters(
            "farm-new", "queue-new", node, preserve_existing=False
        )

    mock_restore.assert_not_called()
    node.setUserData.assert_any_call("deadline_farm_id", "farm-new")
    node.setUserData.assert_any_call("deadline_queue_id", "queue-new")
