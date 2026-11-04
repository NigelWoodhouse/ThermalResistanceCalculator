"""Area-normalized heat-flow resistance calculations for flat walls."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import tempfile
from typing import Iterable


R_SI_TO_US = 5.678263
MAX_PATH_COMBINATIONS = 100_000


@dataclass(frozen=True)
class Material:
    id: str
    name: str
    category: str
    k_w_mk: float
    source: str = ""
    source_url: str = ""
    notes: str = ""


@dataclass(frozen=True)
class PathBranch:
    area_fraction: float
    segment_material_id: str | None
    segment_thickness_m: float
    fill_material_id: str


@dataclass(frozen=True)
class ParallelMaterial:
    material_id: str
    segment_thickness_m: float
    area_fraction: float
    coverage_mode: str = "percentage"
    member_width_m: float | None = None
    on_center_spacing_m: float | None = None


@dataclass(frozen=True)
class WallLayer:
    name: str
    thickness_m: float
    branches: tuple[PathBranch, ...]
    primary_material_id: str | None = None
    parallel_materials: tuple[ParallelMaterial, ...] = ()


@dataclass(frozen=True)
class WallResult:
    r_si: float
    r_us: float
    u_si: float
    path_count: int


def on_center_area_fraction(member_width_m: float, spacing_m: float) -> float:
    """Return the repeating member's fraction of a wall from width / on-center spacing."""
    _positive_finite(member_width_m, "Member width")
    _positive_finite(spacing_m, "On-center spacing")
    if member_width_m > spacing_m:
        raise ValueError("Member width cannot exceed the on-center spacing.")
    return member_width_m / spacing_m


def build_layer_branches(
    primary_material_id: str,
    parallel_materials: Iterable[ParallelMaterial],
    layer_thickness_m: float,
) -> tuple[PathBranch, ...]:
    """Create parallel insert paths and a primary-material path for uncovered area."""
    _positive_finite(layer_thickness_m, "Layer thickness")
    materials = tuple(parallel_materials)
    if not primary_material_id:
        raise ValueError("Select a primary material for the layer.")

    branches: list[PathBranch] = []
    total_fraction = 0.0
    for parallel in materials:
        if parallel.coverage_mode == "on-center":
            if parallel.member_width_m is None or parallel.on_center_spacing_m is None:
                raise ValueError("On-center paths need both member width and spacing.")
            fraction = on_center_area_fraction(
                parallel.member_width_m, parallel.on_center_spacing_m
            )
        elif parallel.coverage_mode == "percentage":
            fraction = parallel.area_fraction
        else:
            raise ValueError(f"Unknown parallel-material coverage mode: {parallel.coverage_mode}")

        if not math.isfinite(fraction) or fraction <= 0 or fraction > 1:
            raise ValueError("Each parallel material must cover more than 0% and at most 100%.")
        if (
            not math.isfinite(parallel.segment_thickness_m)
            or parallel.segment_thickness_m <= 0
            or parallel.segment_thickness_m > layer_thickness_m
        ):
            raise ValueError(
                "Parallel-material depth must be greater than zero and no more than the layer thickness."
            )
        total_fraction += fraction
        if total_fraction > 1 + 1e-9:
            raise ValueError(
                "Parallel materials cannot cover more than 100% of the wall. "
                "Reduce their percentages or adjust their on-center spacing."
            )
        branches.append(
            PathBranch(
                fraction,
                parallel.material_id,
                parallel.segment_thickness_m,
                primary_material_id,
            )
        )

    primary_fraction = 1.0 - total_fraction
    if primary_fraction > 1e-9:
        branches.append(PathBranch(primary_fraction, None, 0.0, primary_material_id))
    return tuple(branches)


def _positive_finite(value: float, description: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{description} must be a positive finite number.")


def calculate_wall(
    layers: Iterable[WallLayer],
    conductivities: dict[str, float],
) -> WallResult:
    """Combine series layers and their area-weighted parallel paths."""
    layer_list = tuple(layers)
    if not layer_list:
        raise ValueError("Add at least one wall layer before calculating.")

    # Each state is one possible through-wall path and its area fraction.
    states: list[tuple[float, float]] = [(1.0, 0.0)]
    for layer_number, layer in enumerate(layer_list, start=1):
        _positive_finite(layer.thickness_m, f"Layer {layer_number} thickness")
        if not layer.branches:
            raise ValueError(f"Layer {layer_number} needs at least one path.")
        coverage = sum(branch.area_fraction for branch in layer.branches)
        if not math.isclose(coverage, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError(
                f"Parallel paths in layer {layer_number} must cover 100% "
                f"of the wall (currently {coverage * 100:.3f}%)."
            )

        layer_paths: list[tuple[float, float]] = []
        for branch in layer.branches:
            if not math.isfinite(branch.area_fraction) or branch.area_fraction <= 0:
                raise ValueError("Every path must have a positive area fraction.")
            if (
                not math.isfinite(branch.segment_thickness_m)
                or branch.segment_thickness_m < 0
                or branch.segment_thickness_m > layer.thickness_m
            ):
                raise ValueError(
                    f"Material thickness in layer {layer_number} must be "
                    "between zero and the layer thickness."
                )
            fill_k = conductivities.get(branch.fill_material_id)
            if fill_k is None:
                raise ValueError("A path refers to a missing surrounding material.")
            _positive_finite(fill_k, "Thermal conductivity")

            remaining_thickness = layer.thickness_m - branch.segment_thickness_m
            branch_resistance = remaining_thickness / fill_k
            if branch.segment_thickness_m > 0:
                if branch.segment_material_id is None:
                    raise ValueError(
                        "A material must be selected for each nonzero insert thickness."
                    )
                segment_k = conductivities.get(branch.segment_material_id)
                if segment_k is None:
                    raise ValueError("A path refers to a missing insert material.")
                _positive_finite(segment_k, "Thermal conductivity")
                branch_resistance += branch.segment_thickness_m / segment_k
            if not math.isfinite(branch_resistance) or branch_resistance <= 0:
                raise ValueError("Each heat-flow path must have positive resistance.")
            layer_paths.append((branch.area_fraction, branch_resistance))

        if len(states) * len(layer_paths) > MAX_PATH_COMBINATIONS:
            raise ValueError(
                "This wall creates more than 100,000 through-wall paths. "
                "Simplify the parallel-path layout to calculate it."
            )
        states = [
            (area_fraction * branch_fraction, resistance + branch_resistance)
            for area_fraction, resistance in states
            for branch_fraction, branch_resistance in layer_paths
        ]

    equivalent_conductance = sum(area_fraction / resistance for area_fraction, resistance in states)
    if not math.isfinite(equivalent_conductance) or equivalent_conductance <= 0:
        raise ValueError("The wall's equivalent conductance could not be calculated.")
    r_si = 1.0 / equivalent_conductance
    return WallResult(
        r_si=r_si,
        r_us=r_si * R_SI_TO_US,
        u_si=equivalent_conductance,
        path_count=len(states),
    )


def calculate_layer(
    layer: WallLayer,
    conductivities: dict[str, float],
) -> WallResult:
    """Return a layer's area-normalized R-value, including its parallel paths."""
    return calculate_wall([layer], conductivities)


def load_materials(path: Path) -> list[Material]:
    """Load and validate the categorized JSON material library."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not load material library '{path}': {error}") from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or not isinstance(document.get("categories"), list)
    ):
        raise ValueError("The material library has an unsupported or invalid format.")

    materials: list[Material] = []
    seen_ids: set[str] = set()
    for category in document["categories"]:
        if not isinstance(category, dict):
            raise ValueError("Every material category must be an object.")
        category_name = category.get("name")
        if not isinstance(category_name, str) or not category_name.strip():
            raise ValueError("Every material category must have a name.")
        entries = category.get("materials")
        if not isinstance(entries, list):
            raise ValueError(f"Materials for category '{category_name}' must be a list.")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"Every material in '{category_name}' must be an object.")
            try:
                material = Material(
                    id=str(entry["id"]),
                    name=str(entry["name"]),
                    category=category_name,
                    k_w_mk=float(entry["k_w_mk"]),
                    source=str(entry.get("source", "")),
                    source_url=str(entry.get("source_url", "")),
                    notes=str(entry.get("notes", "")),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"Invalid material entry in category '{category_name}': {error}"
                ) from error
            if not material.id or not material.name.strip() or material.id in seen_ids:
                raise ValueError(f"Material IDs and names must be valid and IDs unique: {material.id}")
            _positive_finite(material.k_w_mk, f"Conductivity for {material.name}")
            seen_ids.add(material.id)
            materials.append(material)
    if not materials:
        raise ValueError("The material library contains no materials.")
    return materials


def material_id_for(name: str, existing_ids: Iterable[str]) -> str:
    """Create a stable, JSON-safe ID from a user-entered material name."""
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "material"
    ids = set(existing_ids)
    candidate = base
    suffix = 2
    while candidate in ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def save_material(path: Path, material: Material) -> None:
    """Persist a newly created material without changing the bundled library on overrides."""
    _positive_finite(material.k_w_mk, f"Conductivity for {material.name}")
    if not material.id.strip() or not material.name.strip() or not material.category.strip():
        raise ValueError("Material ID, name, and category are required.")
    existing_materials = load_materials(path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        categories = document["categories"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError(f"Could not update material library '{path}': {error}") from error
    if any(
        existing.id == material.id or existing.name.casefold() == material.name.casefold()
        for existing in existing_materials
    ):
        raise ValueError(f"A material named '{material.name}' already exists.")

    target_category = next(
        (category for category in categories if category.get("name", "").casefold() == material.category.casefold()),
        None,
    )
    if target_category is None:
        target_category = {"name": material.category, "materials": []}
        categories.append(target_category)
    target_category["materials"].append(
        {
            "id": material.id,
            "name": material.name,
            "k_w_mk": material.k_w_mk,
            "source": material.source,
            "source_url": material.source_url,
            "notes": material.notes,
        }
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(document, temporary_file, indent=2, ensure_ascii=False)
            temporary_file.write("\n")
        temporary_path.replace(path)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ValueError(f"Could not save material to '{path}': {error}") from error


def update_material(path: Path, material: Material) -> None:
    """Update metadata while retaining the saved conductivity of an existing material."""
    if not material.name.strip() or not material.category.strip():
        raise ValueError("Material name and category are required.")
    existing_materials = load_materials(path)
    existing_material = next(
        (existing for existing in existing_materials if existing.id == material.id),
        None,
    )
    if existing_material is None:
        raise ValueError(f"Material '{material.id}' does not exist in '{path}'.")
    if any(
        existing.id != material.id and existing.name.casefold() == material.name.casefold()
        for existing in existing_materials
    ):
        raise ValueError(f"A material named '{material.name}' already exists.")

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not update material library '{path}': {error}") from error

    original_entry: dict[str, object] | None = None
    original_category: dict[str, object] | None = None
    for category in document["categories"]:
        for entry in category["materials"]:
            if entry.get("id") == material.id:
                original_entry = entry
                original_category = category
                break
        if original_entry is not None:
            break
    if original_entry is None or original_category is None:
        raise ValueError(f"Material '{material.id}' disappeared while updating '{path}'.")

    target_category = next(
        (
            category
            for category in document["categories"]
            if category.get("name", "").casefold() == material.category.casefold()
        ),
        None,
    )
    if target_category is None:
        target_category = {"name": material.category, "materials": []}
        document["categories"].append(target_category)
    if target_category is not original_category:
        original_category["materials"].remove(original_entry)
        target_category["materials"].append(original_entry)
    target_category["name"] = material.category
    original_entry.update(
        {
            "id": material.id,
            "name": material.name,
            "k_w_mk": existing_material.k_w_mk,
            "source": material.source,
            "source_url": material.source_url,
            "notes": material.notes,
        }
    )

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(document, temporary_file, indent=2, ensure_ascii=False)
            temporary_file.write("\n")
        temporary_path.replace(path)
    except OSError as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ValueError(f"Could not save material to '{path}': {error}") from error
