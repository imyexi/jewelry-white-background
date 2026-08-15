from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from PIL import Image


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "create_background_edit_mask.py"


def load_module():
    module_name = "create_background_edit_mask"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def write_case(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "pendant.png"
    Image.new("RGB", (120, 160), (231, 234, 236)).save(source, "PNG")
    digest = hashlib.sha256(source.read_bytes()).hexdigest().upper()
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "schema_version": "vision-geometry-mask-2.0",
                "product_id": "SYROOT",
                "source_sha256": digest,
                "source_size": [120, 160],
                "coordinate_range": [0, 10000],
                "producer": "codex-cloud-vision",
                "primitives": [
                    {
                        "id": "pendant",
                        "type": "polygon",
                        "semantic": "非圆环吊坠",
                        "points": [[3500, 2500], [6500, 2500], [7500, 7600], [2500, 7600]],
                    }
                ],
                "uncertain_regions": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return source, profile


def test_root_wrapper_exports_visual_geometry_api_only(tmp_path: Path) -> None:
    module = load_module()
    source, profile = write_case(tmp_path)

    geometry = module.load_vision_geometry(profile, "SYROOT")
    alpha = module.rasterize_vision_geometry(geometry, (120, 160))
    assessment = module.create_mask(
        source,
        tmp_path / "mask.png",
        profile,
        "SYROOT",
    )

    assert alpha.getpixel((60, 80)) == 255
    assert alpha.getpixel((5, 5)) == 0
    assert assessment.status == "review"
    assert not hasattr(module, "estimate_foreground")
    assert not hasattr(module, "create_annulus_mask")


def test_root_wrapper_reexports_boundary_refinement(tmp_path: Path) -> None:
    module = load_module()
    image = Image.new("RGB", (80, 80), (220, 220, 220))
    candidate = Image.new("L", (80, 80), 0)

    refined, report = module.refine_candidate_boundary(image, candidate)

    assert refined.tobytes() == candidate.tobytes()
    assert report.status == "fallback"


def test_root_wrapper_public_exports_exclude_legacy_ring_api() -> None:
    module = load_module()

    assert "VisionGeometryProfile" in module.__all__
    assert "BoundaryRefinementReport" in module.__all__
    assert "load_vision_geometry" in module.__all__
    assert "rasterize_vision_geometry" in module.__all__
    assert "refine_candidate_boundary" in module.__all__
    assert "estimate_foreground" not in module.__all__
    assert "create_annulus_mask" not in module.__all__
