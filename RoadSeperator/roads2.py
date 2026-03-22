import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

class GeoFilterTool:
    def __init__(self, root):
        self.root = root
        self.root.title("GeoJSON Multi-Filter Tool")
        self.root.geometry("800x700")
        
        self.raw_data = None
        self.plan_vars = {}
        self.cat_vars = {}

        # --- Top Section: File Loading ---
        self.file_frame = ttk.LabelFrame(root, text=" 1. Load File ", padding="10")
        self.file_frame.pack(fill="x", padx=10, pady=5)
        
        self.load_btn = ttk.Button(self.file_frame, text="Select GeoJSON File", command=self.load_file)
        self.load_btn.pack(side="left", padx=5)
        self.status_lbl = ttk.Label(self.file_frame, text="No file selected")
        self.status_lbl.pack(side="left", padx=5)

        # --- Middle Section: Split view for Plans and Categories ---
        self.middle_frame = ttk.Frame(root)
        self.middle_frame.pack(expand=True, fill="both", padx=10, pady=5)

        # --- LEFT: Plan Numbers ---
        self.plan_frame = ttk.LabelFrame(self.middle_frame, text=" 2. Select Plan Numbers (pl_number) ", padding="10")
        self.plan_frame.pack(side="left", expand=True, fill="both", padx=5)
        
        self.p_canvas = tk.Canvas(self.plan_frame)
        self.p_scroll = ttk.Scrollbar(self.plan_frame, orient="vertical", command=self.p_canvas.yview)
        self.p_inner = ttk.Frame(self.p_canvas)
        self.p_inner.bind("<Configure>", lambda e: self.p_canvas.configure(scrollregion=self.p_canvas.bbox("all")))
        self.p_canvas.create_window((0, 0), window=self.p_inner, anchor="nw")
        self.p_canvas.configure(yscrollcommand=self.p_scroll.set)
        self.p_canvas.pack(side="left", expand=True, fill="both")
        self.p_scroll.pack(side="right", fill="y")

        # --- RIGHT: Road Types ---
        self.cat_frame = ttk.LabelFrame(self.middle_frame, text=" 3. Select Road Types (mavat_name) ", padding="10")
        self.cat_frame.pack(side="left", expand=True, fill="both", padx=5)
        
        self.c_canvas = tk.Canvas(self.cat_frame)
        self.c_scroll = ttk.Scrollbar(self.cat_frame, orient="vertical", command=self.c_canvas.yview)
        self.c_inner = ttk.Frame(self.c_canvas)
        self.c_inner.bind("<Configure>", lambda e: self.c_canvas.configure(scrollregion=self.c_canvas.bbox("all")))
        self.c_canvas.create_window((0, 0), window=self.c_inner, anchor="nw")
        self.c_canvas.configure(yscrollcommand=self.c_scroll.set)
        self.c_canvas.pack(side="left", expand=True, fill="both")
        self.c_scroll.pack(side="right", fill="y")

        # --- Bottom Section: Export ---
        self.bottom_frame = ttk.Frame(root, padding="10")
        self.bottom_frame.pack(fill="x")
        
        self.save_btn = ttk.Button(self.bottom_frame, text="Save Filtered GeoJSON", command=self.save_file, state="disabled")
        self.save_btn.pack(side="right")

    def load_file(self):
        path = filedialog.askopenfilename(filetypes=[("GeoJSON", "*.geojson")])
        if not path: return
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                self.raw_data = json.load(f)
            self.status_lbl.config(text=f"Loaded {len(self.raw_data['features'])} features")
            self.save_btn.config(state="normal")
            self.refresh_lists()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load: {e}")

    def refresh_lists(self):
        # Clear UI
        for w in self.p_inner.winfo_children(): w.destroy()
        for w in self.c_inner.winfo_children(): w.destroy()
        
        plans = set()
        categories = set()

        for f in self.raw_data.get('features', []):
            props = f.get('properties', {})
            pl = props.get('pl_number')
            cat = props.get('mavat_name')
            if pl: plans.add(str(pl))
            if cat: categories.add(str(cat))

        # Build Plan Checklist (All checked by default)
        self.plan_vars = {}
        for p in sorted(list(plans)):
            var = tk.BooleanVar(value=True)
            self.plan_vars[p] = var
            ttk.Checkbutton(self.p_inner, text=p, variable=var).pack(anchor="w")

        # Build Category Checklist (Smart defaults: Roads only, no "Proposed")
        self.cat_vars = {}
        for c in sorted(list(categories)):
            is_road = "דרך" in c and "מוצעת" not in c
            var = tk.BooleanVar(value=is_road)
            self.cat_vars[c] = var
            ttk.Checkbutton(self.c_inner, text=c, variable=var).pack(anchor="w")

    def save_file(self):
        selected_plans = [p for p, v in self.plan_vars.items() if v.get()]
        selected_cats = [c for c, v in self.cat_vars.items() if v.get()]
        
        if not selected_plans or not selected_cats:
            messagebox.showwarning("Warning", "Select at least one Plan and one Road Type.")
            return

        filtered = []
        for f in self.raw_data.get('features', []):
            props = f.get('properties', {})
            if str(props.get('pl_number')) in selected_plans and \
               str(props.get('mavat_name')) in selected_cats:
                filtered.append(f)

        if not filtered:
            messagebox.showinfo("Result", "No features found with this specific combination.")
            return

        out_path = filedialog.asksaveasfilename(defaultextension=".geojson")
        if out_path:
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump({"type": "FeatureCollection", "features": filtered}, f, ensure_ascii=False, indent=2)
            messagebox.showinfo("Success", f"Saved {len(filtered)} features.")

if __name__ == "__main__":
    root = tk.Tk()
    app = GeoFilterTool(root)
    root.mainloop()