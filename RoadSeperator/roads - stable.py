import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import threading
import geopandas as gpd
import pandas as pd
import matplotlib.pyplot as plt
from shapely.geometry import MultiPolygon, Polygon, LineString, MultiLineString
import pygeoops
import numpy as np

def chaikins_corner_cutting(coords, refinements=3):
    coords = np.array(coords)
    if len(coords) < 3: return coords
    for _ in range(refinements):
        L, R = coords[:-1], coords[1:]
        new_coords = np.zeros((len(L) * 2, 2))
        new_coords[0::2] = L * 0.75 + R * 0.25
        new_coords[1::2] = L * 0.25 + R * 0.75
        coords = np.vstack([coords[0], new_coords, coords[-1]])
    return coords

class WaveFreeGisTool:
    def __init__(self, root):
        self.root = root
        self.root.title("GIS Road Processor: Wave-Free Mode")
        self.root.geometry("1000x950")
        self.gdf = None
        self.plan_vars, self.cat_vars = {}, {}
        self.setup_ui()

    def setup_ui(self):
        # Global Loader
        load_frame = ttk.LabelFrame(self.root, text=" 1. Data Source ", padding="10")
        load_frame.pack(fill="x", side="top", padx=10, pady=5)
        ttk.Button(load_frame, text="Load GeoJSON", command=self.load_file).pack(side="left", padx=5)
        self.status_lbl = ttk.Label(load_frame, text="No file loaded")
        self.status_lbl.pack(side="left", padx=5)

        self.notebook = ttk.Notebook(self.root)
        self.tab_polygon = ttk.Frame(self.notebook)
        self.tab_polyline = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_polygon, text=" 1. Filter & Dissolve ")
        self.notebook.add(self.tab_polyline, text=" 2. Centerline & Smoothing ")
        self.notebook.pack(expand=True, fill="both", padx=5, pady=5)

        # Tab 1: Filters
        mid = ttk.Frame(self.tab_polygon); mid.pack(expand=True, fill="both", padx=10, pady=5)
        self.p_inner = self.make_list(mid, "Filter by Plans")
        self.c_inner = self.make_list(mid, "Filter by Road Types")
        self.global_merge_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.tab_polygon, text="Global Merge (Neighbors Dissolve)", variable=self.global_merge_var).pack(pady=5)

        # Tab 2: Control Panel
        frame = ttk.Frame(self.tab_polyline, padding="20"); frame.pack(fill="both")
        
        # 1. Straighten Slider (THE WAVE KILLER)
        st_frame = ttk.LabelFrame(frame, text=" 1. Straighten / Simplify (Kill Waves) ", padding="10")
        st_frame.pack(fill="x", pady=5)
        ttk.Label(st_frame, text="Higher = Straighter roads, lower = Snaking roads").pack()
        self.straighten_slider = tk.Scale(st_frame, from_=0.0, to=0.0001, resolution=0.000005, orient="horizontal")
        self.straighten_slider.set(0.00002); self.straighten_slider.pack(fill="x")

        # 2. Pruning
        pr_frame = ttk.LabelFrame(frame, text=" 2. Branch Pruning (Remove tiny 'hairs') ", padding="10")
        pr_frame.pack(fill="x", pady=5)
        self.prune_slider = tk.Scale(pr_frame, from_=0, to=30, orient="horizontal")
        self.prune_slider.set(10); self.prune_slider.pack(fill="x")
        
        # 3. Corner Cutting
        sm_frame = ttk.LabelFrame(frame, text=" 3. Final Corner Smoothing ", padding="10")
        sm_frame.pack(fill="x", pady=5)
        self.smooth_slider = tk.Scale(sm_frame, from_=0, to=5, orient="horizontal")
        self.smooth_slider.set(2); self.smooth_slider.pack(fill="x")

        self.poly_prog = ttk.Label(frame, text="", foreground="blue"); self.poly_prog.pack(pady=10)
        ttk.Button(frame, text="Preview Result (Visual Box)", command=self.preview_lines).pack(pady=5, fill="x")
        ttk.Button(frame, text="Save Wave-Free GeoJSON", command=self.save_lines).pack(pady=5, fill="x")

    def make_list(self, parent, title):
        f = ttk.LabelFrame(parent, text=title, padding="5"); f.pack(side="left", expand=True, fill="both", padx=5)
        c = tk.Canvas(f); s = ttk.Scrollbar(f, orient="vertical", command=c.yview)
        inner = ttk.Frame(c); inner.bind("<Configure>", lambda e: c.configure(scrollregion=c.bbox("all")))
        c.create_window((0, 0), window=inner, anchor="nw"); c.configure(yscrollcommand=s.set)
        c.pack(side="left", expand=True, fill="both"); s.pack(side="right", fill="y")
        return inner

    def load_file(self):
        path = filedialog.askopenfilename(filetypes=[("GeoJSON", "*.geojson")])
        if not path: return
        self.gdf = gpd.read_file(path)
        for w in self.p_inner.winfo_children(): w.destroy()
        for w in self.c_inner.winfo_children(): w.destroy()
        plans = sorted(self.gdf['pl_number'].dropna().unique().astype(str).tolist())
        cats = sorted(self.gdf['mavat_name'].dropna().unique().astype(str).tolist())
        self.plan_vars = {p: tk.BooleanVar(value=True) for p in plans}
        for p in plans: ttk.Checkbutton(self.p_inner, text=p, variable=self.plan_vars[p]).pack(anchor="w")
        self.cat_vars = {c: tk.BooleanVar(value=("דרך" in c)) for c in cats}
        for c in cats: ttk.Checkbutton(self.c_inner, text=c, variable=self.cat_vars[c]).pack(anchor="w")
        self.status_lbl.config(text=f"Loaded {len(self.gdf)} features")

    def get_processed_polygons(self):
        sel_p = [p for p, v in self.plan_vars.items() if v.get()]
        sel_c = [c for c, v in self.cat_vars.items() if v.get()]
        filtered = self.gdf[(self.gdf['pl_number'].astype(str).isin(sel_p)) & (self.gdf['mavat_name'].astype(str).isin(sel_c))].copy()
        if filtered.empty: return None
        if self.global_merge_var.get(): filtered = filtered.dissolve()
        
        # PRE-SIMPLIFY (Kills polygon edge noise before centerline is made)
        tol = self.straighten_slider.get()
        if tol > 0:
            filtered.geometry = filtered.geometry.simplify(tol, preserve_topology=True)
        return filtered

    def preview_lines(self):
        data = self.get_processed_polygons()
        if data is None: return
        self.poly_prog.config(text="Processing... killing wavy patterns...")
        
        prune_val = self.prune_slider.get()
        tol = self.straighten_slider.get()
        smooth_lvl = int(self.smooth_slider.get())
        
        lines = []
        for geom in data.geometry:
            # Step 1: Centerline
            line = pygeoops.centerline(geom, densify_distance=-1, min_branch_length=prune_val)
            if line:
                # Step 2: Straighten (Douglas-Peucker)
                line = line.simplify(tol, preserve_topology=True)
                # Step 3: Smooth curves
                if smooth_lvl > 0:
                    if isinstance(line, LineString):
                        line = LineString(chaikins_corner_cutting(line.coords, smooth_lvl))
                    elif isinstance(line, MultiLineString):
                        line = MultiLineString([LineString(chaikins_corner_cutting(ls.coords, smooth_lvl)) for ls in line.geoms])
                lines.append(line)
        
        self.poly_prog.config(text="")
        if lines:
            fig, ax = plt.subplots(figsize=(10,10))
            data.plot(ax=ax, color='#f0f0f0', edgecolor='#cccccc')
            gpd.GeoSeries(lines).plot(ax=ax, color='blue', linewidth=2)
            plt.title("Visual Box: Wave-Free Result")
            plt.show()

    def save_lines(self):
        data = self.get_processed_polygons()
        if data is None: return
        path = filedialog.asksaveasfilename(defaultextension=".geojson")
        if not path: return
        
        def run():
            try:
                line_data = []
                prune_val, tol = self.prune_slider.get(), self.straighten_slider.get()
                smooth_lvl = int(self.smooth_slider.get())
                for _, row in data.iterrows():
                    line = pygeoops.centerline(row.geometry, densify_distance=-1, min_branch_length=prune_val)
                    if line:
                        line = line.simplify(tol)
                        if smooth_lvl > 0:
                            if isinstance(line, LineString): line = LineString(chaikins_corner_cutting(line.coords, smooth_lvl))
                            elif isinstance(line, MultiLineString): line = MultiLineString([LineString(chaikins_corner_cutting(ls.coords, smooth_lvl)) for ls in line.geoms])
                        new_row = row.copy(); new_row.geometry = line; line_data.append(new_row)
                if line_data:
                    gpd.GeoDataFrame(line_data, crs=data.crs).to_file(path, driver='GeoJSON', engine='pyogrio')
                    messagebox.showinfo("Success", "Wave-free lines saved!")
            except Exception as e: messagebox.showerror("Error", str(e))
        threading.Thread(target=run).start()

if __name__ == "__main__":
    root = tk.Tk(); app = WaveFreeGisTool(root); root.mainloop()