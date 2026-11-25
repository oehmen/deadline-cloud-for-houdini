# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""ODS husk path: output handling for one and for several RenderProducts."""

import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from deadline.houdini_submitter.python.deadline_cloud_for_houdini import submitter
from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    _ODS_HUSK_RUN_SCRIPT,
    _ODS_USD_REMAP_PY,
    _get_job_template,
    _ods_check_unique_param_names,
    _ods_export_render_usd,
    _ods_get_husk_step_template,
    _ods_product_dir_params,
    _ods_products_override_layer,
    _ods_split_product_path,
    _ods_usd_stage_metadata,
    _ods_usd_time_scale,
)

NODE = {"rop": "/stage/beauty", "name": "/stage/beauty-1", "start": 1, "end": 2, "step": 1}


TIME_SCALE = (30.0, 30.0)


def _step(output_products, time_scale=TIME_SCALE, stage_metadata=None):
    with patch.object(submitter.hou, "node", return_value=None):
        return _ods_get_husk_step_template(
            dict(NODE, render_strategy=submitter.RenderStrategy.SEQUENTIAL),
            "RenderUsdFile_x",
            output_products,
            time_scale,
            stage_metadata,
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
    text = _ods_products_override_layer("RenderUsdFile_x", products, TIME_SCALE)

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


def test_husk_step_runs_wrapper_with_rules_file_and_renderer_first():
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


_WRAPPER_ENV = (
    "ODS_HUSK_RENDERER_MAP",
    "ODS_USD_REMAP_VAR",
    "ODS_USD_REMAP_REQUIRED",
    "ODS_USD_ROOT_ALIASES",
    "STUB_ENV",
)


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

    def run(renderer_map, *args, env=None, rules="rules.json", check=True):
        env_vars = dict(os.environ)
        for name in _WRAPPER_ENV:
            env_vars.pop(name, None)
        if renderer_map is not None:
            env_vars["ODS_HUSK_RENDERER_MAP"] = renderer_map
        env_vars.update(env or {})
        if not (env and "PATH" in env):
            env_vars["PATH"] = str(tmp_path) + os.pathsep + env_vars["PATH"]
        done = subprocess.run(
            [bash, "ods_husk_run.sh", rules, *args],
            cwd=tmp_path,
            env=env_vars,
            capture_output=True,
            check=check,
        )
        lines = done.stdout.decode().splitlines()
        return lines if check else (done.returncode, lines)

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

    pairs = parse_aliases(" A:=//srv/one ; B:=\\\\srv\\two\\ ;junk;=x;Q:=")

    assert pairs == [("A:", "//srv/one"), ("B:", "//srv/two")]


def test_build_remaps_adds_the_other_spelling_of_each_root():
    build_remaps = _remap_module()["build_remaps"]
    rules = [
        {"source_path": "A:\\proj\\cube", "destination_path": "/s/r1"},
        {"source_path": "//srv/two/proj", "destination_path": "/s/r2/"},
        {"source_path": "", "destination_path": "/s/ignored"},
        "junk",
    ]

    remaps = build_remaps(rules, [("A:", "//srv/one"), ("B:", "//srv/two")])

    # real roots first, then aliases; longest prefix first in each group
    assert remaps == [
        {"from": "//srv/two/proj", "to": "/s/r2"},
        {"from": "A:/proj/cube", "to": "/s/r1"},
        {"from": "//srv/one/proj/cube", "to": "/s/r1"},
        {"from": "B:/proj", "to": "/s/r2"},
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


def test_build_remaps_keeps_other_existing_entries_after_its_own():
    build_remaps = _remap_module()["build_remaps"]
    previous = [{"from": "x:/PROJ", "to": "/elsewhere"}, {"from": "L:", "to": "//lib"}, {"a": 1}, 2]

    remaps = build_remaps([{"source_path": "X:\\proj\\", "destination_path": "/s/r"}], [], previous)

    assert remaps == [{"from": "X:/proj", "to": "/s/r"}, {"from": "L:", "to": "//lib"}]


def test_build_remaps_drops_existing_entries_equal_to_or_inside_a_root():
    # given: the resolver picks the longest matching prefix, so these would beat our entry
    build_remaps = _remap_module()["build_remaps"]
    previous = [
        {"from": "X:/proj/", "to": "/site/trailing-slash"},
        {"from": "X:\\proj", "to": "/site/backslash"},
        {"from": "X:/proj/assets", "to": "/site/subfolder"},
        {"from": "X:/projects", "to": "/site/lookalike"},
        {"from": "X:", "to": "/site/drive"},
    ]

    remaps = build_remaps([{"source_path": "X:\\proj", "destination_path": "/s/r"}], [], previous)

    # then: only entries that don't overlap the root stay, in their own order
    assert remaps == [
        {"from": "X:/proj", "to": "/s/r"},
        {"from": "X:/projects", "to": "/site/lookalike"},
        {"from": "X:", "to": "/site/drive"},
    ]


def test_build_remaps_ignores_rules_with_non_string_paths():
    build_remaps = _remap_module()["build_remaps"]
    rules = [
        {"source_path": 5, "destination_path": "/x"},
        {"source_path": "X:/a", "destination_path": ["/x"]},
        {"source_path": None, "destination_path": None},
        {"source_path": "X:/ok", "destination_path": "/s/ok"},
    ]

    assert build_remaps(rules, []) == [{"from": "X:/ok", "to": "/s/ok"}]


_RULES = {
    "version": "pathmapping-1.0",
    "path_mapping_rules": [
        {
            "source_path_format": "WINDOWS",
            "source_path": "A:\\proj",
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
        "ODS_USD_ROOT_ALIASES": "A:=//srv/one",
        "STUB_ENV": "STUDIO_REMAP",
    }

    out = run_wrapper(None, "R", "-s", "/a b", env=env)

    data = json.loads((tmp_path / "ods_usd_path_remap.json").read_text())
    assert data == {
        "remaps": [
            {"from": "A:/proj", "to": "/sessions/a/root1"},
            {"from": "//srv/one/proj", "to": "/sessions/a/root1"},
        ]
    }
    assert "ODS: usd remap A:/proj -> /sessions/a/root1" in out
    assert out[-5].startswith("ENV ") and out[-5].endswith("/ods_usd_path_remap.json")
    assert out[-4:] == ["-R", "R", "-s", "/a b"]


def test_wrapper_appends_the_existing_remap_file(run_wrapper, tmp_path):
    (tmp_path / "rules.json").write_text(json.dumps(_RULES))
    (tmp_path / "site.json").write_text(json.dumps({"remaps": [{"from": "L:", "to": "//lib"}]}))

    run_wrapper(None, "R", env={"ODS_USD_REMAP_VAR": "STUDIO_REMAP", "STUDIO_REMAP": "site.json"})

    data = json.loads((tmp_path / "ods_usd_path_remap.json").read_text())
    assert data["remaps"] == [
        {"from": "A:/proj", "to": "/sessions/a/root1"},
        {"from": "L:", "to": "//lib"},
    ]


def test_wrapper_leaves_the_variable_alone_when_the_rules_file_is_missing(run_wrapper, tmp_path):
    # given: the variable already points at an existing remap file
    env = {
        "ODS_USD_REMAP_VAR": "STUDIO_REMAP",
        "STUDIO_REMAP": "site.json",
        "STUB_ENV": "STUDIO_REMAP",
    }

    out = run_wrapper(None, "R", "x", env=env, rules="nope.json")

    # then: husk renders without our remap and still sees the original file
    assert out[0].startswith("ODS: path mapping rules not read")
    assert out[1] == "ODS: USD path remap failed; rendering without it"
    assert out[2:] == ["ENV site.json", "-R", "R", "x"]
    assert not (tmp_path / "ods_usd_path_remap.json").exists()


def test_wrapper_leaves_the_variable_alone_when_python_fails(run_wrapper, tmp_path):
    (tmp_path / "rules.json").write_text(json.dumps(_RULES))
    (tmp_path / "python3").write_bytes(b"#!/bin/bash\nexit 1\n")
    env = {
        "ODS_USD_REMAP_VAR": "STUDIO_REMAP",
        "STUDIO_REMAP": "site.json",
        "STUB_ENV": "STUDIO_REMAP",
    }

    out = run_wrapper(None, "R", "x", env=env)

    assert out == [
        "ODS: USD path remap failed; rendering without it",
        "ENV site.json",
        "-R",
        "R",
        "x",
    ]


def test_wrapper_treats_an_empty_remap_variable_like_an_unset_one(run_wrapper, tmp_path):
    (tmp_path / "rules.json").write_text(json.dumps(_RULES))

    out = run_wrapper(None, "R", "x", env={"ODS_USD_REMAP_VAR": ""})

    assert out == ["-R", "R", "x"]
    assert not (tmp_path / "ods_usd_path_remap.json").exists()


def test_wrapper_still_remaps_when_the_existing_remap_file_is_malformed(run_wrapper, tmp_path):
    (tmp_path / "rules.json").write_text(json.dumps(_RULES))
    (tmp_path / "site.json").write_text("{not json")

    out = run_wrapper(
        None, "R", env={"ODS_USD_REMAP_VAR": "STUDIO_REMAP", "STUDIO_REMAP": "site.json"}
    )

    assert any(line.startswith("ODS: existing remap file site.json not read") for line in out)
    data = json.loads((tmp_path / "ods_usd_path_remap.json").read_text())
    assert data["remaps"] == [{"from": "A:/proj", "to": "/sessions/a/root1"}]


def test_wrapper_python_ignores_modules_in_the_session_directory(run_wrapper, tmp_path):
    # given: a stray json.py next to the wrapper, which a plain `python3 -` would import
    (tmp_path / "rules.json").write_text(json.dumps(_RULES))
    (tmp_path / "json.py").write_text('raise SystemExit("imported json.py from the session dir")\n')

    run_wrapper(None, "R", env={"ODS_USD_REMAP_VAR": "STUDIO_REMAP"})

    assert (tmp_path / "ods_usd_path_remap.json").exists()


def test_wrapper_skips_remap_for_an_invalid_variable_name(run_wrapper, tmp_path):
    out = run_wrapper(None, "R", "x", env={"ODS_USD_REMAP_VAR": "NOT A NAME"})

    assert out[0].startswith("ODS: ODS_USD_REMAP_VAR='NOT A NAME' is not a variable name")
    assert out[-3:] == ["-R", "R", "x"]
    assert not (tmp_path / "ods_usd_path_remap.json").exists()


def test_build_remaps_keeps_posix_roots_case_sensitive():
    build_remaps = _remap_module()["build_remaps"]
    rules = [
        {"source_path_format": "POSIX", "source_path": "/mnt/Proj", "destination_path": "/s/upper"},
        {"source_path_format": "POSIX", "source_path": "/mnt/proj", "destination_path": "/s/lower"},
    ]
    previous = [
        {"from": "/mnt/Proj/assets", "to": "/site/drop-upper"},
        {"from": "/mnt/proj/assets", "to": "/site/drop-lower"},
        {"from": "/mnt/PROJ/assets", "to": "/site/keep-other-case"},
    ]

    remaps = build_remaps(rules, [("/mnt/Proj", "A:/Proj")], previous)

    assert remaps == [
        {"from": "/mnt/Proj", "to": "/s/upper"},
        {"from": "/mnt/proj", "to": "/s/lower"},
        {"from": "A:/Proj", "to": "/s/upper"},
        {"from": "/mnt/PROJ/assets", "to": "/site/keep-other-case"},
    ]


def test_build_remaps_folds_case_for_windows_rules():
    build_remaps = _remap_module()["build_remaps"]
    rules = [
        {"source_path_format": "WINDOWS", "source_path": "A:\\Proj", "destination_path": "/s/r1"},
        {"source_path_format": "WINDOWS", "source_path": "a:\\proj", "destination_path": "/s/r2"},
    ]

    remaps = build_remaps(
        rules, [("a:", "//srv/one")], [{"from": "A:/PROJ/sub", "to": "/site/sub"}]
    )

    assert remaps == [
        {"from": "A:/Proj", "to": "/s/r1"},
        {"from": "//srv/one/Proj", "to": "/s/r1"},
    ]


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on"])
def test_wrapper_required_fails_when_rules_file_is_missing(run_wrapper, flag):
    env = {"ODS_USD_REMAP_VAR": "STUDIO_REMAP", "ODS_USD_REMAP_REQUIRED": flag}

    rc, out = run_wrapper(None, "R", "x", env=env, rules="nope.json", check=False)

    assert rc == 1
    assert out[0].startswith("ODS: path mapping rules not read")
    assert out[1] == "ODS: USD path remap failed; aborting (ODS_USD_REMAP_REQUIRED)"
    assert "-R" not in out


def test_wrapper_required_fails_when_python_is_missing(run_wrapper, tmp_path):
    (tmp_path / "rules.json").write_text(json.dumps(_RULES))
    (tmp_path / "python3").unlink()
    env = {
        "ODS_USD_REMAP_VAR": "STUDIO_REMAP",
        "ODS_USD_REMAP_REQUIRED": "1",
        "PATH": str(tmp_path),
    }

    rc, out = run_wrapper(None, "R", "x", env=env, check=False)

    assert rc == 1
    assert out == ["ODS: python3 not found; aborting (ODS_USD_REMAP_REQUIRED)"]


def test_wrapper_required_fails_when_python_exits_nonzero(run_wrapper, tmp_path):
    (tmp_path / "rules.json").write_text(json.dumps(_RULES))
    (tmp_path / "python3").write_bytes(b"#!/bin/bash\nexit 1\n")
    env = {"ODS_USD_REMAP_VAR": "STUDIO_REMAP", "ODS_USD_REMAP_REQUIRED": "1"}

    rc, out = run_wrapper(None, "R", "x", env=env, check=False)

    assert rc == 1
    assert out == ["ODS: USD path remap failed; aborting (ODS_USD_REMAP_REQUIRED)"]


def test_wrapper_required_fails_for_invalid_variable_name(run_wrapper):
    env = {"ODS_USD_REMAP_VAR": "NOT A NAME", "ODS_USD_REMAP_REQUIRED": "1"}

    rc, out = run_wrapper(None, "R", "x", env=env, check=False)

    assert rc == 1
    assert out == [
        "ODS: ODS_USD_REMAP_VAR='NOT A NAME' is not a variable name; aborting (ODS_USD_REMAP_REQUIRED)"
    ]


def test_wrapper_required_allows_an_empty_rule_list(run_wrapper, tmp_path):
    empty_rules = {"version": "pathmapping-1.0", "path_mapping_rules": []}
    (tmp_path / "rules.json").write_text(json.dumps(empty_rules))
    env = {
        "ODS_USD_REMAP_VAR": "STUDIO_REMAP",
        "ODS_USD_REMAP_REQUIRED": "1",
        "STUB_ENV": "STUDIO_REMAP",
    }

    out = run_wrapper(None, "R", "x", env=env)

    assert json.loads((tmp_path / "ods_usd_path_remap.json").read_text()) == {"remaps": []}
    assert out[-4].startswith("ENV ") and out[-4].endswith("/ods_usd_path_remap.json")
    assert out[-3:] == ["-R", "R", "x"]


def test_wrapper_required_does_nothing_when_remap_variable_is_unset(run_wrapper, tmp_path):
    out = run_wrapper(None, "R", "x", env={"ODS_USD_REMAP_REQUIRED": "1"}, rules="nope.json")

    assert out == ["-R", "R", "x"]
    assert not (tmp_path / "ods_usd_path_remap.json").exists()


# --- time scale of the override layer ---------------------------------------------------------


def _products():
    return [
        ("/Render/Products/preview", "OutputDir_x", "preview.$F4.jpg"),
        ("/Render/Products/main", "OutputDir_x_2", "main.$F4.exr"),
    ]


def test_override_layer_declares_the_time_scale_of_the_export():
    # given: a 30 fps export. A root layer without a rate gets USD's 24, which stretches the
    # export's samples by 30 / 24 and makes every frame read the wrong time.
    text = _ods_products_override_layer("RenderUsdFile_x", _products(), (30.0, 30.0))

    lines = text.split("\n")
    assert lines[:5] == [
        "#usda 1.0",
        "(",
        "    timeCodesPerSecond = 30.0",
        "    framesPerSecond = 30.0",
        "    subLayers = [",
    ]
    assert lines.index("    subLayers = [") < lines.index("    ]") < lines.index(")")


def test_override_layer_keeps_fractional_and_separate_rates():
    text = _ods_products_override_layer("RenderUsdFile_x", _products(), (23.976, 24.0))

    assert "    timeCodesPerSecond = 23.976\n    framesPerSecond = 24.0\n" in text


def test_multi_product_step_embeds_the_time_scale_it_was_given():
    step = _step(_products(), (25.0, 25.0))

    (_, layer) = step["script"]["embeddedFiles"]
    assert "timeCodesPerSecond = 25.0" in layer["data"]
    assert "framesPerSecond = 25.0" in layer["data"]


def test_single_product_step_needs_no_override_layer():
    step = _step([("/Render/Products/main", "OutputDir_x", "main.$F4.exr")], (30.0, 30.0))

    assert [f["name"] for f in step["script"]["embeddedFiles"]] == ["OdsHuskRun"]


def _fake_pxr(layer):
    sdf = types.SimpleNamespace(Layer=types.SimpleNamespace(FindOrOpen=lambda path: layer))
    return {"pxr": types.SimpleNamespace(Sdf=sdf)}


def test_usd_time_scale_reads_the_export_layer():
    layer = types.SimpleNamespace(timeCodesPerSecond=30.0, framesPerSecond=30)

    with patch.dict(sys.modules, _fake_pxr(layer)):
        assert _ods_usd_time_scale("X:/export/__render__.usd") == (30.0, 30.0)


def test_usd_time_scale_raises_when_the_export_cannot_be_opened():
    with patch.dict(sys.modules, _fake_pxr(None)):
        with pytest.raises(RuntimeError, match="cannot open X:/export/__render__.usd"):
            _ods_usd_time_scale("X:/export/__render__.usd")


# --- root-layer stage metadata and _get_job_template husk wire-up -----------------------------


def _fake_stage_layer(
    *,
    start=None,
    end=None,
    meters_per_unit=None,
    up_axis=None,
    cms=None,
    color_cfg=None,
    reload_fn=None,
):
    info = {}
    if meters_per_unit is not None:
        info["metersPerUnit"] = meters_per_unit
    if up_axis is not None:
        info["upAxis"] = up_axis
    pseudo = types.SimpleNamespace(
        HasInfo=lambda k: k in info,
        GetInfo=lambda k: info[k],
    )
    ns = types.SimpleNamespace(
        HasStartTimeCode=lambda: start is not None,
        startTimeCode=0.0 if start is None else start,
        HasEndTimeCode=lambda: end is not None,
        endTimeCode=0.0 if end is None else end,
        pseudoRoot=pseudo,
        HasColorManagementSystem=lambda: cms is not None,
        colorManagementSystem="" if cms is None else cms,
        HasColorConfiguration=lambda: color_cfg is not None,
        colorConfiguration=types.SimpleNamespace(path=color_cfg) if color_cfg is not None else None,
    )
    if reload_fn is not None:
        ns.Reload = reload_fn
    return ns


def test_usd_stage_metadata_reads_authored_fields_and_reloads_layer():
    reloaded = []
    layer = _fake_stage_layer(
        start=1001,
        end=1050,
        meters_per_unit=1,
        up_axis="Z",
        cms="OpenColorIO",
        color_cfg="ocio://default",
        reload_fn=lambda: reloaded.append(True),
    )

    with patch.dict(sys.modules, _fake_pxr(layer)):
        meta = _ods_usd_stage_metadata("A:/proj/export/__render__.usd")

    assert reloaded == [True]
    assert meta == {
        "startTimeCode": 1001.0,
        "endTimeCode": 1050.0,
        "metersPerUnit": 1.0,
        "upAxis": "Z",
        "colorManagementSystem": "OpenColorIO",
        "colorConfiguration": "ocio://default",
    }


def test_usd_stage_metadata_omits_unauthored_and_invalid_fields():
    layer = _fake_stage_layer(
        meters_per_unit=0.0,
        up_axis='bad"axis',
        cms="bad{{brace}}",
        color_cfg="bad}}brace",
    )

    with patch.dict(sys.modules, _fake_pxr(layer)):
        assert _ods_usd_stage_metadata("A:/proj/export/__render__.usd") == {}


@pytest.mark.parametrize(
    ("raw_cfg", "expected"),
    [
        ("../ocio/studio.ocio", "A:/proj/ocio/studio.ocio"),
        ("ocio://cg-config-v1.0.0", "ocio://cg-config-v1.0.0"),
        ("B:\\ocio\\config.ocio", "B:/ocio/config.ocio"),
        ("\\\\srv\\one\\ocio\\config.ocio", "//srv/one/ocio/config.ocio"),
        ("/mnt/ocio/config.ocio", "/mnt/ocio/config.ocio"),
    ],
)
def test_usd_stage_metadata_normalises_color_configuration_paths(raw_cfg, expected):
    layer = _fake_stage_layer(color_cfg=raw_cfg)

    with patch.dict(sys.modules, _fake_pxr(layer)):
        meta = _ods_usd_stage_metadata("A:/proj/export/__render__.usd")

    assert meta["colorConfiguration"] == expected


def test_usd_stage_metadata_raises_when_the_export_cannot_be_opened():
    with patch.dict(sys.modules, _fake_pxr(None)):
        with pytest.raises(RuntimeError, match="cannot open A:/export/__render__.usd"):
            _ods_usd_stage_metadata("A:/export/__render__.usd")


def test_override_layer_forwards_root_stage_metadata_and_escapes_assets():
    meta = {
        "startTimeCode": 1001.0,
        "endTimeCode": 1004.0,
        "metersPerUnit": 1.0,
        "upAxis": "Z",
        "colorManagementSystem": "OpenColorIO",
        "colorConfiguration": "A:/studio/@ocio/config.ocio",
    }

    text = _ods_products_override_layer("RenderUsdFile_x", _products(), (30.0, 30.0), meta)

    lines = text.split("\n")
    assert lines[:11] == [
        "#usda 1.0",
        "(",
        "    timeCodesPerSecond = 30.0",
        "    framesPerSecond = 30.0",
        "    startTimeCode = 1001.0",
        "    endTimeCode = 1004.0",
        "    metersPerUnit = 1.0",
        '    upAxis = "Z"',
        '    colorManagementSystem = "OpenColorIO"',
        "    colorConfiguration = @@@A:/studio/@ocio/config.ocio@@@",
        "    subLayers = [",
    ]
    assert lines.index("    subLayers = [") < lines.index("    ]") < lines.index(")")


def test_override_layer_skips_non_positive_or_nan_time_scale():
    text = _ods_products_override_layer(
        "RenderUsdFile_x", _products(), (24.0, 0.0), {"metersPerUnit": float("nan")}
    )

    assert "timeCodesPerSecond = 24.0" in text
    assert "framesPerSecond" not in text
    assert "metersPerUnit" not in text


def test_multi_product_step_embeds_stage_metadata():
    step = _step(_products(), (30.0, 30.0), {"metersPerUnit": 1.0, "upAxis": "Z"})

    (_, layer) = step["script"]["embeddedFiles"]
    assert "metersPerUnit = 1.0" in layer["data"]
    assert 'upAxis = "Z"' in layer["data"]


def _mock_dc_rop():
    def _parm(name):
        p = Mock()
        if name == "ods_use_husk":
            p.eval.return_value = 1
        elif name == "separate_steps":
            p.eval.return_value = 0
        elif name == "name":
            p.evalAsString.return_value = "Husk Job"
        elif name == "description":
            p.evalAsString.return_value = ""
        elif name == "include_adaptor_wheels":
            p.eval.return_value = 0
        else:
            p.eval.return_value = None
        return p

    rop = Mock()
    rop.path.return_value = "/out/deadline_cloud1"
    rop.userData.return_value = None
    rop.parm.side_effect = _parm
    return rop


def test_get_job_template_husk_multi_product_wires_time_scale_and_stage_metadata():
    dc_rop = _mock_dc_rop()
    step_node = {
        "rop": "/stage/beauty",
        "name": "/stage/beauty-1",
        "start": 1001,
        "end": 1004,
        "step": 1,
        "render_strategy": submitter.RenderStrategy.SEQUENTIAL,
    }
    products = [
        ("/Render/Products/preview", "A:/shot/preview", "preview.$F4.jpg"),
        ("/Render/Products/main", "B:/shot/main", "main.$F4.exr"),
    ]
    stage_meta = {
        "startTimeCode": 1001.0,
        "endTimeCode": 1004.0,
        "metersPerUnit": 1.0,
        "upAxis": "Z",
        "colorManagementSystem": "OpenColorIO",
        "colorConfiguration": "ocio://default",
    }

    with (
        patch.object(submitter, "_get_steps", return_value=[step_node]),
        patch.object(
            submitter, "_ods_export_render_usd", return_value="A:/shot/_ods_husk_export/r.usd"
        ) as export_mock,
        patch.object(
            submitter, "_ods_render_products_from_usd", return_value=products
        ) as products_mock,
        patch.object(submitter, "_ods_usd_time_scale", return_value=(30.0, 29.97)) as scale_mock,
        patch.object(
            submitter, "_ods_usd_stage_metadata", return_value=stage_meta
        ) as metadata_mock,
        patch.object(submitter.hou, "node", return_value=None),
    ):
        template = _get_job_template(dc_rop)

    export_mock.assert_called_once_with("/stage/beauty")
    products_mock.assert_called_once_with(
        "A:/shot/_ods_husk_export/r.usd", "/Render/rendersettings"
    )
    scale_mock.assert_called_once_with("A:/shot/_ods_husk_export/r.usd")
    metadata_mock.assert_called_once_with("A:/shot/_ods_husk_export/r.usd")

    param_names = [p["name"] for p in template["parameterDefinitions"]]
    assert param_names == [
        "RenderUsdFile__stage_beauty_1",
        "OutputDir__stage_beauty_1",
        "OutputDir__stage_beauty_1_2",
    ]
    assert submitter._ODS_HUSK_PARAM_VALUES["/out/deadline_cloud1"] == [
        {"name": "RenderUsdFile__stage_beauty_1", "value": "A:/shot/_ods_husk_export/r.usd"},
        {"name": "OutputDir__stage_beauty_1", "value": "A:/shot/preview"},
        {"name": "OutputDir__stage_beauty_1_2", "value": "B:/shot/main"},
    ]
    (_, layer) = template["steps"][0]["script"]["embeddedFiles"]
    assert "timeCodesPerSecond = 30.0" in layer["data"]
    assert "framesPerSecond = 29.97" in layer["data"]
    assert "startTimeCode = 1001.0" in layer["data"]
    assert "endTimeCode = 1004.0" in layer["data"]
    assert "metersPerUnit = 1.0" in layer["data"]
    assert 'upAxis = "Z"' in layer["data"]
    assert 'colorManagementSystem = "OpenColorIO"' in layer["data"]
    assert "colorConfiguration = @ocio://default@" in layer["data"]


def test_get_job_template_husk_single_product_uses_o_without_override_layer():
    dc_rop = _mock_dc_rop()
    step_node = {
        "rop": "/stage/beauty",
        "name": "/stage/beauty-1",
        "start": 1001,
        "end": 1002,
        "step": 1,
        "render_strategy": submitter.RenderStrategy.SEQUENTIAL,
    }
    products = [("/Render/Products/main", "A:/shot/main", "main.$F4.exr")]

    with (
        patch.object(submitter, "_get_steps", return_value=[step_node]),
        patch.object(
            submitter, "_ods_export_render_usd", return_value="A:/shot/_ods_husk_export/r.usd"
        ),
        patch.object(submitter, "_ods_render_products_from_usd", return_value=products),
        patch.object(submitter, "_ods_usd_time_scale", return_value=(30.0, 30.0)),
        patch.object(submitter, "_ods_usd_stage_metadata", return_value={"metersPerUnit": 1.0}),
        patch.object(submitter.hou, "node", return_value=None),
    ):
        template = _get_job_template(dc_rop)

    step = template["steps"][0]
    assert [f["name"] for f in step["script"]["embeddedFiles"]] == ["OdsHuskRun"]
    assert step["script"]["actions"]["onRun"]["args"][-3:] == [
        "-o",
        "{{Param.OutputDir__stage_beauty_1}}/main.$F4.exr",
        "{{Param.RenderUsdFile__stage_beauty_1}}",
    ]
