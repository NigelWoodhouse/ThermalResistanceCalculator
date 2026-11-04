"""Launch the flat-wall thermal resistance calculator."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from wall_calculator_app import WallCalculatorApp


def main() -> None:
    root = tk.Tk()
    try:
        WallCalculatorApp(root)
    except (OSError, ValueError) as error:
        messagebox.showerror("Could not start calculator", str(error), parent=root)
        root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()