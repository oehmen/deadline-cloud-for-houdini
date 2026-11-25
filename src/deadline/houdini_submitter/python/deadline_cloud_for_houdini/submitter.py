# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

from dataclasses import dataclass
from enum import Enum
import copy
import os
import re
import sys
import time
import yaml
import json
import traceback
from typing import Any, Dict, Optional, cast
from pathlib import Path

from botocore.exceptions import ClientError

from deadline.client.api import BaseSubmitter, BaseSubmitterSettings
from deadline.client.job_bundle._yaml import deadline_yaml_dump
from deadline.client import api
from deadline.client.job_bundle.submission import AssetReferences
from deadline.client.job_bundle import create_job_history_bundle_dir
from deadline.client.config import get_setting
from deadline.client.config.config_file import str2bool
from deadline.client.dataclasses import SubmitterInfo
from deadline.client.exceptions import NonValidInputError

from .queue_parameters import (
    apply_queue_parameter_definitions_to_node,
    get_default_conda_packages,
    get_default_rez_packages,
    get_effective_queue_parameter_default,
    update_queue_parameters,
    get_queue_parameter_values_as_openjd,
    set_queue_parameter_values_from_openjd,
    get_shared_parameter_values_for_dialog,
)
from ._assets import (
    _get_hip_file,
    _get_evaluated_asset_references,
    _get_output_directories,
    _get_scene_asset_references,
    _get_usd_asset_references,
    _normalize_asset_path,
    _parse_files,
)

# For temporary backwards compatibility
from ._assets import (
    _IGNORE_REF_PARMS as IGNORE_REF_PARMS,  # noqa
    _IGNORE_REF_VALUES as IGNORE_REF_VALUES,  # noqa
)
from ._version import version
from .hip_settings import HoudiniSubmitterUISettings
import hou


_NONE_SELECTED_TEXT = "<none selected>"
_REFRESHING_TEXT = "<refreshing>"


class RenderStrategy(Enum):
    SEQUENTIAL = "SEQUENTIAL"
    PARALLEL = "PARALLEL"


@dataclass
class HoudiniSubmitterSettings(BaseSubmitterSettings):
    """Houdini-specific submission settings for the unified :class:`BaseSubmitter`.

    The Houdini submitter derives the job's step graph from the live ROP node
    (via the module-level ``_get_job_template``). Beyond the DCC-agnostic base
    fields (``job_name``, ``description``, ``priority``, ``initial_status``,
    ``max_failed_tasks_count``, ``max_retries_per_task``, the asset-reference
    lists) this carries the ROP node path plus the Houdini-specific
    adaptor-wheels fields, so the build methods can honor consumer edits to
    those values rather than silently re-reading the node.

    Note: the *frame range* is intentionally not a settings-driven input. A
    Houdini job's per-step frames/dependencies are derived from the live ROP
    network via ``hscript render`` (see ``_get_rop_steps``); a single
    ``frame_list`` string cannot represent that graph, so ``get_settings``
    populates ``frame_list`` for information only and the builders take frames
    from the ROP.
    """

    rop_node_path: str = ""
    include_adaptor_wheels: bool = False
    adaptor_wheels: str = ""


class HoudiniSubmitter(BaseSubmitter):
    """Headless :class:`BaseSubmitter` implementation for Houdini.

    This is the unified-API entry point that host-agnostic consumers (e.g. the
    AYON bridge, ``get_submitter_for_host("houdini")``) drive, and the same
    engine the native GUI submitter routes through (see ``_create_job_bundle``).
    ``get_job_template`` reuses the module-level ``_get_job_template`` for the
    ROP-derived step graph and overrides the settings-driven fields; parameter
    values are built from ``settings`` plus the ROP's queue parameters, and
    asset references come from the ``_assets`` scene scan. The submitter
    resolves everything from a single ROP node path; call
    :meth:`set_rop_node_path` (or construct with one) before requesting
    settings/templates.
    """

    def __init__(self, rop_node_path: str = "") -> None:
        self._rop_node_path = rop_node_path

    def set_rop_node_path(self, rop_node_path: str) -> None:
        """Point the submitter at the Deadline Cloud ROP node to submit.

        Lets external callers (e.g. the AYON create/publish plugins) seed the
        node without reaching into the private attribute.
        """
        self._rop_node_path = rop_node_path

    def _get_rop_node(self) -> Optional["hou.Node"]:
        if self._rop_node_path:
            return hou.node(self._rop_node_path)
        return None

    def _require_rop(self) -> "hou.Node":
        rop = self._get_rop_node()
        if rop is None:
            raise RuntimeError(f"Cannot find ROP node at path: {self._rop_node_path}")
        return rop

    def get_settings(self, scan_assets: bool = True) -> HoudiniSubmitterSettings:
        """Collect submission settings from the live scene / ROP node.

        Populates the base-contract fields (job name, frame range, and the
        input/output asset references discovered by walking the scene) so that
        consumers reading them off the returned settings — e.g. the AYON create
        plugin's parameterDefinitions UI and its publish-time job-attachments
        collection — see real values rather than empty defaults. The scene scan
        is the same one the native "Parse Files" button uses.

        ``scan_assets`` (default ``True``, so base-contract callers are
        unchanged) gates the input/output asset-reference scan. Callers that do
        not read ``settings.input_*``/``output_directories`` — notably the GUI
        submit/export path, which writes ``asset_references.yaml`` from the
        dialog-provided references — can pass ``False`` to skip a potentially
        expensive filesystem + USD walk whose output would be discarded.
        """
        settings = HoudiniSubmitterSettings()
        hip_file = _get_hip_file()
        settings.job_name = hou.hipFile.basename() or "Untitled"
        settings.project_path = hou.getenv("HIP", "") or ""
        # Baseline inputs: the hip file only. Overwritten below by the scene scan
        # when it runs and succeeds; otherwise this fallback stands (no ROP, scan
        # skipped, or scan failed).
        settings.input_filenames = [hip_file] if hip_file else []

        # frame_list from the playbar; refined from the ROP's frame-range tuple
        # below when a node is available.
        playbar = hou.playbar.frameRange()
        settings.frame_list = f"{int(playbar[0])}-{int(playbar[1])}"
        settings.output_path = settings.project_path

        rop = self._get_rop_node()
        if rop is not None:
            settings.rop_node_path = rop.path()
            if rop.parm("name"):
                settings.job_name = rop.parm("name").evalAsString() or settings.job_name
            if rop.parm("priority"):
                settings.priority = rop.parm("priority").eval()
            if rop.parm("initial_status"):
                settings.initial_status = rop.parm("initial_status").evalAsString()
            if rop.parm("failed_tasks_limit"):
                settings.max_failed_tasks_count = rop.parm("failed_tasks_limit").eval()
            if rop.parm("task_retry_limit"):
                settings.max_retries_per_task = rop.parm("task_retry_limit").eval()
            if rop.parm("description"):
                settings.description = rop.parm("description").evalAsString()
            if rop.parm("include_adaptor_wheels"):
                settings.include_adaptor_wheels = bool(rop.parm("include_adaptor_wheels").eval())
            if rop.parm("adaptor_wheels"):
                settings.adaptor_wheels = rop.parm("adaptor_wheels").evalAsString()

            frame_range = rop.parmTuple("f")
            if frame_range:
                start = int(frame_range[0].eval())
                end = int(frame_range[1].eval())
                step = int(frame_range[2].eval()) if len(frame_range) > 2 else 1
                settings.frame_list = f"{start}-{end}:{step}" if step != 1 else f"{start}-{end}"

            # Discover the real input/output asset references by scanning the
            # scene (same detection as "Parse Files"), so consumers that read
            # these off the settings get populated directories even when the
            # ROP's Job Attachments multiparms have not been parsed yet. Skipped
            # when scan_assets is False (the caller does not read these fields);
            # on failure the hip-file baseline set above stands.
            if scan_assets:
                try:
                    refs = _get_scene_asset_references(rop, evaluate=True)
                    settings.input_filenames = sorted(refs.input_filenames)
                    settings.input_directories = sorted(refs.input_directories)
                    settings.output_directories = sorted(refs.output_directories)
                    if settings.output_directories:
                        settings.output_path = settings.output_directories[0]
                except Exception:
                    # Scene scan can fail (e.g. missing/locked nodes); keep the
                    # hip-file baseline rather than breaking settings collection.
                    pass

        return settings

    def get_job_template(
        self,
        settings: BaseSubmitterSettings,
        host_requirements: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        houdini_settings = cast(HoudiniSubmitterSettings, settings)
        rop = self._require_rop()

        # The step graph (per-step frames, dependencies, wedges) is derived from
        # the live ROP network by _get_job_template and cannot be reconstructed
        # from settings; the job name/description are settings-driven so a
        # consumer's edits are honored (matching deadline-cloud-for-maya).
        job_template = _get_job_template(rop, host_requirements)

        if houdini_settings.job_name:
            job_template["name"] = houdini_settings.job_name
        if houdini_settings.description:
            job_template["description"] = houdini_settings.description
        elif "description" in job_template and not houdini_settings.description:
            # An explicitly-cleared description should not fall back to the node.
            del job_template["description"]

        # Reconcile the adaptor-wheels override with settings so the template's
        # AdaptorWheels parameterDefinition/jobEnvironment stays consistent with
        # the AdaptorWheels *value* get_parameter_values emits (which is
        # settings-driven). _get_job_template added the override based on the
        # node; a headless consumer that edited the wheels settings could
        # otherwise desync (definition without value, or value without
        # definition -> CreateJob reject).
        wheels_enabled = houdini_settings.include_adaptor_wheels and os.path.exists(
            houdini_settings.adaptor_wheels
        )
        _apply_adaptor_wheels_override(job_template, wheels_enabled)

        return job_template

    def get_parameter_values(
        self,
        settings: BaseSubmitterSettings,
        queue_parameters: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        houdini_settings = cast(HoudiniSubmitterSettings, settings)
        rop = self._require_rop()

        # Scalar job parameters come from settings (honoring consumer edits); the
        # ROP is still consulted for its per-node queue parameter values and hip
        # file, which have no settings equivalent.
        parameter_values: list[dict[str, Any]] = [
            {"name": "deadline:priority", "value": houdini_settings.priority},
            {"name": "deadline:targetTaskRunStatus", "value": houdini_settings.initial_status},
            {
                "name": "deadline:maxFailedTasksCount",
                "value": houdini_settings.max_failed_tasks_count,
            },
            {"name": "deadline:maxRetriesPerTask", "value": houdini_settings.max_retries_per_task},
            *get_queue_parameter_values_as_openjd(rop),
        ]
        if _ods_use_husk(rop):
            # Husk mode: no HipFile (husk never opens the hip). Emit the exported
            # USD / output-dir as absolute parameter VALUES (not template defaults)
            # from the cache the template build populated for this ROP.
            parameter_values.extend(_ODS_HUSK_PARAM_VALUES.get(rop.path(), []))
        else:
            parameter_values.append({"name": "HipFile", "value": _get_hip_file()})
        if houdini_settings.include_adaptor_wheels and os.path.exists(
            houdini_settings.adaptor_wheels
        ):
            parameter_values.append(
                {"name": "AdaptorWheels", "value": houdini_settings.adaptor_wheels}
            )

        # Merge caller-supplied queue_parameters with caller-wins precedence.
        # The node already emits its own queue parameters (CondaPackages,
        # CondaChannels, ...) via get_queue_parameter_values_as_openjd, and a
        # headless consumer (e.g. AYON) passes the SAME queue parameters here as
        # its resolved/edited values -- so the two sets overlap by design. The
        # caller's value supersedes the node's on a name collision (mirroring how
        # the GUI's set_queue_parameter_values_from_openjd writes dialog edits
        # back onto the node before the bundle is read). A plain de-dup also
        # avoids the duplicate parameterValues that CreateJob rejects.
        #
        # An empty/None caller value is treated as "not provided": consumers such
        # as AYON build this list from get_queue_parameters(), which resolves
        # every parameter to its queue default -- and a queue whose CondaPackages
        # default is empty would otherwise clobber the node's computed value
        # (e.g. "houdini=20.5.* houdini-openjd=0.x.*"). We only let a caller value
        # win when it actually carries a value, so a bare queue default never
        # blanks a populated node parameter. Falsy-but-real values (0, False) are
        # preserved.
        caller_values = {
            param["name"]: param["value"]
            for param in queue_parameters
            if "value" in param and param["value"] not in (None, "")
        }
        merged: list[dict[str, Any]] = [
            {"name": param["name"], "value": caller_values.get(param["name"], param["value"])}
            for param in parameter_values
        ]
        existing_names = {param["name"] for param in merged}
        merged.extend(
            {"name": name, "value": value}
            for name, value in caller_values.items()
            if name not in existing_names
        )
        return merged

    def get_asset_references(self, settings: BaseSubmitterSettings) -> AssetReferences:
        rop = self._require_rop()
        # Scan the scene for the authoritative asset references rather than
        # reading the ROP's (possibly unparsed) Job Attachments multiparms, so a
        # headless consumer gets real input/output paths without a prior
        # "Parse Files" pass. Returns the typed AssetReferences per the
        # BaseSubmitter contract; callers serialize with .to_dict().
        return _get_scene_asset_references(rop, evaluate=True)


def _get_houdini_version() -> str:
    return hou.applicationVersionString()


def _get_wedge_render_node(node: hou.Node):
    """Return ROP set as input or parameter to a wedge node

    This ROP may have a network of input ROP nodes that will also be modified
    by the wedge node
    """
    rendernode = None
    if len(node.inputs()) > 0:
        rendernode = node.inputs()[0]
        if rendernode:
            renderpath = rendernode.path()
    if rendernode is None:
        renderpath = node.parm("driver").eval()
        rendernode = node.node(renderpath)
    return rendernode


def _get_steps(node: hou.Node, separate_steps: int):
    """Convert a network of ROP nodes into a list of steps

    Return a list of wedged steps if all the inputs terminate in a valid wedge
    node.
    """
    wedged_steps = _get_wedge_steps(node)
    if wedged_steps is not None:
        # valid wedged network detected
        rop_steps = wedged_steps
    else:
        # standard network
        rop_steps = _get_rop_steps(node)

    if not separate_steps:
        # render the node connected to the deadline cloud node
        # and all its input nodes. The opposite of splitting it
        # up each node by step

        try:
            connected_node = rop_steps[-1]
        except IndexError:
            return []

        # remove deps, as only 1 step
        connected_node.pop("dependency_names", None)
        # remove dependency info from name
        connected_node["name"] = connected_node["rop"]
        rop_steps = [connected_node]

    return rop_steps


def _get_wedge_steps(rop: hou.Node):
    """Convert ancestors of a wedge node into a list of wedged steps"""
    wedge_nodes = []
    # all inputs nodes must be wedge type
    for input_node in rop.inputs():
        if input_node.type().name() != "wedge":
            return None
        else:
            wedge_nodes.append(input_node)
    # wedge inputs must have no ancestor wedge nodes
    for wedge_node in wedge_nodes:
        for node in wedge_node.inputAncestors():
            if node.type().name() == "wedge":
                print("Nested wedge nodes not supported")
                return None
    wedged_steps: list[dict[str, Any]] = []
    for wedge_node in wedge_nodes:
        # houdini uses a prefix to name separate wedge sets
        # NOTE: a prefix name clash will produce invalid job description
        prefix = wedge_node.parm("prefix").eval()
        # compute wedge set
        hm = wedge_node.hdaModule()
        allwedge, stashedparms, errormsg = hm.getwedges(wedge_node)
        rop_node = _get_wedge_render_node(wedge_node)
        # get all the steps driven by the wedge
        rop_steps = _get_rop_steps(rop_node)
        # wedge each list of steps with prefix-wedgenum
        wedgenum = 0
        for _ in allwedge:
            for rop_step in rop_steps:
                wedge = dict(**rop_step)
                # add wedge node and num to use in adaptor
                wedge["wedge_node"] = wedge_node.path()
                wedge["wedgenum"] = str(wedgenum)
                # append wedge suffix to name and dependency names
                suffix = f"{prefix}-{wedgenum}"
                wedge["name"] = f"{rop_step['name']}-{suffix}"
                if "dependency_names" in wedge:
                    dependency_names = [
                        f"{dependency_name}-{suffix}"
                        for dependency_name in wedge["dependency_names"]
                    ]
                    wedge["dependency_names"] = dependency_names
                wedged_steps.append(wedge)
            wedgenum += 1
    return wedged_steps


def _get_rop_steps(rop: hou.Node):
    """
    Parse hscript render command output to steps
    https://www.sidefx.com/docs/houdini/commands/render.html

    Format:
    ```
    <<id>> [ <<dependencies>> ] <<node>> ( <<frames>> )
    ```
    """
    cmd = f"render -p -c -F {rop.path()}"
    out, err = hou.hscript(cmd)
    # hscript emits non-fatal warnings on stderr (e.g. a transient
    # "Local variable 'pdg_input' not found." on the first render evaluation) while still
    # returning a valid step list in `out`. Treat stderr as informational and judge success by
    # whether we got usable output; only fail when the command produced nothing.
    if err and not out.strip():
        raise Exception(f"hscript render: failed to list steps\n\n{str(err)}")
    if err:
        print(f"hscript render reported warnings while listing steps:\n{err}")
    rop_steps: list[dict[str, Any]] = []
    deadline_node_seen = False

    for n in out.split("\n"):
        if not n.strip():
            continue
        # two parts: rops and frame notation
        parts = n.split("\t")
        # id [ deps ] node
        rop_str = parts[0]
        # frames
        frange_part = parts[1]
        frange = frange_part.replace("( ", "")
        frange = frange.replace(" )", "")
        range_parts = frange.split(" ")
        range_ints = [int(f) for f in range_parts]
        # handle single frame
        if len(range_ints) == 1:
            frame = range_ints[0]
            range_ints = [frame, frame, 1]
        rop_parts = rop_str.split(" ")
        # first token is the int id generated by hscript render
        _id = rop_parts[0]
        # full path to rop
        path = rop_parts[-2]
        # section after id lists the dependencies between [ ]
        deps: list[str] = []
        for d in rop_parts[1:-2]:
            if d in ["[", "]"]:
                continue
            # id this depends on
            deps.append(d)

        node = hou.node(path)

        # we only want to skip the single root submission node
        if node.type().name() in ("deadline", "deadline_cloud"):
            if deadline_node_seen:
                raise RuntimeError(
                    "The selected network contains multiple Deadline Cloud nodes. Only a single Deadline Cloud node should be used."
                )
            deadline_node_seen = True
            continue

        step_dict = {
            "id": _id,
            "name": f"{path}-{_id}",
            "dependency_ids": deps,
            "rop": path,
            "wedgenum": "",
            "wedge_node": "",
            "start": range_ints[0],
            "end": range_ints[1],
            "step": range_ints[2],
            "render_strategy": _get_render_strategy_for_node(node),
        }
        rop_steps.append(step_dict)
    # expand full dependency names once the list is complete
    id_steps = {n["id"]: n for n in rop_steps}
    for rop in rop_steps:
        if rop["dependency_ids"]:
            names = [id_steps[n]["name"] for n in rop["dependency_ids"]]
            rop["dependency_names"] = names
    return rop_steps


# .
def _get_render_strategy_for_node(node: hou.Node) -> RenderStrategy:
    """Any changes to this function should be reflected in the user guide: /docs/user-guide.md"""
    render_strategy = RenderStrategy.PARALLEL

    if (
        node.type().nameWithCategory() in ["Driver/geometry", "Sop/rop_geometry"]
        and node.parm("initsim")
        and node.parm("initsim").eval()
    ):
        render_strategy = RenderStrategy.SEQUENTIAL

    if node.parm("deadline_cloud_render_strategy"):
        strategy_string = node.parm("deadline_cloud_render_strategy").evalAsString()
        if strategy_string.upper() == RenderStrategy.SEQUENTIAL.value:
            render_strategy = RenderStrategy.SEQUENTIAL
        elif strategy_string.upper() == RenderStrategy.PARALLEL.value:
            render_strategy = RenderStrategy.PARALLEL
        else:
            raise ValueError(
                f'The node "{node.path()}" has an unexpected value "{strategy_string}" for its "deadline_cloud_render_strategy" parameter. Ensure the value is {RenderStrategy.PARALLEL.value} or {RenderStrategy.SEQUENTIAL.value}'
            )
    return render_strategy


def read_ui_settings_from_node(node: hou.Node) -> HoudiniSubmitterUISettings:
    """Populate a HoudiniSubmitterUISettings from the Deadline Cloud ROP node's parameters.

    This lets the shared SubmitJobToDeadlineDialog (used by the Python panel) open with the
    node's configured values instead of pure dataclass defaults. Parameter names match those
    defined in the HDA DialogScript and read elsewhere in this module (e.g. get_settings).

    Note the deliberate name remapping: the node exposes ``failed_tasks_limit`` /
    ``task_retry_limit``, but deadline-cloud's SharedJobPropertiesWidget reads
    ``max_failed_tasks_count`` / ``max_retries_per_task`` -- the dataclass uses the latter so the
    shared widget picks the values up rather than silently falling back to its own defaults.
    """
    settings = HoudiniSubmitterUISettings()

    settings.name = node.parm("name").evalAsString()
    settings.description = node.parm("description").evalAsString()

    settings.priority = node.parm("priority").eval()
    settings.initial_status = node.parm("initial_status").evalAsString()
    settings.max_failed_tasks_count = node.parm("failed_tasks_limit").eval()
    settings.max_retries_per_task = node.parm("task_retry_limit").eval()

    settings.separate_steps = bool(node.parm("separate_steps").eval())
    if node.parm("ods_use_husk"):
        settings.ods_use_husk = bool(node.parm("ods_use_husk").eval())
    settings.auto_unlock_rops = bool(node.parm("auto_unlock_rops").eval())
    settings.auto_parse_hip = bool(node.parm("auto_parse_hip").eval())
    settings.auto_save_hip = bool(node.parm("auto_save_hip").eval())

    settings.include_adaptor_wheels = bool(node.parm("include_adaptor_wheels").eval())
    settings.adaptor_wheels_dir = node.parm("adaptor_wheels").evalAsString()

    return settings


def write_ui_settings_to_node(node: hou.Node, settings: HoudiniSubmitterUISettings) -> None:
    """Persist the shared dialog's job settings back onto the Deadline Cloud ROP node.

    Inverse of read_ui_settings_from_node. Used by the Python panel's submit callback (Approach A)
    so the panel's edits save with the scene and drive the generated bundle, which is still built
    by reading the node's parameters. As in the reader, the dialog's ``max_failed_tasks_count`` /
    ``max_retries_per_task`` map to the node's ``failed_tasks_limit`` / ``task_retry_limit``.

    Frame-range/take parms (``trange``/``f``/``take``) are intentionally NOT written here: those are
    managed directly by the panel's frame controls, and ``settings`` only carries default values
    for them, so writing them would clobber the user's frame selection.
    """
    node.parm("name").set(settings.name)
    node.parm("description").set(settings.description)

    node.parm("priority").set(settings.priority)
    node.parm("initial_status").set(settings.initial_status)
    node.parm("failed_tasks_limit").set(settings.max_failed_tasks_count)
    node.parm("task_retry_limit").set(settings.max_retries_per_task)

    node.parm("separate_steps").set(int(settings.separate_steps))
    if node.parm("ods_use_husk"):
        node.parm("ods_use_husk").set(int(settings.ods_use_husk))
    node.parm("auto_unlock_rops").set(int(settings.auto_unlock_rops))
    node.parm("auto_parse_hip").set(int(settings.auto_parse_hip))
    node.parm("auto_save_hip").set(int(settings.auto_save_hip))

    node.parm("include_adaptor_wheels").set(int(settings.include_adaptor_wheels))
    node.parm("adaptor_wheels").set(settings.adaptor_wheels_dir or "")


def _follow_fetch_nodes(node: hou.Node) -> hou.Node:
    """Follow a chain of fetch nodes to find the final target node.

    Args:
        node: The starting node, which may or may not be a fetch node

    Returns:
        The final non-fetch node in the chain
    """
    current_node = node
    visited_nodes = set()  # Prevent infinite loops

    while (
        current_node
        and current_node.type().nameWithCategory() == "Driver/fetch"
        and current_node.path() not in visited_nodes
    ):
        visited_nodes.add(current_node.path())
        inner_node = current_node.node(current_node.parm("source").eval())
        if not inner_node:
            break
        current_node = inner_node

    return current_node


def _ods_describe_empty_steps(rop: hou.Node) -> str:
    """Explain why ``hscript render -p`` listed no render steps under the Deadline Cloud ROP.

    Houdini lists nothing, without an error, for a locked ROP or for a Fetch ROP whose source is
    missing or is not a render node (e.g. a plain LOP instead of a USD Render ROP). Only the
    direct inputs need checking: if any of them rendered, there would be a step.
    """
    reasons = []
    if rop.isLocked():
        reasons.append(f"{rop.path()} has its lock flag set.")
    inputs = [n for n in rop.inputs() if n is not None]
    if not inputs:
        reasons.append(f"{rop.path()} has no input ROP connected.")
    for node in inputs:
        target = _follow_fetch_nodes(node)
        # Check the type before any flag: a Fetch can point at a node without isBypassed()
        # (e.g. an ObjNode such as /obj/geo1).
        if node.isBypassed():
            reasons.append(f"{node.path()} is bypassed.")
        elif target.type().nameWithCategory() == "Driver/fetch":
            reasons.append(f"Fetch ROP {target.path()} has no valid Source.")
        elif not isinstance(target, hou.RopNode):
            reasons.append(
                f"{node.path()} doesn't point at a render node. "
                "Point it at the render ROP, e.g. a USD Render ROP."
            )
        elif target.isBypassed():
            reasons.append(f"{target.path()} is bypassed.")
        elif target.isLocked():
            reasons.append(f"{target.path()} has its lock flag set.")
    if not reasons:
        return f"No render steps found for {rop.path()}: the ROPs connected to it render nothing."
    return "\n".join(f"- {r}" for r in reasons)


def _is_node_locked(rop_path: str) -> bool:
    """Check rop path lineage for any locked nodes.

    Locked nodes can not be driven by the adaptor and must be unlocked before
    submission.
    """
    path_parts = rop_path.split("/")
    for i, n in enumerate(path_parts):
        node_path = "/".join(path_parts[0:i])
        node = hou.node(node_path)
        if not node:
            continue
        if node.isLockedHDA():
            return True
    return False


def _unlock_node(rop_path: str) -> bool:
    """Unlock the first locked node in the path lineage"""
    path_parts = rop_path.split("/")
    for i, _ in enumerate(path_parts):
        node_path = "/".join(path_parts[0:i])
        node = hou.node(node_path)
        if not node:
            continue
        if node.isLockedHDA():
            try:
                node.allowEditingOfContents(propagate=True)
                return True
            except Exception as exc:
                print(f"Failed to unlock: {node_path}")
                print(str(exc))
                return False
    return False


def _get_job_template(
    rop: hou.Node, host_requirements: Optional[Dict[str, Any]] = None
) -> dict[str, Any]:
    separate_steps = rop.parm("separate_steps").eval()
    rop_steps = _get_steps(rop, separate_steps)
    if not rop_steps:
        # A template with no steps is only rejected by CreateJob, after every file has been
        # hashed and uploaded, with an error that names neither the node nor the reason. Fail
        # here instead (the bundle is built before the upload) with what is wrong in the network.
        raise NonValidInputError(_ods_describe_empty_steps(rop))
    ignore_input_nodes = bool(separate_steps)
    queue_parameter_definitions_json = rop.userData("queue_parameter_definitions")
    parameter_definitions: list[dict[str, Any]] = (
        json.loads(queue_parameter_definitions_json)
        if queue_parameter_definitions_json is not None
        else []
    )
    steps: list[dict[str, Any]] = []
    if _ods_use_husk(rop):
        # ODS husk-native path: render a pre-exported USD with standalone husk.
        # No HipFile input (husk never opens the hip). Each render node exports
        # its thin USD at submit time; declare the USD as a per-node IN PATH param
        # and its output dir as an OUT PATH param so Deadline Cloud maps both
        # X:\ -> /mnt/projects on the worker.
        #
        # PATH defaults must be RELATIVE (Deadline Cloud rejects an absolute PATH
        # default as "must resolve within the Job Bundle"). So the param defs carry
        # NO default; the absolute exported-USD / output-dir are emitted as
        # parameter *values* by get_parameter_values, via the cache we populate
        # here keyed by this ROP's path.
        husk_values: list[dict[str, Any]] = []
        for node in rop_steps:
            usd_path = _ods_export_render_usd(node["rop"])
            safe = re.sub(r"[^A-Za-z0-9_]", "_", node["name"])
            usd_param = "RenderUsdFile_{}".format(safe)
            outdir_param = "OutputDir_{}".format(safe)
            parameter_definitions.append(
                {
                    "name": usd_param,
                    "type": "PATH",
                    "objectType": "FILE",
                    "dataFlow": "IN",
                }
            )
            husk_values.append({"name": usd_param, "value": usd_path})
            products = _ods_render_products_from_usd(
                usd_path, _ods_rendersettings_path(node["rop"])
            )
            dir_params, output_products = _ods_product_dir_params(outdir_param, products)
            for product_dir, param_name in dir_params.items():
                parameter_definitions.append(
                    {
                        "name": param_name,
                        "type": "PATH",
                        "objectType": "DIRECTORY",
                        "dataFlow": "OUT",
                    }
                )
                husk_values.append({"name": param_name, "value": product_dir})
            steps.append(_ods_get_husk_step_template(node, usd_param, output_products))
        _ods_check_unique_param_names([v["name"] for v in husk_values])
        _ODS_HUSK_PARAM_VALUES[rop.path()] = husk_values
    else:
        parameter_definitions.append(
            {
                "name": "HipFile",
                "type": "PATH",
                "objectType": "FILE",
                "dataFlow": "IN",
                "default": _get_hip_file(),
            }
        )
        for node in rop_steps:
            steps.append(_get_step_template(node, ignore_input_nodes))
    job_template = {
        "specificationVersion": "jobtemplate-2023-09",
        "name": rop.parm("name").evalAsString(),
        "parameterDefinitions": parameter_definitions,
        "steps": steps,
    }

    # If host requirements were configured in the dialog's Host Requirements tab, inject them
    # into every step. The UI sets a single set of requirements for the whole job rather than
    # per-step, so the same block is applied to each step (matching the behavior of the
    # deadline-cloud job bundle/CLI submitters).
    if host_requirements:
        for step in steps:
            step["hostRequirements"] = copy.deepcopy(host_requirements)

    description = rop.parm("description").evalAsString()
    if description:
        job_template["description"] = description

    include_adaptor_wheels = rop.parm("include_adaptor_wheels").eval()
    if include_adaptor_wheels:
        adaptor_wheels = rop.parm("adaptor_wheels").evalAsString()
        if os.path.exists(adaptor_wheels):
            override_file = os.path.join(
                os.path.dirname(__file__), "adaptor_override_environment.yaml"
            )
            with open(override_file) as yaml_file:
                override_environment = yaml.safe_load(yaml_file)
                job_template["parameterDefinitions"].extend(
                    override_environment["parameterDefinitions"]
                )
                if "jobEnvironments" not in job_template:
                    job_template["jobEnvironments"] = []
                job_template["jobEnvironments"].append(override_environment["environment"])

    return job_template


def _load_adaptor_override_environment() -> dict[str, Any]:
    override_file = os.path.join(os.path.dirname(__file__), "adaptor_override_environment.yaml")
    with open(override_file) as yaml_file:
        return yaml.safe_load(yaml_file)


# Names contributed by adaptor_override_environment.yaml, used to detect/strip
# the override so the template can be reconciled with the settings-driven state.
_ADAPTOR_OVERRIDE_PARAM_NAMES = {"AdaptorWheels", "OverrideAdaptorName"}
_ADAPTOR_OVERRIDE_ENV_NAME = "OverrideAdaptor"


def _apply_adaptor_wheels_override(job_template: dict[str, Any], enabled: bool) -> None:
    """Add or remove the adaptor-wheels override on ``job_template`` in place.

    Idempotent: makes the AdaptorWheels ``parameterDefinition``s and the
    ``OverrideAdaptor`` ``jobEnvironment`` present when ``enabled`` and absent
    otherwise, regardless of what the node-driven ``_get_job_template`` already
    added. This keeps the template consistent with the settings-driven
    AdaptorWheels parameter *value* emitted by ``get_parameter_values``.
    """
    already_present = any(
        pd.get("name") in _ADAPTOR_OVERRIDE_PARAM_NAMES
        for pd in job_template.get("parameterDefinitions", [])
    )

    if enabled and not already_present:
        override = _load_adaptor_override_environment()
        job_template.setdefault("parameterDefinitions", []).extend(override["parameterDefinitions"])
        job_template.setdefault("jobEnvironments", []).append(override["environment"])
    elif not enabled and already_present:
        job_template["parameterDefinitions"] = [
            pd
            for pd in job_template.get("parameterDefinitions", [])
            if pd.get("name") not in _ADAPTOR_OVERRIDE_PARAM_NAMES
        ]
        job_template["jobEnvironments"] = [
            env
            for env in job_template.get("jobEnvironments", [])
            if env.get("name") != _ADAPTOR_OVERRIDE_ENV_NAME
        ]
        if not job_template["jobEnvironments"]:
            del job_template["jobEnvironments"]


# --- ODS: husk-native render path -------------------------------------------
# When the DC ROP's 'Render usdrender with husk (ODS)' toggle (ods_use_husk) is
# on, render usdrender ROPs with standalone husk on a pre-exported USD instead of
# the houdini-openjd adaptor (which cooks the scene in hython and, for a
# Driver/usdrender ROP, only exports the USD and never runs husk headless). The
# USD is exported at submit time (_ods_export_render_usd) and declared as a PATH
# input so Deadline Cloud maps it onto the worker. husk is on PATH from the Conda
# queue env's Houdini package. Mirrors the AWS deadline-cloud-samples
# houdini_husk_usd_render bundle.
#
# The toggle is a ROP parameter (not a Qt-dialog checkbox) so BOTH the native GUI
# submit and the AYON bridge honour it: both resolve the same ROP node. The legacy
# ODS_HOUDINI_USE_HUSK environment variable is still honoured as a fallback for
# back-compat (e.g. headless bundles built before the parm existed).
_ODS_USE_HUSK_ENV = "ODS_HOUDINI_USE_HUSK"
_ODS_USE_HUSK_PARM = "ods_use_husk"

# Husk-mode parameter VALUES (absolute exported-USD / output-dir paths) computed
# while building the job template, keyed by the ROP's path. get_parameter_values
# reads these back so the absolute paths are emitted as parameter *values* rather
# than template *defaults* (Deadline Cloud rejects absolute PATH defaults). The
# template build (get_job_template) always runs before get_parameter_values for
# the same ROP, so the cache is populated in time.
_ODS_HUSK_PARAM_VALUES: Dict[str, list[dict[str, Any]]] = {}


def _ods_use_husk(rop: Optional["hou.Node"] = None) -> bool:
    """Whether to use the husk-native render path for this ROP.

    Reads the ROP's ``ods_use_husk`` toggle parameter when available (the
    submitter UI option, honoured by both the GUI submit and the AYON bridge
    since both resolve the same node). Falls back to the legacy
    ``ODS_HOUDINI_USE_HUSK`` environment variable when the ROP is not given or
    the parm is absent (older HDAs / headless callers), so pre-existing setups
    keep working.
    """
    if rop is not None:
        parm = rop.parm(_ODS_USE_HUSK_PARM)
        if parm is not None:
            return bool(parm.eval())
    return str2bool(os.environ.get(_ODS_USE_HUSK_ENV, "false"))


def _ods_export_render_usd(rop_path: str) -> str:
    """Export the usdrender ROP's thin render USD at submit time.

    Calling render() on a Driver/usdrender ROP writes its ``lopoutput`` thin USD
    layer (which references the scene's existing USD on the share — not a flatten)
    and, headless, skips the husk stage. That exported layer is exactly what husk
    needs.

    Returns the absolute path (forward slashes) of the exported ``__render__.usd``.
    The render outputs are read from it by ``_ods_render_products_from_usd``.
    """
    rop = hou.node(rop_path)
    if rop is None:
        raise RuntimeError(f"Cannot find render ROP at path: {rop_path}")

    lopoutput = rop.parm("lopoutput")
    if lopoutput is None:
        raise RuntimeError(
            f"ROP {rop_path} has no 'lopoutput' parm; husk render path needs a "
            "usdrender ROP."
        )

    hip = hou.getenv("HIP") or ""

    # Where render() writes the thin __render__.usd depends on the ROP type:
    #   * Driver/usdrender writes under the ayon/<rop>/usd/... HIP tree;
    #   * a LOP usdrender_rop routes the export through its "Save to Directory" output
    #     processor to $HOUDINI_TEMP_DIR/usd_renders/$RENDERID/ (unique local temp, off
    #     the share and un-mappable by Deadline Cloud).
    # Relying on the side-effect path is therefore unreliable and, for the LOP case,
    # off-share. Instead force a DETERMINISTIC on-share export location under $HIP (so
    # Deadline Cloud maps X:\ -> /mnt/projects) and put the ROP in husk_debug mode so
    # render() EXPORTS the thin USD and prints the husk command WITHOUT actually
    # rendering. All touched parms are restored afterwards so the artist's node is
    # left exactly as it was.
    if not hip:
        raise RuntimeError(
            "husk render path: $HIP is not set; cannot pick a mappable export "
            f"location for {rop_path}."
        )
    # Export to a UNIQUE per-submit subdir under $HIP. Unique because the shared
    # project store (FSx/OpenZFS) can make a previously-written __render__.usd
    # un-overwritable from the submitting workstation (NFS ownership maps the first
    # writer; a later atomic save-then-rename is then "Access denied"). A fresh
    # subdir per submit avoids colliding with — or trying to delete — a prior export,
    # while staying on-share so Deadline Cloud maps X:\ -> /mnt/projects.
    export_dir = os.path.join(
        hip, "_ods_husk_export", rop.name(), str(int(time.time() * 1000))
    ).replace("\\", "/")
    export_usd = export_dir + "/__render__.usd"
    os.makedirs(export_dir, exist_ok=True)

    # Parms we flip for a clean, deterministic, render-free export. Not every parm
    # exists on every usdrender variant, so each is guarded.
    savedir = rop.parm("savetodirectory_directory")
    savedir_enable = rop.parm("enableoutputprocessor_savetodirectory")
    husk_debug = rop.parm("husk_debug")
    rendercommand = rop.parm("rendercommand")
    lopoutput_parm = lopoutput

    def _remember(parm):
        # Remember both the raw value (preserves expressions on string parms) and the
        # evaluated value (for toggles/ints whose rawValue() is "on"/"off" and cannot
        # be passed back to set()). Restore tries raw first, then the numeric eval.
        if parm is None:
            return None
        try:
            raw = parm.rawValue()
        except Exception:
            raw = None
        try:
            ev = parm.eval()
        except Exception:
            ev = None
        return (parm, raw, ev)

    def _restore(entry):
        if entry is None:
            return
        parm, raw, ev = entry
        try:
            parm.set(raw)
            return
        except Exception:
            pass
        try:
            parm.set(ev)
        except Exception:
            pass

    saved = []  # list of (parm, raw, eval) remembered BEFORE mutation
    try:
        if savedir is not None:
            saved.append(_remember(savedir))
            savedir.set(export_dir)
            if savedir_enable is not None:
                saved.append(_remember(savedir_enable))
                savedir_enable.set(1)
        else:
            # Older/alternate ROPs with no savetodirectory: steer lopoutput directly.
            saved.append(_remember(lopoutput_parm))
            lopoutput_parm.set(export_usd)
        if husk_debug is not None:
            saved.append(_remember(husk_debug))
            husk_debug.set(0)
        # Blank the husk render command so the render stage CANNOT launch husk. The
        # usdrender ROP still writes its thin, self-contained __render__.usd during the
        # export pre-pass (references on-share assets, ~KB, no in-memory anon layers);
        # then the now-empty render command fails to launch and raises OperationFailed,
        # which we swallow. This avoids the bug where rop.render() with husk_debug=1 ran
        # a FULL local husk render on the submitting workstation (CPU pegged, GUI hung).
        if rendercommand is not None:
            saved.append(_remember(rendercommand))
            rendercommand.set("")
        try:
            rop.render(verbose=False)
        except hou.OperationFailed:
            # Expected when rendercommand was blanked: the USD export already ran; only
            # the (empty) husk launch failed. Verified below by checking the file exists.
            pass
    finally:
        for entry in saved:
            _restore(entry)

    usd_path = None
    if os.path.isfile(export_usd):
        usd_path = os.path.normpath(export_usd).replace("\\", "/")

    # Fallback: the pre-fix discovery (evaluated lopoutput / Driver naming convention),
    # in case a ROP variant ignored savetodirectory.
    if usd_path is None:
        candidates = []
        evaled = lopoutput.eval()
        if os.path.isabs(evaled):
            candidates.append(evaled)
        hipname = hou.getenv("HIPNAME") or ""
        rop_name = rop.name()
        candidates.append(
            os.path.join(
                hip, "ayon", rop_name, "usd", hipname, rop_name, "__render__.usd"
            )
        )
        if not os.path.isabs(evaled):
            candidates.append(os.path.join(hip, evaled))
        for cand in candidates:
            cand = os.path.normpath(cand)
            if os.path.isfile(cand):
                usd_path = cand.replace("\\", "/")
                break

    if usd_path is None:
        raise RuntimeError(
            f"husk render path: exported USD not found for {rop_path}. Expected "
            f"{export_usd!r} after a husk_debug export."
        )

    return usd_path


def _ods_split_product_path(product_path: str) -> tuple[str, str]:
    """Split a productName into (dir, file pattern with a husk ``$F<pad>`` frame token).

    The productName is an absolute drive path (e.g. ``X:/.../foo.0002.exr``) that does
    not resolve on the Linux worker. The dir becomes an OUT PATH param (Deadline Cloud
    maps it) and the file name gets ``$F<pad>`` so husk fills in each frame.
    """
    product_path = product_path.replace("\\", "/")
    # Absolute means a drive (X:/), UNC (//) or POSIX (/) path. A relative productName
    # would give an empty or relative OUT PATH param that Deadline Cloud can't map.
    if not re.match(r"^([A-Za-z]:)?/", product_path):
        raise RuntimeError(
            f"husk render path: RenderProduct productName {product_path!r} is not an "
            "absolute path; set an absolute output path on the render settings."
        )
    output_dir = os.path.dirname(product_path)
    basename = os.path.basename(product_path)
    # Turn a resolved frame number (e.g. 'foo.0002.exr') into a husk frame token
    # 'foo.$F4.exr' so -o renders the correct per-frame filename. Match the last
    # run of digits before the extension.
    stem, ext = os.path.splitext(basename)
    m = re.search(r"(\d+)$", stem)
    if m:
        pad = len(m.group(1))
        stem = stem[: m.start()] + "$F{}".format(pad)
    pattern = stem + ext
    return output_dir, pattern


def _ods_rendersettings_path(rop_path: str) -> str:
    """The RenderSettings prim path husk renders with (the ROP's `rendersettings` parm)."""
    rop = hou.node(rop_path)
    if rop is not None:
        parm = rop.parm("rendersettings")
        if parm is not None and parm.eval():
            return parm.eval()
    return "/Render/rendersettings"


def _ods_render_products_from_usd(
    usd_path: str, settings_path: str = "/Render/rendersettings"
) -> list[tuple[str, str, str]]:
    """Return ``(prim path, dir, file pattern)`` for every RenderProduct husk renders.

    Products are the targets of the settings' ``products`` relationship, or the
    RenderProduct children of /Render/Products when it has none. husk renders each of
    them, so a product without a productName raises: its output would go to the
    worker's working directory and be lost.
    """
    from pxr import Usd

    stage = Usd.Stage.Open(usd_path)
    if not stage:
        raise RuntimeError(f"husk render path: cannot open {usd_path}")
    prims = []
    settings = stage.GetPrimAtPath(settings_path)
    if settings and settings.IsValid():
        rel = settings.GetRelationship("products")
        if rel:
            for target in rel.GetTargets():
                prim = stage.GetPrimAtPath(target)
                if prim and prim.IsValid() and prim.GetTypeName() == "RenderProduct":
                    prims.append(prim)
    if not prims:
        products = stage.GetPrimAtPath("/Render/Products")
        if products and products.IsValid():
            prims = [c for c in products.GetChildren() if c.GetTypeName() == "RenderProduct"]
    if not prims:
        raise RuntimeError(f"husk render path: no RenderProduct in {usd_path}")
    result = []
    for prim in prims:
        attr = prim.GetAttribute("productName")
        name = attr.Get(0) if attr else None
        if not name:
            raise RuntimeError(
                f"husk render path: RenderProduct {prim.GetPath()} in {usd_path} has no "
                "productName; its output would not be uploaded."
            )
        result.append((str(prim.GetPath()), *_ods_split_product_path(str(name))))
    return result


def _ods_product_dir_params(
    outdir_param: str, products: list[tuple[str, str, str]]
) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Give each distinct product folder its own OUT PATH param name.

    husk renders every RenderProduct in one pass, so every folder must be mapped and
    uploaded. The first folder keeps ``outdir_param`` (``OutputDir_<node>``), later ones
    get ``_2``, ``_3``, ...; products in the same folder share one param. Returns
    ``(folder -> param name, [(prim path, param name, file pattern), ...])``.
    """
    dir_params: dict[str, str] = {}
    for _, product_dir, _ in products:
        if product_dir not in dir_params:
            dir_params[product_dir] = (
                f"{outdir_param}_{len(dir_params) + 1}" if dir_params else outdir_param
            )
    return dir_params, [(prim, dir_params[d], base) for prim, d, base in products]


def _ods_check_unique_param_names(names: list[str]) -> None:
    """Raise if two husk-path job parameters share a name.

    Node names are sanitised to ``[A-Za-z0-9_]``, so one ROP's ``OutputDir_<node>_2`` can
    equal another ROP's ``OutputDir_<node>``; two products would then write into the
    same param.
    """
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise RuntimeError(
                f"husk render path: job parameter {name!r} is defined twice; rename one "
                "of the render nodes so their output parameters differ."
            )
        seen.add(name)


def _ods_products_override_layer(
    usd_param_name: str, output_products: list[tuple[str, str, str]]
) -> str:
    """usda that sublayers the render USD and points every productName at its mapped folder.

    Written as an OpenJD embedded file, so ``{{Param.*}}`` resolve on the worker. Used
    instead of a comma separated ``-o`` list, which HdArnold (HtoA 6.5.3.1) gave whole to
    the first non-raster product as its file name. husk expands ``$F<pad>`` in
    productName, and this layer's default value wins over the exported time samples.
    """
    lines = [
        "#usda 1.0",
        "(",
        "    subLayers = [",
        f"        @{{{{Param.{usd_param_name}}}}}@",
        "    ]",
        ")",
        "",
    ]

    # Merge the prim paths into one tree: a layer can't hold two sibling specs with the
    # same name (e.g. two `over "Render"`). The "" key holds a product's file name.
    tree: dict[str, Any] = {}
    for prim_path, param, basename in output_products:
        node = tree
        for name in prim_path.strip("/").split("/"):
            node = node.setdefault(name, {})
        node[""] = f"{{{{Param.{param}}}}}/{basename}"

    def _emit(node: dict, pad: str) -> None:
        for name, child in node.items():
            if name == "":
                lines.append(f'{pad}token productName = "{child}"')
            else:
                lines.extend([f'{pad}over "{name}"', f"{pad}{{"])
                _emit(child, pad + "    ")
                lines.append(f"{pad}}}")

    _emit(tree, "")
    return "\n".join(lines) + "\n"


# ODS: USD path remap, run by _ODS_HUSK_RUN_SCRIPT on the worker with the system python3.
# Keep it Python 3.9 compatible, and never write two curly braces in a row: the wrapper is an
# OpenJD embedded file, whose text is a format string.
# argv: <OpenJD path mapping rules file> <remap file to write> [<existing remap file>]
# Writes {"remaps": [{"from": ..., "to": ...}]}: one entry per job attachment root (its
# submit-machine path -> its copy in the session), then the other spelling of each root from
# ODS_USD_ROOT_ALIASES ("A:=//server/share;..."), then the entries of an existing remap file
# that neither equal nor fall inside one of those roots. A resolver that rewrites by longest
# matching prefix (not by order) then reads every asset from the session: an existing entry
# inside a root would take that part of the root away from its session copy.
_ODS_USD_REMAP_PY = r"""import json
import os
import sys


def norm(path):
    return path.replace("\\", "/").rstrip("/")


def under(path, root):
    path, root = path.lower(), root.lower()
    return path == root or path.startswith(root + "/")


def parse_aliases(text):
    pairs = []
    for entry in text.split(";"):
        if "=" not in entry:
            continue
        a, b = (norm(x.strip()) for x in entry.split("=", 1))
        if a and b:
            pairs.append((a, b))
    return pairs


def build_remaps(rules, aliases, previous=()):
    real = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        src, dst = rule.get("source_path"), rule.get("destination_path")
        if isinstance(src, str) and isinstance(dst, str):
            src, dst = norm(src), dst.rstrip("/")
            if src and dst:
                real.append((src, dst))
    alias = []
    for src, dst in real:
        for a, b in aliases:
            for x, y in ((a, b), (b, a)):
                if under(src, x):
                    alias.append((y + src[len(x) :], dst))
    # The resolver uses the longest matching prefix. An alias inside a real root of the same
    # spelling would take that root's files, so it is dropped.
    alias = [e for e in alias if not any(under(e[0], r[0]) for r in real)]
    remaps = []
    seen = set()

    def add(frm, to):
        if frm.lower() not in seen:
            seen.add(frm.lower())
            remaps.append({"from": frm, "to": to})

    for group in (real, alias):
        for frm, to in sorted(group, key=lambda e: -len(e[0])):
            add(frm, to)
    roots = [r[0] for r in real] + [a[0] for a in alias]
    for entry in previous:
        if isinstance(entry, dict):
            frm, to = entry.get("from"), entry.get("to")
            if isinstance(frm, str) and isinstance(to, str):
                if not any(under(norm(frm), root) for root in roots):
                    add(frm, to)
    return remaps


def read_list(path, key):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    value = data.get(key, []) if isinstance(data, dict) else []
    return value if isinstance(value, list) else []


def main(argv):
    rules_file, out_file = argv[1], argv[2]
    previous_file = argv[3] if len(argv) > 3 else ""
    try:
        rules = read_list(rules_file, "path_mapping_rules")
    except (OSError, ValueError) as e:
        print("ODS: path mapping rules not read (%s)" % e)
        return 1
    previous = []
    if previous_file:
        try:
            previous = read_list(previous_file, "remaps")
        except (OSError, ValueError) as e:
            print("ODS: existing remap file %s not read (%s)" % (previous_file, e))
    aliases = parse_aliases(os.environ.get("ODS_USD_ROOT_ALIASES", ""))
    remaps = build_remaps(rules, aliases, previous)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump({"remaps": remaps}, f, indent=1)
    for entry in remaps:
        print("ODS: usd remap %s -> %s" % (entry["from"], entry["to"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
"""

# ODS: wrapper around husk. $1 is the OpenJD path mapping rules file, $2 the renderer taken
# from the scene; the rest are husk's own arguments. Both features are set by the queue
# environment, so customer-specific values stay out of the submitter:
# * ODS_HUSK_RENDERER_MAP ("From=To;From2=To2") renames a scene renderer to one the worker has.
# * ODS_USD_REMAP_VAR names the environment variable through which the queue's USD resolver
#   reads a prefix remap file (see _ODS_USD_REMAP_PY). The wrapper writes that file and points
#   the variable at it, so absolute asset paths inside the USD (any drive letter or UNC share)
#   resolve to the job attachment copies. ODS_USD_ROOT_ALIASES adds other spellings.
# Unset or empty: no rename, no remap.
_ODS_HUSK_RUN_SCRIPT = (
    r"""#!/bin/bash
rules_file="$1"
renderer="$2"
shift 2

trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

IFS=';' read -r -a entries <<< "${ODS_HUSK_RENDERER_MAP-}"
for entry in "${entries[@]}"; do
    [[ "$entry" == *=* ]] || continue
    from="$(trim "${entry%%=*}")"
    to="$(trim "${entry#*=}")"
    if [[ -n "$to" && "$from" == "$renderer" ]]; then
        echo "ODS: renderer $renderer -> $to (ODS_HUSK_RENDERER_MAP)"
        renderer="$to"
        break
    fi
done

if [[ -n "${ODS_USD_REMAP_VAR-}" ]]; then
    if [[ ! "$ODS_USD_REMAP_VAR" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo "ODS: ODS_USD_REMAP_VAR='$ODS_USD_REMAP_VAR' is not a variable name; USD path remap skipped"
    elif ! command -v python3 >/dev/null 2>&1; then
        echo "ODS: python3 not found; USD path remap skipped"
    else
        remap_file="$PWD/ods_usd_path_remap.json"
        if python3 -I - "$rules_file" "$remap_file" "${!ODS_USD_REMAP_VAR}" <<'ODS_PY_EOF'
"""
    + _ODS_USD_REMAP_PY
    + r"""ODS_PY_EOF
        then
            export "$ODS_USD_REMAP_VAR=$remap_file"
            echo "ODS: $ODS_USD_REMAP_VAR=$remap_file"
        else
            echo "ODS: USD path remap failed; rendering without it"
        fi
    fi
fi

exec husk -R "$renderer" "$@"
"""
)


def _ods_get_husk_step_template(
    node: Dict,
    usd_param_name: str,
    output_products: list[tuple[str, str, str]],
) -> dict[str, Any]:
    """Build a standalone-husk render step for one render node.

    No stepEnvironments / no houdini-openjd daemon: husk is on PATH from the
    Conda queue env. Reads the pre-exported USD (``usd_param_name``, a PATH input,
    so Deadline Cloud maps X:\\ -> /mnt/projects on the worker). The USD's
    productNames are absolute ``X:\\`` paths that do NOT resolve on the Linux worker
    (husk would write to a throwaway ``./X:\\`` dir), so every product is redirected
    into its mapped OUT PATH param. ``output_products`` holds ``(product prim path,
    out dir param name, file pattern)`` per product:

    * one product: ``-o <OutputDir>/<file pattern>``;
    * several: husk renders an embedded override layer
      (``_ods_products_override_layer``) and gets no ``-o``.

    husk is started through ``_ODS_HUSK_RUN_SCRIPT``, which applies the optional
    ``ODS_HUSK_RENDERER_MAP`` renderer rename and the ``ODS_USD_REMAP_VAR`` USD path remap
    on the worker. It gets the session's path mapping rules file as its first argument.

    Linux-only (husk on SMF is Linux).
    """
    rop = hou.node(node["rop"])
    renderer = "BRAY_HdKarma"
    purpose = "geometry,render"
    complexity = "veryhigh"
    rendersettings = _ods_rendersettings_path(node["rop"])
    if rop is not None:
        if rop.parm("renderer") is not None and rop.parm("renderer").eval():
            renderer = rop.parm("renderer").eval()
        if rop.parm("husk_purpose") is not None and rop.parm("husk_purpose").eval():
            purpose = rop.parm("husk_purpose").eval()
        if rop.parm("husk_complexity") is not None and rop.parm("husk_complexity").eval():
            complexity = rop.parm("husk_complexity").eval()

    if node["render_strategy"] == RenderStrategy.SEQUENTIAL:
        parameter_space = {
            "taskParameterDefinitions": [
                {
                    "name": "Frame",
                    "type": "INT",
                    "range": "{start}-{end}:{step}".format(**node),
                }
            ]
        }
    else:
        parameter_space = {
            "taskParameterDefinitions": [
                {
                    "name": "Frame",
                    "type": "INT",
                    "range": "{start}-{end}:{step}".format(**node),
                }
            ]
        }

    allowed_procedurals = "basic"
    if rop is not None and rop.parm("husk_allowed_procedurals") is not None:
        val = rop.parm("husk_allowed_procedurals").eval()
        if val:
            allowed_procedurals = val

    # --make-output-path creates missing output dirs. -V 2 keeps logs useful.
    # --allowed-procedurals 'basic' lets factory procedurals (e.g. hair) render; it dispatches
    # the procedural manager which briefly checks out a Houdini Engine licence (transient, not the
    # whole render). Use 'none' to avoid any Engine checkout (drops procedural geometry).
    # ODS: the houdini Conda package puts husk in $CONDA_PREFIX/bin and starts hserver on
    # activation, once per worker. The step runs husk through _ODS_HUSK_RUN_SCRIPT, which may
    # rename the renderer and remap USD paths, then execs husk. Each argument is passed as-is
    # (the rules file and the renderer are $1 and $2, never part of the script text), so paths
    # with spaces or quotes need no shell quoting.
    husk_args = [
        renderer,
        "-s",
        rendersettings,
        "--make-output-path",
        "-f",
        "{{Task.Param.Frame}}",
        "--allowed-procedurals",
        allowed_procedurals,
        "--purpose",
        purpose,
        "--complexity",
        complexity,
        "-V",
        "2",
    ]
    multi_product = len(output_products) > 1
    if multi_product:
        husk_args.append("{{Task.File.OdsRenderProducts}}")
    else:
        _, outdir_param, pattern = output_products[0]
        husk_args += [
            "-o",
            f"{{{{Param.{outdir_param}}}}}/{pattern}",
            f"{{{{Param.{usd_param_name}}}}}",
        ]

    step: dict[str, Any] = {
        "name": node["name"],
        "parameterSpace": parameter_space,
        "script": {
            "actions": {
                "onRun": {
                    "command": "bash",
                    "args": [
                        "{{Task.File.OdsHuskRun}}",
                        "{{Session.PathMappingRulesFile}}",
                        *husk_args,
                    ],
                },
            },
        },
        "hostRequirements": {
            "attributes": [
                {"name": "attr.worker.os.family", "anyOf": ["linux"]},
            ]
        },
    }

    embedded_files: list[dict[str, Any]] = [
        {
            "name": "OdsHuskRun",
            "type": "TEXT",
            "filename": "ods_husk_run.sh",
            "data": _ODS_HUSK_RUN_SCRIPT,
        }
    ]
    if multi_product:
        embedded_files.append(
            {
                "name": "OdsRenderProducts",
                "type": "TEXT",
                "filename": "ods_render_products.usda",
                "data": _ods_products_override_layer(usd_param_name, output_products),
            }
        )
    step["script"]["embeddedFiles"] = embedded_files

    if "dependency_names" in node:
        step["dependencies"] = [{"dependsOn": d} for d in node["dependency_names"]]
    return step
# --- end ODS husk-native render path -----------------------------------------


def _get_step_template(node: Dict, ignore_input_nodes: bool):
    init_data = {
        "scene_file": "{{Param.HipFile}}",
        "render_node": node["rop"],
        "version": _get_houdini_version(),
        "ignore_input_nodes": ignore_input_nodes,
        "wedgenum": node["wedgenum"],
        "wedge_node": node["wedge_node"],
    }
    init_data_attachment = {
        "name": "initData",
        "filename": "init-data.yaml",
        "type": "TEXT",
        # Convert to dict to YAML string using the prettier nested object format
        "data": yaml.safe_dump(init_data, default_flow_style=False),
    }

    environments = get_houdini_environments(init_data_attachment)

    task_data_dict = {"render_node": node["rop"], "ignore_input_nodes": ignore_input_nodes}

    if node["render_strategy"] == RenderStrategy.SEQUENTIAL:
        # Generate a single task that renders the whole frame range
        parameter_space = None
        task_data_dict["frame_range"] = {
            "start": node["start"],
            "end": node["end"],
            "step": node["step"],
        }
    else:
        # Generate one task per frame using step parameters
        range_expression = "{start}-{end}:{step}".format(**node)
        parameter_space = {
            "taskParameterDefinitions": [
                {"name": "Frame", "range": range_expression, "type": "INT"}
            ]
        }
        task_data_dict["frame_range"] = {
            "start": "{{Task.Param.Frame}}",
            "end": "{{Task.Param.Frame}}",
            "step": 1,
        }

    task_data = yaml.safe_dump(task_data_dict, default_flow_style=False)
    # Remove single quotes around the frame parameter so it gets interpreted as a int and not a string
    task_data = task_data.replace("'{{Task.Param.Frame}}'", "{{Task.Param.Frame}}")

    step = {
        "name": node["name"],
        "stepEnvironments": environments,
        "script": {
            "embeddedFiles": [
                {
                    "name": "runData",
                    "filename": "run-data.yaml",
                    "type": "TEXT",
                    "data": task_data,
                },
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
                    "cancelation": {
                        "mode": "NOTIFY_THEN_TERMINATE",
                    },
                },
            },
        },
    }

    if parameter_space:
        step["parameterSpace"] = parameter_space

    if "dependency_names" in node:
        deps = [{"dependsOn": d} for d in node["dependency_names"]]
        step["dependencies"] = deps
    return step


def _ods_add_husk_export_dependencies(
    rop_node: hou.Node,
    parameter_values: list[dict[str, Any]],
    asset_references: AssetReferences,
) -> None:
    """Add everything the exported husk USD depends on to *asset_references*.

    Finds the exported ``__render__.usd`` among the husk parameter values, walks its
    dependencies through USD (so a custom Ar resolver expands tokens like ``<LATEST>``),
    and adds the resolved layers/assets as input files. Unresolved paths are printed
    rather than raised, so a partial scan never blocks the submit.
    """
    usd_paths = [
        p["value"]
        for p in parameter_values
        if str(p.get("name", "")).startswith("RenderUsdFile_")
        and str(p.get("value", "")).lower().endswith(_ODS_USD_EXTS)
    ]
    for usd_path in usd_paths:
        if not os.path.isfile(usd_path):
            continue
        inputs, _outputs, unresolved = _get_usd_asset_references({usd_path})
        # The export itself is already attached via its PATH parameter; keep the
        # rest. Only real files are kept so the attachment upload cannot fail.
        for path in inputs:
            if os.path.isfile(path):
                asset_references.input_filenames.add(path)
        print(
            f"ODS husk export deps for {usd_path}: {len(inputs)} found, "
            f"{len(unresolved)} unresolved"
        )
        for p in unresolved:
            print(f"  unresolved: {p}")


_ODS_USD_EXTS = (".usd", ".usda", ".usdc", ".usdz")


def _create_job_bundle(
    rop_node: hou.Node,
    job_bundle_dir: str,
    asset_references: AssetReferences,
    host_requirements: Optional[Dict[str, Any]] = None,
) -> None:
    # Drive the unified HoudiniSubmitter engine so the GUI submit/export path
    # and headless consumers (e.g. AYON) build the template + parameter values
    # through one implementation. Pass the submitter's own get_settings() (read
    # from this ROP) so the settings-driven builders see the node's real values;
    # the produced bundle matches what the node configures.
    # The dialog-provided asset_references are written as-is (they carry the
    # user's Job Attachments edits) rather than re-scanned, so scan_assets=False
    # skips get_settings()'s scene/USD asset walk whose output this path would
    # only discard (the dialog already scanned the scene once when it opened).
    submitter = HoudiniSubmitter(rop_node.path())
    settings = submitter.get_settings(scan_assets=False)
    job_template = submitter.get_job_template(settings, host_requirements)
    parameter_values = submitter.get_parameter_values(settings, [])

    # ODS: in husk mode the job renders the exported __render__.usd, which was
    # generated just now (during get_job_template) and so was never seen by the
    # dialog's earlier scene scan. Its nested references (resolver-token layers such
    # as <LATEST>, textures) must be traversed with the Ar resolver active and added
    # to the attachments, or the worker gets a black frame.
    if _ods_use_husk(rop_node):
        _ods_add_husk_export_dependencies(rop_node, parameter_values, asset_references)

    job_bundle_path = Path(job_bundle_dir)
    with open(job_bundle_path / "template.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump(job_template, f, indent=1)
    with open(job_bundle_path / "parameter_values.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump({"parameterValues": parameter_values}, f, indent=1)
    with open(job_bundle_path / "asset_references.yaml", "w", encoding="utf8") as f:
        deadline_yaml_dump(asset_references.to_dict(), f, indent=1)


def _pre_gui_hook_confirm_callback(parent):
    """Choose the confirmation callback for pre-GUI hooks based on the auto_accept setting.

    Returns ``None`` (run hooks without prompting) when ``settings.auto_accept`` is enabled,
    otherwise the standard Qt confirmation dialog from ``qt_hook_confirmation``. Kept as a small
    helper here (rather than inline in the panel) so the auto_accept branch can be unit-tested
    headlessly -- the panel module isn't importable outside Houdini.
    """
    if str2bool(get_setting("settings.auto_accept")):
        return None

    from deadline.client.ui.pre_gui_hooks import qt_hook_confirmation

    return qt_hook_confirmation(parent)


def callback(kwargs):
    """ROP parameter callback wrapper"""
    function_name = f"{kwargs['parm'].name()}_callback"
    globals()[function_name](kwargs)


def parse_files_callback(kwargs):
    node = kwargs["node"]
    _parse_files(node)


def save_bundle_callback(kwargs):
    node = kwargs["node"]
    name = node.parm("name").evalAsString()
    asset_references = _get_evaluated_asset_references(node)
    try:
        job_bundle_dir = create_job_history_bundle_dir("houdini", name)
        _create_job_bundle(node, job_bundle_dir, asset_references)
        print("Saved the submission as a job bundle:")
        print(job_bundle_dir)
        if sys.platform == "win32":
            os.startfile(job_bundle_dir)
        hou.ui.displayMessage(
            f"Saved the submission as a job bundle: {job_bundle_dir}",
            title="Deadline Cloud Job Submission",
        )
    except Exception as exc:
        print("Error saving bundle")
        hou.ui.displayMessage(
            str(exc),
            title="Deadline Cloud Job Submission",
            severity=hou.severityType.Warning,
            details=traceback.format_exc(),
        )


def _run_pre_submission_checks(
    node: hou.Node,
    *,
    auto_unlock_rops: Optional[bool] = None,
    auto_parse_hip: Optional[bool] = None,
    auto_save_hip: Optional[bool] = None,
) -> None:
    """Run the interactive pre-submission safety checks shared by every submission entry point.

    Both submission entry points -- the ROP "Submit" button (``submit_callback``) and the Python
    Panel -- open the shared ``SubmitJobToDeadlineDialog`` and submit through the same guarded
    Submit button (see ``_install_pre_submission_gate``). Running these checks from that gate
    guarantees they are enforced consistently by both paths (and on every submission from the
    long-lived panel), rather than only by the ROP button.

    The checks prompt the user to resolve locked input ROPs (for example Karma), refresh the parsed
    file references when the hip file is missing from them, and save unsaved scene changes before
    the job bundle is built and uploaded. Each prompt is skipped when the corresponding
    ``auto_unlock_rops`` / ``auto_parse_hip`` / ``auto_save_hip`` toggle is enabled.

    The ``auto_*`` keyword arguments carry the user's *current* choices from the dialog's Scene
    Settings tab. They must be passed because the dialog only writes those toggles back to the node
    *after* submission begins -- reading them from the node here would use stale values and ignore
    the user's latest changes (e.g. unchecking "Automatically save scene"). When an argument is
    ``None`` the corresponding node parameter is used as a fallback (e.g. for direct/legacy callers
    and tests).

    Raises:
        UserInitiatedCancel: if the render node has no input ROPs, or the user chooses "Cancel" at
            any prompt. Raising (rather than returning a status) lets the shared dialog abort the
            submission gracefully via its own ``UserInitiatedCancel`` handling.
    """
    # Imported lazily: this runs only while the Qt dialog is active, so importing here keeps the
    # module import path free of the deadline-cloud UI dependency for headless hython.
    from deadline.client.exceptions import UserInitiatedCancel

    def _toggle(override: Optional[bool], parm_name: str) -> bool:
        # Prefer the value supplied by the caller (the dialog's current checkbox state); otherwise
        # fall back to the node parameter.
        if override is not None:
            return bool(override)
        return bool(node.parm(parm_name).eval())

    all_inputs = node.inputAncestors()

    if not all_inputs:
        # there are no inputs to the AWS Deadline Cloud render node
        raise UserInitiatedCancel(
            "The AWS Deadline Cloud render node (ROP) must have an input ROP specified to submit a job"
        )

    asset_references = _get_evaluated_asset_references(node)

    # check for locked rops, Karma for example
    locked_rops = []
    for n in all_inputs:

        # Check for and follow any fetch nodes
        n = _follow_fetch_nodes(n)

        node_path = n.path()
        if _is_node_locked(node_path):
            locked_rops.append(node_path)

    if locked_rops:
        auto_unlock = _toggle(auto_unlock_rops, "auto_unlock_rops")
        if not auto_unlock:
            buttons = (
                "Unlock and save",
                "Always unlock and save",
                "Ignore",
                "Cancel",
            )
            unlock_choice = hou.ui.displayMessage(
                "Locked ROPs found in network",
                title="Warning",
                buttons=buttons,
                details="\n".join(locked_rops),
            )
            if unlock_choice == 3:
                raise UserInitiatedCancel("Submission canceled.")
            if unlock_choice == 0:
                for n in locked_rops:
                    _unlock_node(n)
                hou.hipFile.save()
            if unlock_choice == 1:
                for n in locked_rops:
                    _unlock_node(n)
                node.parm("auto_unlock_rops").set(1)
                hou.hipFile.save()
        else:
            for n in locked_rops:
                _unlock_node(n)
            hou.hipFile.save()

    # check hip is listed in input_filenames
    hip_file = _get_hip_file()
    hip_norm = os.path.normcase(_normalize_asset_path(hip_file))
    hip_input = any(
        os.path.normcase(_normalize_asset_path(p)) == hip_norm
        for p in asset_references.input_filenames
    )
    if not hip_input:
        auto_parse = _toggle(auto_parse_hip, "auto_parse_hip")
        if not auto_parse:
            buttons = (
                "Parse and save",
                "Always parse and save",
                "Ignore",
                "Cancel",
            )
            parse_choice = hou.ui.displayMessage(
                "Hip file not found in file references", title="Warning", buttons=buttons
            )
            if parse_choice == 3:
                raise UserInitiatedCancel("Submission canceled.")
            if parse_choice == 0:
                _parse_files(node)
                hou.hipFile.save()
            if parse_choice == 1:
                _parse_files(node)
                node.parm("auto_parse_hip").set(1)
                hou.hipFile.save()
        else:
            _parse_files(node)
            hou.hipFile.save()

    # check for unsaved changes
    hip_unsaved = hou.hipFile.hasUnsavedChanges()
    if hip_unsaved:
        auto_save = _toggle(auto_save_hip, "auto_save_hip")
        if not auto_save:
            buttons = (
                "Save",
                "Always save",
                "Ignore",
                "Cancel",
            )
            save_choice = hou.ui.displayMessage(
                "Hip file has unsaved changes", title="Warning", buttons=buttons
            )
            if save_choice == 3:
                raise UserInitiatedCancel("Submission canceled.")
            if save_choice == 0:
                hou.hipFile.save()
            if save_choice == 1:
                node.parm("auto_save_hip").set(1)
                hou.hipFile.save()
        else:
            hou.hipFile.save()


def _is_hou_node_alive(node: Any) -> bool:
    """Return True if *node* is a valid, non-deleted Houdini Node (or unit-test mock)."""
    if node is None:
        return False
    try:
        node.path()
        return True
    except Exception:
        return False


def _safe_node_path(node: Any) -> str:
    """Return ``node.path()`` if *node* is alive and returns a string, else ``''``."""
    if node is None:
        return ""
    try:
        path = node.path()
        return path if isinstance(path, str) else ""
    except Exception:
        return ""


def _is_deadline_cloud_rop(node: Any) -> bool:
    """Return True if *node* is a live ``Driver/deadline_cloud`` (or ``deadline``) ROP node."""
    if not _is_hou_node_alive(node):
        return False
    try:
        type_name = node.type().name()
        if isinstance(type_name, str):
            return type_name in ("deadline", "deadline_cloud")
    except Exception:
        pass
    return False


def _resolve_live_rop_node(
    node: hou.Node,
    last_known_path: str = "",
    dialog: Any = None,
) -> hou.Node:
    """Return a live Deadline Cloud ROP ``hou.Node``, recovering if *node* was deleted.

    Houdini's Python Panel for ``<showInParametersPane optype="Driver/deadline_cloud"/>``
    runs ``onCreateInterface()`` once per Parameter Pane tab and reuses the panel widget when
    the artist deletes a Deadline Cloud ROP and drops a new one (or undo/redoes node creation,
    or switches between two Deadline Cloud ROPs). A ``hou.Node`` wraps the underlying C++
    ``OP_Node`` unique ID rather than a path, so the original ``hou.Node`` reference raises
    ``hou.ObjectWasDeleted`` even when a new node now exists at the same path.
    """
    dialog_dict = getattr(dialog, "__dict__", {}) if dialog is not None else {}

    pane_tab = dialog_dict.get("_deadline_pane_tab")
    if pane_tab is not None:
        try:
            current = pane_tab.currentNode()
            if _is_deadline_cloud_rop(current):
                dialog._deadline_active_rop_node = current
                return current
        except Exception:
            pass

    active = dialog_dict.get("_deadline_active_rop_node")
    if active is not None and _is_hou_node_alive(active):
        return active

    if _is_hou_node_alive(node):
        return node

    if last_known_path:
        try:
            by_path = hou.node(last_known_path)
            if _is_deadline_cloud_rop(by_path) or _is_hou_node_alive(by_path):
                if dialog is not None and "_deadline_active_rop_node" in dialog_dict:
                    dialog._deadline_active_rop_node = by_path
                return by_path
        except Exception:
            pass

    try:
        for selected in hou.selectedNodes():
            if _is_deadline_cloud_rop(selected):
                if dialog is not None and "_deadline_active_rop_node" in dialog_dict:
                    dialog._deadline_active_rop_node = selected
                return selected
    except Exception:
        pass

    return node


def _refresh_bundle_asset_references(
    live_node: hou.Node,
    asset_references: AssetReferences,
    widget: Any = None,
) -> None:
    """Refresh auto-detected scene attachments at bundle-creation time.

    ``SubmitJobToDeadlineDialog`` snapshots ``auto_detected_attachments`` once when the Python
    Panel or submit dialog opens. If the user does ``File -> Save As`` (e.g. ``v021.hip`` ->
    ``v022.hip``), edits USD/texture references, or wires upstream ROPs while the panel stays
    open, that snapshot becomes stale (uploading old ``.hip`` / gallery files or missing newly
    added dependencies and output directories).

    When *widget* exposes a real ``job_attachments`` widget with an ``AssetReferences``
    ``attachments`` attribute (manual user additions), this re-scans
    ``_get_scene_asset_references(live_node, evaluate=True)`` and replaces only the
    auto-detected portion while preserving every manual user attachment. Otherwise it falls back
    to populating empty ``output_directories`` from *live_node*'s current input ancestors.
    """
    if not isinstance(asset_references, AssetReferences):
        return

    job_attachments_widget = getattr(widget, "job_attachments", None) if widget is not None else None
    user_attachments = (
        getattr(job_attachments_widget, "attachments", None)
        if job_attachments_widget is not None
        else None
    )

    if isinstance(user_attachments, AssetReferences):
        try:
            fresh_auto = _get_scene_asset_references(live_node, evaluate=True)
        except Exception as exc:
            print(f"Warning: could not refresh scene asset references on bundle creation: {exc}")
        else:
            merged = fresh_auto.union(user_attachments)
            asset_references.input_filenames = set(merged.input_filenames)
            asset_references.input_directories = set(merged.input_directories)
            asset_references.output_directories = set(merged.output_directories)
            asset_references.referenced_paths = set(merged.referenced_paths)
            try:
                if hasattr(job_attachments_widget, "refresh_ui"):
                    job_attachments_widget.refresh_ui(fresh_auto, None)
                else:
                    job_attachments_widget.auto_detected_attachments = fresh_auto
            except Exception:
                try:
                    job_attachments_widget.auto_detected_attachments = fresh_auto
                except Exception:
                    pass
            return

    if not asset_references.output_directories:
        try:
            for anc in live_node.inputAncestors():
                asset_references.output_directories.update(
                    _normalize_asset_path(d) for d in _get_output_directories(anc) if d
                )
        except Exception:
            pass


def _make_create_bundle_callback(rop_node):
    """
    Create an on_create_job_bundle_callback compatible with SubmitJobToDeadlineDialog's Protocol.

    The returned function captures the ROP node and delegates to _create_job_bundle
    when the dialog invokes it during submission or bundle export.
    """
    state = {"node": rop_node, "path": _safe_node_path(rop_node)}

    def _on_create_job_bundle(
        widget,
        job_bundle_dir: str,
        settings,
        queue_parameters: list,
        asset_references: AssetReferences,
        host_requirements: Optional[Dict[str, Any]] = None,
        *,
        purpose,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        # Approach A: persist the dialog's edits back onto the node so they save with the scene and
        # drive the generated bundle (which is still built by reading the node). Frame range/take
        # are written by the panel's own frame controls, so they're excluded from
        # write_ui_settings_to_node.
        #
        # NOTE: the pre-submission safety checks (locked ROPs / file parse / unsaved-scene save) are
        # deliberately NOT run here. By the time the dialog invokes this callback it has already
        # shown its modal submission-progress dialog, which suppresses the hou.ui.displayMessage
        # prompts (they never appear). The checks instead run when the Submit button is clicked,
        # before on_submit -- see _install_pre_submission_gate.
        live_node = _resolve_live_rop_node(
            state["node"], last_known_path=state["path"], dialog=widget
        )
        state["node"] = live_node
        state["path"] = _safe_node_path(live_node) or state["path"]
        _refresh_bundle_asset_references(live_node, asset_references, widget=widget)
        write_ui_settings_to_node(live_node, settings)
        set_queue_parameter_values_from_openjd(live_node, queue_parameters)
        _ods_store_host_requirements(live_node, host_requirements)
        _create_job_bundle(live_node, job_bundle_dir, asset_references, host_requirements)
        return None

    return _on_create_job_bundle


# ODS: node user-data key holding the Host requirements tab's last value (OpenJD
# "hostRequirements" dict as JSON). User data is saved with the hip, needs no HDA parm, and is
# ignored by stock code.
_ODS_HOST_REQUIREMENTS_USERDATA = "ods_host_requirements"


def _ods_store_host_requirements(
    node: hou.Node, host_requirements: Optional[Dict[str, Any]]
) -> None:
    """Remember the dialog's host requirements on the node so the next dialog can restore them.

    ``None`` means the tab was left on "Use all available worker hosts", so the stored value is
    removed and the next dialog opens in that default mode as well.
    """
    try:
        if host_requirements:
            node.setUserData(_ODS_HOST_REQUIREMENTS_USERDATA, json.dumps(host_requirements))
        elif node.userData(_ODS_HOST_REQUIREMENTS_USERDATA) is not None:
            node.destroyUserData(_ODS_HOST_REQUIREMENTS_USERDATA)
    except Exception as exc:  # never block a submit over this
        print(f"ODS: could not store host requirements on {node.path()}: {exc}")


def get_saved_host_requirements(node: hou.Node):
    """Return the host requirements stored on *node* as a dialog ``HostRequirements``, or None.

    Used to seed ``SubmitJobToDeadlineDialog(host_requirements=...)`` so the Host requirements tab
    keeps its values across dialog opens, hip saves and Houdini restarts.
    """
    raw = node.userData(_ODS_HOST_REQUIREMENTS_USERDATA)
    if not raw:
        return None
    try:
        from deadline.client.ui.dataclasses import HostRequirements

        return HostRequirements.from_dict(json.loads(raw))
    except Exception as exc:  # bad/old data or an older client without from_dict
        print(f"ODS: ignoring stored host requirements on {node.path()}: {exc}")
        return None


def _install_pre_submission_gate(dialog, node) -> None:
    """Rewire the dialog's Submit button so the pre-submission checks run BEFORE submission.

    The shared ``SubmitJobToDeadlineDialog`` wires its Submit button directly to ``on_submit``,
    which immediately shows a modal submission-progress dialog and only then invokes the job-bundle
    callback. Running the interactive checks (locked ROPs / file parse / unsaved-scene save) from
    that callback is too late: the modal progress dialog is already on screen and suppresses the
    ``hou.ui.displayMessage`` prompts, so the user never sees them.

    Instead we intercept the Submit button's ``clicked`` signal (mirroring the login-signal
    rewiring in the Python Panel) so the checks -- and the chance to cancel -- happen before
    ``on_submit`` runs. This gate is shared by both submission entry points: the ROP "Submit" button
    (``submit_callback``) and the Python Panel.
    """
    from deadline.client.exceptions import UserInitiatedCancel

    state = {"node": node, "path": _safe_node_path(node)}

    def _guarded_submit() -> None:
        # Read the user's CURRENT auto_* choices from the dialog's Scene Settings tab. The dialog
        # only writes these toggles back to the node during on_submit (after this gate runs), so
        # reading the node here would use stale values -- e.g. the user unchecking "Automatically
        # save scene" would be ignored. Fall back to the node parms if the widget is missing or its
        # structure changes.
        live_node = _resolve_live_rop_node(
            state["node"], last_known_path=state["path"], dialog=dialog
        )
        state["node"] = live_node
        state["path"] = _safe_node_path(live_node) or state["path"]

        toggles: Dict[str, Any] = {}
        scene_widget = getattr(dialog, "job_settings", None)
        if scene_widget is not None:
            try:
                toggles = {
                    "auto_unlock_rops": scene_widget.auto_unlock_rops_check.isChecked(),
                    "auto_parse_hip": scene_widget.auto_parse_hip_check.isChecked(),
                    "auto_save_hip": scene_widget.auto_save_hip_check.isChecked(),
                }
            except AttributeError:
                toggles = {}

        try:
            _run_pre_submission_checks(live_node, **toggles)
        except UserInitiatedCancel:
            # The node has no inputs, or the user chose "Cancel" at a prompt: abort quietly without
            # submitting. The dialog stays open so the user can adjust and try again.
            return
        dialog.on_submit()

    # Disconnect the default Submit->on_submit wiring and route through the guarded wrapper. Fall
    # back to clearing all slots if the bound-method match fails (mirrors the login rewiring).
    try:
        dialog.submit_button.clicked.disconnect(dialog.on_submit)
    except (TypeError, RuntimeError):
        dialog.submit_button.clicked.disconnect()
    dialog.submit_button.clicked.connect(_guarded_submit)
    # Keep a strong reference to the closure on the dialog so PySide does not garbage-collect the
    # connected slot (a known binding gotcha where connections to bare Python closures can silently
    # drop once the local goes out of scope).
    dialog._deadline_guarded_submit = _guarded_submit  # type: ignore[attr-defined]


def _install_queue_parameter_sync(dialog, node: hou.Node) -> None:
    """Keep dialog and ROP queue parameters synchronized across farm/queue switches.

    ``SharedJobSettingsWidget`` caches ``initial_shared_parameter_values`` for the life of the
    dialog and re-applies them on every ``_handle_queue_parameters_update`` call. When the user
    switches to a different farm or queue in the UI, that stale cache overwrites the new queue's
    defaults with the previous queue's values, and the ROP node's spare parameters remain tied to
    the previous queue.

    This hook intercepts queue-parameter updates from ``DeadlineUIController`` so that:
    * Switching to a new farm or queue clears ``initial_shared_parameter_values`` and resets the
      UI and the ROP node's spare parameters to the new queue's defaults.
    * A queue's non-empty ``CondaPackages`` / ``RezPackages`` default is honoured; the Houdini
      version wildcard fallback (``houdini=X.Y.* houdini-openjd=A.B.*``) is used only when the
      queue's own default is empty or unset.
    * Interactive edits to queue parameters in the dialog are persisted to the ROP node immediately.
    """
    shared_settings = getattr(dialog, "shared_job_settings", None)
    if shared_settings is None:
        return
    orig_handler = getattr(shared_settings, "_handle_queue_parameters_update", None)
    if not callable(orig_handler):
        return

    state = {"node": node, "path": _safe_node_path(node)}

    def _live_node() -> hou.Node:
        live = _resolve_live_rop_node(
            state["node"], last_known_path=state["path"], dialog=dialog
        )
        state["node"] = live
        state["path"] = _safe_node_path(live) or state["path"]
        return live

    initial_farm_id = get_setting("defaults.farm_id") or ""
    initial_queue_id = get_setting("defaults.queue_id") or ""
    try:
        curr_node = _live_node()
        node_farm_id = curr_node.userData("deadline_farm_id")
        node_queue_id = curr_node.userData("deadline_queue_id")
    except Exception:
        node_farm_id = None
        node_queue_id = None

    if (
        isinstance(node_queue_id, str)
        and node_queue_id
        and initial_queue_id
        and (node_farm_id or initial_farm_id, node_queue_id) != (initial_farm_id, initial_queue_id)
    ):
        shared_settings.initial_shared_parameter_values.clear()

    active_target = {
        "farm_id": initial_farm_id,
        "queue_id": initial_queue_id,
        "user_switched": False,
    }

    def _mark_user_switched(*_args) -> None:
        active_target["user_switched"] = True
        shared_settings.initial_shared_parameter_values.clear()

    settings_box = getattr(shared_settings, "deadline_cloud_settings_box", None)
    if settings_box is not None:
        for box_name in ("farm_box", "queue_box"):
            box_ctrl = getattr(settings_box, box_name, None)
            user_selected_sig = getattr(box_ctrl, "user_selected", None)
            if user_selected_sig is not None and hasattr(user_selected_sig, "connect"):
                user_selected_sig.connect(_mark_user_switched)

    def _on_queue_parameters_updated(queue_parameters: list) -> None:
        farm_id = get_setting("defaults.farm_id") or ""
        queue_id = get_setting("defaults.queue_id") or ""
        target_changed = active_target["user_switched"] or (farm_id, queue_id) != (
            active_target["farm_id"],
            active_target["queue_id"],
        )
        active_target["farm_id"] = farm_id
        active_target["queue_id"] = queue_id
        if queue_id:
            active_target["user_switched"] = False

        if target_changed:
            shared_settings.initial_shared_parameter_values.clear()

        raw_definitions = [
            {k: v for k, v in copy.deepcopy(param).items() if k != "value"}
            for param in queue_parameters
        ]

        default_conda = get_default_conda_packages()
        default_rez = get_default_rez_packages()

        for parameter in queue_parameters:
            if target_changed:
                parameter.pop("value", None)
            raw_default = parameter.get("default")
            has_queue_default = isinstance(raw_default, str) and bool(raw_default.strip())
            effective_default = get_effective_queue_parameter_default(parameter)
            if effective_default is not None:
                parameter["default"] = effective_default
            name = parameter.get("name")
            if has_queue_default and name == "CondaPackages":
                if shared_settings.initial_shared_parameter_values.get(name) == default_conda:
                    shared_settings.initial_shared_parameter_values.pop(name, None)
            elif has_queue_default and name == "RezPackages":
                if shared_settings.initial_shared_parameter_values.get(name) == default_rez:
                    shared_settings.initial_shared_parameter_values.pop(name, None)

        orig_handler(queue_parameters)

        if not (farm_id and queue_id):
            return

        try:
            live_node = _live_node()
            current_values = shared_settings.get_parameters()
            apply_queue_parameter_definitions_to_node(
                live_node,
                raw_definitions,
                farm_id=farm_id,
                queue_id=queue_id,
                preserve_existing=False,
            )
            set_queue_parameter_values_from_openjd(live_node, current_values)
            if settings_box is not None:
                farm_combo = getattr(getattr(settings_box, "farm_box", None), "box", None)
                queue_combo = getattr(getattr(settings_box, "queue_box", None), "box", None)
                if farm_combo is not None and live_node.parm("farm") is not None:
                    farm_text = farm_combo.currentText()
                    if farm_text:
                        live_node.parm("farm").set(farm_text)
                if queue_combo is not None and live_node.parm("queue") is not None:
                    queue_text = queue_combo.currentText()
                    if queue_text:
                        live_node.parm("queue").set(queue_text)
        except Exception:
            pass

    def _on_parameter_changed(parameter: dict) -> None:
        name = parameter.get("name", "")
        if not name or name.startswith("deadline:"):
            return
        shared_settings.initial_shared_parameter_values[name] = parameter.get("value")
        try:
            set_queue_parameter_values_from_openjd(_live_node(), [parameter])
        except Exception:
            pass

    controller = getattr(shared_settings, "_controller", None)
    if controller is not None and hasattr(controller, "queue_parameters_updated"):
        try:
            from qtpy.QtCore import Qt  # type: ignore

            conn_type = Qt.QueuedConnection
        except Exception:
            conn_type = None
        try:
            controller.queue_parameters_updated.disconnect(orig_handler)
        except (TypeError, RuntimeError):
            pass
        if conn_type is not None:
            controller.queue_parameters_updated.connect(_on_queue_parameters_updated, conn_type)
        else:
            controller.queue_parameters_updated.connect(_on_queue_parameters_updated)

        destroyed_sig = getattr(shared_settings, "destroyed", None)
        if destroyed_sig is not None and hasattr(destroyed_sig, "connect"):
            def _disconnect_on_destroy(*_args) -> None:
                try:
                    controller.queue_parameters_updated.disconnect(_on_queue_parameters_updated)
                except (TypeError, RuntimeError):
                    pass

            destroyed_sig.connect(_disconnect_on_destroy)
            dialog._deadline_queue_sync_destroy = _disconnect_on_destroy  # type: ignore[attr-defined]

    param_changed_sig = getattr(shared_settings, "parameter_changed", None)
    if param_changed_sig is not None and hasattr(param_changed_sig, "connect"):
        param_changed_sig.connect(_on_parameter_changed)

    shared_settings._handle_queue_parameters_update = _on_queue_parameters_updated
    dialog._deadline_queue_parameter_sync = _on_queue_parameters_updated  # type: ignore[attr-defined]
    dialog._deadline_queue_param_changed = _on_parameter_changed  # type: ignore[attr-defined]
    dialog._deadline_mark_user_switched = _mark_user_switched  # type: ignore[attr-defined]


def submit_callback(kwargs):
    node = kwargs["node"]

    # Early guard: an inputless node cannot produce a job, so fail fast before opening the dialog.
    # _run_pre_submission_checks re-checks this at submission time to also cover the Python Panel
    # path, which opens the dialog directly without going through submit_callback.
    if not node.inputAncestors():
        hou.ui.displayMessage(
            "The AWS Deadline Cloud render node (ROP) must have an input ROP specified to submit a job",
            title="Missing Input Render Node",
            severity=hou.severityType.Warning,
        )
        return

    try:
        # Initialize telemetry client, opt-out is respected
        api.get_deadline_cloud_library_telemetry_client().update_common_details(
            {
                "deadline-cloud-for-houdini-submitter-version": version,
                "houdini-version": _get_houdini_version(),
            }
        )
        farm_id = get_setting("defaults.farm_id")
        if not farm_id:
            hou.ui.displayMessage(
                "Please configure the farm ID in the AWS Deadline Cloud render node (ROP) settings",
                title="Farm ID Required",
                severity=hou.severityType.Warning,
            )
            return

        queue_id = get_setting("defaults.queue_id")
        if not queue_id:
            hou.ui.displayMessage(
                "Please configure the queue ID in the AWS Deadline Cloud render node (ROP) settings",
                title="Queue ID Required",
                severity=hou.severityType.Warning,
            )
            return

        ui_settings = read_ui_settings_from_node(node)

        # ODS G6: seed from the node's saved queue parameters, defaults only where unset.
        shared_parameter_values: dict = get_shared_parameter_values_for_dialog(node)

        # Lazy import: keep deadline-cloud's Qt/PySide UI out of the module-import path so
        # headless hython (e.g. integ tests importing _create_job_bundle) does not pull in Qt.
        from deadline.client.ui.dialogs.submit_job_to_deadline_dialog import (
            SubmitJobToDeadlineDialog,
        )
        from .houdini_submitter_widget import SceneSettingsWidget
        from qtpy.QtCore import Qt  # type: ignore

        dialog = SubmitJobToDeadlineDialog(
            job_setup_widget_type=SceneSettingsWidget,
            initial_job_settings=ui_settings,
            initial_shared_parameter_values=shared_parameter_values,
            auto_detected_attachments=_get_scene_asset_references(node, evaluate=True),
            attachments=AssetReferences(),
            on_create_job_bundle_callback=_make_create_bundle_callback(node),
            submitter_info=SubmitterInfo(
                submitter_name="Houdini",
                submitter_package_name="deadline-cloud-for-houdini",
                submitter_package_version=version,
                host_application_name="Houdini",
                host_application_version=hou.applicationVersionString(),
            ),
            f=Qt.Tool,
            show_host_requirements_tab=True,
            # ODS: restore the Host requirements tab from the node (persisted on submit/export).
            host_requirements=get_saved_host_requirements(node),
            use_deadline_cloud_v2_channel=True,
            parent=hou.qt.mainWindow(),
        )
        # Run the pre-submission checks when Submit is clicked, before the dialog opens its modal
        # progress dialog (which would otherwise hide the hou.ui.displayMessage prompts).
        _install_pre_submission_gate(dialog, node)
        _install_queue_parameter_sync(dialog, node)
        dialog.exec_()
    except Exception as exc:
        api.get_deadline_cloud_library_telemetry_client().record_error(
            event_details={"exception_scope": "caught", "error_operation": "submit_callback"},
            exception_type=str(type(exc)),
            from_gui=True,
        )
        print(str(exc))
        hou.ui.displayMessage(
            str(exc),
            title="Deadline Cloud Job Submission",
            severity=hou.severityType.Warning,
            details=traceback.format_exc(),
        )


def settings_callback(kwargs):
    node = kwargs["node"]
    prev_farm_id = get_setting("defaults.farm_id")
    prev_queue_id = get_setting("defaults.queue_id")
    _show_farm_and_queue_as_refreshing(node)
    # Lazy import: keep deadline-cloud's Qt/PySide UI out of the module-import path so
    # headless hython (e.g. integ tests importing _create_job_bundle) does not pull in Qt.
    from deadline.client.ui.dialogs import DeadlineConfigDialog

    DeadlineConfigDialog.configure_settings(parent=hou.qt.mainWindow())
    try:
        new_farm_id = get_setting("defaults.farm_id")
        new_queue_id = get_setting("defaults.queue_id")
        target_changed = (new_farm_id, new_queue_id) != (prev_farm_id, prev_queue_id)
        _apply_farm_and_queue_settings(node, preserve_existing=not target_changed)
    except Exception as exc:
        hou.ui.displayMessage(
            str(exc),
            title="Deadline Cloud",
            severity=hou.severityType.Warning,
            details=traceback.format_exc(),
        )


def login_callback(kwargs):
    node = kwargs["node"]
    _show_farm_and_queue_as_refreshing(node)
    # Lazy import: keep deadline-cloud's Qt/PySide UI out of the module-import path so
    # headless hython (e.g. integ tests importing _create_job_bundle) does not pull in Qt.
    from deadline.client.ui.dialogs import DeadlineLoginDialog

    DeadlineLoginDialog.login(parent=hou.qt.mainWindow())
    try:
        _apply_farm_and_queue_settings(node)
    except Exception as exc:
        hou.ui.displayMessage(
            str(exc),
            title="Deadline Cloud",
            severity=hou.severityType.Warning,
            details=traceback.format_exc(),
        )


def logout_callback(kwargs):
    node = kwargs["node"]
    node.parm("farm").set(_NONE_SELECTED_TEXT)
    node.parm("queue").set(_NONE_SELECTED_TEXT)
    api.logout()


def update_queue_parameters_callback(kwargs):
    node = kwargs["node"]
    _show_farm_and_queue_as_refreshing(node)
    _apply_farm_and_queue_settings(node)


def _show_farm_and_queue_as_refreshing(node):
    node.parm("farm").set(_REFRESHING_TEXT)
    node.parm("queue").set(_REFRESHING_TEXT)


def _apply_farm_and_queue_settings(node, *, preserve_existing: bool = True):
    farm_id = get_setting("defaults.farm_id")
    if not farm_id:
        node.parm("farm").set(_NONE_SELECTED_TEXT)
        node.parm("queue").set(_NONE_SELECTED_TEXT)
        return
    deadline = api.get_boto3_client("deadline")
    try:
        farm_response = deadline.get_farm(farmId=farm_id)
        node.parm("farm").set(farm_response["displayName"])
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "AccessDeniedException":
            node.parm("farm").set(farm_id)
        else:
            raise
    queue_id = get_setting("defaults.queue_id")
    if not queue_id:
        node.parm("queue").set(_NONE_SELECTED_TEXT)
        return
    queue_response = deadline.get_queue(farmId=farm_id, queueId=queue_id)
    node.parm("queue").set(queue_response["displayName"])
    if preserve_existing:
        update_queue_parameters(farm_id, queue_id, node)
    else:
        update_queue_parameters(farm_id, queue_id, node, preserve_existing=False)


# TODO: remove this and swap to default job template
def get_houdini_environments(init_data_attachment: dict[str, Any]) -> list[dict[str, Any]]:
    """Returns a list of environments that set things up to run frame renders
    for the specified DCC.
    """
    return [
        {
            "name": "Houdini",
            "description": "Runs Houdini in the background.",
            "script": {
                "embeddedFiles": [
                    init_data_attachment,
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
                        "cancelation": {
                            "mode": "NOTIFY_THEN_TERMINATE",
                        },
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
                        "cancelation": {
                            "mode": "NOTIFY_THEN_TERMINATE",
                        },
                        "timeout": 120,
                    },
                },
            },
        }
    ]
