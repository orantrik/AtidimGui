import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import geopandas as gpd
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.widgets import RectangleSelector
from shapely.geometry import box, LineString, MultiLineString, Point
from shapely.ops import snap, unary_union
import pygeoops
import numpy as np

# --- ALGORITHM: WAVE-FREE SMOOTHING ---
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

# --- UI COMPONENT: DRAG-SELECT CHECKLIST ---
class DragSelectChecklist:
    def __init__(self, parent, title):
        self.frame = ttk.LabelFrame(parent, text=title, padding="5")
        self.frame.pack(side="left", expand=True, fill="both", padx=5)
        
        btn_frame = ttk.Frame(self.frame)
        btn_frame.pack(fill="x", pady=(0, 5))
        ttk.Button(btn_frame, text="Select All", command=self.select_all).pack(side="left", expand=True)
        ttk.Button(btn_frame, text="Deselect All", command=self.deselect_all).pack(side="left", expand=True)

        self.canvas = tk.Canvas(self.frame, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self.frame, command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        
        self.canvas.pack(side="left", expand=True, fill="both")
        self.scrollbar.pack(side="right", fill="y")
        
        self.vars = {}
        self.checkbuttons = [] 
        self.drag_target_state = None

    def populate(self, items, default_condition):
        for w in self.inner.winfo_children(): w.destroy()
        self.vars.clear(); self.checkbuttons.clear()
        for item in items:
            var = tk.BooleanVar(value=default_condition(item))
            cb = ttk.Checkbutton(self.inner, text=item, variable=var)
            cb.pack(anchor="w", fill="x")
            self.vars[item] = var
            self.checkbuttons.append(cb)
            cb.bind("<ButtonPress-1>", lambda e, v=var: self.start_drag(v))
            cb.bind("<B1-Motion>", self.do_drag)
            cb.bind("<ButtonRelease-1>", self.stop_drag)

    def select_all(self):
        for var in self.vars.values(): var.set(True)
    def deselect_all(self):
        for var in self.vars.values(): var.set(False)
    def start_drag(self, var):
        self.drag_target_state = not var.get(); var.set(self.drag_target_state)
        return "break"
    def do_drag(self, event):
        if self.drag_target_state is None: return
        target = self.inner.winfo_containing(event.x_root, event.y_root)
        for cb in self.checkbuttons:
            if target == cb or target in cb.winfo_children():
                var_name = cb.cget("variable")
                self.inner.getvar(var_name).set(self.drag_target_state)
    def stop_drag(self, event):
        self.drag_target_state = None

# --- MAIN APPLICATION ---
class IntegratedGisMaster:
    def __init__(self, root):
        self.root = root
        self.root.title("GIS Road Master: Undo & Snap Pro")
        self.root.geometry("1100x950")
        
        self.gdf = None
        self.master_lines_gdf = None
        self.selected_box_geom = None
        
        # Precision & Eraser State
        self.precision_lines_list = [] 
        self.eraser_history = [] # For Undo functionality

        self.setup_ui()

    def setup_ui(self):
        load_frame = ttk.LabelFrame(self.root, text=" 1. Data Source ", padding="10")
        load_frame.pack(fill="x", side="top", padx=10, pady=5)
        ttk.Button(load_frame, text="Load GeoJSON", command=self.load_file).pack(side="left", padx=5)
        self.status_lbl = ttk.Label(load_frame, text="No file loaded")
        self.status_lbl.pack(side="left", padx=5)

        self.notebook = ttk.Notebook(self.root)
        self.tab1, self.tab2, self.tab3 = ttk.Frame(self.notebook), ttk.Frame(self.notebook), ttk.Frame(self.notebook)
        self.notebook.add(self.tab1, text=" 1. Filters ")
        self.notebook.add(self.tab2, text=" 2. Global View ")
        self.notebook.add(self.tab3, text=" 3. Precision + Eraser ")
        self.notebook.pack(expand=True, fill="both", padx=5, pady=5)

        # TAB 1: FILTERS
        mid = ttk.Frame(self.tab1); mid.pack(expand=True, fill="both", padx=10, pady=5)
        self.p_list = DragSelectChecklist(mid, "Plans")
        self.c_list = DragSelectChecklist(mid, "Road Types")
        ttk.Button(self.tab1, text="INITIALIZE GLOBAL MAP", command=self.init_global_map).pack(pady=10)

        # TAB 2: GLOBAL
        t2_f = ttk.Frame(self.tab2, padding="20"); t2_f.pack(fill="both")
        self.g_prune = self.add_scale(t2_f, "Global Pruning", 0.0, 1.0, 0.1, 0.01)
        self.g_straight = self.add_scale(t2_f, "Global Straighten", 0.0, 0.0001, 0.00002, 0.000005)
        self.g_smooth = self.add_scale(t2_f, "Global Smoothing", 0, 5, 2, 1)
        ttk.Button(t2_f, text="REFRESH GLOBAL VIEW", command=self.preview_master).pack(pady=5, fill="x")
        ttk.Button(t2_f, text="DRAW EDIT BOX (ENTER TO CONFIRM)", command=self.start_box_selection, style="Accent.TButton").pack(pady=10, fill="x")

        # TAB 3: PRECISION + UNDO
        t3_f = ttk.Frame(self.tab3, padding="20"); t3_f.pack(fill="both")
        self.p_prune = self.add_scale(t3_f, "Precision Pruning", 0.0, 1.0, 0.15, 0.01)
        self.p_straight = self.add_scale(t3_f, "Precision Straighten", 0.0, 0.0001, 0.00002, 0.000005)
        self.p_smooth = self.add_scale(t3_f, "Precision Smoothing", 0, 5, 3, 1)
        
        btn_box = ttk.Frame(t3_f)
        btn_box.pack(fill="x", pady=10)
        ttk.Button(btn_box, text="OPEN ERASER PREVIEW", command=self.preview_precision).pack(side="left", expand=True, fill="x", padx=2)
        
        ttk.Button(t3_f, text="APPLY & SNAP TO GLOBAL", command=self.apply_and_return, style="Accent.TButton").pack(pady=10, fill="x")

    def add_scale(self, parent, label, f, t, v, r):
        ttk.Label(parent, text=label).pack(); s = tk.Scale(parent, from_=f, to=t, resolution=r, orient="horizontal")
        s.set(v); s.pack(fill="x"); return s

    def load_file(self):
        path = filedialog.askopenfilename()
        if not path: return
        self.gdf = gpd.read_file(path)
        self.p_list.populate(sorted(self.gdf['pl_number'].dropna().unique().astype(str)), lambda x: True)
        self.c_list.populate(sorted(self.gdf['mavat_name'].dropna().unique().astype(str)), lambda x: "דרך" in x)
        self.status_lbl.config(text=f"Loaded {len(self.gdf)} features")

    def process_geom(self, geom, prune, straight, smooth):
        line = pygeoops.centerline(geom, densify_distance=-1, min_branch_length=prune)
        if line:
            line = line.simplify(straight)
            if smooth > 0:
                if isinstance(line, LineString): line = LineString(chaikins_corner_cutting(line.coords, int(smooth)))
                elif isinstance(line, MultiLineString): line = MultiLineString([LineString(chaikins_corner_cutting(ls.coords, int(smooth))) for ls in line.geoms])
        return line

    def init_global_map(self):
        sel_p = [p for p, v in self.p_list.vars.items() if v.get()]
        sel_c = [c for c, v in self.c_list.vars.items() if v.get()]
        df = self.gdf[(self.gdf['pl_number'].astype(str).isin(sel_p)) & (self.gdf['mavat_name'].astype(str).isin(sel_c))].copy()
        if df.empty: return
        dissolved = df.dissolve()
        lines = [self.process_geom(g, self.g_prune.get(), self.g_straight.get(), int(self.g_smooth.get())) for g in dissolved.geometry]
        self.master_lines_gdf = gpd.GeoDataFrame(geometry=[l for l in lines if l], crs=self.gdf.crs)
        self.notebook.select(self.tab2); self.preview_master()

    def preview_master(self):
        if self.master_lines_gdf is None: return
        fig, ax = plt.subplots(figsize=(10,8)); self.master_lines_gdf.plot(ax=ax, color='blue', linewidth=1)
        plt.title("Master Global Map"); plt.show()

    def start_box_selection(self):
        if self.master_lines_gdf is None: return
        fig, ax = plt.subplots(figsize=(10,8))
        self.master_lines_gdf.plot(ax=ax, color='blue', alpha=0.6, linewidth=1)
        def on_select(e, r): self.selected_box_geom = box(e.xdata, e.ydata, r.xdata, r.ydata)
        def on_key(event):
            if event.key == 'enter' and self.selected_box_geom: plt.close(); self.notebook.select(self.tab3)
        self.rs = RectangleSelector(ax, on_select, interactive=True, button=[1])
        fig.canvas.mpl_connect('key_press_event', on_key)
        plt.title("SELECT AREA & PRESS ENTER"); plt.show()

    # --- TAB 3: ERASER WITH UNDO ---
    def preview_precision(self):
        if not self.selected_box_geom: return
        sel_p = [p for p, v in self.p_list.vars.items() if v.get()]
        sel_c = [c for c, v in self.c_list.vars.items() if v.get()]
        df = self.gdf[(self.gdf['pl_number'].astype(str).isin(sel_p)) & (self.gdf['mavat_name'].astype(str).isin(sel_c))].copy()
        clipped_poly = gpd.clip(df.dissolve(), self.selected_box_geom.buffer(1e-6))
        
        raw_lines = [self.process_geom(g, self.p_prune.get(), self.p_straight.get(), int(self.p_smooth.get())) for g in clipped_poly.geometry]
        self.precision_lines_list = []
        for l in raw_lines:
            if not l: continue
            if isinstance(l, MultiLineString): self.precision_lines_list.extend(list(l.geoms))
            else: self.precision_lines_list.append(l)
        
        self.eraser_history = [] # Reset history for new session
        self.show_eraser_window(clipped_poly)

    def show_eraser_window(self, background_poly):
        fig, ax = plt.subplots(figsize=(10,10))
        
        def redraw():
            ax.clear(); background_poly.plot(ax=ax, color='#f0f0f0', alpha=0.5)
            for i, line in enumerate(self.precision_lines_list):
                x, y = line.xy
                ax.plot(x, y, color='green', linewidth=2, picker=5, label=str(i))
            ax.set_title("ERASER: Click lines to delete | Ctrl+Z to Undo")
            fig.canvas.draw()

        def on_pick(event):
            idx = int(event.artist.get_label())
            # Save to history before deleting
            self.eraser_history.append((idx, self.precision_lines_list[idx]))
            self.precision_lines_list.pop(idx); redraw()

        def on_key(event):
            # Undo on Ctrl+Z or just 'z'
            if (event.key == 'ctrl+z' or event.key == 'z') and self.eraser_history:
                idx, line = self.eraser_history.pop()
                self.precision_lines_list.insert(idx, line)
                redraw()

        fig.canvas.mpl_connect('pick_event', on_pick)
        fig.canvas.mpl_connect('key_press_event', on_key)
        redraw(); plt.show()

    def apply_and_return(self):
        if not self.selected_box_geom or not self.precision_lines_list: return

        # 1. Precise Cut of Master Map
        master_union = unary_union(self.master_lines_gdf.geometry)
        remaining_blue = master_union.difference(self.selected_box_geom.buffer(1e-8))

        # 2. Prep Green Lines
        processed_new = [l.intersection(self.selected_box_geom) for l in self.precision_lines_list]
        processed_new = [l for l in processed_new if not l.is_empty]

        # 3. Connect/Snap Logic
        blue_endpoints = []
        if not remaining_blue.is_empty:
            geoms = remaining_blue.geoms if hasattr(remaining_blue, 'geoms') else [remaining_blue]
            for g in geoms:
                if not g.is_empty: blue_endpoints.extend([Point(g.coords[0]), Point(g.coords[-1])])
        
        endpoint_cloud = unary_union(blue_endpoints)
        final_lines = [snap(line, endpoint_cloud, tolerance=0.0001) for line in processed_new]

        # 4. Merge Back
        new_gdf = gpd.GeoDataFrame(geometry=final_lines, crs=self.gdf.crs)
        remaining_gdf = gpd.GeoDataFrame(geometry=[remaining_blue], crs=self.gdf.crs)
        self.master_lines_gdf = pd.concat([remaining_gdf, new_gdf], ignore_index=True)

        messagebox.showinfo("Success", "Correction applied and snapped.")
        self.notebook.select(self.tab2); self.preview_master()

if __name__ == "__main__":
    root = tk.Tk(); app = IntegratedGisMaster(root); root.mainloop()