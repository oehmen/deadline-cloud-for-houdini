# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from unittest.mock import Mock, patch
import pytest
from deadline.client.exceptions import NonValidInputError
from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    _get_job_template,
    _ods_describe_empty_steps,
    RenderStrategy,
)


@pytest.fixture
def mock_parm():
    def mock_implementation(name: str):
        if name == "name":
            mock_name = Mock()
            mock_name.evalAsString.return_value = "My Job"
            return mock_name
        if name == "include_adaptor_wheels":
            mock_name = Mock()
            mock_name.eval.return_value = False
            return mock_name
        if name == "description":
            mock_name = Mock()
            mock_name.evalAsString.return_value = "My job description"
            return mock_name
        if name == "ods_use_husk":
            # A bare Mock().eval() is truthy and would switch the template to the husk path.
            mock_name = Mock()
            mock_name.eval.return_value = 0
            return mock_name
        return Mock()

    return mock_implementation


@pytest.fixture(autouse=True)
def init_mocks():
    with (
        patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._get_houdini_version",
            Mock(return_value="19.5.435"),
        ),
        patch(
            "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._get_hip_file",
            Mock(return_value="/path/to/hip.hip"),
        ),
    ):
        yield


@pytest.fixture()
def mock_get_steps():
    with patch(
        "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._get_steps"
    ) as mock:
        yield mock


def test_job_template(mock_get_steps, mock_parm):
    mock_node = Mock()
    mock_node.userData.return_value = None
    mock_node.name = "Test Job"
    mock_node.parm = mock_parm

    mock_get_steps.return_value = [
        {
            "id": "1",
            "name": "/mantra-1",
            "dependency_ids": [],
            "rop": "/mantra",
            "wedgenum": "",
            "wedge_node": "",
            "start": 1,
            "end": 5,
            "step": 1,
            "render_strategy": RenderStrategy.PARALLEL,
        }
    ]
    template = _get_job_template(mock_node)

    assert template == {
        "specificationVersion": "jobtemplate-2023-09",
        "name": "My Job",
        "description": "My job description",
        "parameterDefinitions": [
            {
                "name": "HipFile",
                "type": "PATH",
                "objectType": "FILE",
                "dataFlow": "IN",
                "default": "/path/to/hip.hip",
            }
        ],
        "steps": [
            {
                "name": "/mantra-1",
                "parameterSpace": {
                    "taskParameterDefinitions": [{"name": "Frame", "range": "1-5:1", "type": "INT"}]
                },
                "stepEnvironments": [
                    {
                        "name": "Houdini",
                        "description": "Runs Houdini in the background.",
                        "script": {
                            "embeddedFiles": [
                                {
                                    "name": "initData",
                                    "filename": "init-data.yaml",
                                    "type": "TEXT",
                                    "data": "ignore_input_nodes: true\nrender_node: /mantra\nscene_file: '{{Param.HipFile}}'\nversion: 19.5.435\nwedge_node: ''\nwedgenum: ''\n",
                                }
                            ],
                            "actions": {
                                "onEnter": {
                                    "command": "houdini-openjd",
                                    "args": [
                                        "daemon",
                                        "start",
                                        "--path-mapping-rules",
                                        "file://{{Session.PathMappingRulesFile}}",
                                        "--connection-file",
                                        "{{ Session.WorkingDirectory }}/connection.json",
                                        "--init-data",
                                        "file://{{ Env.File.initData }}",
                                    ],
                                    "cancelation": {"mode": "NOTIFY_THEN_TERMINATE"},
                                    "timeout": 400,
                                },
                                "onExit": {
                                    "command": "houdini-openjd",
                                    "args": [
                                        "daemon",
                                        "stop",
                                        "--connection-file",
                                        "{{ Session.WorkingDirectory }}/connection.json",
                                    ],
                                    "cancelation": {"mode": "NOTIFY_THEN_TERMINATE"},
                                    "timeout": 120,
                                },
                            },
                        },
                    }
                ],
                "script": {
                    "embeddedFiles": [
                        {
                            "name": "runData",
                            "filename": "run-data.yaml",
                            "type": "TEXT",
                            "data": "frame_range:\n  end: {{Task.Param.Frame}}\n  start: {{Task.Param.Frame}}\n  step: 1\nignore_input_nodes: true\nrender_node: /mantra\n",
                        }
                    ],
                    "actions": {
                        "onRun": {
                            "command": "houdini-openjd",
                            "args": [
                                "daemon",
                                "run",
                                "--connection-file",
                                "{{ Session.WorkingDirectory }}/connection.json",
                                "--run-data",
                                "file://{{ Task.File.runData }}",
                            ],
                            "cancelation": {"mode": "NOTIFY_THEN_TERMINATE"},
                        }
                    },
                },
            }
        ],
    }


def test_job_template_applies_host_requirements_to_all_steps(mock_get_steps, mock_parm):
    mock_node = Mock()
    mock_node.userData.return_value = None
    mock_node.name = "Test Job"
    mock_node.parm = mock_parm

    mock_get_steps.return_value = [
        {
            "id": "1",
            "name": "/mantra-1",
            "dependency_ids": [],
            "rop": "/mantra",
            "wedgenum": "",
            "wedge_node": "",
            "start": 1,
            "end": 5,
            "step": 1,
            "render_strategy": RenderStrategy.PARALLEL,
        },
        {
            "id": "2",
            "name": "/geo-1",
            "dependency_ids": [],
            "rop": "/geo",
            "wedgenum": "",
            "wedge_node": "",
            "start": 1,
            "end": 5,
            "step": 1,
            "render_strategy": RenderStrategy.SEQUENTIAL,
        },
    ]

    host_requirements = {
        "amounts": [{"name": "amount.worker.vcpu", "min": 8}],
        "attributes": [{"name": "attr.worker.os.family", "anyOf": ["linux"]}],
    }

    template = _get_job_template(mock_node, host_requirements)

    # Every step gets the same requirements block...
    for step in template["steps"]:
        assert step["hostRequirements"] == host_requirements

    # ...but as an independent deep copy, not a shared reference (so later mutation of one
    # step cannot leak into another or back into the caller's dict).
    assert template["steps"][0]["hostRequirements"] is not host_requirements
    assert template["steps"][0]["hostRequirements"] is not template["steps"][1]["hostRequirements"]


def test_job_template_omits_host_requirements_when_not_provided(mock_get_steps, mock_parm):
    mock_node = Mock()
    mock_node.userData.return_value = None
    mock_node.name = "Test Job"
    mock_node.parm = mock_parm

    mock_get_steps.return_value = [
        {
            "id": "1",
            "name": "/mantra-1",
            "dependency_ids": [],
            "rop": "/mantra",
            "wedgenum": "",
            "wedge_node": "",
            "start": 1,
            "end": 5,
            "step": 1,
            "render_strategy": RenderStrategy.PARALLEL,
        }
    ]

    template = _get_job_template(mock_node)

    assert "hostRequirements" not in template["steps"][0]


def test_job_template_sequential_node(mock_get_steps, mock_parm):
    mock_node = Mock()
    mock_node.userData.return_value = None
    mock_node.name = "Test Job"
    mock_node.parm = mock_parm

    mock_get_steps.return_value = [
        {
            "id": "1",
            "name": "/geo-1",
            "dependency_ids": [],
            "rop": "/geo",
            "wedgenum": "",
            "wedge_node": "",
            "start": 1,
            "end": 5,
            "step": 1,
            "render_strategy": RenderStrategy.SEQUENTIAL,
        }
    ]
    template = _get_job_template(mock_node)

    assert "parameterSpace" not in template["steps"][0]
    assert (
        template["steps"][0]["script"]["embeddedFiles"][0]["data"]
        == "frame_range:\n  end: 5\n  start: 1\n  step: 1\nignore_input_nodes: true\nrender_node: /geo\n"
    )


def _fake_node(path, type_name, render_node=True, bypassed=False, locked=False):
    from .mock_hou import hou_module as hou

    node = Mock()
    if render_node:
        node.__class__ = hou.RopNode
    node.path.return_value = path
    node.isBypassed.return_value = bypassed
    node.isLocked.return_value = locked
    node.type.return_value.nameWithCategory.return_value = type_name
    return node


def _describe(rop, inputs, follow):
    rop.inputs.return_value = inputs
    with patch(
        "deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter._follow_fetch_nodes",
        side_effect=follow,
    ):
        return _ods_describe_empty_steps(rop)


def test_job_template_raises_before_upload_when_no_steps(mock_get_steps, mock_parm):
    """An empty step list must fail at bundle creation, not later at CreateJob."""
    mock_node = _fake_node("/stage/ropnet1/deadline_cloud1", "Driver/deadline_cloud")
    mock_node.userData.return_value = None
    mock_node.parm = mock_parm
    mock_node.inputs.return_value = []
    mock_get_steps.return_value = []

    with pytest.raises(
        NonValidInputError, match="/stage/ropnet1/deadline_cloud1 has no input ROP connected"
    ):
        _get_job_template(mock_node)


def test_describe_empty_steps_fetch_source_not_a_render_node():
    """The v026 case: fetch1 points at a plain LOP, so render -p lists nothing."""
    rop = _fake_node("/stage/ropnet1/deadline_cloud1", "Driver/deadline_cloud")
    fetch = _fake_node("/stage/ropnet1/fetch1", "Driver/fetch")
    lop = _fake_node(
        "/stage/houdinipreviewprocedurals2", "Lop/houdinipreviewprocedurals", render_node=False
    )

    message = _describe(rop, [fetch], follow=lambda n: lop)

    assert message == (
        "- /stage/ropnet1/fetch1 doesn't point at a render node. "
        "Point it at the render ROP, e.g. a USD Render ROP."
    )


def test_describe_empty_steps_fetch_without_valid_source():
    rop = _fake_node("/out/deadline_cloud1", "Driver/deadline_cloud")
    fetch = _fake_node("/out/fetch1", "Driver/fetch")

    message = _describe(rop, [fetch], follow=lambda n: n)

    assert "Fetch ROP /out/fetch1 has no valid Source" in message


def test_describe_empty_steps_locked_rop_and_bypassed_input():
    rop = _fake_node("/out/deadline_cloud1", "Driver/deadline_cloud", locked=True)
    karma = _fake_node("/out/karma1", "Driver/karma", bypassed=True)

    message = _describe(rop, [karma, None], follow=lambda n: n)

    assert "/out/deadline_cloud1 has its lock flag set" in message
    assert "/out/karma1 is bypassed" in message


def test_describe_empty_steps_no_inputs():
    rop = _fake_node("/out/deadline_cloud1", "Driver/deadline_cloud")

    message = _describe(rop, [], follow=lambda n: n)

    assert "/out/deadline_cloud1 has no input ROP connected" in message


def test_describe_empty_steps_fetch_to_node_without_flags():
    """A Fetch pointing at e.g. /obj/geo1 (an ObjNode, no isBypassed()) must not raise."""
    rop = _fake_node("/out/deadline_cloud1", "Driver/deadline_cloud")
    fetch = _fake_node("/out/fetch1", "Driver/fetch")
    obj = Mock(spec=["path", "type"])
    obj.path.return_value = "/obj/geo1"
    obj.type.return_value.nameWithCategory.return_value = "Object/geo"

    message = _describe(rop, [fetch], follow=lambda n: obj)

    assert message == (
        "- /out/fetch1 doesn't point at a render node. "
        "Point it at the render ROP, e.g. a USD Render ROP."
    )


def test_describe_empty_steps_bypassed_fetch_reports_the_fetch():
    rop = _fake_node("/out/deadline_cloud1", "Driver/deadline_cloud")
    fetch = _fake_node("/out/fetch1", "Driver/fetch", bypassed=True)
    usd = _fake_node("/stage/usdrender_rop1", "Lop/usdrender_rop")

    assert _describe(rop, [fetch], follow=lambda n: usd) == "- /out/fetch1 is bypassed."


def test_describe_empty_steps_locked_fetch_target():
    rop = _fake_node("/out/deadline_cloud1", "Driver/deadline_cloud")
    fetch = _fake_node("/out/fetch1", "Driver/fetch")
    usd = _fake_node("/stage/usdrender_rop1", "Lop/usdrender_rop", locked=True)

    assert (
        _describe(rop, [fetch], follow=lambda n: usd)
        == "- /stage/usdrender_rop1 has its lock flag set."
    )


def test_describe_empty_steps_fallback_when_no_reason_found():
    rop = _fake_node("/out/deadline_cloud1", "Driver/deadline_cloud")
    karma = _fake_node("/out/karma1", "Driver/karma")

    assert _describe(rop, [karma], follow=lambda n: n) == (
        "No render steps found for /out/deadline_cloud1: "
        "the ROPs connected to it render nothing."
    )
