# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""ODS husk path: output handling for one and for several RenderProducts."""

from unittest.mock import patch

import pytest

from deadline.houdini_submitter.python.deadline_cloud_for_houdini import submitter
from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    _ods_check_unique_param_names,
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
    assert "embeddedFiles" not in step["script"]


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
    (embedded,) = step["script"]["embeddedFiles"]
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
