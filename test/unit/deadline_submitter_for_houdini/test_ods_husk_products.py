# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""ODS husk path: output handling for one and for several RenderProducts."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from deadline.houdini_submitter.python.deadline_cloud_for_houdini import submitter
from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    _ODS_HUSK_RUN_SCRIPT,
    _ODS_USD_REMAP_PY,
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

    # then: husk is started by the wrapper, which gets the session's path mapping rules as $1,
    # the renderer as $2 and no -R
    action = step["script"]["actions"]["onRun"]
    assert action["command"] == "bash"
    assert action["args"][:3] == [
        "{{Task.File.OdsHuskRun}}",
        "{{Session.PathMappingRulesFile}}",
        "BRAY_HdKarma",
    ]
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


def _bash_path(path):
    # A #! stub run by Git Bash on Windows needs /c/... paths; POSIX paths stay as they are.
    text = str(path).replace("\\", "/")
    if os.name == "nt" and len(text) > 1 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text


_WRAPPER_ENV = ("ODS_HUSK_RENDERER_MAP", "ODS_USD_REMAP_VAR", "ODS_USD_ROOT_ALIASES", "STUB_ENV")


@pytest.fixture
def run_wrapper(tmp_path):
    bash = _find_bash()
    if bash is None:
        pytest.skip("bash not available")
    (tmp_path / "ods_husk_run.sh").write_bytes(_ODS_HUSK_RUN_SCRIPT.encode())
    # stub husk: the value of the variable named by STUB_ENV (if set), then one argument per line
    (tmp_path / "husk").write_bytes(
        b'#!/bin/bash\n[[ -n "${STUB_ENV-}" ]] && echo "ENV ${!STUB_ENV}"\nprintf "%s\\n" "$@"\n'
    )
    # python3 -> this interpreter, so the remap doesn't depend on the machine's python3
    (tmp_path / "python3").write_bytes(
        ('#!/bin/bash\nexec "%s" "$@"\n' % _bash_path(sys.executable)).encode()
    )
    for stub in ("husk", "python3"):
        (tmp_path / stub).chmod(0o755)

    def run(renderer_map, *args, env=None, rules="rules.json"):
        env_vars = dict(os.environ)
        for name in _WRAPPER_ENV:
            env_vars.pop(name, None)
        if renderer_map is not None:
            env_vars["ODS_HUSK_RENDERER_MAP"] = renderer_map
        env_vars.update(env or {})
        env_vars["PATH"] = str(tmp_path) + os.pathsep + env_vars["PATH"]
        done = subprocess.run(
            [bash, "ods_husk_run.sh", rules, *args],
            cwd=tmp_path,
            env=env_vars,
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


# --- USD path remap -------------------------------------------------------------------------


def _remap_module():
    namespace = {"__name__": "ods_usd_remap"}
    exec(_ODS_USD_REMAP_PY, namespace)
    return namespace


def test_wrapper_has_no_format_string_braces():
    # the wrapper is an OpenJD embedded file, whose text is a format string
    assert "{{" not in _ODS_HUSK_RUN_SCRIPT
    assert "}}" not in _ODS_HUSK_RUN_SCRIPT


def test_parse_aliases_normalises_and_skips_malformed_entries():
    parse_aliases = _remap_module()["parse_aliases"]

    pairs = parse_aliases(" S:=//nas/p3 ; P:=\\\\nas\\p2\\ ;junk;=x;Q:=")

    assert pairs == [("S:", "//nas/p3"), ("P:", "//nas/p2")]


def test_build_remaps_adds_the_other_spelling_of_each_root():
    build_remaps = _remap_module()["build_remaps"]
    rules = [
        {"source_path": "S:\\proj\\cube", "destination_path": "/s/r1"},
        {"source_path": "//nas/p2/proj", "destination_path": "/s/r2/"},
        {"source_path": "", "destination_path": "/s/ignored"},
        "junk",
    ]

    remaps = build_remaps(rules, [("S:", "//nas/p3"), ("P:", "//nas/p2")])

    # real roots first, then aliases; longest prefix first in each group
    assert remaps == [
        {"from": "//nas/p2/proj", "to": "/s/r2"},
        {"from": "S:/proj/cube", "to": "/s/r1"},
        {"from": "//nas/p3/proj/cube", "to": "/s/r1"},
        {"from": "P:/proj", "to": "/s/r2"},
    ]


def test_build_remaps_drops_an_alias_inside_a_real_root():
    # given: one storage uploaded twice, as X:\\proj and its assets subfolder by UNC spelling
    build_remaps = _remap_module()["build_remaps"]
    rules = [
        {"source_path": "X:\\proj", "destination_path": "/s/drive"},
        {"source_path": "\\\\fs\\share\\proj\\assets", "destination_path": "/s/unc"},
    ]

    remaps = build_remaps(rules, [("X:", "//fs/share")])

    # then: no "X:/proj/assets -> /s/unc", which would take the files uploaded under X:\\proj
    assert remaps == [
        {"from": "//fs/share/proj/assets", "to": "/s/unc"},
        {"from": "X:/proj", "to": "/s/drive"},
        {"from": "//fs/share/proj", "to": "/s/drive"},
    ]


def test_build_remaps_keeps_existing_entries_after_its_own():
    build_remaps = _remap_module()["build_remaps"]
    previous = [{"from": "x:/PROJ", "to": "/elsewhere"}, {"from": "L:", "to": "//lib"}, {"a": 1}, 2]

    remaps = build_remaps([{"source_path": "X:\\proj\\", "destination_path": "/s/r"}], [], previous)

    assert remaps == [{"from": "X:/proj", "to": "/s/r"}, {"from": "L:", "to": "//lib"}]


_RULES = {
    "version": "pathmapping-1.0",
    "path_mapping_rules": [
        {
            "source_path_format": "WINDOWS",
            "source_path": "S:\\proj",
            "destination_path": "/sessions/a/root1",
        }
    ],
}


def test_wrapper_without_remap_variable_writes_nothing(run_wrapper, tmp_path):
    (tmp_path / "rules.json").write_text(json.dumps(_RULES))

    out = run_wrapper(None, "R", "x")

    assert out == ["-R", "R", "x"]
    assert not (tmp_path / "ods_usd_path_remap.json").exists()


def test_wrapper_writes_remap_and_points_the_named_variable_at_it(run_wrapper, tmp_path):
    (tmp_path / "rules.json").write_text(json.dumps(_RULES))
    env = {
        "ODS_USD_REMAP_VAR": "STUDIO_REMAP",
        "ODS_USD_ROOT_ALIASES": "S:=//nas/p3",
        "STUB_ENV": "STUDIO_REMAP",
    }

    out = run_wrapper(None, "R", "-s", "/a b", env=env)

    data = json.loads((tmp_path / "ods_usd_path_remap.json").read_text())
    assert data == {
        "remaps": [
            {"from": "S:/proj", "to": "/sessions/a/root1"},
            {"from": "//nas/p3/proj", "to": "/sessions/a/root1"},
        ]
    }
    assert "ODS: usd remap S:/proj -> /sessions/a/root1" in out
    assert out[-5].startswith("ENV ") and out[-5].endswith("/ods_usd_path_remap.json")
    assert out[-4:] == ["-R", "R", "-s", "/a b"]


def test_wrapper_appends_the_existing_remap_file(run_wrapper, tmp_path):
    (tmp_path / "rules.json").write_text(json.dumps(_RULES))
    (tmp_path / "site.json").write_text(json.dumps({"remaps": [{"from": "L:", "to": "//lib"}]}))

    run_wrapper(None, "R", env={"ODS_USD_REMAP_VAR": "STUDIO_REMAP", "STUDIO_REMAP": "site.json"})

    data = json.loads((tmp_path / "ods_usd_path_remap.json").read_text())
    assert data["remaps"] == [
        {"from": "S:/proj", "to": "/sessions/a/root1"},
        {"from": "L:", "to": "//lib"},
    ]


def test_wrapper_renders_without_rules_when_the_rules_file_is_missing(run_wrapper, tmp_path):
    out = run_wrapper(None, "R", "x", env={"ODS_USD_REMAP_VAR": "STUDIO_REMAP"}, rules="nope.json")

    assert any(line.startswith("ODS: no path mapping rules read") for line in out)
    assert json.loads((tmp_path / "ods_usd_path_remap.json").read_text()) == {"remaps": []}
    assert out[-3:] == ["-R", "R", "x"]


def test_wrapper_skips_remap_for_an_invalid_variable_name(run_wrapper, tmp_path):
    out = run_wrapper(None, "R", "x", env={"ODS_USD_REMAP_VAR": "NOT A NAME"})

    assert out[0].startswith("ODS: ODS_USD_REMAP_VAR='NOT A NAME' is not a variable name")
    assert out[-3:] == ["-R", "R", "x"]
    assert not (tmp_path / "ods_usd_path_remap.json").exists()
