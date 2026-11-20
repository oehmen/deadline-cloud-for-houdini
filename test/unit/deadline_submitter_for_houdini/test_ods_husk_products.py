# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""ODS husk path: output handling for one and for several RenderProducts."""

from unittest.mock import patch

from deadline.houdini_submitter.python.deadline_cloud_for_houdini import submitter
from deadline.houdini_submitter.python.deadline_cloud_for_houdini.submitter import (
    _ods_get_husk_step_template,
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
