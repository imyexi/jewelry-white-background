#!/usr/bin/env python3
"""项目入口：加载 Skill 内的权威视觉几何 Mask 实现。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_IMPLEMENTATION = Path(__file__).parents[1] / "skills" / "jewelry-white-background" / "scripts" / "create_background_edit_mask.py"
_MODULE_NAME = "_jewelry_white_background_mask"
_SPEC = importlib.util.spec_from_file_location(_MODULE_NAME, _IMPLEMENTATION)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"无法加载 Mask 实现：{_IMPLEMENTATION}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_MODULE_NAME] = _MODULE
_SPEC.loader.exec_module(_MODULE)

VisionGeometryProfile = _MODULE.VisionGeometryProfile
BoundaryVariantReport = _MODULE.BoundaryVariantReport
BoundaryRefinementReport = _MODULE.BoundaryRefinementReport
MaskAssessment = _MODULE.MaskAssessment
ASSESSMENT_EXIT_CODES = _MODULE.ASSESSMENT_EXIT_CODES
MAX_IMAGE_PIXELS = _MODULE.MAX_IMAGE_PIXELS
load_vision_geometry = _MODULE.load_vision_geometry
rasterize_vision_geometry = _MODULE.rasterize_vision_geometry
refine_candidate_boundary = _MODULE.refine_candidate_boundary
create_background_edit_assets = _MODULE.create_background_edit_assets
create_mask = _MODULE.create_mask
main = _MODULE.main

__all__ = [
    "VisionGeometryProfile",
    "BoundaryVariantReport",
    "BoundaryRefinementReport",
    "MaskAssessment",
    "ASSESSMENT_EXIT_CODES",
    "MAX_IMAGE_PIXELS",
    "load_vision_geometry",
    "rasterize_vision_geometry",
    "refine_candidate_boundary",
    "create_background_edit_assets",
    "create_mask",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
