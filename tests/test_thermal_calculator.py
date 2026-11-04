import json
from pathlib import Path
import tempfile
import unittest

from thermal_calculator import (
    Material,
    ParallelMaterial,
    PathBranch,
    WallLayer,
    build_layer_branches,
    calculate_layer,
    calculate_wall,
    load_materials,
    on_center_area_fraction,
    save_material,
    update_material,
)


class CalculateWallTests(unittest.TestCase):
    def test_on_center_coverage_is_member_width_over_spacing(self) -> None:
        self.assertAlmostEqual(on_center_area_fraction(0.0381, 0.4064), 0.09375)

    def test_on_center_member_cannot_be_wider_than_spacing(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            on_center_area_fraction(0.5, 0.4)

    def test_primary_material_fills_area_not_covered_by_parallel_material(self) -> None:
        parallel = ParallelMaterial(
            "wood",
            segment_thickness_m=0.09,
            area_fraction=0.09375,
            coverage_mode="on-center",
            member_width_m=0.0381,
            on_center_spacing_m=0.4064,
        )

        branches = build_layer_branches("insulation", [parallel], 0.14)

        self.assertEqual(len(branches), 2)
        self.assertAlmostEqual(branches[0].area_fraction, 0.09375)
        self.assertEqual(branches[0].segment_material_id, "wood")
        self.assertEqual(branches[0].fill_material_id, "insulation")
        self.assertAlmostEqual(branches[1].area_fraction, 0.90625)
        self.assertIsNone(branches[1].segment_material_id)

    def test_parallel_materials_cannot_cover_over_100_percent(self) -> None:
        parallels = [
            ParallelMaterial("wood", 0.1, 0.6),
            ParallelMaterial("steel", 0.1, 0.5),
        ]

        with self.assertRaisesRegex(ValueError, "more than 100%"):
            build_layer_branches("insulation", parallels, 0.14)

    def test_layer_r_value_combines_parallel_materials_and_primary_remainder(self) -> None:
        layer = WallLayer(
            "Framed cavity",
            0.14,
            build_layer_branches(
                "insulation",
                [
                    ParallelMaterial(
                        "wood",
                        segment_thickness_m=0.09,
                        area_fraction=0.15,
                    )
                ],
                0.14,
            ),
            primary_material_id="insulation",
        )

        result = calculate_layer(layer, {"wood": 0.12, "insulation": 0.04})
        stud_path_r = 0.09 / 0.12 + 0.05 / 0.04
        cavity_path_r = 0.14 / 0.04
        expected = 1 / (0.15 / stud_path_r + 0.85 / cavity_path_r)

        self.assertAlmostEqual(result.r_si, expected)
        self.assertAlmostEqual(result.r_us, expected * 5.678263)

    def test_series_layers_add_resistance(self) -> None:
        layers = [
            WallLayer("A", 0.1, (PathBranch(1.0, None, 0.0, "a"),)),
            WallLayer("B", 0.2, (PathBranch(1.0, None, 0.0, "b"),)),
        ]

        result = calculate_wall(layers, {"a": 0.5, "b": 0.5})

        self.assertAlmostEqual(result.r_si, 0.6)
        self.assertAlmostEqual(result.u_si, 1 / 0.6)
        self.assertAlmostEqual(result.r_us, 0.6 * 5.678263)
        self.assertEqual(result.path_count, 1)

    def test_parallel_paths_are_area_weighted(self) -> None:
        layer = WallLayer(
            "Parallel",
            0.1,
            (
                PathBranch(0.5, None, 0.0, "low_k"),
                PathBranch(0.5, None, 0.0, "high_k"),
            ),
        )

        result = calculate_wall([layer], {"low_k": 0.1, "high_k": 0.5})

        self.assertAlmostEqual(result.r_si, 1 / (0.5 / 1.0 + 0.5 / 0.2))
        self.assertEqual(result.path_count, 2)

    def test_partial_insert_automatically_uses_remaining_fill_thickness(self) -> None:
        layer = WallLayer(
            "Framing",
            0.14,
            (
                PathBranch(0.15, "wood", 0.09, "insulation"),
                PathBranch(0.85, None, 0.0, "insulation"),
            ),
        )

        result = calculate_wall([layer], {"wood": 0.12, "insulation": 0.04})
        stud_path_r = 0.09 / 0.12 + (0.14 - 0.09) / 0.04
        cavity_path_r = 0.14 / 0.04
        expected = 1 / (0.15 / stud_path_r + 0.85 / cavity_path_r)

        self.assertAlmostEqual(result.r_si, expected)

    def test_parallel_choices_in_different_layers_form_area_weighted_paths(self) -> None:
        layers = [
            WallLayer(
                "First",
                0.1,
                (PathBranch(0.5, None, 0.0, "a"), PathBranch(0.5, None, 0.0, "b")),
            ),
            WallLayer("Second", 0.1, (PathBranch(1.0, None, 0.0, "a"),)),
        ]

        result = calculate_wall(layers, {"a": 0.1, "b": 0.2})

        path_a_r = 0.1 / 0.1 + 0.1 / 0.1
        path_b_r = 0.1 / 0.2 + 0.1 / 0.1
        self.assertAlmostEqual(result.r_si, 1 / (0.5 / path_a_r + 0.5 / path_b_r))
        self.assertEqual(result.path_count, 2)

    def test_paths_must_cover_the_wall(self) -> None:
        layer = WallLayer("Incomplete", 0.1, (PathBranch(0.9, None, 0.0, "a"),))

        with self.assertRaisesRegex(ValueError, "cover 100%"):
            calculate_wall([layer], {"a": 0.1})

    def test_insert_cannot_exceed_layer_thickness(self) -> None:
        layer = WallLayer("Too thick", 0.1, (PathBranch(1.0, "a", 0.2, "a"),))

        with self.assertRaisesRegex(ValueError, "between zero and the layer thickness"):
            calculate_wall([layer], {"a": 0.1})

    def test_empty_wall_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one"):
            calculate_wall([], {})

    def test_excessive_path_combinations_are_rejected(self) -> None:
        layer = WallLayer(
            "Parallel",
            0.1,
            (PathBranch(0.5, None, 0.0, "a"), PathBranch(0.5, None, 0.0, "b")),
        )

        with self.assertRaisesRegex(ValueError, "more than 100,000"):
            calculate_wall([layer] * 17, {"a": 0.1, "b": 0.2})


class MaterialLibraryTests(unittest.TestCase):
    def test_bundled_material_library_loads(self) -> None:
        materials = load_materials(Path(__file__).parents[1] / "materials.json")

        self.assertGreaterEqual(len(materials), 10)
        self.assertIn("Metals", {material.category for material in materials})
        self.assertIn("Insulation", {material.category for material in materials})

    def test_user_material_is_saved_without_changing_other_categories(self) -> None:
        source = Path(__file__).parents[1] / "materials.json"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "materials.json"
            original = json.loads(source.read_text(encoding="utf-8"))
            target.write_text(json.dumps(original), encoding="utf-8")
            save_material(
                target,
                Material("custom-panel", "Custom panel", "Custom", 0.23, "User-created material"),
            )

            updated = load_materials(target)

        self.assertEqual(len(updated), sum(len(group["materials"]) for group in original["categories"]) + 1)
        self.assertEqual(updated[-1].name, "Custom panel")
        self.assertEqual(updated[-1].category, "Custom")

    def test_existing_material_metadata_can_be_edited_without_changing_conductivity(self) -> None:
        source = Path(__file__).parents[1] / "materials.json"
        original_materials = load_materials(source)
        original = original_materials[0]
        changed = Material(
            id=original.id,
            name=f"{original.name} edited",
            category="Edited category",
            k_w_mk=original.k_w_mk * 1.1,
            source="Edited source",
            source_url="https://example.test/material",
            notes="Edited notes",
        )
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "materials.json"
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

            update_material(target, changed)

            updated = load_materials(target)
            saved = next(material for material in updated if material.id == original.id)

        self.assertEqual(saved.id, changed.id)
        self.assertEqual(saved.name, changed.name)
        self.assertEqual(saved.category, changed.category)
        self.assertEqual(saved.k_w_mk, original.k_w_mk)
        self.assertEqual(saved.source, changed.source)
        self.assertEqual(saved.source_url, changed.source_url)
        self.assertEqual(saved.notes, changed.notes)
        self.assertEqual(load_materials(source), original_materials)


if __name__ == "__main__":
    unittest.main()
