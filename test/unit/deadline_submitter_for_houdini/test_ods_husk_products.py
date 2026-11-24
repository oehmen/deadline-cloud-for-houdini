# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""ODS husk path: output handling for one and for several RenderProducts."""

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from deadline.houdini_submitter.python.deadline_cloud_for_houdini import submitter
from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    _ODS_HUSK_RUN_SCRIPT,
    _ods_check_unique_param_names,
    _ods_export_render_usd,
    _ods_get_husk_step_template,
    _ods_product_dir_params,
    _ods_products_override_layer,
    _ods_split_product_path,
)

NODE = {"rop": "/stage/beauty", "name": "/stage/beauty-1", "start": 1, "end": 2, "step": 1}


def _step(output_products):
    with patch.object(submitter.hou, "node", return_value=None):
        return _ods_get_husk_step_template(
            dict(NODE, render_strategy=submitter.RenderStrategy.SEQUENTIAL),
            "RenderUsdFile_x",
            output_products,
        )


def test_split_product_path_tokenises_frame():
    assert _ods_split_product_path("X:\\shot\\main\\beauty.0012.exr") == (
        "X:/shot/main",
        "beauty.$F4.exr",
    )


def test_single_product_keeps_o_argument():
    # given / when
    step = _step([("/Render/Products/main", "OutputDir_x", "main.$F4.exr")])

    # then: same shape as before the multi-product change
    args = step["script"]["actions"]["onRun"]["args"]
    assert args[-3:] == ["-o", "{{Param.OutputDir_x}}/main.$F4.exr", "{{Param.RenderUsdFile_x}}"]
    assert [f["name"] for f in step["script"]["embeddedFiles"]] == ["OdsHuskRun"]


def test_several_products_render_override_layer_without_o():
    # given
    products = [
        ("/Render/Products/preview", "OutputDir_x", "preview.$F4.jpg"),
        ("/Render/Products/main", "OutputDir_x_2", "main.$F4.exr"),
    ]

    # when
    step = _step(products)

    # then
    args = step["script"]["actions"]["onRun"]["args"]
    assert "-o" not in args
    assert args[-1] == "{{Task.File.OdsRenderProducts}}"
    wrapper, embedded = step["script"]["embeddedFiles"]
    assert wrapper["name"] == "OdsHuskRun"
    assert embedded["name"] == "OdsRenderProducts"
    assert embedded["filename"].endswith(".usda")
    assert "@{{Param.RenderUsdFile_x}}@" in embedded["data"]


def test_override_layer_merges_shared_parent_prims():
    # given
    products = [
        ("/Render/Products/preview", "OutputDir_x", "preview.$F4.jpg"),
        ("/Render/Products/main", "OutputDir_x_2", "main.$F4.exr"),
    ]

    # when
    text = _ods_products_override_layer("RenderUsdFile_x", products)

    # then: one spec per prim (a layer can't hold two sibling `over "Render"`)
    assert text.startswith("#usda 1.0\n")
    assert text.count('over "Render"') == 1
    assert text.count('over "Products"') == 1
    assert 'token productName = "{{Param.OutputDir_x}}/preview.$F4.jpg"' in text
    assert 'token productName = "{{Param.OutputDir_x_2}}/main.$F4.exr"' in text


@pytest.mark.parametrize("name", ["beauty.0001.exr", "render/beauty.0001.exr", "$HIP/b.exr"])
def test_split_product_path_rejects_relative_names(name):
    with pytest.raises(RuntimeError, match="not an absolute path"):
        _ods_split_product_path(name)


@pytest.mark.parametrize("name", ["/mnt/shot/b.0001.exr", "//server/share/b.0001.exr"])
def test_split_product_path_accepts_posix_and_unc(name):
    assert _ods_split_product_path(name)[1] == "b.$F4.exr"


def test_product_dir_params_groups_shared_folders():
    # given: two products in "main", one in "crypto"
    products = [
        ("/Render/Products/a", "X:/shot/main", "a.$F4.exr"),
        ("/Render/Products/b", "X:/shot/crypto", "b.$F4.exr"),
        ("/Render/Products/c", "X:/shot/main", "c.$F4.exr"),
    ]

    # when
    dir_params, output_products = _ods_product_dir_params("OutputDir_n", products)

    # then: first folder keeps the base name, shared folders share one param
    assert dir_params == {"X:/shot/main": "OutputDir_n", "X:/shot/crypto": "OutputDir_n_2"}
    assert output_products == [
        ("/Render/Products/a", "OutputDir_n", "a.$F4.exr"),
        ("/Render/Products/b", "OutputDir_n_2", "b.$F4.exr"),
        ("/Render/Products/c", "OutputDir_n", "c.$F4.exr"),
    ]


def test_check_unique_param_names_raises_on_collision():
    # given: ROP "n" has a second folder, ROP "n_2" has its main folder
    names = [
        "RenderUsdFile_n",
        "OutputDir_n",
        "OutputDir_n_2",
        "RenderUsdFile_n_2",
        "OutputDir_n_2",
    ]

    with pytest.raises(RuntimeError, match="'OutputDir_n_2' is defined twice"):
        _ods_check_unique_param_names(names)


def test_check_unique_param_names_accepts_distinct_names():
    _ods_check_unique_param_names(["RenderUsdFile_n", "OutputDir_n", "OutputDir_n_2"])


def _make_rop_for_export(rop_name="usdrender_rop1"):
    class _FakeOperationFailed(Exception):
        pass

    def _parm(raw_val, eval_val=None):
        state = {"raw": raw_val, "eval": raw_val if eval_val is None else eval_val}
        p = Mock()
        p.rawValue.side_effect = lambda: state["raw"]
        p.eval.side_effect = lambda: state["eval"]

        def _set(val):
            state["raw"] = val
            state["eval"] = val

        p.set.side_effect = _set
        return p

    parms = {
        "lopoutput": _parm("$HIP/render/__render__.usd", ""),
        "savetodirectory_directory": _parm("$HOUDINI_TEMP_DIR/usd_renders/$RENDERID"),
        "enableoutputprocessor_savetodirectory": _parm("on", 1),
        "husk_debug": _parm("off", 0),
        "rendercommand": _parm("husk"),
    }
    rop = Mock()
    rop.name.return_value = rop_name
    rop.parm.side_effect = parms.get
    return rop, parms, _FakeOperationFailed


def test_export_render_usd_uses_neutral_hip_dir(tmp_path):
    rop, parms, op_failed = _make_rop_for_export("usdrender_rop1")

    def _fake_render(verbose=False):
        export_dir = Path(parms["savetodirectory_directory"].eval())
        (export_dir / "__render__.usd").write_text("#usda 1.0\n", encoding="utf-8")
        raise op_failed("blanked rendercommand")

    rop.render.side_effect = _fake_render

    with (
        patch.object(submitter.hou, "node", return_value=rop),
        patch.object(
            submitter.hou,
            "getenv",
            side_effect=lambda k: str(tmp_path) if k == "HIP" else "",
            create=True,
        ),
        patch.object(submitter.hou, "OperationFailed", op_failed, create=True),
        patch.object(submitter.time, "time", return_value=1795441351.976),
    ):
        usd_path = _ods_export_render_usd("/stage/usdrender_rop1")

    expected = (
        tmp_path / "_ods_husk_export" / "usdrender_rop1" / "1795441351976" / "__render__.usd"
    ).as_posix()
    assert usd_path == expected
    assert "ayon" not in Path(usd_path).parts
    assert (
        parms["savetodirectory_directory"].rawValue() == "$HOUDINI_TEMP_DIR/usd_renders/$RENDERID"
    )
    assert parms["rendercommand"].rawValue() == "husk"


def test_export_render_usd_falls_back_to_ayon_driver_path(tmp_path):
    rop, _parms, _op_failed = _make_rop_for_export("renderLightingMain")
    legacy = (
        tmp_path
        / "ayon"
        / "renderLightingMain"
        / "usd"
        / "shot_v001"
        / "renderLightingMain"
        / "__render__.usd"
    )
    legacy.parent.mkdir(parents=True)
    legacy.write_text("#usda 1.0\n", encoding="utf-8")

    env = {"HIP": str(tmp_path), "HIPNAME": "shot_v001"}
    with (
        patch.object(submitter.hou, "node", return_value=rop),
        patch.object(submitter.hou, "getenv", side_effect=lambda k: env.get(k, ""), create=True),
    ):
        usd_path = _ods_export_render_usd("/out/renderLightingMain")

    assert usd_path == legacy.as_posix()


def test_husk_step_runs_wrapper_with_renderer_first():
    # given / when: the renderer comes from the ROP parm, here the default
    step = _step([("/Render/Products/main", "OutputDir_x", "main.$F4.exr")])

    # then: husk is started by the wrapper, which gets the renderer as $1 and no -R
    action = step["script"]["actions"]["onRun"]
    assert action["command"] == "bash"
    assert action["args"][:2] == ["{{Task.File.OdsHuskRun}}", "BRAY_HdKarma"]
    assert "-R" not in action["args"]
    (wrapper,) = step["script"]["embeddedFiles"]
    assert wrapper["filename"] == "ods_husk_run.sh"
    assert wrapper["data"] == _ODS_HUSK_RUN_SCRIPT


def _find_bash():
    candidates = [shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"]
    for candidate in candidates:
        # WSL's bash.exe stub in System32 is not usable here
        if candidate and os.path.exists(candidate) and "system32" not in candidate.lower():
            return candidate
    return None


@pytest.fixture
def run_wrapper(tmp_path):
    bash = _find_bash()
    if bash is None:
        pytest.skip("bash not available")
    (tmp_path / "ods_husk_run.sh").write_bytes(_ODS_HUSK_RUN_SCRIPT.encode())
    # stub husk: one argument per line
    (tmp_path / "husk").write_bytes(b'#!/bin/bash\nprintf "%s\\n" "$@"\n')

    def run(renderer_map, *args):
        env = dict(os.environ)
        env.pop("ODS_HUSK_RENDERER_MAP", None)
        if renderer_map is not None:
            env["ODS_HUSK_RENDERER_MAP"] = renderer_map
        env["PATH"] = str(tmp_path) + os.pathsep + env["PATH"]
        done = subprocess.run(
            [bash, "ods_husk_run.sh", *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            check=True,
        )
        return done.stdout.decode().splitlines()

    return run


@pytest.mark.parametrize(
    "renderer_map", [None, "", "  ", "Other=Thing", "Studio-Husk", "=x", "Studio-Husk="]
)
def test_wrapper_keeps_scene_renderer_when_no_mapping_applies(run_wrapper, renderer_map):
    out = run_wrapper(renderer_map, "Studio-Husk", "-s", "/a b/c", "-f", "5")

    assert out == ["-R", "Studio-Husk", "-s", "/a b/c", "-f", "5"]


def test_wrapper_maps_renderer_and_logs_it(run_wrapper):
    out = run_wrapper("Studio-Husk=HdArnoldRendererPlugin", "Studio-Husk", "-s", "/a b/c")

    assert out == [
        "ODS: renderer Studio-Husk -> HdArnoldRendererPlugin (ODS_HUSK_RENDERER_MAP)",
        "-R",
        "HdArnoldRendererPlugin",
        "-s",
        "/a b/c",
    ]


def test_wrapper_picks_matching_entry_and_ignores_malformed_ones(run_wrapper):
    out = run_wrapper(" junk ; A=B ; Studio-Husk = HdArnoldRendererPlugin ;", "Studio-Husk", "x")

    assert out[-3:] == ["-R", "HdArnoldRendererPlugin", "x"]


def test_wrapper_matches_exactly_and_does_not_chain(run_wrapper):
    out = run_wrapper("studio-husk=Wrong;Studio-Husk=Mid;Mid=End", "Studio-Husk")

    assert out[-2:] == ["-R", "Mid"]
