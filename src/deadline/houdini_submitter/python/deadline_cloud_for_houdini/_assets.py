# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
import difflib
import glob
import os
from os.path import normpath as _os_normpath
import re
from typing import Optional

from deadline.client.config.config_file import str2bool
from deadline.client.job_bundle.submission import AssetReferences
from pxr import Ar, Usd, UsdUtils

import hou


# --- ODS: UNC -> mapped-drive remap --------------------------------------------
# Houdini/USD resolve a mapped network drive (e.g. X:) to its UNC target via
# layer.realPath / os.path.realpath(). The DC asset collector then uploads files
# under that UNC path, while the render USD still references them by drive letter
# (X:\...). On the worker, X:\ and the UNC root map to DIFFERENT job-attachment
# assetroots, so a file uploaded as UNC is not found where the USD looks for it
# (e.g. a camera sublayer -> "no cameras found"). Remapping UNC back to the drive
# letter before building the manifest keeps both forms in one assetroot.
def _ods_build_unc_drive_map() -> dict:
    """Map UNC target (lowercased, no trailing slash) -> 'X:' for each mapped drive.

    Uses os.path.realpath("<letter>:/") which resolves a mapped network drive to its
    UNC target (e.g. 'X:/' -> '\\\\host\\fsx\\'). Reliable inside hython, unlike
    WNetGetConnectionW via ctypes. Only drives whose realpath differs as a UNC are mapped.
    """
    mapping = {}
    for i in range(26):
        letter = "{}:".format(chr(ord("A") + i))
        root = letter + "\\"
        if not os.path.exists(root):
            continue
        try:
            real = os.path.realpath(root)
        except Exception:
            continue
        real_n = real.replace("/", "\\").rstrip("\\")
        if real_n.startswith("\\\\"):  # a UNC target (mapped network drive)
            mapping[real_n.lower()] = letter
    return mapping

_ODS_UNC_DRIVE_MAP = None

def _ods_unc_to_drive(path: str) -> str:
    """If *path* is under a mapped drive's UNC target, rewrite it to the drive letter."""
    global _ODS_UNC_DRIVE_MAP
    if not path:
        return path
    norm = path.replace("/", "\\")
    if not norm.startswith("\\\\"):
        return path  # not a UNC path
    if _ODS_UNC_DRIVE_MAP is None:
        _ODS_UNC_DRIVE_MAP = _ods_build_unc_drive_map()
    low = norm.lower()
    for unc, letter in _ODS_UNC_DRIVE_MAP.items():
        if low == unc or low.startswith(unc + "\\"):
            return letter + norm[len(unc):]
    return path

_IGNORE_REF_VALUES = ("opdef:", "oplib:", "temp:", "op:")
_IGNORE_REF_PARMS = (
    "taskgraphfile",
    "pdg_workingdir",
    "soho_program",
    "BakeView_img_file_path",
    "OutputDeepWriter_file",
    "SettingsOutput_img_file_path",
    "RS_outputFileNamePrefix",
    "savetodirectory_directory",
)


_ODS_USE_HUSK_ENV = "ODS_HOUDINI_USE_HUSK"
_ODS_USE_HUSK_PARM = "ods_use_husk"


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


def _get_hip_file() -> str:
    return hou.hipFile.path()


# Houdini time variables ($F, $F4, $FF, $T, $SF, $ST and their ${...} forms). The negative
# lookahead keeps $TEMP, $TMP, $STAGE... from being read as $T or $ST.
_TIME_VAR_REGEX = "|".join(
    (
        r"\$\{(?:FF|F\d*|T|SF|ST)\}",
        r"\$(?:FF|F\d*)",
        r"\$(?:T|SF|ST)(?![A-Za-z0-9_])",
    )
)
_EXPLICIT_TIME_VAR_PATTERN = re.compile(_TIME_VAR_REGEX)
# References whose value depends on the node that owns the parm: $OS / ${OS} and any
# backtick expression (chs(), opname(), @context_option, ...). Copying such a string
# onto another node (the Deadline Cloud ROP) changes what it evaluates to.
_NODE_LOCAL_REF_PATTERN = re.compile(r"\$OS\b|\$\{OS\}|`")
# UDIM tile tokens used by USD/Arnold/Mari texture paths (e.g. tex.<UDIM>.png or tex.%(UDIM)d.png).
_UDIM_TOKEN_PATTERN = re.compile(r"<UDIM>|%\(UDIM\)0?4?d")


def _normalize_asset_path(path: str) -> str:
    """Normalize path separators and remap mapped-drive UNC targets to drive letters.

    Args:
        path: Filesystem path to normalize.

    Returns:
        Normalized path string, or the original value if empty.
    """
    if not path:
        return path
    return _os_normpath(_ods_unc_to_drive(path))


def _is_parm_time_dependent(parm, ref: str) -> bool:
    """Return True if *ref* varies over time and should be glob-expanded across frames.

    Explicit frame/time variables ($F, $FF, $T, $SF, $ST) are always time-dependent.
    Backtick expressions may be constant context options (e.g. ``@SHOT_VERSION``) or
    channel references (``chs(...)``), so ``parm.isTimeDependent()`` is consulted when
    it returns a real bool.

    Args:
        parm: Houdini Parm owning the reference.
        ref: Unexpanded Houdini path string.

    Returns:
        True when the path should be expanded as a frame glob.
    """
    if _EXPLICIT_TIME_VAR_PATTERN.search(ref):
        return True
    if "`" in ref:
        try:
            td = parm.isTimeDependent()
        except Exception:
            td = None
        if isinstance(td, bool):
            return td
        return True
    return False


def _is_node_local_ref(ref: str) -> bool:
    """Return True if *ref* cannot be copied verbatim onto another node (e.g. the DC ROP).

    ``$OS`` and backtick expressions evaluate against whichever node holds the parameter.
    """
    return bool(_NODE_LOCAL_REF_PATTERN.search(ref))


_GLOB_SENTINEL = "\ue000"


def _glob_from_two_evaluations(s1: str, s2: str) -> str:
    """Build a glob from one parm evaluated at two different frames.

    Every span that differs between *s1* and *s2* becomes ``*`` (so ``$F4/shot_$F4.exr``
    yields two wildcards). Digits next to a wildcard are dropped from the constant text,
    because they may be the shared leading/trailing digits of the frame number. Constant
    text is glob-escaped.
    """
    ops = difflib.SequenceMatcher(None, s1, s2, autojunk=False).get_opcodes()
    pieces: list[Optional[str]] = [
        s1[i1:i2] if tag == "equal" else None for tag, i1, i2, _, _ in ops
    ]
    out: list[str] = []
    for index, piece in enumerate(pieces):
        if piece is None:
            out.append("*")
            continue
        if index > 0:
            piece = piece.lstrip("0123456789")
        if index < len(pieces) - 1:
            piece = piece.rstrip("0123456789")
        out.append(glob.escape(piece))
    return re.sub(r"\*+", "*", "".join(out))


def _derive_frame_glob_from_parm(parm, ref: str, evaluated_path: str) -> str:
    """Build a filesystem glob for a time-dependent *parm* without mutating it.

    Mutating arbitrary scene parameters fails on locked HDAs and dirties the hip file.

    * Global-only refs (no ``$OS``, no backticks): swap each time variable for a sentinel,
      let ``hou.text.expandString`` expand the remaining variables (``$HIP`` ...), then turn
      the sentinels into ``*``. This also covers ``$SF``/``$ST``, which do not change when
      the parm is evaluated at a different frame.
    * Node-local refs: compare ``parm.evalAsStringAtFrame(f)`` with ``(f + 1)`` in the
      owning node's own context and wildcard every span that differs.

    Returns:
        Filesystem glob pattern, or *evaluated_path* if no glob can be derived.
    """
    if not _is_node_local_ref(ref):
        try:
            marked = _EXPLICIT_TIME_VAR_PATTERN.sub(_GLOB_SENTINEL, ref)
            expanded = hou.text.expandString(marked)
            if (
                isinstance(expanded, str)
                and expanded
                and not re.search(r"\$(?:[A-Za-z_]\w*|\{[A-Za-z_]\w*\})", expanded)
            ):
                return glob.escape(expanded).replace(_GLOB_SENTINEL, "*")
        except Exception:
            pass

    try:
        frame = hou.frame()
        if not isinstance(frame, (int, float)):
            frame = 1.0
        s1 = parm.evalAsStringAtFrame(frame)
        s2 = parm.evalAsStringAtFrame(frame + 1)
    except Exception:
        return evaluated_path

    if not (isinstance(s1, str) and isinstance(s2, str)) or not s1 or s1 == s2:
        return evaluated_path
    return _glob_from_two_evaluations(s1, s2)


def _expand_udim_paths(path: str) -> set[str]:
    """Expand a path containing a ``<UDIM>`` or ``%(UDIM)d`` token into concrete tile files.

    Replaces each UDIM token with ``[0-9][0-9][0-9][0-9]`` (while glob-escaping the rest of the
    path) and returns all matching on-disk files. Returns an empty set if *path* has no UDIM token
    or no tiles exist on disk.
    """
    if not path or not _UDIM_TOKEN_PATTERN.search(path):
        return set()
    marked = _UDIM_TOKEN_PATTERN.sub(_GLOB_SENTINEL, path)
    pattern = glob.escape(marked).replace(_GLOB_SENTINEL, "[0-9][0-9][0-9][0-9]")
    return {_normalize_asset_path(m) for m in glob.iglob(pattern) if os.path.isfile(m)}


def _expand_parm_file_paths(parm, ref: str, evaluated_path: str) -> set[str]:
    """Expand a scene parameter file reference into concrete normalized filesystem paths.

    Args:
        parm: Houdini Parm owning the reference.
        ref: Unexpanded Houdini path string from ``hou.fileReferences()``.
        evaluated_path: Result of ``parm.evalAsString()``.

    Returns:
        Set of concrete normalized file paths on disk.
    """
    if not _is_parm_time_dependent(parm, ref):
        udim_matches = _expand_udim_paths(evaluated_path)
        if udim_matches:
            return udim_matches
        return {_normalize_asset_path(evaluated_path)}

    glob_pattern = _derive_frame_glob_from_parm(parm, ref, evaluated_path)
    if _UDIM_TOKEN_PATTERN.search(glob_pattern):
        glob_pattern = _UDIM_TOKEN_PATTERN.sub("[0-9][0-9][0-9][0-9]", glob_pattern)
    matches = {
        _normalize_asset_path(match) for match in glob.iglob(glob_pattern) if os.path.isfile(match)
    }
    if matches:
        return matches
    return {_normalize_asset_path(evaluated_path)}


def _houdini_time_vars_to_glob(path: str) -> str:
    """
    Given a path, replace any houdini time-based variables or expressions with globs
    to capture complete file sequences for job attachments. If there is no time-based
    parameterization, then the original string is returned.

    These are the useful timebased variables that can be included in files.
    ref: https://www.sidefx.com/docs/houdini/render/expressions.html
    """
    # A backtick expression is replaced one span at a time ([^`]*), so two expressions in
    # one path do not swallow the text between them.
    return re.sub(rf"`[^`]*`|{_TIME_VAR_REGEX}", "*", path)


def _get_evaluated_glob_path(parm, globbed_path: str) -> str:
    """Given a parm that contains a path, temporarily set it to a globbed path
    and perform an evaluation to remove all other variables, return the value
    and ensure that we put the original value back.

    Rationale: Houdini does not appear to provide a way to evaluate an arbitary
    string, only as a Parm. Thus, we can use this function to put a value into
    a parm to then evaluate.
    """
    orig_path = parm.unexpandedString()
    try:
        parm.set(globbed_path)
        return parm.eval()
    finally:
        parm.set(orig_path)


def _get_path_parms(rop_node: hou.Node, name: str) -> tuple[hou.Parm, ...]:
    """Return the path multiparm instances when the ROP has the parameter."""
    parm = rop_node.parm(name)
    return parm.multiParmInstances() if parm is not None else ()


def _get_evaluated_asset_references(
    rop_node: hou.Node, *, manual_only: bool = False, use_husk: Optional[bool] = None
) -> AssetReferences:
    """Evaluate paths saved on the ROP, filtering old automatic attachments.

    Automatic directory inputs are obsolete in both render modes. Husk also
    replaces automatically detected files with dependencies from the export.
    """
    asset_references = AssetReferences()
    saved_auto_refs = _get_saved_auto_detected_asset_references(rop_node)
    filter_auto_files = manual_only or (_ods_use_husk(rop_node) if use_husk is None else use_husk)

    for n in _get_path_parms(rop_node, "input_filenames"):
        filepath = n.unexpandedString()
        if filter_auto_files and filepath in saved_auto_refs.input_filenames:
            continue
        filepath_glob = _houdini_time_vars_to_glob(filepath)
        if filepath == filepath_glob or not _is_parm_time_dependent(n, filepath):
            evaluated = n.eval()
            if evaluated:
                udim_matches = _expand_udim_paths(evaluated)
                if udim_matches:
                    asset_references.input_filenames.update(udim_matches)
                else:
                    asset_references.input_filenames.add(_normalize_asset_path(evaluated))
            continue

        evaluated_glob = _get_evaluated_glob_path(parm=n, globbed_path=filepath_glob)
        if _UDIM_TOKEN_PATTERN.search(evaluated_glob):
            evaluated_glob = _UDIM_TOKEN_PATTERN.sub("[0-9][0-9][0-9][0-9]", evaluated_glob)
        # This is a work-around to avoid evaluating the offsets for
        # scene introspection. It's possible that this will capture
        # extra files since a glob will not enforce numbers, but
        # handles well-defined file path patterns
        for file in glob.iglob(evaluated_glob):
            asset_references.input_filenames.add(_normalize_asset_path(file))

    asset_references.input_directories.update(
        _normalize_asset_path(path)
        for parm in _get_path_parms(rop_node, "input_directories")
        if parm.unexpandedString() not in saved_auto_refs.input_directories
        and (path := parm.eval())
    )
    asset_references.output_directories.update(
        _normalize_asset_path(path)
        for parm in _get_path_parms(rop_node, "output_directories")
        if (not manual_only or parm.unexpandedString() not in saved_auto_refs.output_directories)
        and (path := parm.eval())
    )

    return asset_references


def _get_unevaluated_asset_references(rop_node: hou.Node) -> AssetReferences:
    """
    Get the current paths stored in the parms backing the UI and return them as
    an AssetReferences object without evaluating any Houdini tokens
    """
    asset_references = AssetReferences()
    asset_references.input_filenames.update(
        [n.unexpandedString() for n in rop_node.parm("input_filenames").multiParmInstances()]
    )
    asset_references.input_directories.update(
        [n.unexpandedString() for n in rop_node.parm("input_directories").multiParmInstances()]
    )
    asset_references.output_directories.update(
        [n.unexpandedString() for n in rop_node.parm("output_directories").multiParmInstances()]
    )

    return asset_references


def _get_saved_auto_detected_asset_references(rop_node: hou.Node) -> AssetReferences:
    """
    Get all of the paths saved in the hidden auto_* parms on the node and return
    them as an AssetReferences object in their unevaluated forms.
    """
    return AssetReferences(
        input_filenames={
            parm.unexpandedString() for parm in _get_path_parms(rop_node, "auto_input_filenames")
        },
        input_directories={
            parm.unexpandedString() for parm in _get_path_parms(rop_node, "auto_input_directories")
        },
        output_directories={
            parm.unexpandedString() for parm in _get_path_parms(rop_node, "auto_output_directories")
        },
    )


def _parse_files(node: hou.Node, *, use_husk: Optional[bool] = None):
    """
    Generate the lists of input filenames, input directories and output directories
    based on the detected paths in the scene, any previously saved values and the
    current values in the UI. Then update the UI with the new lists of paths.
    """
    display_asset_refs = _get_unevaluated_asset_references(node)
    auto_asset_refs = _get_scene_asset_references(node, use_husk=use_husk)
    prev_auto_asset_refs = _get_saved_auto_detected_asset_references(node)

    # Remove all previous and current auto detected assets from the lists currently
    # displayed in the UI to determine which ones have been manually added
    display_asset_refs.input_filenames.difference_update(auto_asset_refs.input_filenames)
    display_asset_refs.input_filenames.difference_update(prev_auto_asset_refs.input_filenames)
    display_asset_refs.input_directories.difference_update(auto_asset_refs.input_directories)
    display_asset_refs.input_directories.difference_update(prev_auto_asset_refs.input_directories)
    display_asset_refs.output_directories.difference_update(auto_asset_refs.output_directories)
    display_asset_refs.output_directories.difference_update(prev_auto_asset_refs.output_directories)

    manual_input_filenames = sorted(list(display_asset_refs.input_filenames))
    manual_input_directories = sorted(list(display_asset_refs.input_directories))
    manual_output_directories = sorted(list(display_asset_refs.output_directories))

    auto_detected_input_filenames = sorted(list(auto_asset_refs.input_filenames))
    auto_detected_input_directories = sorted(list(auto_asset_refs.input_directories))
    auto_detected_output_directories = sorted(list(auto_asset_refs.output_directories))

    new_display_input_filenames = manual_input_filenames + auto_detected_input_filenames
    new_display_input_directories = manual_input_directories + auto_detected_input_directories
    new_display_output_directories = manual_output_directories + auto_detected_output_directories

    _update_paths_parm(node, "input_filenames", new_display_input_filenames)
    _update_paths_parm(node, "input_directories", new_display_input_directories)
    _update_paths_parm(node, "output_directories", new_display_output_directories)
    _update_paths_parm(node, "auto_input_filenames", auto_detected_input_filenames)
    _update_paths_parm(node, "auto_input_directories", auto_detected_input_directories)
    _update_paths_parm(node, "auto_output_directories", auto_detected_output_directories)


def _update_paths_parm(node: hou.Node, parm_name: str, paths: list[str]):
    """
    Clear the existing path multiparm parm_name and rebuild it based on the list
    of paths passed in.
    """
    p = node.parm(parm_name)
    while p.multiParmInstancesCount():
        p.removeMultiParmInstance(0)
    p.set(len(paths))
    for i, n in enumerate(p.multiParmInstances()):
        n.set(paths[i])


_USD_EXTENSIONS: set[str] = {".usd", ".usda", ".usdc", ".usdz"}


def _get_usd_output_directories(usd_path: str) -> set[str]:
    """
    Open a USD stage and return output directories from RenderProduct prims
    at /Render/Products.
    """
    output_directories: set[str] = set()
    stage: Usd.Stage = Usd.Stage.Open(usd_path)
    if not stage:
        return output_directories
    products: Usd.Prim = stage.GetPrimAtPath("/Render/Products")
    if not products.IsValid():
        return output_directories
    for child in products.GetChildren():
        if child.GetTypeName() == "RenderProduct":
            product_name_attr: Usd.Attribute = child.GetAttribute("productName")
            if product_name_attr:
                path: str = product_name_attr.Get(0)
                if path:
                    resolved: str = os.path.normpath(os.path.join(os.path.dirname(usd_path), path))
                    output_directories.add(os.path.dirname(resolved))
    return output_directories


def _get_usd_asset_references(
    usd_file_paths: set[str],
    *,
    include_outputs: bool = True,
    visited: set[str] | None = None,
) -> tuple[set[str], set[str], list[str]]:
    """Traverse USD composition arcs to find all dependencies of the given USD files.

    Deduplicates already-visited USD layers (``visited``) so traversing layers pulled from a
    composed LOP stage does not repeatedly re-scan shared sublayers (O(L) instead of O(L^2)).

    Args:
        usd_file_paths: Root USD file paths to inspect with ``UsdUtils.ComputeAllDependencies``.
        include_outputs: When True, also open each USD file with ``Usd.Stage.Open`` to inspect
            ``/Render/Products``. Set False for layers already extracted from a live LOP stage
            (whose ``/Render/Products`` is already read via ``_usd_render_outputs``) to avoid
            redundant ``Usd.Stage.Open`` calls on geometry/material layers.
        visited: Optional set of normalized case-folded USD paths already traversed.

    Returns:
        A tuple of (input_files, output_directories, unresolved_paths).
    """
    input_files: set[str] = set()
    output_directories: set[str] = set()
    all_unresolved: list[str] = []
    seen = visited if visited is not None else set()

    for usd_path in usd_file_paths:
        norm_key = os.path.normcase(_normalize_asset_path(usd_path))
        if norm_key in seen:
            continue
        seen.add(norm_key)

        layers, assets, unresolved = UsdUtils.ComputeAllDependencies(usd_path)
        usd_dir: str = os.path.dirname(usd_path)

        for layer in layers:
            if layer.realPath:
                norm_layer = _ods_unc_to_drive(layer.realPath)
                input_files.add(norm_layer)
                seen.add(os.path.normcase(_normalize_asset_path(norm_layer)))

        for asset in assets:
            input_files.add(_ods_unc_to_drive(os.path.normpath(os.path.join(usd_dir, asset))))
        all_unresolved.extend(unresolved)
        if include_outputs:
            output_directories.update(_get_usd_output_directories(usd_path))

    return input_files, output_directories, all_unresolved


def _get_lop_stage_usd_files(rop_node: hou.Node) -> set[str]:
    """Collect on-disk USD layer paths composed by upstream Solaris/LOP stages.

    Houdini's ``hou.fileReferences()`` only inspects parameters whose ``StringParmType`` is
    ``FileReference``. SideFX's ``Lop/reference::2.0`` (and similar pattern-based LOP nodes)
    declare ``filepath1`` as ``Regular``, so ``hou.fileReferences()`` misses their USD layers
    entirely (including layers using custom Ar resolver tokens such as ``<LATEST>``). Reading
    ``stage.GetUsedLayers()`` on upstream LOP / ``usdrender`` stages returns the concrete,
    resolver-expanded on-disk layer paths.

    Performance note (heavy production scenes):
        Calling ``lop_node.stage()`` returns immediately when the LOP stage is already cooked in
        Houdini's cache, but triggers a synchronous LOP cook on the UI thread if the stage is
        dirty/uncooked (for example, right after opening a large production scene with heavy SOP
        imports or many payloads). Once cooked, ``stage.GetUsedLayers()`` reads already-loaded
        layer handles without traversing prims; callers should pass these paths to
        ``_get_usd_asset_references(..., include_outputs=False)`` so each unique layer is scanned
        at most once and does not open redundant ``Usd.Stage`` instances.
    """
    stage_files: set[str] = set()
    try:
        ancestors = list(rop_node.inputAncestors())
    except Exception:
        return stage_files

    resolved_nodes: list[hou.Node] = []
    seen_node_paths: set[str] = set()

    def _follow(n: hou.Node) -> None:
        if n is None:
            return
        try:
            n_path = n.path()
        except Exception:
            return
        if not isinstance(n_path, str) or n_path in seen_node_paths:
            return
        seen_node_paths.add(n_path)

        try:
            type_name = n.type().nameWithCategory()
        except Exception:
            return

        if type_name == "Driver/fetch":
            try:
                src_parm = n.parm("source")
                inner = n.node(src_parm.eval()) if src_parm is not None else None
                if inner is not None:
                    _follow(inner)
            except Exception:
                pass
            return

        if type_name == "Driver/wedge":
            try:
                drv_parm = n.parm("driver")
                inner = n.node(drv_parm.eval()) if drv_parm is not None else None
                if inner is not None:
                    _follow(inner)
            except Exception:
                pass
            return

        resolved_nodes.append(n)

    for anc in ancestors:
        _follow(anc)

    lop_nodes: list[hou.Node] = []
    seen_lop_paths: set[str] = set()

    def _add_lop(lop_node: hou.Node) -> None:
        if lop_node is None:
            return
        try:
            lp = lop_node.path()
        except Exception:
            return
        if isinstance(lp, str) and lp not in seen_lop_paths:
            seen_lop_paths.add(lp)
            lop_nodes.append(lop_node)

    for n in resolved_nodes:
        try:
            type_name = n.type().nameWithCategory()
        except Exception:
            continue
        if type_name in ("Driver/usdrender", "Lop/usdrender_rop"):
            lop_path_node = None
            try:
                lop_parm = n.parm("loppath")
                if lop_parm is not None and not lop_parm.isDisabled():
                    lop_path = lop_parm.evalAsNodePath()
                    if lop_path:
                        lop_path_node = hou.node(lop_path)
            except Exception:
                lop_path_node = None
            if lop_path_node is not None:
                _add_lop(lop_path_node)
            else:
                try:
                    for inp in n.inputs():
                        _add_lop(inp)
                except Exception:
                    pass
        else:
            try:
                if n.type().category().name() == "Lop":
                    _add_lop(n)
            except Exception:
                pass

    for lop_node in lop_nodes:
        try:
            stage = lop_node.stage()
            if stage is None:
                continue
            for layer in stage.GetUsedLayers():
                real_path = getattr(layer, "realPath", "")
                if not real_path:
                    continue
                mapped = _ods_unc_to_drive(real_path)
                if os.path.isfile(mapped):
                    stage_files.add(_normalize_asset_path(mapped))
        except Exception:
            continue

    return stage_files


def _ods_ar_resolve(path: str) -> str:
    """Resolve *path* through USD's active Ar resolver; return "" if it doesn't resolve.

    A plain ``os.path.isfile`` check misses references whose path carries a
    *resolver* token rather than a Houdini ``$`` variable -- e.g. a version token
    like ``asset_<LATEST>.usd`` handled by a custom Ar resolver (plugged in via
    ``PXR_PLUGINPATH_NAME``). Houdini does NOT expand such tokens in
    ``parm.evalAsString()``, so the on-disk ``os.path.isfile`` fails and the whole
    reference (and its nested dependencies) gets silently dropped from the upload.
    Asking the Ar resolver gives the concrete file the token maps to.
    """
    try:
        resolved = Ar.GetResolver().Resolve(path)
        resolved_str = str(resolved)
        if resolved_str and os.path.isfile(resolved_str):
            return resolved_str
    except Exception:
        pass
    return ""


def _get_scene_asset_references(
    rop_node: hou.Node, *, evaluate: bool = False, use_husk: Optional[bool] = None
) -> AssetReferences:
    """Collect automatic file inputs and output directories for the selected render mode.

    Args:
        rop_node: The Deadline Cloud ROP node.
        evaluate: When True, return concrete filesystem paths only: ``$HIP``, ``$OS``,
            backticks and frame sequences are expanded in each parm's own node context.
            Separators are normalized too. Use it when the result goes
            straight into the submit dialog or ``asset_references.yaml``. When False (``_parse_files``), refs that only use
            global variables (``$HIP/foo.$F4.vdb``) stay unexpanded so a sequence is one row
            on the ROP multiparm and is evaluated later by ``_get_evaluated_asset_references``.
            Node-local refs (``$OS``, backticks) are always expanded here, because the DC ROP
            would evaluate them differently.
        use_husk: Override the ROP's mode while checking pending dialog settings.

    Returns:
        Populated ``AssetReferences`` for the scene.
    """
    asset_references = AssetReferences()
    all_inputs: list[hou.Node] = rop_node.inputAncestors()
    for node in all_inputs:
        out_dirs = _get_output_directories(node)
        asset_references.output_directories.update(
            {_normalize_asset_path(d) for d in out_dirs} if evaluate else out_dirs
        )

    if use_husk is None:
        use_husk = _ods_use_husk(rop_node)
    if use_husk:
        # Bundle creation collects inputs from the USD exported by the render ROP.
        return asset_references

    hip_file = _get_hip_file()
    asset_references.input_filenames.add(_normalize_asset_path(hip_file) if evaluate else hip_file)
    usd_files: set[str] = set()

    for parm, ref in hou.fileReferences():
        if (
            (not parm)
            or (parm.node() == rop_node)
            or (ref.startswith(_IGNORE_REF_VALUES))
            or (parm.name() in _IGNORE_REF_PARMS)
        ):
            continue

        path: str = parm.evalAsString()
        if not path or path == ".deadline":
            continue
        keep_raw = not evaluate and not _is_node_local_ref(ref)

        # A directory can pull an entire asset tree into the job. Only manual
        # directory additions are eligible for recursive attachment.
        if os.path.isdir(path):
            continue
        if os.path.isfile(path):
            if keep_raw:
                asset_references.input_filenames.add(ref)
            else:
                asset_references.input_filenames.update(_expand_parm_file_paths(parm, ref, path))
            if os.path.splitext(path)[1].lower() in _USD_EXTENSIONS:
                # Collect USD files for dependency traversal in the next step.
                # We use the resolved absolute path because UsdUtils.ComputeAllDependencies
                # needs a real filesystem path, not a Houdini variable like $HIP/scene.usda.
                usd_files.add(_ods_unc_to_drive(os.path.realpath(path)))
        else:
            # Check for UDIM texture patterns (e.g. tex.<UDIM>.png or tex.%(UDIM)d.png),
            # which fail os.path.isfile() because of the tile token.
            udim_matches = _expand_udim_paths(path)
            if udim_matches:
                if keep_raw:
                    asset_references.input_filenames.add(ref)
                else:
                    asset_references.input_filenames.update(udim_matches)
                continue

            # ODS: the on-disk path doesn't exist -- it may carry a custom-resolver
            # token (e.g. <LATEST>) that only USD's Ar resolver can expand. Without
            # this, such a top-level reference (and everything it pulls in) is
            # silently dropped from the job attachments. Resolve via Ar and, if it
            # lands on a real file, attach that concrete file for upload and traverse
            # it for nested deps. The scene/USD itself keeps the token, so on the worker
            # the resolver sees only the uploaded version.
            resolved = _ods_ar_resolve(path)
            if resolved:
                asset_references.input_filenames.add(_normalize_asset_path(resolved))
                if os.path.splitext(resolved)[1].lower() in _USD_EXTENSIONS:
                    usd_files.add(_ods_unc_to_drive(os.path.realpath(resolved)))

    # Also collect on-disk USD layers composed by upstream LOP stages (catches Lop/reference::2.0
    # and other LOP nodes whose filepath parm is Regular rather than FileReference).
    lop_stage_usd_files = _get_lop_stage_usd_files(rop_node)
    if lop_stage_usd_files:
        asset_references.input_filenames.update(lop_stage_usd_files)

    # Traverse USD files to find nested dependencies (deduplicated across parm-referenced USD
    # files and LOP-stage layers so shared layers are never scanned twice).
    visited_usd: set[str] = set()
    if usd_files:
        usd_inputs: set[str]
        usd_outputs: set[str]
        unresolved: list[str]
        usd_inputs, usd_outputs, unresolved = _get_usd_asset_references(
            usd_files, include_outputs=True, visited=visited_usd
        )
        asset_references.input_filenames.update(
            {_normalize_asset_path(p) for p in usd_inputs} if evaluate else usd_inputs
        )
        asset_references.output_directories.update(
            {_normalize_asset_path(d) for d in usd_outputs} if evaluate else usd_outputs
        )
        if unresolved:
            print(f"WARNING: {len(unresolved)} USD asset paths could not be resolved:")
            for p in unresolved:
                print(f"  {p}")

    if lop_stage_usd_files:
        stage_inputs, _stage_outputs, stage_unresolved = _get_usd_asset_references(
            lop_stage_usd_files, include_outputs=False, visited=visited_usd
        )
        asset_references.input_filenames.update(
            {_normalize_asset_path(p) for p in stage_inputs} if evaluate else stage_inputs
        )
        if stage_unresolved:
            print(f"WARNING: {len(stage_unresolved)} LOP stage USD asset paths could not be resolved:")
            for p in stage_unresolved:
                print(f"  {p}")

    return asset_references


def _get_output_directories(node: hou.Node) -> set[str]:
    """
    Find and return the set of all output directories detected from the passed
    in node.
    """
    type_name = node.type().nameWithCategory()
    out_parm = _NODE_DIR_MAP.get(type_name, None)

    if not out_parm:
        return set()

    if not callable(out_parm):
        path = node.parm(out_parm).eval()
        return {os.path.dirname(path)}

    return out_parm(node)


def _fetch_outputs(node: hou.Node) -> set[str]:
    """
    Check the source parm of the fetch node to get the pointed to node and its output directories.
    """
    inner_node = node.node(node.parm("source").eval())
    if not inner_node:
        return set()
    return _get_output_directories(inner_node)


def _wedge_outputs(node: hou.Node) -> set[str]:
    """
    Check the driver parm of the wedge node to get the pointed to node and its output directories.
    """
    inner_node = node.node(node.parm("driver").eval())
    if not inner_node:
        return set()
    return _get_output_directories(inner_node)


def _renderman_outputs(node: hou.Node) -> set[str]:
    """Obtain list of outputs from renderman displays with a file device"""
    output_directories: set[str] = set()
    displays = node.parm("ri_displays").eval()
    for index in range(displays):
        device = node.parm(f"ri_device_{index}").eval()
        # skip display devices, only collect file devices
        if device in ("it", "houdini"):
            continue
        path = node.parm(f"ri_display_{index}").eval()
        output_directories.add(os.path.dirname(path))
    return output_directories


def _husk_outputs(node: hou.Node) -> set[str]:
    """Obtain list of outputs by searching the standard /Render/Products scope
    of the USD stage input
    """
    output_directories: set[str] = set()

    for n in node.inputs():
        try:
            products = n.stage().GetPrimAtPath("/Render/Products")
            # GetPrimAtPath returns an invalid null Prim object
            # if a Prim doesn't exist at the specified path
            if not products.IsValid():
                continue
        except Exception:
            # no products found in standard namespace
            continue

        for child in products.GetChildren():
            if child.GetTypeName() == "RenderProduct":
                product_name_attr = child.GetAttribute("productName")
                path = product_name_attr.Get(0)
                output_directories.add(os.path.dirname(path))
    return output_directories


def _usd_render_outputs(node: hou.Node) -> set[str]:
    """Obtain list of outputs by checking in priority order for:
    1) The override output image value
    2) The input nodes to the node set by the LOP Path
    3) The input nodes to the current USD Render ROP
    """
    output_directories: set[str] = set()

    if override_path := node.parm("outputimage").eval():
        output_directories.add(os.path.dirname(override_path))
    elif (lop_path_node := hou.node(node.parm("loppath").evalAsNodePath())) and not node.parm(
        "loppath"
    ).isDisabled():
        output_directories.update(_husk_outputs(lop_path_node))
    else:
        output_directories.update(_husk_outputs(node))

    return output_directories


_NODE_DIR_MAP = {
    "Driver/alembic": "filename",  # Alembic
    "Driver/arnold": "ar_picture",  # Arnold
    "Driver/baketexture::3.0": "vm_uvoutputpicture1",  # Bake Texture
    "Driver/channel": "chopoutput",  # Channel
    "Driver/comp": "copoutput",  # Composite
    "Driver/dop": "dopoutput",  # Dynamics
    "Driver/fetch": _fetch_outputs,  # Fetch
    "Sop/filecache": "file",  # File Cache
    "Sop/filecache::2.0": "file",  # File Cache v2
    "Driver/filmboxfbx": "sopoutput",  # Filmbox FBX
    "Driver/geometry": "sopoutput",  # Geometry
    "Driver/karma": "picture",  # Karma
    "Driver/ifd": "vm_picture",  # Mantra
    "Driver/opengl": "picture",  # OpenGL
    "Driver/ris::3.0": _renderman_outputs,  # Renderman
    "Driver/Redshift_ROP": "RS_outputFileNamePrefix",  # Redshift
    "Sop/rop_alembic": "filename",  # ROP Alembic Output
    "Sop/rop_geometry": "sopoutput",  # ROP Geometry Output
    "Dop/rop_dop": "dopoutput",  # ROP Output Driver
    "Driver/usdrender": _usd_render_outputs,  # USD Render ROP
    "Lop/usdrender_rop": _usd_render_outputs,  # USD Render LOP ROP
    "Driver/vray_renderer": "SettingsOutput_img_file_path",  # Vray
    "Sop/rop_vrayproxy": "filepath",  # Vray
    "Driver/rop_vrayproxy": "filepath",  # Vray
    "Driver/wedge": _wedge_outputs,  # Wedge
}
