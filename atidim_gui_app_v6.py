#!/usr/bin/env python3
import json
import re
import threading
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import tkinter as tk

import pyproj
from shapely import wkb
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.ops import transform, triangulate


DEFAULT_USAGE_COLORS = {
    "תיירות": "#ff7fa0",
    "מגורים": "#dbe03a",
    "ציבורי פתוח": "#92cf5d",
    "שמורת טבע יער גן לאומי נחל וסביבותיו": "#92cf5d",
    "ציבורי פתוח משולב": "#92cf5d",
    "שטח לבניני ציבור": "#9c6127",
    "תעשיה": "#722ebf",
    "תעסוקה": "#722ebf",
    "דרך": "#c91e1e",
    "נושאים שונים": "#82e1ff",
    "מסחר": "#666666",
    "מסחר ויעודים נוספים": "#666666",
    "יעודים מעורבים": "#ff96cc",
    "קרקע חקלאית ושטחים פתוחים": "#14b360",
    "יעוד אחר": "#ffffff",
    "מגורים משולב": "#ff7f00",

    # English fallbacks
    "tourism": "#ff7fa0",
    "residential": "#dbe03a",
    "open space": "#92cf5d",
    "public building": "#9c6127",
    "industry": "#722ebf",
    "employment": "#722ebf",
    "road": "#c91e1e",
    "mixed use": "#ff96cc",
    "agriculture": "#14b360",
    "other": "#ffffff",
    "residential mixed": "#ff7f00",
}


def sanitize_name(s: Any, max_len: int = 60) -> str:
    s = str(s)
    s = re.sub(r"\s+", "_", s.strip())
    s = re.sub(r"[^0-9A-Za-z_\-\.]", "_", s)
    return (s or "feature")[:max_len]


def parse_float(value: Any) -> Optional[float]:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except Exception:
        return None


def find_height(props: Dict[str, Any], height_keys: List[str]) -> float:
    for k in height_keys:
        if k in props:
            val = parse_float(props[k])
            if val is not None:
                return val
    for k, v in props.items():
        if "height" in str(k).lower():
            val = parse_float(v)
            if val is not None:
                return val
    return 0.0


def iter_polygons(geom) -> List[Polygon]:
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return list(geom.geoms)
    return []


def bounds_midpoint(geoms) -> Tuple[float, float]:
    minx, miny, maxx, maxy = geoms[0].bounds
    for g in geoms[1:]:
        bx = g.bounds
        minx, miny, maxx, maxy = min(minx, bx[0]), min(miny, bx[1]), max(maxx, bx[2]), max(maxy, bx[3])
    return (minx + maxx) / 2.0, (miny + maxy) / 2.0


def hex_to_rgb01(hex_color: str) -> Tuple[float, float, float]:
    s = str(hex_color).strip()
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6 or not re.fullmatch(r"[0-9A-Fa-f]{6}", s):
        return (0.8, 0.8, 0.8)
    return (int(s[0:2], 16) / 255.0, int(s[2:4], 16) / 255.0, int(s[4:6], 16) / 255.0)


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def parse_color_from_props(props: Dict[str, Any], color_keys: List[str], usage_key: str) -> Tuple[str, Tuple[float, float, float], str]:
    for k in color_keys:
        if k in props and props[k] not in (None, "", "null"):
            s = str(props[k]).strip()
            if s.startswith("#") or re.fullmatch(r"[0-9A-Fa-f]{6}|[0-9A-Fa-f]{3}", s):
                if not s.startswith("#"):
                    s = "#" + s
                return s, hex_to_rgb01(s), f"property:{k}"

    rgb_sets = [
        ("color_r", "color_g", "color_b"),
        ("r", "g", "b"),
        ("red", "green", "blue"),
        ("fill_r", "fill_g", "fill_b"),
    ]
    for rk, gk, bk in rgb_sets:
        if rk in props and gk in props and bk in props:
            rv = parse_float(props[rk])
            gv = parse_float(props[gk])
            bv = parse_float(props[bk])
            if rv is not None and gv is not None and bv is not None:
                if max(rv, gv, bv) > 1.0:
                    rgb = (clamp01(rv / 255.0), clamp01(gv / 255.0), clamp01(bv / 255.0))
                else:
                    rgb = (clamp01(rv), clamp01(gv), clamp01(bv))
                hx = "#{:02x}{:02x}{:02x}".format(
                    int(round(rgb[0] * 255)), int(round(rgb[1] * 255)), int(round(rgb[2] * 255))
                )
                return hx, rgb, f"properties:{rk},{gk},{bk}"

    usage_val = str(props.get(usage_key, "")).strip().lower()
    if usage_val:
        if usage_val in DEFAULT_USAGE_COLORS:
            hx = DEFAULT_USAGE_COLORS[usage_val]
            return hx, hex_to_rgb01(hx), f"usage:{usage_key}"
        for key, hx in DEFAULT_USAGE_COLORS.items():
            if key in usage_val:
                return hx, hex_to_rgb01(hx), f"usage-match:{usage_key}"

    return "#cccccc", (0.8, 0.8, 0.8), "default"


def point2d_key(x: float, y: float, precision: int = 9) -> Tuple[int, int]:
    scale = 10 ** precision
    return (int(round(x * scale)), int(round(y * scale)))


def polygon_rings_without_closing(poly: Polygon) -> Tuple[List[Tuple[float, float]], List[List[Tuple[float, float]]]]:
    def to_xy(coords):
        out = []
        for pt in coords:
            if len(pt) >= 2:
                out.append((float(pt[0]), float(pt[1])))
        return out

    ext = to_xy(list(poly.exterior.coords))
    if ext and ext[0] == ext[-1]:
        ext = ext[:-1]

    holes = []
    for ring in poly.interiors:
        coords = to_xy(list(ring.coords))
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        holes.append(coords)

    return ext, holes


def triangulate_polygon_faces(poly: Polygon) -> List[List[Tuple[float, float]]]:
    tris = triangulate(poly)
    kept = []
    for tri in tris:
        rep = tri.representative_point()
        if poly.covers(rep):
            coords = []
            raw = list(tri.exterior.coords)
            if raw and raw[0] == raw[-1]:
                raw = raw[:-1]
            for pt in raw:
                if len(pt) >= 2:
                    coords.append((float(pt[0]), float(pt[1])))
            if len(coords) == 3:
                kept.append(coords)
    return kept


def add_material_if_needed(mtl_lines: List[str], material_cache: Dict[str, str], color_hex: str, rgb: Tuple[float, float, float]) -> str:
    key = color_hex.lower()
    if key in material_cache:
        return material_cache[key]
    name = f"mat_{sanitize_name(key.replace('#', ''))}"
    material_cache[key] = name
    r, g, b = rgb
    mtl_lines.extend([
        f"newmtl {name}",
        f"Kd {r:.4f} {g:.4f} {b:.4f}",
        f"Ka {max(0.05, r * 0.2):.4f} {max(0.05, g * 0.2):.4f} {max(0.05, b * 0.2):.4f}",
        "Ks 0.0500 0.0500 0.0500",
        "Ns 10.0000",
        "d 1.0",
        "illum 2",
        "",
    ])
    return name


def write_simple_prism(obj_lines, vertex_index, poly, obj_name, cx, cy, height, material_name):
    ring = list(poly.exterior.coords)
    if len(ring) > 2 and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        return vertex_index, 0, 0
    bottom = [(x - cx, y - cy, 0.0) for (x, y, *_) in ring]
    top = [(x - cx, y - cy, float(height)) for (x, y, *_) in ring]
    n = len(ring)
    b0 = vertex_index
    t0 = vertex_index + n
    obj_lines.append(f"o {obj_name}")
    obj_lines.append(f"usemtl {material_name}")
    for (x, y, z) in bottom + top:
        obj_lines.append(f"v {x:.4f} {y:.4f} {z:.4f}")
    obj_lines.append("f " + " ".join(str(b0 + i) for i in reversed(range(n))))
    obj_lines.append("f " + " ".join(str(t0 + i) for i in range(n)))
    for i in range(n):
        i2 = (i + 1) % n
        obj_lines.append(f"f {b0+i} {b0+i2} {t0+i2} {t0+i}")
    obj_lines.append("")
    return vertex_index + (2 * n), 2 * n, 2 + n


def write_polygon_mesh_blender(obj_lines, vertex_index, poly, obj_name, cx, cy, height, material_name):
    if poly.is_empty:
        return vertex_index, 0, 0
    ext, holes = polygon_rings_without_closing(poly)
    if len(ext) < 3:
        return vertex_index, 0, 0
    all_points = ext[:]
    for hole in holes:
        all_points.extend(hole)
    unique_points = []
    point_to_local = {}
    for x, y in all_points:
        k = point2d_key(x, y)
        if k not in point_to_local:
            point_to_local[k] = len(unique_points)
            unique_points.append((x, y))
    n_unique = len(unique_points)
    b0 = vertex_index
    t0 = vertex_index + n_unique
    obj_lines.append(f"o {obj_name}")
    obj_lines.append(f"usemtl {material_name}")
    for x, y in unique_points:
        obj_lines.append(f"v {x - cx:.4f} {y - cy:.4f} 0.0000")
    for x, y in unique_points:
        obj_lines.append(f"v {x - cx:.4f} {y - cy:.4f} {float(height):.4f}")
    faces_added = 0
    for tri in triangulate_polygon_faces(poly):
        ids = [point_to_local[point2d_key(x, y)] for x, y in tri]
        obj_lines.append("f " + " ".join(str(b0 + i) for i in reversed(ids)))
        obj_lines.append("f " + " ".join(str(t0 + i) for i in ids))
        faces_added += 2

    def emit_wall(ring, is_hole=False):
        f = 0
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % n]
            a = point_to_local[point2d_key(x1, y1)]
            b = point_to_local[point2d_key(x2, y2)]
            bb1, bb2, tt1, tt2 = b0 + a, b0 + b, t0 + a, t0 + b
            if not is_hole:
                obj_lines.append(f"f {bb1} {bb2} {tt2}")
                obj_lines.append(f"f {bb1} {tt2} {tt1}")
            else:
                obj_lines.append(f"f {bb1} {tt2} {bb2}")
                obj_lines.append(f"f {bb1} {tt1} {tt2}")
            f += 2
        return f

    faces_added += emit_wall(ext, False)
    for hole in holes:
        faces_added += emit_wall(hole, True)
    obj_lines.append("")
    return vertex_index + (2 * n_unique), 2 * n_unique, faces_added


def chaikins_smooth_ring(coords, refinements: int = 3):
    """Chaikin corner-cutting smoothing for a closed polygon ring (wraps around)."""
    pts = [(float(c[0]), float(c[1])) for c in coords]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return coords
    for _ in range(refinements):
        n = len(pts)
        new_pts = []
        for i in range(n):
            a = pts[i]
            b = pts[(i + 1) % n]
            new_pts.append((0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]))
            new_pts.append((0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]))
        pts = new_pts
    pts.append(pts[0])
    return pts


def smooth_polygon_geom(poly: Polygon, refinements: int, simplify_tol: float) -> Polygon:
    """Apply optional simplification then Chaikin smoothing to a single polygon."""
    if simplify_tol > 0:
        poly = poly.simplify(simplify_tol, preserve_topology=True)
    if poly.is_empty:
        return poly
    ext = chaikins_smooth_ring(list(poly.exterior.coords), refinements)
    holes = [chaikins_smooth_ring(list(ring.coords), refinements) for ring in poly.interiors]
    try:
        return Polygon(ext, holes)
    except Exception:
        return poly


def geojson_smooth_curves(input_path: Path, output_path: Path, refinements: int, simplify_tol: float, src_epsg: int, dst_epsg: int, logger):
    """Load a GeoJSON, smooth polygon rings with Chaikin algorithm, write output."""
    with input_path.open("r", encoding="utf-8") as f:
        gj = json.load(f)
    if gj.get("type") == "FeatureCollection":
        features = gj.get("features", [])
    elif gj.get("type") == "Feature":
        features = [gj]
    else:
        raise ValueError("Expected GeoJSON FeatureCollection or Feature.")

    transformer = None
    if src_epsg != dst_epsg:
        transformer = pyproj.Transformer.from_crs(src_epsg, dst_epsg, always_xy=True).transform

    output_features = []
    smoothed = 0
    skipped = 0

    for feat in features:
        geom = feat.get("geometry")
        if geom is None:
            skipped += 1
            continue
        try:
            g = shape(geom)
        except Exception:
            skipped += 1
            continue
        if transformer:
            try:
                g = transform(transformer, g)
            except Exception:
                skipped += 1
                continue
        if isinstance(g, Polygon):
            g = smooth_polygon_geom(g, refinements, simplify_tol)
        elif isinstance(g, MultiPolygon):
            smoothed_polys = [smooth_polygon_geom(p, refinements, simplify_tol) for p in g.geoms]
            g = MultiPolygon([p for p in smoothed_polys if not p.is_empty])
        if g.is_empty:
            skipped += 1
            continue
        output_features.append({
            "type": "Feature",
            "properties": feat.get("properties") or {},
            "geometry": mapping(g),
        })
        smoothed += 1

    with output_path.open("w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": output_features}, f, ensure_ascii=False, indent=2)
    logger(f"Wrote {smoothed} smoothed features to {output_path}")
    if skipped:
        logger(f"Skipped {skipped} features (no valid polygon geometry)")


def convert_response_to_geojson(input_path: Path, output_path: Path, logger):
    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("Expected the input file to be a JSON array of objects.")
    features = []
    skipped = 0
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            skipped += 1
            continue
        props = dict(item)
        geom_hex = props.pop("geom", None)
        if not geom_hex:
            skipped += 1
            continue
        try:
            geom = wkb.loads(bytes.fromhex(geom_hex))
        except Exception as e:
            logger(f"Skipping record {idx}: invalid WKB ({e})")
            skipped += 1
            continue
        features.append({"type": "Feature", "properties": props, "geometry": mapping(geom)})
    with output_path.open("w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f, ensure_ascii=False, indent=2)
    logger(f"Wrote {len(features)} features to {output_path}")
    if skipped:
        logger(f"Skipped {skipped} records")


def geojson_to_obj_export(input_path: Path, output_base: Path, src_epsg: int, dst_epsg: int, height_keys: List[str], flat: bool, make_zip: bool, logger, blender_mode: bool, color_keys: List[str], usage_key: str):
    with input_path.open("r", encoding="utf-8") as f:
        gj = json.load(f)
    if gj.get("type") == "FeatureCollection":
        features = gj.get("features", [])
    elif gj.get("type") == "Feature":
        features = [gj]
    else:
        features = [{"type": "Feature", "properties": {}, "geometry": gj}]
    transformer = pyproj.Transformer.from_crs(src_epsg, dst_epsg, always_xy=True).transform
    proj_geoms = []
    for feat in features:
        geom = feat.get("geometry")
        if geom is None:
            continue
        try:
            g = transform(transformer, shape(geom))
            if not g.is_empty:
                proj_geoms.append(g)
        except Exception:
            continue
    if not proj_geoms:
        raise RuntimeError("No valid polygon geometries found in input.")
    cx, cy = bounds_midpoint(proj_geoms)

    obj_path = output_base.with_suffix(".obj")
    mtl_path = output_base.with_suffix(".mtl")
    meta_path = output_base.parent / f"{output_base.name}_metadata.json"

    obj_lines = [
        "# OBJ generated from GeoJSON",
        f"# Source: {input_path.name}",
        f"# Reprojected: EPSG:{src_epsg} -> EPSG:{dst_epsg} and centered",
        "# Blender-friendly export enabled" if blender_mode else "# Simple prism export",
        "",
        f"mtllib {mtl_path.name}",
        "",
    ]
    mtl_lines = ["# Materials", ""]
    material_cache: Dict[str, str] = {}
    meta = {"source": input_path.name, "crs": f"EPSG:{dst_epsg} (centered)", "center_xy": [cx, cy], "objects": []}
    vertex_index = 1
    total_vertices = 0
    total_faces = 0

    for i, feat in enumerate(features):
        props = feat.get("properties") or {}
        geom = feat.get("geometry")
        if geom is None:
            continue
        try:
            g = transform(transformer, shape(geom))
        except Exception:
            continue
        if g.is_empty:
            continue
        polys = iter_polygons(g)
        if not polys:
            continue

        gid = props.get("gid", props.get("id", i))
        base_name = sanitize_name(gid)
        height = 0.0 if flat else find_height(props, height_keys)
        color_hex, rgb, color_source = parse_color_from_props(props, color_keys, usage_key)
        material_name = add_material_if_needed(mtl_lines, material_cache, color_hex, rgb)

        meta["objects"].append({
            "object_name_base": base_name,
            "feature_index": i,
            "height_used": height,
            "color_hex_used": color_hex,
            "color_rgb_used": [round(rgb[0], 6), round(rgb[1], 6), round(rgb[2], 6)],
            "color_source": color_source,
            "material_name": material_name,
            "properties": props,
            "has_holes": any(len(p.interiors) > 0 for p in polys),
        })

        for j, poly in enumerate(polys):
            obj_name = base_name if j == 0 else f"{base_name}_{j}"
            if blender_mode:
                vertex_index, v_added, f_added = write_polygon_mesh_blender(obj_lines, vertex_index, poly, obj_name, cx, cy, height, material_name)
            else:
                vertex_index, v_added, f_added = write_simple_prism(obj_lines, vertex_index, poly, obj_name, cx, cy, height, material_name)
            total_vertices += v_added
            total_faces += f_added

    if not material_cache:
        add_material_if_needed(mtl_lines, material_cache, "#cccccc", (0.8, 0.8, 0.8))

    with obj_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(obj_lines))
    with mtl_path.open("w", encoding="utf-8") as f:
        f.write("\n".join(mtl_lines))
    with meta_path.open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    if make_zip:
        zip_path = output_base.parent / f"{output_base.name}_bundle.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(obj_path, arcname=obj_path.name)
            z.write(mtl_path, arcname=mtl_path.name)
            z.write(meta_path, arcname=meta_path.name)
        logger(f"Wrote: {zip_path}")
    else:
        logger(f"Wrote: {obj_path}")
        logger(f"Wrote: {mtl_path}")
        logger(f"Wrote: {meta_path}")
    logger(f"Objects/Features processed: {len(meta['objects'])}")
    logger(f"Unique materials: {len(material_cache)}")
    logger(f"Total vertices written: {total_vertices}")
    logger(f"Total faces written: {total_faces}")


def polygon_to_wkt(coords: List[List[float]]) -> str:
    ring = list(coords)
    if len(ring) < 3:
        raise ValueError("Polygon needs at least 3 points.")
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return "POLYGON ((" + ", ".join(f"{pt[0]:.10f} {pt[1]:.10f}" for pt in ring) + "))"


def parse_polygon_wkt(wkt_text: str) -> Polygon:
    m = re.match(r"^\s*POLYGON\s*\(\(\s*(.*?)\s*\)\)\s*$", wkt_text, flags=re.I | re.S)
    if not m:
        raise ValueError("Expected a simple POLYGON ((lon lat, ...)) WKT.")
    pts = []
    for pair in m.group(1).split(","):
        parts = pair.strip().split()
        if len(parts) < 2:
            raise ValueError("Invalid WKT coordinate pair.")
        pts.append((float(parts[0]), float(parts[1])))
    poly = Polygon(pts)
    if poly.is_empty or not poly.is_valid:
        raise ValueError("Drawn polygon is invalid.")
    return poly


def extract_body_object(fetch_text: str) -> dict:
    m = re.search(r'"body"\s*:\s*"((?:\\.|[^"\\])*)"', fetch_text, flags=re.S)
    if not m:
        raise ValueError('Could not find "body" string in fetch text.')
    return json.loads(json.loads('"' + m.group(1) + '"'))


def replace_body_object(fetch_text: str, body_obj: dict) -> str:
    m = re.search(r'"body"\s*:\s*"((?:\\.|[^"\\])*)"', fetch_text, flags=re.S)
    if not m:
        raise ValueError('Could not find "body" string in fetch text.')
    body_json_str = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":"))
    encoded = json.dumps(body_json_str, ensure_ascii=False)[1:-1]
    return fetch_text[:m.start(1)] + encoded + fetch_text[m.end(1):]


def try_extract_referrer_pos(fetch_text: str) -> Optional[Tuple[float, float]]:
    m = re.search(r'"referrer"\s*:\s*"([^"]+)"', fetch_text, flags=re.S)
    if not m:
        return None
    q = parse_qs(urlparse(m.group(1)).query)
    pos = q.get("pos")
    if not pos:
        return None
    vals = [float(x) for x in pos[0].split(",")]
    return (vals[0], vals[1]) if len(vals) >= 2 else None


def update_referrer_pos(fetch_text: str, lon: float, lat: float) -> str:
    m = re.search(r'("referrer"\s*:\s*")([^"]+)(")', fetch_text, flags=re.S)
    if not m:
        return fetch_text
    parsed = urlparse(m.group(2))
    q = parse_qs(parsed.query)
    pos_vals = q.get("pos", [""])[0].split(",")
    alt = pos_vals[2] if len(pos_vals) >= 3 else "1085.974"
    q["pos"] = [f"{lon:.7f},{lat:.7f},{alt}"]
    new_ref = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(q, doseq=True), parsed.fragment))
    return fetch_text[:m.start(2)] + new_ref + fetch_text[m.end(2):]


class PolygonMapServer:
    def __init__(self, logger):
        self.logger = logger
        self.httpd = None
        self.thread = None
        self.port = None
        self.last_polygon_wkt = ""
        self.last_center = (31.78, 35.21)
        self._lock = threading.Lock()

    def set_default_center(self, lat: float, lon: float):
        with self._lock:
            self.last_center = (lat, lon)

    def get_last_polygon_wkt(self):
        with self._lock:
            return self.last_polygon_wkt

    def get_last_center(self):
        with self._lock:
            return self.last_center

    def save_polygon(self, coords):
        poly = Polygon(coords)
        if poly.is_empty or not poly.is_valid:
            raise ValueError("Invalid polygon received from map.")
        wkt_text = polygon_to_wkt([[float(pt[0]), float(pt[1])] for pt in list(poly.exterior.coords)[:-1] if len(pt) >= 2])
        centroid = poly.centroid
        with self._lock:
            self.last_polygon_wkt = wkt_text
            self.last_center = (centroid.y, centroid.x)
        self.logger("Received polygon from browser map.")

    def start(self):
        if self.httpd is not None:
            return self.port
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/" or self.path.startswith("/?"):
                    lat, lon = outer.get_last_center()
                    data = outer.render_html(lat, lon).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                if self.path != "/save_polygon":
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    coords = payload.get("coords")
                    if not isinstance(coords, list) or len(coords) < 3:
                        raise ValueError("Missing coords.")
                    outer.save_polygon(coords)
                    resp = json.dumps({"ok": True, "wkt": outer.get_last_polygon_wkt()}).encode("utf-8")
                    self.send_response(200)
                except Exception as e:
                    resp = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
                    self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

            def log_message(self, *args):
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.logger(f"Map drawer server started on http://127.0.0.1:{self.port}")
        return self.port

    def render_html(self, lat: float, lon: float) -> str:
        return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>Draw Polygon</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<link rel="stylesheet" href="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css" />
<style>
html, body, #map {{ height: 100%; margin: 0; }}
.toolbar {{ position:absolute; z-index:1000; top:10px; left:10px; background:white; padding:10px; border-radius:8px; box-shadow:0 2px 10px rgba(0,0,0,.2); width:300px; font-family:Arial,sans-serif; }}
button {{ width:100%; margin-top:8px; padding:8px; }}
textarea {{ width:100%; height:90px; margin-top:8px; }}
.small {{ font-size:12px; color:#444; }}
</style>
</head>
<body>
<div class="toolbar">
  <div><b>Draw Polygon</b></div>
  <div class="small">Use the polygon tool, keep one polygon, then click Save Polygon.</div>
  <button onclick="savePolygon()">Save Polygon to App</button>
  <textarea id="status" readonly></textarea>
</div>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js"></script>
<script>
const map = L.map('map').setView([{lat:.7f}, {lon:.7f}], 18);
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{maxZoom:22, attribution:'&copy; OpenStreetMap contributors'}}).addTo(map);
const drawnItems = new L.FeatureGroup(); map.addLayer(drawnItems);
map.addControl(new L.Control.Draw({{
  edit: {{ featureGroup: drawnItems }},
  draw: {{ polyline:false, rectangle:false, circle:false, marker:false, circlemarker:false, polygon:{{ allowIntersection:false, showArea:true }} }}
}}));
function setStatus(msg) {{ document.getElementById('status').value = msg; }}
map.on(L.Draw.Event.CREATED, function(e) {{ drawnItems.clearLayers(); drawnItems.addLayer(e.layer); setStatus("Polygon ready. Click Save Polygon."); }});
function savePolygon() {{
  const layers = drawnItems.getLayers();
  if (!layers.length) {{ setStatus("Draw a polygon first."); return; }}
  const coords = layers[0].getLatLngs()[0].map(p => [p.lng, p.lat]);
  fetch('/save_polygon', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{coords}}) }})
    .then(r => r.json())
    .then(data => setStatus(data.ok ? "Saved to desktop app.\\n\\n" + data.wkt : "Error: " + data.error))
    .catch(err => setStatus("Error: " + err));
}}
</script>
</body>
</html>"""


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Atidim GeoJSON / OBJ GUI")
        self.geometry("1120x860")
        self.minsize(1000, 740)

        self.input_response = tk.StringVar()
        self.output_geojson = tk.StringVar()
        self.input_geojson = tk.StringVar()
        self.output_obj_base = tk.StringVar()
        self.src_epsg = tk.StringVar(value="4326")
        self.dst_epsg = tk.StringVar(value="2039")
        self.height_keys = tk.StringVar(value="building_height_approx,height,building_height")
        self.color_keys = tk.StringVar(value="color,fill,hex_color,main_color")
        self.usage_key = tk.StringVar(value="main_usage")
        self.flat = tk.BooleanVar(value=False)
        self.make_zip = tk.BooleanVar(value=True)
        self.blender_mode = tk.BooleanVar(value=True)

        self.curves_input = tk.StringVar()
        self.curves_output = tk.StringVar()
        self.curves_refinements = tk.IntVar(value=3)
        self.curves_simplify = tk.StringVar(value="0.0")
        self.curves_src_epsg = tk.StringVar(value="4326")
        self.curves_dst_epsg = tk.StringVar(value="4326")

        self.update_referrer_var = tk.BooleanVar(value=True)
        self.drawn_wkt_var = tk.StringVar()
        self.map_server = PolygonMapServer(self.logger)

        self._build_ui()

    def _build_ui(self):
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="Atidim GeoJSON / OBJ GUI", font=("Segoe UI", 15, "bold")).pack(anchor="w", pady=(0, 10))
        nb = ttk.Notebook(root)
        nb.pack(fill="x", expand=False)

        tab1 = ttk.Frame(nb, padding=10)
        tab2 = ttk.Frame(nb, padding=10)
        tab3 = ttk.Frame(nb, padding=10)
        tab4 = ttk.Frame(nb, padding=10)
        tab5 = ttk.Frame(nb, padding=10)
        nb.add(tab1, text="1. Response -> GeoJSON")
        nb.add(tab2, text="2. GeoJSON -> OBJ")
        nb.add(tab3, text="3. Run Both")
        nb.add(tab4, text="4. Fetch Polygon Editor")
        nb.add(tab5, text="5. GeoJSON Smooth Curves")

        self._build_response_tab(tab1)
        self._build_obj_tab(tab2)
        self._build_both_tab(tab3)
        self._build_fetch_tab(tab4)
        self._build_curves_tab(tab5)

        log_frame = ttk.LabelFrame(root, text="Log", padding=8)
        log_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.log = tk.Text(log_frame, wrap="word", height=16)
        self.log.pack(fill="both", expand=True)

    def _row(self, parent, label, var, browse_cmd, save=False, row=0):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(parent, textvariable=var, width=72).grid(row=row, column=1, sticky="ew", pady=6)
        ttk.Button(parent, text="Save As..." if save else "Browse...", command=browse_cmd).grid(row=row, column=2, padx=(8, 0), pady=6)

    def _build_response_tab(self, parent):
        parent.columnconfigure(1, weight=1)
        self._row(parent, "Input JSON/TXT", self.input_response, self.browse_input_response, row=0)
        self._row(parent, "Output GeoJSON", self.output_geojson, self.browse_output_geojson, save=True, row=1)
        ttk.Button(parent, text="Convert to GeoJSON", command=self.run_response_to_geojson).grid(row=2, column=1, sticky="w", pady=(12, 0))

    def _build_obj_tab(self, parent):
        parent.columnconfigure(1, weight=1)
        self._row(parent, "Input GeoJSON", self.input_geojson, self.browse_input_geojson, row=0)
        self._row(parent, "Output basename", self.output_obj_base, self.browse_output_obj_base, save=True, row=1)

        ttk.Label(parent, text="Source EPSG").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=self.src_epsg, width=20).grid(row=2, column=1, sticky="w", pady=6)

        ttk.Label(parent, text="Target EPSG").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=self.dst_epsg, width=20).grid(row=3, column=1, sticky="w", pady=6)

        ttk.Label(parent, text="Height keys (comma-separated)").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=self.height_keys, width=72).grid(row=4, column=1, sticky="ew", pady=6)

        ttk.Label(parent, text="Color keys (comma-separated)").grid(row=5, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=self.color_keys, width=72).grid(row=5, column=1, sticky="ew", pady=6)

        ttk.Label(parent, text="Usage fallback key (main_usage / יעוד קרקע)").grid(row=6, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=self.usage_key, width=30).grid(row=6, column=1, sticky="w", pady=6)

        ttk.Checkbutton(parent, text="Use Blender export mode with colors", variable=self.blender_mode).grid(row=7, column=1, sticky="w", pady=6)
        ttk.Checkbutton(parent, text="Flat (no extrusion)", variable=self.flat).grid(row=8, column=1, sticky="w", pady=6)
        ttk.Checkbutton(parent, text="Zip OBJ/MTL/metadata", variable=self.make_zip).grid(row=9, column=1, sticky="w", pady=6)

        ttk.Button(parent, text="Convert to OBJ", command=self.run_geojson_to_obj).grid(row=10, column=1, sticky="w", pady=(12, 0))

    def _build_both_tab(self, parent):
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text="This runs the full pipeline: WKB response file -> GeoJSON -> OBJ bundle.", wraplength=700).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Button(parent, text="Run Full Pipeline", command=self.run_both).grid(row=1, column=1, sticky="w")

    def _build_fetch_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        top = ttk.Frame(parent)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(top, text="Load Fetch File...", command=self.load_fetch_file).pack(side="left")
        ttk.Button(top, text="Save Edited Fetch...", command=self.save_fetch_file).pack(side="left", padx=6)
        ttk.Button(top, text="Open Draw Map", command=self.open_draw_map).pack(side="left", padx=20)
        ttk.Button(top, text="Use Drawn Polygon", command=self.use_drawn_polygon).pack(side="left")
        ttk.Button(top, text="Apply Polygon to Fetch", command=self.apply_polygon_to_fetch).pack(side="left", padx=6)
        ttk.Checkbutton(parent, text="Also update referrer pos to polygon centroid", variable=self.update_referrer_var).grid(row=1, column=0, sticky="w", pady=(0, 8))
        ttk.Label(parent, text="Current drawn polygon WKT").grid(row=2, column=0, sticky="w")
        ttk.Entry(parent, textvariable=self.drawn_wkt_var).grid(row=3, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(parent, text="Fetch JavaScript").grid(row=4, column=0, sticky="w")
        self.fetch_text = scrolledtext.ScrolledText(parent, wrap="word", height=18)
        self.fetch_text.grid(row=5, column=0, sticky="nsew")
        ttk.Label(parent, text="Edited output").grid(row=6, column=0, sticky="w", pady=(8, 0))
        self.fetch_output_text = scrolledtext.ScrolledText(parent, wrap="word", height=14)
        self.fetch_output_text.grid(row=7, column=0, sticky="nsew")
        parent.rowconfigure(5, weight=1)
        parent.rowconfigure(7, weight=1)

    def _build_curves_tab(self, parent):
        parent.columnconfigure(1, weight=1)
        self._row(parent, "Input GeoJSON", self.curves_input, self.browse_curves_input, row=0)
        self._row(parent, "Output GeoJSON", self.curves_output, self.browse_curves_output, save=True, row=1)

        ttk.Label(parent, text="Smoothing refinements (1–6)").grid(row=2, column=0, sticky="w", pady=6)
        spin = ttk.Spinbox(parent, from_=1, to=6, textvariable=self.curves_refinements, width=6)
        spin.grid(row=2, column=1, sticky="w", pady=6)

        ttk.Label(parent, text="Simplify tolerance (0 = off, e.g. 0.00001 for WGS84)").grid(row=3, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=self.curves_simplify, width=20).grid(row=3, column=1, sticky="w", pady=6)

        ttk.Label(parent, text="Source EPSG").grid(row=4, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=self.curves_src_epsg, width=20).grid(row=4, column=1, sticky="w", pady=6)

        ttk.Label(parent, text="Target EPSG (same = no reprojection)").grid(row=5, column=0, sticky="w", pady=6)
        ttk.Entry(parent, textvariable=self.curves_dst_epsg, width=20).grid(row=5, column=1, sticky="w", pady=6)

        ttk.Label(
            parent,
            text=(
                "Applies Chaikin corner-cutting smoothing to all polygon boundaries, "
                "producing smooth closed curves. Higher refinements = smoother but more vertices."
            ),
            wraplength=700,
            foreground="#555555",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 8))

        ttk.Button(parent, text="Export Smooth Curves GeoJSON", command=self.run_geojson_smooth_curves).grid(
            row=7, column=1, sticky="w", pady=(8, 0)
        )

    def browse_curves_input(self):
        path = filedialog.askopenfilename(filetypes=[("GeoJSON", "*.geojson *.json"), ("All Files", "*.*")])
        if path:
            self.curves_input.set(path)
            if not self.curves_output.get():
                p = Path(path)
                self.curves_output.set(str(p.parent / (p.stem + "_smooth" + p.suffix)))

    def browse_curves_output(self):
        path = filedialog.asksaveasfilename(defaultextension=".geojson", filetypes=[("GeoJSON", "*.geojson")])
        if path:
            self.curves_output.set(path)

    def run_geojson_smooth_curves(self):
        def task():
            input_path = Path(self.curves_input.get().strip())
            output_path = Path(self.curves_output.get().strip())
            if not input_path.exists():
                raise FileNotFoundError(f"Input file not found: {input_path}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                simplify_tol = float(self.curves_simplify.get().strip() or "0")
            except ValueError:
                simplify_tol = 0.0
            refinements = max(1, min(6, self.curves_refinements.get()))
            src_epsg = int(self.curves_src_epsg.get().strip() or "4326")
            dst_epsg = int(self.curves_dst_epsg.get().strip() or "4326")
            self.logger(f"Smoothing polygons (refinements={refinements}, simplify={simplify_tol})...")
            geojson_smooth_curves(input_path, output_path, refinements, simplify_tol, src_epsg, dst_epsg, self.logger)
        self._run_async(task)

    def browse_input_response(self):
        path = filedialog.askopenfilename(filetypes=[("JSON/TXT Files", "*.json *.txt"), ("All Files", "*.*")])
        if path:
            self.input_response.set(path)
            if not self.output_geojson.get():
                self.output_geojson.set(str(Path(path).with_suffix(".geojson")))
            if not self.input_geojson.get():
                self.input_geojson.set(str(Path(path).with_suffix(".geojson")))
            if not self.output_obj_base.get():
                self.output_obj_base.set(str(Path(path).with_suffix("")))

    def browse_output_geojson(self):
        path = filedialog.asksaveasfilename(defaultextension=".geojson", filetypes=[("GeoJSON", "*.geojson")])
        if path:
            self.output_geojson.set(path)
            if not self.input_geojson.get():
                self.input_geojson.set(path)

    def browse_input_geojson(self):
        path = filedialog.askopenfilename(filetypes=[("GeoJSON", "*.geojson *.json"), ("All Files", "*.*")])
        if path:
            self.input_geojson.set(path)
            if not self.output_obj_base.get():
                self.output_obj_base.set(str(Path(path).with_suffix("")))

    def browse_output_obj_base(self):
        path = filedialog.asksaveasfilename(defaultextension="", filetypes=[("All Files", "*.*")])
        if path:
            p = Path(path)
            if p.suffix.lower() in {".obj", ".mtl", ".json", ".zip", ".geojson"}:
                p = p.with_suffix("")
            self.output_obj_base.set(str(p))

    def logger(self, msg):
        try:
            self.log.insert("end", str(msg) + "\n")
            self.log.see("end")
            self.update_idletasks()
        except Exception:
            pass

    def _run_async(self, fn):
        def worker():
            try:
                fn()
                self.logger("Done.")
            except Exception as e:
                self.logger(f"ERROR: {e}")
                self.after(0, lambda: messagebox.showerror("Error", str(e)))
        threading.Thread(target=worker, daemon=True).start()

    def run_response_to_geojson(self):
        def task():
            input_path = Path(self.input_response.get().strip())
            output_path = Path(self.output_geojson.get().strip())
            if not input_path.exists():
                raise FileNotFoundError(f"Input file not found: {input_path}")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            self.logger("Running Response -> GeoJSON...")
            convert_response_to_geojson(input_path, output_path, self.logger)
        self._run_async(task)

    def _run_obj_export(self, input_path: Path, output_base: Path):
        height_keys = [x.strip() for x in self.height_keys.get().split(",") if x.strip()]
        color_keys = [x.strip() for x in self.color_keys.get().split(",") if x.strip()]
        geojson_to_obj_export(
            input_path=input_path,
            output_base=output_base,
            src_epsg=int(self.src_epsg.get()),
            dst_epsg=int(self.dst_epsg.get()),
            height_keys=height_keys,
            flat=self.flat.get(),
            make_zip=self.make_zip.get(),
            logger=self.logger,
            blender_mode=self.blender_mode.get(),
            color_keys=color_keys,
            usage_key=self.usage_key.get().strip() or "main_usage",
        )

    def run_geojson_to_obj(self):
        def task():
            input_path = Path(self.input_geojson.get().strip())
            output_base = Path(self.output_obj_base.get().strip())
            if not input_path.exists():
                raise FileNotFoundError(f"Input file not found: {input_path}")
            output_base.parent.mkdir(parents=True, exist_ok=True)
            self.logger("Running GeoJSON -> OBJ...")
            self._run_obj_export(input_path, output_base)
        self._run_async(task)

    def run_both(self):
        def task():
            input_response = Path(self.input_response.get().strip())
            output_geojson = Path(self.output_geojson.get().strip())
            output_base = Path(self.output_obj_base.get().strip())
            if not input_response.exists():
                raise FileNotFoundError(f"Input file not found: {input_response}")
            output_geojson.parent.mkdir(parents=True, exist_ok=True)
            output_base.parent.mkdir(parents=True, exist_ok=True)
            self.logger("Running full pipeline...")
            convert_response_to_geojson(input_response, output_geojson, self.logger)
            self._run_obj_export(output_geojson, output_base)
        self._run_async(task)

    def load_fetch_file(self):
        path = filedialog.askopenfilename(filetypes=[("JavaScript/TXT", "*.js *.txt"), ("All Files", "*.*")])
        if path:
            text = Path(path).read_text(encoding="utf-8")
            self.fetch_text.delete("1.0", "end")
            self.fetch_text.insert("1.0", text)
            self.fetch_output_text.delete("1.0", "end")
            self.fetch_output_text.insert("1.0", text)
            self.logger(f"Loaded fetch template: {path}")

    def save_fetch_file(self):
        path = filedialog.asksaveasfilename(defaultextension=".js", filetypes=[("JavaScript", "*.js"), ("Text", "*.txt")])
        if path:
            text = self.fetch_output_text.get("1.0", "end-1c").strip() or self.fetch_text.get("1.0", "end-1c").strip()
            Path(path).write_text(text, encoding="utf-8")
            self.logger(f"Saved edited fetch to: {path}")

    def open_draw_map(self):
        ref = try_extract_referrer_pos(self.fetch_text.get("1.0", "end-1c"))
        if ref:
            self.map_server.set_default_center(ref[1], ref[0])
        port = self.map_server.start()
        webbrowser.open(f"http://127.0.0.1:{port}")
        self.logger("Opened browser map. Draw a polygon and click Save Polygon.")

    def use_drawn_polygon(self):
        wkt_text = self.map_server.get_last_polygon_wkt()
        if not wkt_text:
            messagebox.showwarning("No Polygon", "No polygon received yet. Open the map, draw one, and save it.")
            return
        self.drawn_wkt_var.set(wkt_text)
        self.logger("Loaded drawn polygon into the editor.")

    def apply_polygon_to_fetch(self):
        fetch_text = self.fetch_text.get("1.0", "end-1c").strip()
        wkt_text = self.drawn_wkt_var.get().strip()
        if not fetch_text:
            messagebox.showwarning("Missing Fetch", "Paste or load the fetch JavaScript first.")
            return
        if not wkt_text:
            messagebox.showwarning("Missing Polygon", "Draw a polygon and click Use Drawn Polygon first.")
            return
        poly = parse_polygon_wkt(wkt_text)
        body_obj = extract_body_object(fetch_text)
        body_obj["geomwkt"] = wkt_text
        new_text = replace_body_object(fetch_text, body_obj)
        if self.update_referrer_var.get():
            c = poly.centroid
            new_text = update_referrer_pos(new_text, c.x, c.y)
        self.fetch_output_text.delete("1.0", "end")
        self.fetch_output_text.insert("1.0", new_text)
        self.logger("Updated geomwkt in fetch body.")
        if self.update_referrer_var.get():
            self.logger("Updated referrer pos to polygon centroid.")

    def on_close(self):
        try:
            if self.map_server.httpd:
                self.map_server.httpd.shutdown()
                self.map_server.httpd.server_close()
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
