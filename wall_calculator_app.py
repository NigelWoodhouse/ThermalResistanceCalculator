"""Tkinter interface for configuring and calculating a flat wall."""

from __future__ import annotations

from pathlib import Path
import math
import tkinter as tk
from tkinter import messagebox, ttk

from thermal_calculator import (
    Material,
    ParallelMaterial,
    PathBranch,
    WallLayer,
    build_layer_branches,
    calculate_layer,
    calculate_wall,
    load_materials,
    material_id_for,
    on_center_area_fraction,
    save_material,
    update_material,
)


MATERIALS_PATH = Path(__file__).with_name("materials.json")
UNIT_METERS = {"in": 0.0254, "mm": 0.001, "cm": 0.01, "m": 1.0}
METRIC_UNITS = {"mm", "cm", "m"}


def _read_number(value: str, label: str, *, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise ValueError(f"{label} must be a number.") from error
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        requirement = "zero or greater" if allow_zero else "greater than zero"
        raise ValueError(f"{label} must be {requirement}.")
    return number


class WallCalculatorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.materials = load_materials(MATERIALS_PATH)
        self.material_by_id = {material.id: material for material in self.materials}
        self.overrides: dict[str, float] = {}
        self.layers: list[WallLayer] = []
        self.unit_var = tk.StringVar(value="in")
        self.system_var = tk.StringVar(value="imperial")
        self._build_window()
        self.refresh_layers()

    def _build_window(self) -> None:
        self.root.title("Flat Wall R-Value Calculator")
        self.root.geometry("1020x790")
        self.root.minsize(820, 620)
        self.root.configure(bg="#f3f6f8")

        style = ttk.Style(self.root)
        style.configure("Title.TLabel", font=("Segoe UI", 19, "bold"), foreground="#14334b")
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10), foreground="#526777")
        style.configure("Card.TFrame", background="white")
        style.configure("CardTitle.TLabel", background="white", font=("Segoe UI", 11, "bold"))
        style.configure("Result.TLabel", background="white", font=("Segoe UI", 13, "bold"))

        outer = ttk.Frame(self.root, padding=(22, 18))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Flat wall thermal resistance", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Build the wall from inside to outside. Add parallel paths for framing and cavity areas.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(3, 14))

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=(0, 10))
        actions = ttk.Frame(toolbar)
        actions.pack(side="left", anchor="w")
        settings = ttk.Frame(toolbar)
        settings.pack(side="right", anchor="ne")

        display_row = ttk.Frame(settings)
        display_row.pack(anchor="e")
        ttk.Label(display_row, text="Display:").pack(side="left", padx=(0, 6))
        ttk.Radiobutton(
            display_row,
            text="Metric",
            value="metric",
            variable=self.system_var,
            command=lambda: self.set_unit_system("metric"),
        ).pack(side="left")
        ttk.Radiobutton(
            display_row,
            text="Imperial",
            value="imperial",
            variable=self.system_var,
            command=lambda: self.set_unit_system("imperial"),
        ).pack(side="left")

        units_row = ttk.Frame(settings)
        units_row.pack(anchor="e", pady=(4, 0))
        ttk.Label(units_row, text="Length units:").pack(side="left", padx=(0, 8))
        unit_picker = ttk.Combobox(
            units_row, textvariable=self.unit_var, values=tuple(UNIT_METERS), width=5, state="readonly"
        )
        unit_picker.pack(side="left")
        unit_picker.bind("<<ComboboxSelected>>", self.on_length_unit_changed)

        ttk.Button(actions, text="Add layer", command=self.add_layer).pack(side="left")
        ttk.Button(actions, text="Materials...", command=self.show_materials).pack(side="left", padx=(8, 0))

        layer_panel = ttk.Frame(outer)
        layer_panel.pack(fill="both", expand=True)
        self.layer_canvas = tk.Canvas(layer_panel, bg="#f3f6f8", highlightthickness=0)
        scrollbar = ttk.Scrollbar(layer_panel, orient="vertical", command=self.layer_canvas.yview)
        self.layer_canvas.configure(yscrollcommand=scrollbar.set)
        self.layer_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.layer_frame = ttk.Frame(self.layer_canvas)
        self.canvas_window = self.layer_canvas.create_window(
            (0, 0), window=self.layer_frame, anchor="nw"
        )
        self.layer_frame.bind(
            "<Configure>",
            lambda _: self.layer_canvas.configure(
                scrollregion=self.layer_canvas.bbox("all")
            ),
        )
        self.layer_canvas.bind(
            "<Configure>",
            lambda event: self.layer_canvas.itemconfigure(self.canvas_window, width=event.width),
        )

        footer = ttk.Frame(outer, style="Card.TFrame", padding=14)
        footer.pack(fill="x", pady=(12, 0))
        self.result_label = ttk.Label(
            footer, text="Add a layer to calculate the wall R-value.", style="Result.TLabel"
        )
        self.result_label.pack(anchor="w")
        self.result_detail = ttk.Label(
            footer,
            text="Conduction-only, area-normalized result. Surface films and cavity convection/radiation are excluded.",
            style="Subtitle.TLabel",
            wraplength=950,
        )
        self.result_detail.pack(anchor="w", pady=(5, 0))

    def _display_thickness(self, thickness_m: float) -> str:
        return f"{thickness_m / UNIT_METERS[self.unit_var.get()]:.3g} {self.unit_var.get()}"

    def set_unit_system(self, system: str) -> None:
        if system not in {"metric", "imperial"}:
            raise ValueError(f"Unknown unit system: {system}")
        self.system_var.set(system)
        self.unit_var.set("mm" if system == "metric" else "in")
        self.refresh_layers()
        self.calculate()

    def on_length_unit_changed(self, _event: tk.Event[tk.Misc]) -> None:
        unit = self.unit_var.get()
        self.system_var.set("metric" if unit in METRIC_UNITS else "imperial")
        self.refresh_layers()
        self.calculate()

    def _format_r_value(self, r_si: float, r_us: float) -> str:
        if self.system_var.get() == "metric":
            return f"R = {r_si:.3f} m²·K/W"
        return f"R = {r_us:.2f} ft²·°F·h/Btu"

    def _effective_conductivities(self) -> dict[str, float]:
        return {
            material.id: self.overrides.get(material.id, material.k_w_mk)
            for material in self.materials
        }

    def _material_label(self, material_id: str) -> str:
        material = self.material_by_id[material_id]
        marker = " *" if material_id in self.overrides else ""
        return f"{material.name}{marker}"

    def refresh_layers(self) -> None:
        for child in self.layer_frame.winfo_children():
            child.destroy()

        if not self.layers:
            ttk.Label(
                self.layer_frame,
                text="No layers yet. Select “Add layer” to start defining the wall.",
                padding=20,
            ).pack(anchor="w")
            return

        for index, layer in enumerate(self.layers):
            card = ttk.Frame(self.layer_frame, style="Card.TFrame", padding=14)
            card.pack(fill="x", pady=(0, 9))
            heading = ttk.Frame(card, style="Card.TFrame")
            heading.pack(fill="x")
            ttk.Label(
                heading,
                text=f"{index + 1}. {layer.name}  ·  {self._display_thickness(layer.thickness_m)}",
                style="CardTitle.TLabel",
            ).pack(side="left")
            ttk.Button(
                heading, text="Remove", command=lambda i=index: self.remove_layer(i)
            ).pack(side="right")
            ttk.Button(
                heading, text="Edit", command=lambda i=index: self.edit_layer(i)
            ).pack(side="right", padx=(0, 6))
            ttk.Button(
                heading, text="Down", width=5, command=lambda i=index: self.move_layer(i, 1)
            ).pack(side="right", padx=(0, 6))
            ttk.Button(
                heading, text="Up", width=5, command=lambda i=index: self.move_layer(i, -1)
            ).pack(side="right", padx=(0, 6))

            try:
                layer_result = calculate_layer(layer, self._effective_conductivities())
                layer_r_text = self._format_r_value(layer_result.r_si, layer_result.r_us)
            except ValueError as error:
                layer_r_text = f"R unavailable: {error}"
            ttk.Label(
                card,
                text=f"Layer R-value: {layer_r_text}",
                style="Result.TLabel",
            ).pack(anchor="w", pady=(8, 2))

            if layer.primary_material_id is not None:
                used_fraction = sum(item.area_fraction for item in layer.parallel_materials)
                ttk.Label(
                    card,
                    text=(
                        f"  Primary: {self._material_label(layer.primary_material_id)} "
                        f"({max(0.0, 1.0 - used_fraction) * 100:.2f}%)"
                    ),
                    background="white",
                    foreground="#405565",
                ).pack(anchor="w", pady=(5, 0))
                for parallel in layer.parallel_materials:
                    coverage = f"{parallel.area_fraction * 100:.2f}%"
                    if parallel.coverage_mode == "on-center":
                        coverage = (
                            f"{self._display_thickness(parallel.member_width_m or 0)} wide "
                            f"on {self._display_thickness(parallel.on_center_spacing_m or 0)} centers "
                            f"({coverage})"
                        )
                    ttk.Label(
                        card,
                        text=(
                            f"  Parallel: {self._material_label(parallel.material_id)} · {coverage} · "
                            f"depth {self._display_thickness(parallel.segment_thickness_m)}"
                        ),
                        background="white",
                        foreground="#405565",
                    ).pack(anchor="w", pady=(5, 0))
            else:
                for branch in layer.branches:
                    insert = ""
                    if branch.segment_thickness_m > 0 and branch.segment_material_id:
                        insert = (
                            f"{self._material_label(branch.segment_material_id)} "
                            f"({self._display_thickness(branch.segment_thickness_m)}) + "
                        )
                    filled_depth = layer.thickness_m - branch.segment_thickness_m
                    fill = self._material_label(branch.fill_material_id)
                    if filled_depth > 1e-9:
                        fill += f" ({self._display_thickness(filled_depth)})"
                    ttk.Label(
                        card,
                        text=f"  {branch.area_fraction * 100:.2f}% of wall: {insert}{fill}",
                        background="white",
                        foreground="#405565",
                    ).pack(anchor="w", pady=(5, 0))

        self.layer_canvas.configure(scrollregion=self.layer_canvas.bbox("all"))

    def _default_material_id(self) -> str:
        return next(
            (material.id for material in self.materials if material.id == "fiberglass-batt"),
            self.materials[0].id,
        )

    def add_layer(self) -> None:
        self._open_layer_dialog(None)

    def edit_layer(self, index: int) -> None:
        self._open_layer_dialog(index)

    def _open_layer_dialog(self, index: int | None) -> None:
        existing = self.layers[index] if index is not None else None
        dialog = LayerDialog(
            self.root,
            self.materials,
            self.unit_var.get(),
            existing,
            self._default_material_id(),
            f"Layer {index + 1}" if index is not None else f"Layer {len(self.layers) + 1}",
        )
        self.root.wait_window(dialog.window)
        if dialog.result is None:
            return
        if index is None:
            self.layers.append(dialog.result)
        else:
            self.layers[index] = dialog.result
        self.refresh_layers()
        self.calculate()

    def remove_layer(self, index: int) -> None:
        if not messagebox.askyesno(
            "Remove layer", f"Remove '{self.layers[index].name}'?", parent=self.root
        ):
            return
        self.layers.pop(index)
        self.refresh_layers()
        self.calculate()

    def move_layer(self, index: int, offset: int) -> None:
        target = index + offset
        if 0 <= target < len(self.layers):
            self.layers[index], self.layers[target] = self.layers[target], self.layers[index]
            self.refresh_layers()
            self.calculate()

    def calculate(self) -> None:
        if not self.layers:
            self.result_label.configure(text="Add a layer to calculate the wall R-value.")
            self.result_detail.configure(
                text="Conduction-only, area-normalized result. Surface films and cavity convection/radiation are excluded."
            )
            return
        try:
            result = calculate_wall(self.layers, self._effective_conductivities())
        except ValueError as error:
            messagebox.showerror("Cannot calculate wall", str(error), parent=self.root)
            return
        self.result_label.configure(
            text=self._format_r_value(result.r_si, result.r_us)
        )
        self.result_detail.configure(
            text=(
                f"U = {result.u_si:.3f} W/(m²·K)  ·  {result.path_count} through-wall path(s). "
                "Parallel paths across different layers are combined assuming their area fractions "
                "are independently distributed. Temporary material overrides are managed in Materials."
            )
        )

    def show_materials(self) -> None:
        MaterialsDialog(self.root, self)


class LayerDialog:
    def __init__(
        self,
        parent: tk.Misc,
        materials: list[Material],
        unit: str,
        existing: WallLayer | None,
        default_fill_id: str,
        default_name: str,
    ) -> None:
        self.materials = materials
        self.unit = unit
        self.material_by_id = {material.id: material for material in materials}
        self.result: WallLayer | None = None
        self.parallel_materials = list(existing.parallel_materials) if existing else []
        if existing and existing.primary_material_id is not None:
            primary_id = existing.primary_material_id
        elif existing:
            primary_id = existing.branches[0].fill_material_id
            self.parallel_materials = [
                ParallelMaterial(
                    material_id=branch.segment_material_id,
                    segment_thickness_m=branch.segment_thickness_m,
                    area_fraction=branch.area_fraction,
                )
                for branch in existing.branches
                if branch.segment_material_id is not None
            ]
        else:
            primary_id = default_fill_id

        self.window = tk.Toplevel(parent)
        self.window.title("Edit wall layer" if existing else "Add wall layer")
        self.window.transient(parent)
        self.window.grab_set()
        self.window.geometry("680x520")

        form = ttk.Frame(self.window, padding=16)
        form.pack(fill="both", expand=True)
        self.name_var = tk.StringVar(value=existing.name if existing else default_name)
        old_depth = existing.thickness_m / UNIT_METERS[unit] if existing else 0
        self.depth_var = tk.StringVar(value=f"{old_depth:g}" if old_depth else "")
        self.primary_var = tk.StringVar(
            value=next(
                (material.name for material in materials if material.id == primary_id),
                materials[0].name,
            )
        )
        ttk.Label(form, text="Layer name").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.name_var, width=32).grid(
            row=0, column=1, sticky="ew", padx=8, pady=4
        )
        ttk.Label(form, text=f"Total layer thickness ({unit})").grid(row=1, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.depth_var, width=16).grid(
            row=1, column=1, sticky="w", padx=8, pady=4
        )
        ttk.Label(form, text="Primary material").grid(row=2, column=0, sticky="w", pady=(7, 4))
        primary_picker = ttk.Combobox(
            form,
            textvariable=self.primary_var,
            values=[material.name for material in materials],
            state="readonly",
            width=38,
        )
        primary_picker.grid(row=2, column=1, sticky="ew", padx=8, pady=(7, 4))
        ttk.Label(
            form,
            text="The primary material fills the whole layer unless parallel materials are added.",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 8))
        ttk.Label(form, text="Parallel materials", font=("Segoe UI", 10, "bold")).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(5, 4)
        )
        self.parallel_list = ttk.Frame(form)
        self.parallel_list.grid(row=5, column=0, columnspan=2, sticky="nsew")
        ttk.Button(
            form, text="Add Parallel Material", command=lambda: self.edit_parallel(None)
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Label(
            form,
            text=(
                "Parallel material coverage replaces the same fraction of the primary material. "
                "On-center coverage is member width divided by spacing."
            ),
            wraplength=610,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(0, 8))
        buttons = ttk.Frame(form)
        buttons.grid(row=8, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.window.destroy).pack(side="right")
        ttk.Button(buttons, text="Save layer", command=self.save).pack(side="right", padx=8)
        form.columnconfigure(1, weight=1)
        form.rowconfigure(5, weight=1)
        self.refresh_parallel_list()

    def refresh_parallel_list(self) -> None:
        for child in self.parallel_list.winfo_children():
            child.destroy()
        if not self.parallel_materials:
            ttk.Label(
                self.parallel_list,
                text="None — the primary material fills 100% of this layer.",
                padding=(4, 8),
            ).pack(anchor="w")
            return

        for index, parallel in enumerate(self.parallel_materials):
            row = ttk.Frame(self.parallel_list)
            row.pack(fill="x", pady=3)
            material_name = self.material_by_id[parallel.material_id].name
            if parallel.coverage_mode == "on-center":
                coverage = (
                    f"{parallel.member_width_m / UNIT_METERS[self.unit]:g} {self.unit} wide "
                    f"on {parallel.on_center_spacing_m / UNIT_METERS[self.unit]:g} {self.unit} centers "
                    f"({parallel.area_fraction * 100:.2f}%)"
                )
            else:
                coverage = f"{parallel.area_fraction * 100:g}% of wall"
            ttk.Label(
                row,
                text=(
                    f"{material_name} · {coverage} · "
                    f"depth {parallel.segment_thickness_m / UNIT_METERS[self.unit]:g} {self.unit}"
                ),
            ).pack(side="left", fill="x", expand=True)
            ttk.Button(
                row, text="Remove", command=lambda i=index: self.remove_parallel(i)
            ).pack(side="right")
            ttk.Button(
                row, text="Edit", command=lambda i=index: self.edit_parallel(i)
            ).pack(side="right", padx=(0, 5))

    def remove_parallel(self, index: int) -> None:
        self.parallel_materials.pop(index)
        self.refresh_parallel_list()

    def edit_parallel(self, index: int | None) -> None:
        existing = self.parallel_materials[index] if index is not None else None
        default_depth = 0.0
        try:
            default_depth = _read_number(
                self.depth_var.get(), "Layer thickness"
            ) * UNIT_METERS[self.unit]
        except ValueError:
            pass
        dialog = ParallelMaterialDialog(
            self.window, self.materials, self.unit, existing, default_depth
        )
        self.window.wait_window(dialog.window)
        if dialog.result is None:
            return
        if index is None:
            self.parallel_materials.append(dialog.result)
        else:
            self.parallel_materials[index] = dialog.result
        self.refresh_parallel_list()

    def save(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Invalid layer", "Enter a layer name.", parent=self.window)
            return
        try:
            thickness = _read_number(self.depth_var.get(), "Layer thickness") * UNIT_METERS[self.unit]
            primary_id = next(
                material.id
                for material in self.materials
                if material.name == self.primary_var.get()
            )
            branches = build_layer_branches(
                primary_id, self.parallel_materials, thickness
            )
        except ValueError as error:
            messagebox.showerror("Invalid layer", str(error), parent=self.window)
            return
        self.result = WallLayer(
            name,
            thickness,
            branches,
            primary_material_id=primary_id,
            parallel_materials=tuple(self.parallel_materials),
        )
        self.window.destroy()


class ParallelMaterialDialog:
    def __init__(
        self,
        parent: tk.Misc,
        materials: list[Material],
        unit: str,
        existing: ParallelMaterial | None,
        default_depth_m: float,
    ) -> None:
        self.unit = unit
        self.result: ParallelMaterial | None = None
        self.materials_by_name = {material.name: material.id for material in materials}
        self.window = tk.Toplevel(parent)
        self.window.title("Edit parallel material" if existing else "Add parallel material")
        self.window.transient(parent)
        self.window.grab_set()
        self.window.resizable(False, False)
        form = ttk.Frame(self.window, padding=18)
        form.pack(fill="both", expand=True)
        self.material_var = tk.StringVar(
            value=next(
                (
                    material.name
                    for material in materials
                    if existing and material.id == existing.material_id
                ),
                materials[0].name,
            )
        )
        self.mode_var = tk.StringVar(
            value=existing.coverage_mode if existing else "percentage"
        )
        self.percentage_var = tk.StringVar(
            value=f"{existing.area_fraction * 100:g}" if existing else ""
        )
        self.width_var = tk.StringVar(
            value=(
                f"{existing.member_width_m / UNIT_METERS[unit]:g}"
                if existing and existing.member_width_m is not None
                else ""
            )
        )
        self.spacing_var = tk.StringVar(
            value=(
                f"{existing.on_center_spacing_m / UNIT_METERS[unit]:g}"
                if existing and existing.on_center_spacing_m is not None
                else ""
            )
        )
        self.depth_var = tk.StringVar(
            value=(
                f"{existing.segment_thickness_m / UNIT_METERS[unit]:g}"
                if existing
                else f"{default_depth_m / UNIT_METERS[unit]:g}" if default_depth_m else ""
            )
        )

        ttk.Label(form, text="Parallel material").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Combobox(
            form,
            textvariable=self.material_var,
            values=list(self.materials_by_name),
            state="readonly",
            width=34,
        ).grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=5)
        ttk.Label(form, text="Coverage type").grid(row=1, column=0, sticky="w", pady=5)
        mode_picker = ttk.Combobox(
            form,
            textvariable=self.mode_var,
            values=("percentage", "on-center"),
            state="readonly",
            width=34,
        )
        mode_picker.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=5)

        self.coverage_fields = ttk.Frame(form)
        self.coverage_fields.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.percentage_row = ttk.Frame(self.coverage_fields)
        ttk.Label(self.percentage_row, text="Wall area covered (%)").grid(
            row=0, column=0, sticky="w", pady=5
        )
        ttk.Entry(self.percentage_row, textvariable=self.percentage_var).grid(
            row=0, column=1, sticky="ew", padx=(12, 0), pady=5
        )
        self.percentage_row.columnconfigure(1, weight=1)
        self.on_center_row = ttk.Frame(self.coverage_fields)
        ttk.Label(self.on_center_row, text=f"Member width ({unit})").grid(
            row=0, column=0, sticky="w", pady=5
        )
        ttk.Entry(self.on_center_row, textvariable=self.width_var).grid(
            row=0, column=1, sticky="ew", padx=(12, 0), pady=5
        )
        ttk.Label(self.on_center_row, text=f"On-center spacing ({unit})").grid(
            row=1, column=0, sticky="w", pady=5
        )
        ttk.Entry(self.on_center_row, textvariable=self.spacing_var).grid(
            row=1, column=1, sticky="ew", padx=(12, 0), pady=5
        )
        self.on_center_row.columnconfigure(1, weight=1)

        ttk.Label(form, text=f"Depth through this layer ({unit})").grid(
            row=3, column=0, sticky="w", pady=5
        )
        ttk.Entry(form, textvariable=self.depth_var).grid(
            row=3, column=1, sticky="ew", padx=(12, 0), pady=5
        )
        ttk.Label(
            form,
            text=(
                "Coverage is the portion of the wall where this material replaces the primary material. "
                "On-center coverage = member width ÷ spacing."
            ),
            wraplength=430,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(7, 10))
        buttons = ttk.Frame(form)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.window.destroy).pack(side="right")
        ttk.Button(buttons, text="Save parallel material", command=self.save).pack(
            side="right", padx=8
        )
        form.columnconfigure(1, weight=1)
        mode_picker.bind("<<ComboboxSelected>>", lambda _: self.refresh_coverage_fields())
        self.refresh_coverage_fields()

    def refresh_coverage_fields(self) -> None:
        self.percentage_row.grid_forget()
        self.on_center_row.grid_forget()
        if self.mode_var.get() == "on-center":
            self.on_center_row.grid(row=0, column=0, sticky="ew")
        else:
            self.percentage_row.grid(row=0, column=0, sticky="ew")

    def save(self) -> None:
        try:
            depth = _read_number(self.depth_var.get(), "Material depth") * UNIT_METERS[self.unit]
            material_id = self.materials_by_name[self.material_var.get()]
            if self.mode_var.get() == "on-center":
                width = _read_number(self.width_var.get(), "Member width") * UNIT_METERS[self.unit]
                spacing = _read_number(
                    self.spacing_var.get(), "On-center spacing"
                ) * UNIT_METERS[self.unit]
                fraction = on_center_area_fraction(width, spacing)
                self.result = ParallelMaterial(
                    material_id=material_id,
                    segment_thickness_m=depth,
                    area_fraction=fraction,
                    coverage_mode="on-center",
                    member_width_m=width,
                    on_center_spacing_m=spacing,
                )
            else:
                fraction = _read_number(
                    self.percentage_var.get(), "Wall area coverage"
                ) / 100
                if fraction > 1:
                    raise ValueError("Wall area coverage cannot exceed 100%.")
                self.result = ParallelMaterial(
                    material_id=material_id,
                    segment_thickness_m=depth,
                    area_fraction=fraction,
                    coverage_mode="percentage",
                )
        except ValueError as error:
            messagebox.showerror("Invalid parallel material", str(error), parent=self.window)
            return
        self.window.destroy()


class MaterialsDialog:
    def __init__(self, parent: tk.Misc, app: WallCalculatorApp) -> None:
        self.app = app
        self.window = tk.Toplevel(parent)
        self.window.title("Material library")
        self.window.transient(parent)
        self.window.geometry("900x560")
        self.window.minsize(720, 400)

        container = ttk.Frame(self.window, padding=16)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text="Materials", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            container,
            text=(
                "Edit material properties to save them in materials.json. Temporary conductivity "
                "overrides apply only until the app closes or you clear them."
            ),
            style="Subtitle.TLabel",
            wraplength=850,
        ).pack(anchor="w", pady=(3, 12))

        table_frame = ttk.Frame(container)
        table_frame.pack(fill="both", expand=True)
        columns = ("category", "name", "conductivity", "effective", "source")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        for column, heading, width in (
            ("category", "Category", 130),
            ("name", "Material", 200),
            ("conductivity", "k (W/m·K)", 105),
            ("effective", "Active k", 105),
            ("source", "Source", 300),
        ):
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width, anchor="w")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _: self.edit_selected())

        actions = ttk.Frame(container)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="New material...", command=self.add_material).pack(side="left")
        ttk.Button(actions, text="Edit selected...", command=self.edit_selected).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(
            actions,
            text="Temporary k override...",
            command=self.override_selected,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            actions,
            text="Clear overrides",
            command=self.clear_overrides,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Close", command=self.window.destroy).pack(side="right")
        self.refresh()

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for material in sorted(self.app.materials, key=lambda item: (item.category.casefold(), item.name.casefold())):
            effective_k = self.app.overrides.get(material.id, material.k_w_mk)
            self.tree.insert(
                "",
                "end",
                iid=material.id,
                values=(
                    material.category,
                    material.name,
                    f"{material.k_w_mk:g}",
                    f"{effective_k:g}" + (" (temporary)" if material.id in self.app.overrides else ""),
                    material.source,
                ),
            )

    def _selected_material(self) -> Material | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo(
                "Select a material", "Select a material from the table first.", parent=self.window
            )
            return None
        return self.app.material_by_id[selection[0]]

    def _refresh_app(self) -> None:
        self.app.refresh_layers()
        self.app.calculate()
        self.refresh()

    def add_material(self) -> None:
        dialog = NewMaterialDialog(self.window)
        self.window.wait_window(dialog.window)
        if dialog.result is None:
            return
        name, category, conductivity = dialog.result
        if any(material.name.casefold() == name.casefold() for material in self.app.materials):
            messagebox.showerror(
                "Duplicate material", f"'{name}' is already in the material library.", parent=self.window
            )
            return
        material = Material(
            id=material_id_for(name, self.app.material_by_id),
            name=name,
            category=category,
            k_w_mk=conductivity,
            source="User-created material",
        )
        try:
            save_material(MATERIALS_PATH, material)
        except ValueError as error:
            messagebox.showerror("Could not save material", str(error), parent=self.window)
            return
        self.app.materials.append(material)
        self.app.material_by_id[material.id] = material
        self._refresh_app()
        self.tree.selection_set(material.id)
        self.tree.see(material.id)

    def edit_selected(self) -> None:
        material = self._selected_material()
        if material is None:
            return
        dialog = EditMaterialDialog(self.window, material)
        self.window.wait_window(dialog.window)
        if dialog.result is None:
            return
        try:
            update_material(MATERIALS_PATH, dialog.result)
        except ValueError as error:
            messagebox.showerror("Could not update material", str(error), parent=self.window)
            return
        self.app.materials = [
            dialog.result if item.id == material.id else item for item in self.app.materials
        ]
        self.app.material_by_id[material.id] = dialog.result
        self._refresh_app()
        self.tree.selection_set(material.id)

    def override_selected(self) -> None:
        material = self._selected_material()
        if material is None:
            return
        dialog = OverrideDialog(self.window, self.app.materials, self.app.overrides, material.id)
        self.window.wait_window(dialog.window)
        if dialog.result is None:
            return
        material_id, conductivity = dialog.result
        self.app.overrides[material_id] = conductivity
        self._refresh_app()
        if self.tree.exists(material_id):
            self.tree.selection_set(material_id)

    def clear_overrides(self) -> None:
        if not self.app.overrides:
            messagebox.showinfo(
                "No overrides", "There are no temporary overrides to clear.", parent=self.window
            )
            return
        self.app.overrides.clear()
        self._refresh_app()


class NewMaterialDialog:
    def __init__(self, parent: tk.Misc) -> None:
        self.result: tuple[str, str, float] | None = None
        self.window = tk.Toplevel(parent)
        self.window.title("Create material")
        self.window.transient(parent)
        self.window.grab_set()
        form = ttk.Frame(self.window, padding=16)
        form.pack(fill="both", expand=True)
        self.name_var = tk.StringVar()
        self.category_var = tk.StringVar(value="Custom")
        self.k_var = tk.StringVar()
        for row, (label, variable) in enumerate(
            (("Material name", self.name_var), ("Category", self.category_var), ("Conductivity k (W/m·K)", self.k_var))
        ):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(form, textvariable=variable, width=34).grid(row=row, column=1, padx=(12, 0), pady=5)
        ttk.Label(
            form,
            text="New materials are saved in materials.json.",
            wraplength=400,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 10))
        buttons = ttk.Frame(form)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.window.destroy).pack(side="right")
        ttk.Button(buttons, text="Save material", command=self.save).pack(side="right", padx=8)

    def save(self) -> None:
        name, category = self.name_var.get().strip(), self.category_var.get().strip()
        try:
            if not name or not category:
                raise ValueError("Material name and category are required.")
            conductivity = _read_number(self.k_var.get(), "Conductivity")
        except ValueError as error:
            messagebox.showerror("Invalid material", str(error), parent=self.window)
            return
        self.result = name, category, conductivity
        self.window.destroy()


class EditMaterialDialog:
    def __init__(self, parent: tk.Misc, material: Material) -> None:
        self.material = material
        self.result: Material | None = None
        self.window = tk.Toplevel(parent)
        self.window.title(f"Edit {material.name}")
        self.window.transient(parent)
        self.window.grab_set()
        self.window.resizable(False, False)
        form = ttk.Frame(self.window, padding=16)
        form.pack(fill="both", expand=True)
        self.name_var = tk.StringVar(value=material.name)
        self.category_var = tk.StringVar(value=material.category)
        self.source_var = tk.StringVar(value=material.source)
        self.source_url_var = tk.StringVar(value=material.source_url)
        for row, (label, variable) in enumerate(
            (
                ("Name", self.name_var),
                ("Category", self.category_var),
                ("Source", self.source_var),
                ("Source URL", self.source_url_var),
            )
        ):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=5)
            ttk.Entry(form, textvariable=variable, width=48).grid(
                row=row, column=1, padx=(12, 0), pady=5
            )
        ttk.Label(form, text="Saved conductivity k (W/m·K)").grid(
            row=2, column=0, sticky="w", pady=5
        )
        ttk.Label(
            form,
            text=f"{material.k_w_mk:g} (read-only; use a temporary override to change it for this session)",
        ).grid(row=2, column=1, sticky="w", padx=(12, 0), pady=5)
        ttk.Label(form, text="Notes").grid(row=4, column=0, sticky="nw", pady=5)
        self.notes_text = tk.Text(form, width=48, height=4, wrap="word")
        self.notes_text.grid(row=4, column=1, padx=(12, 0), pady=5)
        self.notes_text.insert("1.0", material.notes)
        buttons = ttk.Frame(form)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.window.destroy).pack(side="right")
        ttk.Button(buttons, text="Save changes", command=self.save).pack(side="right", padx=8)

    def save(self) -> None:
        name = self.name_var.get().strip()
        category = self.category_var.get().strip()
        try:
            if not name or not category:
                raise ValueError("Material name and category are required.")
        except ValueError as error:
            messagebox.showerror("Invalid material", str(error), parent=self.window)
            return
        self.result = Material(
            id=self.material.id,
            name=name,
            category=category,
            k_w_mk=self.material.k_w_mk,
            source=self.source_var.get().strip(),
            source_url=self.source_url_var.get().strip(),
            notes=self.notes_text.get("1.0", "end-1c").strip(),
        )
        self.window.destroy()


class OverrideDialog:
    def __init__(
        self,
        parent: tk.Misc,
        materials: list[Material],
        overrides: dict[str, float],
        selected_material_id: str | None = None,
    ) -> None:
        self.material_by_name = {material.name: material for material in materials}
        self.overrides = overrides
        self.result: tuple[str, float] | None = None
        self.window = tk.Toplevel(parent)
        self.window.title("Temporary conductivity override")
        self.window.transient(parent)
        self.window.grab_set()
        form = ttk.Frame(self.window, padding=16)
        form.pack(fill="both", expand=True)
        selected_material = next(
            (material for material in materials if material.id == selected_material_id),
            materials[0],
        )
        self.material_var = tk.StringVar(value=selected_material.name)
        self.k_var = tk.StringVar()
        picker = ttk.Combobox(
            form, textvariable=self.material_var, values=list(self.material_by_name), state="readonly", width=36
        )
        ttk.Label(form, text="Material").grid(row=0, column=0, sticky="w", pady=5)
        picker.grid(row=0, column=1, padx=(12, 0), pady=5)
        ttk.Label(form, text="New conductivity k (W/m·K)").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.k_var).grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=5)
        ttk.Label(
            form,
            text="Applies to calculations in this session only; the bundled JSON material value is unchanged.",
            wraplength=410,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 10))
        picker.bind("<<ComboboxSelected>>", lambda _: self.set_current_value())
        self.set_current_value()
        buttons = ttk.Frame(form)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.window.destroy).pack(side="right")
        ttk.Button(buttons, text="Apply for session", command=self.save).pack(side="right", padx=8)

    def set_current_value(self) -> None:
        material = self.material_by_name[self.material_var.get()]
        self.k_var.set(str(self.overrides.get(material.id, material.k_w_mk)))

    def save(self) -> None:
        try:
            conductivity = _read_number(self.k_var.get(), "Conductivity")
        except ValueError as error:
            messagebox.showerror("Invalid override", str(error), parent=self.window)
            return
        self.result = self.material_by_name[self.material_var.get()].id, conductivity
        self.window.destroy()
