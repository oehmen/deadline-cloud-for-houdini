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
