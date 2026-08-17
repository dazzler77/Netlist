#!/usr/bin/env python3
from __future__ import annotations

import inkex
from pathlib import Path

# ---- your existing imports ----
import csv
import math
import re
import sys
import xml.etree.ElementTree as ET
from lxml import etree
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple
import tempfile


import argparse
import csv
import math
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

from svg_helpers import (local_name, child_title, find_element_by_id, inkscape_label, element_name, 
    parse_numbers, build_parent_map, get_title, get_description, is_inside_definition, descendants, 
    is_layer, layer_name, element_in_selected_layers, wire_distance, nearest_wire, 
    component_distance, nearest_component,
    dist, point_to_segment_distance, point_key,point_on_segment, segment_intersection ,
    mat_identity, mat_mul, mat_apply,parse_transform, element_world_matrix,pin_relative_path,  )
    
from spice_netlist_clean import make_spice_netlist as make_spice_netlist_external
from spice_wirelist import make_wirelist
from netlist_common import build_wire_nets, pin_touching_nets, endpoint_pins

Point = Tuple[float, float]
Matrix = Tuple[float, float, float, float, float, float]

NUMBER = r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?"
PATH_TOKEN_RE = re.compile(rf"[MmLlHhVvZz]|{NUMBER}")
TRANSFORM_RE = re.compile(r"(matrix|translate|scale|rotate)\s*\(([^)]*)\)")

COMPONENT_TYPES = {
    "resistor": {
        "prefix": "R",
        "default_arg": "resistance",
    },
    "inductor": {
        "prefix": "L",
        "default_arg": "inductance",
    },
    "capacitor": {
        "prefix": "C",
        "default_arg": "capacitance",
    },
    "diode": {
        "prefix": "D",
        "default_arg": "diode_model",
    },
}

COMPONENT_ALIASES = {
    "r": "resistor",
    "res": "resistor",
    "resistor": "resistor",

    "l": "inductor",
    "ind": "inductor",
    "inductor": "inductor",

    "c": "capacitor",
    "cap": "capacitor",
    "capacitor": "capacitor",

    "d": "diode",
    "diode": "diode",
}

COMPONENT_PREFIXES = {
    "R": "resistor",
    "C": "capacitor",
    "L": "inductor",
    "D": "diode",

    # future
    "SW": "switch",
    "J": "connector",
    "TP": "testpoint",
}


TEXT_TAGS = {
    "#id",
    "#wire",
    "#node",
    "#_node",
    "#title",
    "#label",
    "#description",

    "@id",
    "@title",
    "@label",
    "@description",
}


def get_match_value(el: ET.Element, source: str) -> str:
    """Return the selected SVG metadata field for regex matching."""
    source = (source or "id").lower()

    if source == "id":
        return element_name(el)

    if source == "label":
        return inkscape_label(el)

    if source == "title":
        return get_title(el)

    if source == "description":
        return get_description(el)

    return ""




# ---------------------------------------------------------------------
# SVG path parsing
# ---------------------------------------------------------------------

def parse_path_segments(d: str) -> List[Tuple[Point, Point]]:
    """
    Converts simple SVG path data into straight segments.

    Supports:
        M/m, L/l, H/h, V/v, Z/z

    This suits normal Inkscape schematic wires and pin legs.
    """
    toks = PATH_TOKEN_RE.findall(d or "")
    i = 0
    cmd: Optional[str] = None
    cur: Point = (0.0, 0.0)
    start: Point = (0.0, 0.0)
    out: List[Tuple[Point, Point]] = []

    def is_cmd(t: str) -> bool:
        return len(t) == 1 and t.isalpha()

    def read_float() -> float:
        nonlocal i

        if i >= len(toks) or is_cmd(toks[i]):
            raise ValueError("Expected number in path data")

        v = float(toks[i])
        i += 1

        return v

    while i < len(toks):
        if is_cmd(toks[i]):
            cmd = toks[i]
            i += 1

        if cmd is None:
            raise ValueError("Path data starts without command")

        if cmd in "Mm":
            first = True

            while i < len(toks) and not is_cmd(toks[i]):
                x = read_float()
                y = read_float()

                new = (
                    (cur[0] + x, cur[1] + y)
                    if cmd == "m"
                    else (x, y)
                )

                if first:
                    cur = start = new
                    first = False
                else:
                    out.append((cur, new))
                    cur = new

            cmd = "l" if cmd == "m" else "L"

        elif cmd in "Ll":
            while i < len(toks) and not is_cmd(toks[i]):
                x = read_float()
                y = read_float()

                new = (
                    (cur[0] + x, cur[1] + y)
                    if cmd == "l"
                    else (x, y)
                )

                out.append((cur, new))
                cur = new

        elif cmd in "Hh":
            while i < len(toks) and not is_cmd(toks[i]):
                x = read_float()

                new = (
                    (cur[0] + x, cur[1])
                    if cmd == "h"
                    else (x, cur[1])
                )

                out.append((cur, new))
                cur = new

        elif cmd in "Vv":
            while i < len(toks) and not is_cmd(toks[i]):
                y = read_float()

                new = (
                    (cur[0], cur[1] + y)
                    if cmd == "v"
                    else (cur[0], y)
                )

                out.append((cur, new))
                cur = new

        elif cmd in "Zz":
            if dist(cur, start) > 0:
                out.append((cur, start))

            cur = start
            cmd = None

        else:
            # Curves/arcs are ignored for schematic extraction.
            while i < len(toks) and not is_cmd(toks[i]):
                i += 1

    return out



# ---------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------

@dataclass
class Segment:
    a: Point
    b: Point


@dataclass
class Wire:
    element_id: str
    start: Point
    end: Point
    segments: List[Segment]
    title: str = ""
    owner: str = ""


@dataclass
class Pin:
    component: str
    pin_number: str
    element_id: str
    a: Point
    b: Point
    shape: str = "line"
    center: Optional[Point] = None
    radius: float = 0.0
    bbox: Optional[Tuple[float, float, float, float]] = None


@dataclass
class Component:
    ref: str
    component_type: str
    group_id: str
    pins: Dict[str, Pin]
    title: str = ""
    label: str = ""
    description: str = ""
    netlist: str = ""
    owner: str = ""
    is_subckt: bool = False
    subckt_name: str = ""
    order: int = 0


# ---------------------------------------------------------------------
# Component and pin detection
# ---------------------------------------------------------------------

def extract_netlist_description(el: ET.Element) -> str:
    """Return the object's netlist description without the optional Netlist= prefix.

    Accepts:
        netlist=...
        netlist = ...
        Netlist = ...
    """
    desc = get_description(el).strip()

    desc = re.sub(
        r"^\s*netlist\s*=\s*",
        "",
        desc,
        flags=re.I,
    )

    return desc.strip()

def first_netlist_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def parse_subckt_name(netlist: str) -> Optional[str]:
    """Return the .SUBCKT name from a netlist block, if present."""
    line = first_netlist_line(netlist)
    m = re.match(r"^\.subckt\s+(\S+)", line, flags=re.I)
    return m.group(1) if m else None


def split_explicit_owner(netlist: str) -> Tuple[str, str]:
    """
    If the first token is owner.local, return (owner, local).

    Example:
        part1.S1 10 0 1 0 switch1 OFF -> (part1, S1)
    """
    line = first_netlist_line(netlist)
    if not line or line.startswith("."):
        return "", ""

    first = line.split(None, 1)[0]
    if "." not in first:
        return "", ""

    owner, local = first.split(".", 1)
    if not owner or not local:
        return "", ""

    return owner, local


def group_component_type(el: ET.Element) -> Optional[str]:
    """
    Generic component detection.

    A group is a component if it either:
      - has a Netlist= description, including .SUBCKT blocks, or
      - contains at least one named pin object.

    Component type is no longer inferred from R/C/L/D prefixes.
    The netlist text defines the actual output format.
    """
    if local_name(el.tag) != "g":
        return None

    if is_layer(el):
        return None

    if extract_netlist_description(el):
        return "generic"

    for child in list(el):
        if get_pin_number(child) is not None:
            return "generic"

    return None


def _simple_object_name(el: ET.Element) -> str:
    text = (
        child_title(el)
        or inkscape_label(el)
        or el.attrib.get("id", "")
    ).strip()

    if "." in text:
        text = text.rsplit(".", 1)[-1]

    return text.strip()


def get_pin_number(el: ET.Element) -> Optional[str]:
    """
    Returns the pin name exactly as the user labelled it.

    Priority:
        1. <title>
        2. inkscape:label
        3. id

    """

    tag = local_name(el.tag)

    if tag not in {
        "line",
        "path",
        "circle",
        "ellipse",
        "rect",
    }:
        return None

    candidates = [
        child_title(el),
        inkscape_label(el),
        el.attrib.get("id", ""),
    ]

    for text in candidates:

        text = (text or "").strip()

        if not text:
            continue

        # "#12345" defines node names on wires, not pin names.
        if text.startswith("#"):
            continue

        # Allow hierarchical naming:
        #
        # part1.NO  -> NO
        # relay1.A1 -> A1
        #
        if "." in text:
            text = text.rsplit(".", 1)[-1]

        # Ignore default autogenerated ids.
        if re.match(
            r"^(path|rect|circle|ellipse|line)\d+$",
            text,
            flags=re.I,
        ):
            continue

        return text

    return None


def circle_pin_points(el: ET.Element, matrix: Matrix) -> Optional[Tuple[Point, float]]:
    try:
        cx = float(el.attrib.get("cx", "0"))
        cy = float(el.attrib.get("cy", "0"))
        r = float(el.attrib.get("r", "0"))
    except ValueError:
        return None

    c = mat_apply(matrix, (cx, cy))
    rx = mat_apply(matrix, (cx + r, cy))
    ry = mat_apply(matrix, (cx, cy + r))
    radius = max(dist(c, rx), dist(c, ry))
    return c, radius


def ellipse_pin_points(el: ET.Element, matrix: Matrix) -> Optional[Tuple[Point, float]]:
    try:
        cx = float(el.attrib.get("cx", "0"))
        cy = float(el.attrib.get("cy", "0"))
        rx0 = float(el.attrib.get("rx", "0"))
        ry0 = float(el.attrib.get("ry", "0"))
    except ValueError:
        return None

    c = mat_apply(matrix, (cx, cy))
    rx = mat_apply(matrix, (cx + rx0, cy))
    ry = mat_apply(matrix, (cx, cy + ry0))
    radius = max(dist(c, rx), dist(c, ry))
    return c, radius


def rect_pin_bbox(el: ET.Element, matrix: Matrix) -> Optional[Tuple[float, float, float, float]]:
    try:
        x = float(el.attrib.get("x", "0"))
        y = float(el.attrib.get("y", "0"))
        w = float(el.attrib.get("width", "0"))
        h = float(el.attrib.get("height", "0"))
    except ValueError:
        return None

    pts = [
        mat_apply(matrix, (x, y)),
        mat_apply(matrix, (x + w, y)),
        mat_apply(matrix, (x + w, y + h)),
        mat_apply(matrix, (x, y + h)),
    ]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def element_pin(
    el: ET.Element,
    matrix: Matrix,
    component_ref: str,
    pin_number: str,
) -> Optional[Pin]:
    """Build a Pin from line/path/circle/ellipse/rect geometry."""
    tag = local_name(el.tag)
    element_id = element_name(el)

    if tag in {"line", "path"}:
        pts = element_endpoints(el, matrix)
        if pts is None:
            return None
        return Pin(component_ref, pin_number, element_id, pts[0], pts[1], shape=tag)

    if tag == "circle":
        data = circle_pin_points(el, matrix)
        if not data:
            return None
        c, r = data
        return Pin(component_ref, pin_number, element_id, c, c, shape="circle", center=c, radius=r)

    if tag == "ellipse":
        data = ellipse_pin_points(el, matrix)
        if not data:
            return None
        c, r = data
        return Pin(component_ref, pin_number, element_id, c, c, shape="ellipse", center=c, radius=r)

    if tag == "rect":
        bbox = rect_pin_bbox(el, matrix)
        if not bbox:
            return None
        x1, y1, x2, y2 = bbox
        c = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        return Pin(component_ref, pin_number, element_id, c, c, shape="rect", center=c, bbox=bbox)

    return None





def line_points(
    el: ET.Element,
    matrix: Matrix,
) -> Optional[Tuple[Point, Point]]:
    try:
        x1 = float(el.attrib.get("x1", "0"))
        y1 = float(el.attrib.get("y1", "0"))
        x2 = float(el.attrib.get("x2", "0"))
        y2 = float(el.attrib.get("y2", "0"))
    except ValueError:
        return None

    return (
        mat_apply(matrix, (x1, y1)),
        mat_apply(matrix, (x2, y2)),
    )


def path_segments_transformed(
    el: ET.Element,
    matrix: Matrix,
) -> List[Segment]:
    raw = parse_path_segments(el.attrib.get("d", ""))
    out: List[Segment] = []

    for a0, b0 in raw:
        a = mat_apply(matrix, a0)
        b = mat_apply(matrix, b0)

        if dist(a, b) > 1e-9:
            out.append(Segment(a, b))

    return out


def element_segments(
    el: ET.Element,
    matrix: Matrix,
) -> List[Segment]:
    tag = local_name(el.tag)

    if tag == "path":
        return path_segments_transformed(el, matrix)

    if tag == "line":
        pts = line_points(el, matrix)

        if pts and dist(pts[0], pts[1]) > 1e-9:
            return [Segment(pts[0], pts[1])]

    return []


def element_endpoints(
    el: ET.Element,
    matrix: Matrix,
) -> Optional[Tuple[Point, Point]]:
    tag = local_name(el.tag)

    if tag == "line":
        return line_points(el, matrix)

    if tag == "path":
        segs = path_segments_transformed(el, matrix)

        if not segs:
            return None

        return segs[0].a, segs[-1].b

    return None


# ---------------------------------------------------------------------
# SVG collection
# ---------------------------------------------------------------------

def collect_svg(
    svg_file: Path,
    include_layers=None,
    component_source="id",
    component_regex=".*",
    wire_source="id",
    wire_regex="^wire",
    pin_source="id",
    pin_regex="^pin",
):
    """
    Collect wires and components from the SVG.

    Component detection:
        - any group with a netlist description is a component
        - otherwise the selected component metadata field is matched with component_regex

    Wire detection:
        - only line/path elements whose selected metadata field matches wire_regex are wires

    Pin detection:
        - if component has netlist text, pin names are inferred from that netlist and
          matching child objects become pins
        - if component has no netlist text, pin_regex is used against the selected pin field
    """

    component_re = re.compile(component_regex or r".*", re.I)
    wire_re = re.compile(wire_regex or r"^wire", re.I)
    pin_re = re.compile(pin_regex or r"^pin", re.I)

    tree = ET.parse(svg_file)
    root = tree.getroot()
    parent = build_parent_map(root)

    selected_layers = include_layers if include_layers else None


    def nearest_component_group(el: ET.Element):
        cur = parent.get(el)

        while cur is not None:
            if cur in component_group_set:
                return cur
            cur = parent.get(cur)

        return None

    def in_selected_scope(el: ET.Element) -> bool:
        if not selected_layers:
            return True
        return element_in_selected_layers(el, parent, selected_layers)

    def name_from_source(el: ET.Element, source: str) -> str:
        value = get_match_value(el, source).strip()

        if value.startswith("#"):
            return ""

        return value.strip()

    def is_default_auto_id(name: str) -> bool:
        return re.match(r"^(path|rect|circle|ellipse|line)\d+$", name or "", flags=re.I) is not None
        
        
        

    def netlist_declared_pin_names(netlist: str) -> Set[str]:
        """Infer formal pin names used by a component netlist template."""
        names: Set[str] = set()

        for m in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_\-]*)\b", netlist or ""):
            names.add(m.group(1))

        for raw in (netlist or "").splitlines():
            line = raw.strip()
            if not line:
                continue

            line = line.split(";", 1)[0].strip()
            if not line:
                continue

            tokens = line.split()
            if not tokens:
                continue

            first = tokens[0]
            lower_first = first.lower()

            if lower_first == ".subckt" and len(tokens) >= 3:
                names.update(tokens[2:])
                continue

            if lower_first.startswith(".ends"):
                continue

            if first.lower().startswith("x") and len(tokens) >= 4:
                names.update(tokens[1:-1])
                continue

            if len(tokens) >= 3:
                names.update(tokens[1:3])

        blocked = {"dc", "ac", "pulse", "pwl"}
        return {n for n in names if n and n.lower() not in blocked}

    component_groups: List[Tuple[ET.Element, str, str, bool, str]] = []
    subckt_group_to_name: Dict[ET.Element, str] = {}

    # -------------------------------------------------------------
    # COMPONENT/SUBCKT COLLECTION
    # -------------------------------------------------------------
    for el in root.iter():
        if is_inside_definition(el, parent):
            continue

        if not in_selected_scope(el):
            continue

        if local_name(el.tag) != "g":
            continue

        if is_layer(el):
            continue

        netlist = extract_netlist_description(el)
        has_netlist = bool(netlist)

        component_value = get_match_value(el, component_source)
        is_component = has_netlist or bool(component_re.search(component_value or ""))

        if not is_component:
            continue

        subckt_name = parse_subckt_name(netlist) or ""
        is_subckt = bool(subckt_name)

        component_groups.append((el, "generic", netlist, is_subckt, subckt_name))

        if is_subckt:
            subckt_group_to_name[el] = subckt_name

    def ancestor_subckt_name(el: ET.Element) -> str:
        cur = parent.get(el)
        while cur is not None:
            name = subckt_group_to_name.get(cur, "")
            if name:
                return name
            cur = parent.get(cur)
        return ""

    group_to_ref = {
        group: element_name(group)
        for group, _ctype, _netlist, _is_subckt, _subckt_name in component_groups
    }

    def nearest_component_owner(el: ET.Element) -> str:
        cur = parent.get(el)
        while cur is not None:
            if cur in group_to_ref:
                return group_to_ref[cur]
            cur = parent.get(cur)
        return ""

    # -------------------------------------------------------------
    # COMPONENT OBJECTS
    # -------------------------------------------------------------
    components: List[Component] = []
    pin_elements: Set[ET.Element] = set()
    component_group_set = {g for g, *_rest in component_groups}

    for order, (group, ctype, netlist, is_subckt, subckt_name) in enumerate(component_groups):

        ref = element_name(group)



    for order, (group, ctype, netlist, is_subckt, subckt_name) in enumerate(component_groups):
        ref = element_name(group)
        pins: Dict[str, Pin] = {}
        declared_pins = {p.lower(): p for p in netlist_declared_pin_names(netlist)}
        has_netlist = bool(netlist)

        for el in descendants(group):
            if local_name(el.tag) == "g" and el in component_group_set and el is not group:
                continue

            tag = local_name(el.tag)
            if tag not in {"line", "path", "circle", "ellipse", "rect"}:
                continue

            pin_name = None

            candidates = [
                name_from_source(el, "id"),
                name_from_source(el, "label"),
                name_from_source(el, "title"),
                name_from_source(el, "description"),
            ]

            for candidate in candidates:

                if not candidate:
                    continue

                key = candidate.lower()

                # Explicitly declared in netlist
                if key in declared_pins:
                    pin_name = declared_pins[key]
                    break

                # Or matches pin regex
                if pin_re.search(candidate):
                    pin_name = candidate
                    break

            if not pin_name:
                continue

            if is_default_auto_id(pin_name):
                continue

            owner_group = nearest_component_group(el)

            cur = el
            chain = []

            while cur is not None:
                chain.append(element_name(cur))
                cur = parent.get(cur)

            if owner_group is not group:
                inkex.utils.debug(
                f"SKIP PIN group={ref} pin={element_name(el)}"
                )
                continue

            pin = element_pin(
                el,
                element_world_matrix(el, parent),
                ref,
                pin_name,
            )

            if pin is None:
                continue

            if pin_name in pins:
                inkex.utils.debug(
                    f"Warning: {ref} has duplicate pin {pin_name}",
                    file=sys.stderr,
                )

            pins[pin_name] = pin
            pin_elements.add(el)

        explicit_owner, local_ref = split_explicit_owner(netlist)
        owner = explicit_owner or ("" if is_subckt else ancestor_subckt_name(group))

        if explicit_owner and local_ref:
            ref = local_ref

        components.append(
            Component(
                ref=ref,
                component_type=ctype,
                group_id=element_name(group),
                pins=pins,
                title=get_title(group),
                label=inkscape_label(group) or "",
                description=get_description(group),
                netlist=netlist,
                owner=owner,
                is_subckt=is_subckt,
                subckt_name=subckt_name,
                order=order,
            )
        )

    # -------------------------------------------------------------
    # WIRE COLLECTION
    # -------------------------------------------------------------
    wires: List[Wire] = []

    for el in root.iter():
        if is_inside_definition(el, parent):
            continue

        if not in_selected_scope(el):
            continue

        tag = local_name(el.tag)
        if tag not in {"path", "line"}:
            continue

        if el in pin_elements:
            continue

        wire_value = get_match_value(el, wire_source)
        if not wire_re.search(wire_value or ""):
            continue

        try:
            segs = element_segments(
                el,
                element_world_matrix(el, parent),
            )
        except Exception as exc:
            print(
                f"Warning: skipping {element_name(el)!r}: {exc}",
                file=sys.stderr,
            )
            continue

        if not segs:
            continue
        wire_owner = nearest_component_owner(el)
        wires.append(
            Wire(
                element_id=element_name(el),
                start=segs[0].a,
                end=segs[-1].b,
                segments=segs,
                title=get_title(el),
                owner=wire_owner,
            )
        )

    return wires, components




# ---------------------------------------------------------------------
# Wirelist generation
# ---------------------------------------------------------------------





def write_wirelist_csv(
    rows: List[Dict[str, str]],
    out_path: Path,
) -> None:
    fields = [
        "wire_id",
        "from_component",
        "from_pin",
        "to_component",
        "to_pin",
        "net",
    ]

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Extract SPICE-like netlist and point-to-point wirelist from an SVG schematic."
    )

    ap.add_argument(
        "svg",
        type=Path,
        help="Input SVG file",
    )

    ap.add_argument(
        "--out",
        type=Path,
        default=Path("netlist.cir"),
        help="Output SPICE-like netlist file",
    )  

    ap.add_argument(
        "--wire-out",
        type=Path,
        default=Path("wirelist.csv"),
        help="Output point-to-point wirelist CSV file",
    )

    ap.add_argument(
        "--tol",
        type=float,
        default=0.5,
        help="Coordinate tolerance in SVG units",
    )

    ap.add_argument(
        "--wire_source",
        default="id",
        help="id, label, title or description",
    )

    ap.add_argument(
        "--wire_regex",
        default="^(wire|net).*",
        help="regex for what defines a wire",
    )

    
    ap.add_argument(
        "--layers",
        type=str,
        default="",
        help="Comma-separated list of Inkscape layer names to include (empty = all layers)",
    )


    args = ap.parse_args()

    layer_set = {s.strip() for s in args.layers.split(",") if s.strip()}

    wires, components = collect_svg(
        tmp_svg,
        include_layers=layer_set if layer_set else None,
        component_source=self.options.component_source,
        component_regex=self.options.component_regex,
        wire_source=self.options.wire_source,
        wire_regex=self.options.wire_regex,
        pin_source=self.options.pin_source,
        pin_regex=self.options.pin_regex,
    )

    inkex.errormsg(f"Wires found: {len(wires)}")
    inkex.errormsg(f"Components found: {len(components)}")
    
    wire_params = {
        "wire_source": args.wire_source,
        "wire_regex": args.wire_regex,
    }

    netlist_lines, net_warnings = make_spice_netlist_external(
        wires=wires,
        components=components,
        tol=args.tol,
        defaults=None,
        build_wire_nets=build_wire_nets,
        pin_touching_nets=pin_touching_nets,
        split_explicit_owner=split_explicit_owner,
        first_netlist_line=first_netlist_line,
    )

    wire_rows, wire_warnings = make_wirelist(
        wires=wires,
        components=components,
        tol=args.tol,
    )

    with args.out.open("w", encoding="utf-8") as f:
        f.write("* SVG extracted SPICE-like netlist\n")

        for line in netlist_lines:
            f.write(line + "\n")

        f.write(".end\n")

    write_wirelist_csv(wire_rows, args.wire_out)

    print(f"Read {len(wires)} wire object(s)")
    print(f"Read {len(components)} component group(s)")
    print(f"Wrote {len(netlist_lines)} SPICE netlist line(s) to {args.out}")
    print(f"Wrote {len(wire_rows)} wirelist row(s) to {args.wire_out}")

    for warning in net_warnings + wire_warnings:
        print("Warning:", warning, file=sys.stderr)

    return 0



class SvgToSpice(inkex.EffectExtension):

    def add_arguments(self, pars):

        pars.add_argument("--out", type=str, default="netlist.cir")
        pars.add_argument("--wire_out", type=str, default="wirelist.csv")
        pars.add_argument("--tol", type=float, default=0.5)

        pars.add_argument("--resistance", type=str, default="1k")
        pars.add_argument("--inductance", type=str, default="1m")
        pars.add_argument("--capacitance", type=str, default="1u")
        pars.add_argument("--diode_model", type=str, default="Ddefault")
        pars.add_argument("--layers", type=str, default="")

        pars.add_argument("--component_source", type=str, default="id")
        pars.add_argument("--component_regex", type=str, default=".*")
        pars.add_argument("--wire_source", type=str, default="id")
        pars.add_argument("--wire_regex", type=str, default="^wire")
        pars.add_argument("--pin_source", type=str, default="id")
        pars.add_argument("--pin_regex", type=str, default="^pin")

        pars.add_argument("--show_netlist", type=inkex.Boolean, default=False)
        pars.add_argument("--show_wirelist", type=inkex.Boolean, default=False)

        pars.add_argument("--text_x", type=float, default=0.0)
        pars.add_argument("--text_y", type=float, default=0.0)
 

    def update_text_placeholders(
        self,
        wires,
        wire_rows,
        components,
        root,
    ):

        # Build lookup from wire_id -> wirelist row
        wire_lookup = {
            row["wire_id"]: row
            for row in wire_rows
        }

        for el in root.iter():

            if local_name(el.tag) != "text":
                continue

            # Read all tspan text
            text_content = ""

            for child in el:

                if local_name(child.tag) == "tspan":
                    text_content += (child.text or "")

            token = child_title(el).lower()
            
            is_wire_tag = token.startswith("#")
            is_component_tag = token.startswith("@")
            

            if token not in TEXT_TAGS:
                continue

            try:
                x = float(el.get("x"))
                y = float(el.get("y"))
            except Exception:
                continue

            nearest_wire_obj = None
            nearest_component_obj = None

            if is_wire_tag:

                nearest_wire_obj = nearest_wire(
                    (x, y),
                    wires,
                )

            if is_component_tag:

                nearest_component_obj = nearest_component(
                    (x, y),
                    components,
                )

            if is_wire_tag and nearest_wire_obj is None:
                continue

            if is_component_tag and nearest_component_obj is None:
                continue

            replacement = ""

            if token == "#id":

                replacement = nearest_wire_obj.element_id

            elif token == "#node":

                row = wire_lookup.get(
                    nearest_wire_obj.element_id
                )

                if row:
                    replacement = row["net"]

            elif token == "#_node":


                row = wire_lookup.get(
                    nearest_wire_obj.element_id
                )

                if row:

                    replacement = row["net"]



                    if (
                        replacement
                        and replacement[0].lower() == "n"
                    ):
                        replacement = replacement[1:]

            
            elif token == "#wire":

                row = wire_lookup.get(
                    nearest_wire_obj.element_id
                )

                if row:

                    replacement = (
                        f'{row["from_component"]}.'
                        f'{row["from_pin"]}'
                        ' -> '
                        f'{row["to_component"]}.'
                        f'{row["to_pin"]}'
                    )
                    
            elif token == "@id":

                replacement = nearest_component_obj.ref
                
                
            elif token == "@title":

                group_el = find_element_by_id(
                    root,
                    nearest_component_obj.group_id,
                )

                if group_el is not None:

                    replacement = child_title(
                        group_el
                    )
                    
                
                
            elif token == "@label":

                group_el = find_element_by_id(
                    root,
                    nearest_component_obj.group_id,
                )

                if group_el is not None:

                    replacement = (
                        inkscape_label(group_el)
                        or ""
                    )


            if not replacement:
                continue

            # replace text
            for child in list(el):

                if local_name(child.tag) == "tspan":

                    child.text = replacement
                    
                    break

        
    def copy_labels_to_ids(
        self,
        root,
        parent,
        selected_layers=None,
    ):

        LABEL_ATTR = (
            "{http://www.inkscape.org/namespaces/inkscape}label"
        )

        GROUPMODE_ATTR = (
            "{http://www.inkscape.org/namespaces/inkscape}groupmode"
        )

        count = 0

        for el in root.iter():

            # Layer filter
            if (
                selected_layers
                and not element_in_selected_layers(
                    el,
                    parent,
                    selected_layers,
                )
            ):
                continue

            # Ignore text objects
            if local_name(el.tag) in {
                "text",
                "tspan",
            }:
                continue

            label = el.get(LABEL_ATTR)

            if not label:
                continue

            label = label.strip()

            if not label:
                continue

            # -------------------------------------------------
            # Find nearest labelled NON-LAYER parent
            # -------------------------------------------------

            parent_id_prefix = ""

            p = parent.get(el)

            while p is not None:

                # Skip layers completely
                if (
                    local_name(p.tag) == "g"
                    and p.get(GROUPMODE_ATTR) == "layer"
                ):
                    p = parent.get(p)
                    continue

                if local_name(p.tag) != "g":
                    p = parent.get(p)
                    continue

                parent_name = (
                    p.get(LABEL_ATTR)
                    or p.get("id")
                )

                if parent_name:

                    parent_name = parent_name.strip()

                    if parent_name:

                        parent_id_prefix = parent_name
                        break

                p = parent.get(p)

            # -------------------------------------------------
            # Build hierarchical ID
            # -------------------------------------------------

            if parent_id_prefix:

                # Don't generate R1.R1
                if label == parent_id_prefix:

                    new_id = label

                else:

                    new_id = f"{parent_id_prefix}.{label}"

            else:

                new_id = label

            # -------------------------------------------------
            # Don't rename if it's already correct
            # -------------------------------------------------

            old_id = el.get("id")

            if old_id == new_id:
                continue


            try:

                el.set("id", new_id)
                count += 1

            except ValueError:

                label = el.get(LABEL_ATTR) or ""

                if label:

                    new_label = f"{label}_{old_id}"

                    el.set(LABEL_ATTR, new_label)

                continue

        return count

    
        
    def find_text_by_label(self, label):

        label_attr = (
            "{http://www.inkscape.org/namespaces/inkscape}label"
        )

        for el in self.svg.iter():

            if isinstance(el, inkex.TextElement):

                if el.get(label_attr) == label:
                    return el

        return None


    def update_or_create_text_block(
        self,
        parent,
        x,
        y,
        text,
        label,
    ):

        txt = self.find_text_by_label(label)

        if txt is None:

            txt = inkex.TextElement()
            txt.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            txt.set(
                "{http://www.inkscape.org/namespaces/inkscape}label",
                label,
            )

            txt.set("x", str(x))
            txt.set("y", str(y))

            txt.style = {
                "font-size": "10px",
                "font-family": "monospace",
            }

            parent.add(txt)

        else:

            x = float(txt.get("x", x))
            y = float(txt.get("y", y))

            for child in list(txt):
                txt.remove(child)

        for i, line in enumerate(text.splitlines()):

            span = inkex.Tspan()
            span.set(
                "{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}role",
                "line"
            )

            span.set("x", str(x))
            span.set("y", str(y + i * 12))

            span.text = line

            txt.add(span)

        return txt
        


    def add_text_block(self, parent, x, y, text, title):

        group = inkex.Group()
        parent.add(group)

        title_text = inkex.TextElement()
        title_text.set("x", str(x))
        title_text.set("y", str(y))
        title_text.style = {
            "font-size": "14px",
            "font-family": "monospace",
            "font-weight": "bold",
        }
        title_text.text = title
        group.add(title_text)

        txt = inkex.TextElement()
        txt.set("x", str(x))
        txt.set("y", str(y + 20))
        txt.style = {
            "font-size": "10px",
            "font-family": "monospace",
        }

        for i, line in enumerate(text.splitlines()):
            span = inkex.Tspan()
            span.set(
                "{http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd}role",
                "line"
            )
            span.set("x", str(x))
            span.set("y", str(y + 35 + i * 12))
            span.text = line
            txt.add(span)

        group.add(txt)

    def effect(self):

        import tempfile
        from pathlib import Path
        import xml.etree.ElementTree as ET

        root = self.document.getroot()
        tree = ET.ElementTree(root)

        tmp_svg = Path(tempfile.mkstemp(suffix=".svg")[1])
        
        parent = build_parent_map(root)

        layer_set = {
            s.strip()
            for s in self.options.layers.split(",")
            if s.strip()
        }

        updated = self.copy_labels_to_ids(
            root,
            parent,
            layer_set if layer_set else None,
        )

        try:

            tree.write(tmp_svg)

            layer_set = {
                s.strip()
                for s in self.options.layers.split(",")
                if s.strip()
            }

            wires, components = collect_svg(
                tmp_svg,
                include_layers=layer_set if layer_set else None,
                component_source=self.options.component_source,
                component_regex=self.options.component_regex,
                wire_source=self.options.wire_source,
                wire_regex=self.options.wire_regex,
                pin_source=self.options.pin_source,
                pin_regex=self.options.pin_regex,
            )

            # ---------------------------------------------------------
            # Quick Connectivity Debug
            # ---------------------------------------------------------


            defaults = {
                "resistance": self.options.resistance,
                "inductance": self.options.inductance,
                "capacitance": self.options.capacitance,
                "diode_model": self.options.diode_model,
            }

            netlist_lines, net_warnings, external_node_alias = make_spice_netlist_external(
                wires=wires,
                components=components,
                tol=self.options.tol,
                defaults=defaults,
                build_wire_nets=build_wire_nets,
                pin_touching_nets=pin_touching_nets,
                split_explicit_owner=split_explicit_owner,
                first_netlist_line=first_netlist_line,
            )

            wire_rows, wire_warnings = make_wirelist(
                wires=wires,
                components=components,
                tol=self.options.tol,
            )

            # ---------------------------------------------------------
            # Apply propagated node aliases to wirelist
            # ---------------------------------------------------------

            for row in wire_rows:

                net = row.get("net", "")

                while net in external_node_alias:
                    new_net = external_node_alias[net]

                    if new_net == net:
                        break

                    net = new_net

                row["net"] = net

            self.update_text_placeholders(
                wires,
                wire_rows,
                components,
                root,
)

            svg_dir = self.svg_path()
            svg_name = self.svg.name

            if not svg_dir or not svg_name:
                inkex.errormsg(
                    "Please save the SVG before running this extension."
                )
                return

            svg_path = Path(svg_dir) / svg_name

            output_folder = svg_path.parent
            base = svg_path.stem

            out_path = output_folder / f"{base}.cir"
            wire_out_path = output_folder / f"{base}_wirelist.csv"

            # ---------------------------------------------------------
            # Write files only if NOT displaying on canvas
            # ---------------------------------------------------------

            if not self.options.show_netlist:

                with out_path.open("w", encoding="utf-8") as f:

                    f.write("* SVG extracted SPICE-like netlist\n")

                    for line in netlist_lines:
                        f.write(line + "\n")

                    f.write(".end\n")

                inkex.utils.debug(
                    f"Wrote netlist: {out_path}"
                )

            else:

                inkex.utils.debug(
                    "Netlist file skipped (displaying on canvas)"
                )


            if not self.options.show_wirelist:

                write_wirelist_csv(
                    wire_rows,
                    wire_out_path,
                )

                inkex.utils.debug(
                    f"Wrote wirelist: {wire_out_path}"
                )

            else:

                inkex.utils.debug(
                    "Wirelist file skipped (displaying on canvas)"
                )

            # ---------------------------------------------------------
            # Optional canvas output
            # ---------------------------------------------------------

            layer = self.svg.get_current_layer()

            if self.options.show_netlist:

                netlist_text = "* SVG extracted SPICE-like netlist\n"
                netlist_text += "\n".join(netlist_lines)
                netlist_text += "\n.end"

                self.update_or_create_text_block(
                    layer,
                    self.options.text_x,
                    self.options.text_y,
                    netlist_text,
                    "netlist_output",
                )

            if self.options.show_wirelist:

                wirelist_lines = [
                    "wire_id,from_component,from_pin,to_component,to_pin,net"
                ]

                for row in wire_rows:

                    wirelist_lines.append(
                        ",".join([
                            row["wire_id"],
                            row["from_component"],
                            row["from_pin"],
                            row["to_component"],
                            row["to_pin"],
                            row["net"],
                        ])
                    )

                wirelist_text = "\n".join(wirelist_lines)

                self.update_or_create_text_block(
                    layer,
                    self.options.text_x + 400,
                    self.options.text_y,
                    wirelist_text,
                    "wirelist_output",
                )

            for warning in net_warnings + wire_warnings:
                inkex.errormsg(
                    "Warning: " + warning
                )



        finally:

            try:
                tmp_svg.unlink(missing_ok=True)

            except Exception:
                pass


if __name__ == "__main__":
    SvgToSpice().run()
