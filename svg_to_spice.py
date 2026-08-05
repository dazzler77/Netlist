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
    "#title",
    "#label",
    "#description",

    "@id",
    "@wire",
    "@node",
    "@title",
    "@label",
    "@description",
}

# ---------------------------------------------------------------------
# SVG helpers
# ---------------------------------------------------------------------

def local_name(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def child_title(el: ET.Element) -> str:
    for child in list(el):
        if local_name(child.tag) == "title" and child.text:
            return child.text.strip()
    return ""

def find_element_by_id(root, element_id):

    for el in root.iter():

        if el.get("id") == element_id:
            return el

    return None


def inkscape_label(el: ET.Element) -> str:
    return el.attrib.get(
        "{http://www.inkscape.org/namespaces/inkscape}label",
        "",
    ).strip()


def element_name(el: ET.Element) -> str:
    return (
        el.attrib.get("id")
        or inkscape_label(el)
        or local_name(el.tag)
    )


def parse_numbers(s: str) -> List[float]:
    return [float(x) for x in re.findall(NUMBER, s)]


def build_parent_map(root: ET.Element) -> Dict[ET.Element, ET.Element]:
    return {
        child: parent
        for parent in root.iter()
        for child in list(parent)
    }

SVG_DEFINITION_TAGS = {
    "defs",
    "clipPath",
    "mask",
    "marker",
    "pattern",
    "symbol",
    "filter",
    "linearGradient",
    "radialGradient",
}


def is_inside_definition(
    el: ET.Element,
    parent_map: Dict[ET.Element, ET.Element],
) -> bool:

    cur = el

    while cur is not None:

        tag = local_name(cur.tag)

        if tag in SVG_DEFINITION_TAGS:

            inkex.utils.debug(
                f"Ignoring definition object: "
                f"id={element_name(el)} "
                f"tag={local_name(el.tag)} "
                f"inside {tag}"
            )

            return True

        cur = parent_map.get(cur)

    return False


def descendants(el: ET.Element) -> Iterable[ET.Element]:
    for child in list(el):
        yield child
        yield from descendants(child)

def is_layer(el: ET.Element) -> bool:
    return (
        local_name(el.tag) == "g"
        and el.attrib.get("{http://www.inkscape.org/namespaces/inkscape}groupmode") == "layer"
    )


def layer_name(el: ET.Element) -> str:
    return inkscape_label(el) or el.attrib.get("id", "")


def element_in_selected_layers(
    el: ET.Element,
    parent_map: Dict[ET.Element, ET.Element],
    selected_layers: Set[str],
) -> bool:
    """
    Walks up the tree to find the containing Inkscape layer.
    """
    cur = el

    while cur is not None:
        if is_layer(cur):
            name = layer_name(cur)
            return name in selected_layers
        cur = parent_map.get(cur)

    # No layer = include (safe fallback)
    return True
    
def wire_distance(wire, p):

    best = float("inf")

    for seg in wire.segments:

        best = min(
            best,
            point_to_segment_distance(
                p,
                seg.a,
                seg.b
            )
        )

    return best


def nearest_wire(p, wires):

    best_wire = None
    best_dist = float("inf")

    for wire in wires:

        d = wire_distance(wire, p)

        if d < best_dist:
            best_dist = d
            best_wire = wire

    return best_wire
    
    
def component_distance(comp, p):

    best = float("inf")

    for pin in comp.pins.values():

        best = min(best, dist(p, pin.a))
        best = min(best, dist(p, pin.b))

    return best


def nearest_component(p, components):

    best = None
    best_dist = float("inf")

    for comp in components:

        d = component_distance(comp, p)

        if d < best_dist:
            best_dist = d
            best = comp

    return best
  

# ---------------------------------------------------------------------
# Transform handling
# ---------------------------------------------------------------------

def mat_identity() -> Matrix:
    return (1, 0, 0, 1, 0, 0)


def mat_mul(m1: Matrix, m2: Matrix) -> Matrix:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2

    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def mat_apply(m: Matrix, p: Point) -> Point:
    a, b, c, d, e, f = m
    x, y = p

    return (
        a * x + c * y + e,
        b * x + d * y + f,
    )


def parse_transform(transform: str) -> Matrix:
    m = mat_identity()

    for name, raw_args in TRANSFORM_RE.findall(transform or ""):
        vals = parse_numbers(raw_args)

        if name == "matrix" and len(vals) == 6:
            t = tuple(vals)  # type: ignore[assignment]

        elif name == "translate":
            tx = vals[0] if vals else 0.0
            ty = vals[1] if len(vals) > 1 else 0.0
            t = (1, 0, 0, 1, tx, ty)

        elif name == "scale":
            sx = vals[0] if vals else 1.0
            sy = vals[1] if len(vals) > 1 else sx
            t = (sx, 0, 0, sy, 0, 0)

        elif name == "rotate" and vals:
            angle = math.radians(vals[0])
            c = math.cos(angle)
            s = math.sin(angle)
            r = (c, s, -s, c, 0, 0)

            if len(vals) >= 3:
                cx, cy = vals[1], vals[2]
                t = mat_mul(
                    mat_mul((1, 0, 0, 1, cx, cy), r),
                    (1, 0, 0, 1, -cx, -cy),
                )
            else:
                t = r

        else:
            continue

        m = mat_mul(m, t)

    return m


def element_world_matrix(
    el: ET.Element,
    parent: Dict[ET.Element, ET.Element],
) -> Matrix:
    chain: List[ET.Element] = []
    cur: Optional[ET.Element] = el

    while cur is not None:
        chain.append(cur)
        cur = parent.get(cur)

    m = mat_identity()

    for item in reversed(chain):
        m = mat_mul(m, parse_transform(item.attrib.get("transform", "")))

    return m


# ---------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------

def dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])

def point_to_segment_distance(
    p,
    a,
    b,
):

    ax, ay = a
    bx, by = b
    px, py = p

    dx = bx - ax
    dy = by - ay

    length2 = dx * dx + dy * dy

    if length2 == 0:
        return dist(p, a)

    t = (
        ((px - ax) * dx + (py - ay) * dy)
        / length2
    )

    t = max(0.0, min(1.0, t))

    closest = (
        ax + t * dx,
        ay + t * dy,
    )

    return dist(p, closest)

def point_key(p: Point, tol: float) -> Tuple[int, int]:
    return (
        round(p[0] / tol),
        round(p[1] / tol),
    )


def point_on_segment(p, a, b, tol):

    ax, ay = a
    bx, by = b
    px, py = p

    dx = bx - ax
    dy = by - ay

    length2 = dx * dx + dy * dy

    if length2 <= tol * tol:
        return dist(p, a) <= tol

    t = ((px - ax) * dx + (py - ay) * dy) / length2

    # Before start of segment:
    # only accept if actually close to endpoint a.
    if t < 0.0:
        return dist(p, a) <= tol

    # After end of segment:
    # only accept if actually close to endpoint b.
    if t > 1.0:
        return dist(p, b) <= tol

    closest = (
        ax + t * dx,
        ay + t * dy,
    )

    return dist(p, closest) <= tol


def segment_intersection(
    a: Point,
    b: Point,
    c: Point,
    d: Point,
    tol: float,
) -> Optional[Point]:
    ax, ay = a
    bx, by = b
    cx, cy = c
    dx, dy = d

    r = (bx - ax, by - ay)
    s = (dx - cx, dy - cy)

    den = r[0] * s[1] - r[1] * s[0]

    if abs(den) <= tol:
        return None

    qmp = (cx - ax, cy - ay)

    t = (qmp[0] * s[1] - qmp[1] * s[0]) / den
    u = (qmp[0] * r[1] - qmp[1] * r[0]) / den

    if -tol <= t <= 1 + tol and -tol <= u <= 1 + tol:
        return (
            ax + t * r[0],
            ay + t * r[1],
        )

    return None


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
# Union find
# ---------------------------------------------------------------------

class DSU:
    def __init__(self) -> None:
        self.parent: Dict[Tuple[int, int], Tuple[int, int]] = {}

    def add(self, x: Tuple[int, int]) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: Tuple[int, int]) -> Tuple[int, int]:
        self.add(x)

        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])

        return self.parent[x]

    def union(self, a: Tuple[int, int], b: Tuple[int, int]) -> None:
        ra = self.find(a)
        rb = self.find(b)

        if ra != rb:
            self.parent[rb] = ra


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


@dataclass
class Pin:
    component: str
    pin_number: str
    element_id: str
    a: Point
    b: Point


@dataclass
class Component:
    ref: str
    component_type: str
    group_id: str
    pins: Dict[str, Pin]


# ---------------------------------------------------------------------
# Component and pin detection
# ---------------------------------------------------------------------

def infer_component_type(ref):

    ref = ref.upper()

    for prefix in sorted(
        COMPONENT_PREFIXES,
        key=len,
        reverse=True,
    ):
        if ref.startswith(prefix):
            return COMPONENT_PREFIXES[prefix]

    return None


def group_component_type(el: ET.Element) -> Optional[str]:

    if local_name(el.tag) != "g":
        return None

    if is_layer(el):
        return None

    pin_count = 0

    for child in list(el):

        if get_pin_number(child) is not None:
            pin_count += 1

    if pin_count < 2:
        return None

    ctype = infer_component_type(
        element_name(el)
    )

    return ctype or "generic"

def get_pin_number(el: ET.Element) -> Optional[str]:
    """
    Detects pin objects.

    Accepts:
        <title>pin 1</title>
        inkscape:label="pin 1"
        id="pin_1"
        id="R1_pin_1"
    """
    candidates = [
        child_title(el),
        inkscape_label(el),
        el.attrib.get("id", ""),
    ]

    for text in candidates:
        m = re.search(
            r"(?:^|[_\-\s])pin[_\-\s:#]*(\w+)",
            text,
            flags=re.I,
        )

        if m:
            return m.group(1)

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
    svg_path: Path,
    include_layers: Optional[Set[str]] = None,
) -> Tuple[List[Wire], List[Component]]:

    tree = ET.parse(svg_path)
    root = tree.getroot()
    parent = build_parent_map(root)

    selected_layers = include_layers if include_layers else None

    component_groups: List[Tuple[ET.Element, str]] = []
    element_to_component_group: Dict[ET.Element, ET.Element] = {}

    # -------------------------------------------------------------
    # COMPONENT COLLECTION (layer filtered)
    # -------------------------------------------------------------
    for el in root.iter():

        if is_inside_definition(el, parent):
            continue

        # LAYER FILTER
        if selected_layers and not element_in_selected_layers(
            el,
            parent,
            selected_layers,
        ):
            continue

        ctype = group_component_type(el)

        if ctype is None:
            continue

        component_groups.append((el, ctype))

        for d in descendants(el):
            element_to_component_group[d] = el

    components: List[Component] = []

    for group, ctype in component_groups:
        ref = element_name(group)
        pins: Dict[str, Pin] = {}

        for el in descendants(group):
            pin_number = get_pin_number(el)

            if pin_number is None:
                continue

            pts = element_endpoints(
                el,
                element_world_matrix(el, parent),
            )

            if pts is None:
                continue

            if pin_number in pins:
                print(
                    f"Warning: {ref} has duplicate pin {pin_number}",
                    file=sys.stderr,
                )

            pins[pin_number] = Pin(
                component=ref,
                pin_number=pin_number,
                element_id=element_name(el),
                a=pts[0],
                b=pts[1],
            )

        components.append(
            Component(
                ref=ref,
                component_type=ctype,
                group_id=element_name(group),
                pins=pins,
            )
        )

    wires: List[Wire] = []

    # -------------------------------------------------------------
    # WIRE COLLECTION (layer filtered)
    # -------------------------------------------------------------
    for el in root.iter():

        if is_inside_definition(el, parent):
            continue

        tag = local_name(el.tag)

        if tag not in {"path", "line"}:
            continue


        # LAYER FILTER
        if selected_layers and not element_in_selected_layers(
            el,
            parent,
            selected_layers,
        ):
            continue

        # Anything inside a component group is part of the component,
        # not an external wire.
        if el in element_to_component_group:

            owner = element_to_component_group[el]

            inkex.utils.debug(
                f"Skipping as component geometry: "
                f"{element_name(el)} "
                f"owned by {element_name(owner)}"
            )

            continue

        # A standalone titled pin is not a wire.
        if get_pin_number(el) is not None:
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

        wires.append(
            Wire(
                element_id=element_name(el),
                start=segs[0].a,
                end=segs[-1].b,
                segments=segs,
            )
        )

    return wires, components

# ---------------------------------------------------------------------
# Net solving
# ---------------------------------------------------------------------

def all_wire_segments(wires: List[Wire]) -> List[Tuple[str, Segment]]:
    out: List[Tuple[str, Segment]] = []

    for w in wires:
        for s in w.segments:
            out.append((w.element_id, s))

    return out


def build_wire_nets(wires, tol):
    """
    Build electrical nets from wire geometry.

    Connects:
        - endpoint to endpoint
        - endpoint to segment
        - segment chains within the same path

    Does NOT connect:
        - plain middle-to-middle visual crossings
    """

    segments = all_wire_segments(wires)
    dsu = DSU()

    # Only real segment endpoints are allowed to become junction candidates.
    points = []

    for wire_id, s in segments:
        points.append(s.a)
        points.append(s.b)

    # Add every endpoint to the union-find.
    for p in points:
        dsu.add(point_key(p, tol))

    # For each segment, find all existing endpoints that lie on it.
    # Then union them along that segment.
    for wire_id, s in segments:

        ax, ay = s.a
        bx, by = s.b

        dx = bx - ax
        dy = by - ay

        length2 = dx * dx + dy * dy

        if length2 <= tol * tol:
            continue

        on_this = []

        for p in points:

            if point_on_segment(p, s.a, s.b, tol):

                t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2

                on_this.append(
                    (
                        max(0.0, min(1.0, t)),
                        point_key(p, tol),
                    )
                )

        on_this.sort(key=lambda item: item[0])

        # Remove duplicate endpoint keys.
        unique_on_this = []

        last_key = None

        for t, key in on_this:

            if key != last_key:
                unique_on_this.append((t, key))
                last_key = key

        # If two or more known endpoints lie on this segment,
        # they are electrically connected by this segment.
        for item1, item2 in zip(unique_on_this, unique_on_this[1:]):

            k1 = item1[1]
            k2 = item2[1]

            inkex.utils.debug(
                f"CONNECT endpoint-on-segment: "
                f"{wire_id} {k1} <-> {k2}"
            )

            dsu.union(k1, k2)

    roots = sorted(
        {dsu.find(k) for k in dsu.parent}
    )

    root_to_net = {
        root: f"N{idx + 1:03d}"
        for idx, root in enumerate(roots)
    }

    key_to_net = {
        k: root_to_net[dsu.find(k)]
        for k in dsu.parent
    }

    return key_to_net, dsu


def net_for_wire_segment(
    s: Segment,
    key_to_net: Dict[Tuple[int, int], str],
    tol: float,
) -> Optional[str]:
    return (
        key_to_net.get(point_key(s.a, tol))
        or key_to_net.get(point_key(s.b, tol))
    )


def pin_touching_nets(
    pin: Pin,
    wires: List[Wire],
    key_to_net: Dict[Tuple[int, int], str],
    tol: float,
) -> Set[str]:
    """
    A pin connects if either end of the pin touches a wire/path.

    For a 2-pin component, each pin should touch exactly one net.
    """
    nets: Set[str] = set()

    for endpoint in (pin.a, pin.b):
        for _, s in all_wire_segments(wires):
            if point_on_segment(endpoint, s.a, s.b, tol):
                net = net_for_wire_segment(s, key_to_net, tol)

                if net is not None:
                    nets.add(net)

    return nets


def spice_ref(ref: str, ctype: str) -> str:

    info = COMPONENT_TYPES.get(ctype)

    if info is None:

        inkex.utils.debug(
            f"Unknown component type: {ctype}"
        )

        return ref

    prefix = str(info["prefix"])

    if ref.upper().startswith(prefix):
        return ref

    return f"{prefix}_{ref}"


def component_value(
    ctype: str,
    defaults: Dict[str, str],
) -> str:

    info = COMPONENT_TYPES.get(ctype)

    if info is None:

        inkex.utils.debug(
            f"Unknown component type: {ctype}"
        )

        return ""

    arg = str(info["default_arg"])

    return defaults[arg]


def make_spice_netlist(
    wires: List[Wire],
    components: List[Component],
    tol: float,
    defaults: Dict[str, str],
) -> Tuple[List[str], List[str]]:

    key_to_net, _ = build_wire_nets(wires, tol)

    lines: List[str] = []
    warnings: List[str] = []
    used_diode_models: Set[str] = set()

    def not_connected_node_for_pin_id(pin_id: str) -> str:
        return (
            pin_id.replace(".", "_")
            + "_not_connected"
        )

    def not_connected_node_for_missing_pin(
        comp_ref: str,
        pin_num: str,
    ) -> str:
        return (
            f"{comp_ref}.pin{pin_num}".replace(".", "_")
            + "_not_connected"
        )

    def node_for_pin(
        comp: Component,
        pin_num: str,
    ) -> str:
        """
        Return the SPICE node for a component pin.

        Rules:
            - if the pin touches exactly one net, use that net
            - if the pin touches no nets, create a synthetic not_connected node
            - if the pin touches multiple nets, warn and choose one deterministically
            - if the pin does not exist, create a synthetic missing-pin node
        """

        pin = comp.pins.get(pin_num)

        if pin is None:
            node = not_connected_node_for_missing_pin(
                comp.ref,
                pin_num,
            )

            warnings.append(
                f"{comp.ref}: missing pin {pin_num}; "
                f"using synthetic node {node}"
            )

            return node

        nets = pin_touching_nets(
            pin,
            wires,
            key_to_net,
            tol,
        )

        if len(nets) == 1:
            return next(iter(nets))

        if len(nets) == 0:
            node = not_connected_node_for_pin_id(
                pin.element_id
            )

            warnings.append(
                f"{comp.ref}: pin {pin_num} is not connected; "
                f"using synthetic node {node}"
            )

            return node

        # More than one net touching the same pin.
        # Still emit the component, but make the choice deterministic.
        chosen = sorted(nets)[0]

        warnings.append(
            f"{comp.ref}: pin {pin_num} touches multiple nets "
            f"{sorted(nets)}; using {chosen}"
        )

        return chosen

    for comp in sorted(
        components,
        key=lambda c: c.ref,
    ):

        if comp.component_type == "generic":

            warnings.append(
                f"{comp.ref}: generic component skipped"
            )

            continue

        # -----------------------------------------------------
        # Include every non-generic component that has at least
        # one pin. Do not skip just because one pin is floating.
        # -----------------------------------------------------

        if not comp.pins:

            warnings.append(
                f"{comp.ref}: {comp.component_type} has no pins; skipped"
            )

            continue

        extra_pins = sorted(
            set(comp.pins) - {"1", "2"}
        )

        if extra_pins:

            warnings.append(
                f"{comp.ref}: {comp.component_type} only supports pin 1 and pin 2. "
                f"Extra pins ignored: {extra_pins}"
            )

        # -----------------------------------------------------
        # Two-terminal SPICE output.
        #
        # If pin 1 or pin 2 is missing or unconnected, use a
        # synthetic not_connected node instead of skipping.
        # -----------------------------------------------------

        net1 = node_for_pin(
            comp,
            "1",
        )

        net2 = node_for_pin(
            comp,
            "2",
        )

        if net1 == net2:

            warnings.append(
                f"{comp.ref}: pin 1 and pin 2 are connected to the same net {net1}"
            )

        value = component_value(
            comp.component_type,
            defaults,
        )

        lines.append(
            f"{spice_ref(comp.ref, comp.component_type)} {net1} {net2} {value}"
        )

        if comp.component_type == "diode":
            used_diode_models.add(value)

    for model in sorted(used_diode_models):

        if model == defaults["diode_model"]:

            lines.append(
                f".model {model} D"
            )

    return lines, warnings


# ---------------------------------------------------------------------
# Wirelist generation
# ---------------------------------------------------------------------


def endpoint_pins(
    point: Point,
    components: List[Component],
    tol: float,
) -> List[Pin]:
    hits: List[Pin] = []

    for comp in components:
        for pin in comp.pins.values():
            if dist(point, pin.a) <= tol or dist(point, pin.b) <= tol:
                hits.append(pin)

    return hits


def make_wirelist(
    wires: List[Wire],
    components: List[Component],
    tol: float,
) -> Tuple[List[Dict[str, str]], List[str]]:
    """
    Point-to-point wire list.

    Rules:
        - Normal wire:
              component <-> component

        - Dangling wire:
              component <-> open circuit

        - Floating component pins:
              warning only
              no wirelist row

    """

    import re

    rows: List[Dict[str, str]] = []
    warnings: List[str] = []

    key_to_net, _ = build_wire_nets(wires, tol)

    connected_pins = set()

    # ---------------------------------------------------------
    # Determine next available net number
    # ---------------------------------------------------------

    max_net_num = 0

    for net_name in key_to_net.values():

        if not isinstance(net_name, str):
            continue

        m = re.fullmatch(r"N(\d+)", net_name)

        if m:
            max_net_num = max(
                max_net_num,
                int(m.group(1)),
            )

    next_net_num = max_net_num + 1

    fallback_wire_nets = {}

    def get_wire_net(wire):

        nonlocal next_net_num

        net = ""

        if wire.segments:

            net = (
                net_for_wire_segment(
                    wire.segments[0],
                    key_to_net,
                    tol,
                )
                or ""
            )

        if net:
            return net

        if wire.element_id not in fallback_wire_nets:

            fallback_wire_nets[wire.element_id] = (
                f"N{next_net_num:03d}"
            )

            next_net_num += 1

        return fallback_wire_nets[wire.element_id]

    # ---------------------------------------------------------
    # Process each wire
    # ---------------------------------------------------------

    for wire in sorted(
        wires,
        key=lambda w: w.element_id,
    ):

        start_hits = endpoint_pins(
            wire.start,
            components,
            tol,
        )

        end_hits = endpoint_pins(
            wire.end,
            components,
            tol,
        )

        # -----------------------------------------------------
        # Any pin touched by a wire endpoint is considered
        # connected, even if the wire is dangling.
        # -----------------------------------------------------

        for p in start_hits:

            connected_pins.add(
                (
                    p.component,
                    str(p.pin_number),
                )
            )

        for p in end_hits:

            connected_pins.add(
                (
                    p.component,
                    str(p.pin_number),
                )
            )

        if not start_hits and not end_hits:
            continue

        net = get_wire_net(wire)

        # -----------------------------------------------------
        # Normal connection:
        # one component pin at each end
        # -----------------------------------------------------

        if (
            len(start_hits) == 1
            and len(end_hits) == 1
        ):

            a = start_hits[0]
            b = end_hits[0]

            if (
                a.component == b.component
                and a.pin_number == b.pin_number
            ):

                warnings.append(
                    f"wire {wire.element_id}: "
                    f"both endpoints touch "
                    f"{a.component}.{a.pin_number}"
                )

                continue

            rows.append(
                {
                    "wire_id": wire.element_id,
                    "from_component": a.component,
                    "from_pin": str(a.pin_number),
                    "to_component": b.component,
                    "to_pin": str(b.pin_number),
                    "net": net,
                }
            )

            continue

        # -----------------------------------------------------
        # Dangling wire:
        # component at start only
        # -----------------------------------------------------

        if (
            len(start_hits) == 1
            and len(end_hits) == 0
        ):

            a = start_hits[0]

            rows.append(
                {
                    "wire_id": wire.element_id,
                    "from_component": a.component,
                    "from_pin": str(a.pin_number),
                    "to_component": "not_connected",
                    "to_pin": "not_connected",
                    "net": net,
                }
            )

            warnings.append(
                f"wire {wire.element_id}: "
                f"dangling wire connected to "
                f"{a.component}.{a.pin_number}"
            )

            continue

        # -----------------------------------------------------
        # Dangling wire:
        # component at end only
        # -----------------------------------------------------

        if (
            len(start_hits) == 0
            and len(end_hits) == 1
        ):

            b = end_hits[0]

            rows.append(
                {
                    "wire_id": wire.element_id,
                    "from_component": b.component,
                    "from_pin": str(b.pin_number),
                    "to_component": "not_connected",
                    "to_pin": "not_connected",
                    "net": net,
                }
            )

            warnings.append(
                f"wire {wire.element_id}: "
                f"dangling wire connected to "
                f"{b.component}.{b.pin_number}"
            )

            continue

        # -----------------------------------------------------
        # Ambiguous connection
        # -----------------------------------------------------

        warnings.append(
            f"wire {wire.element_id}: "
            f"wirelist requires zero or one component pin "
            f"at each endpoint. "
            f"Start: "
            f"{[p.component + '.' + str(p.pin_number) for p in start_hits] or 'none'}, "
            f"End: "
            f"{[p.component + '.' + str(p.pin_number) for p in end_hits] or 'none'}"
        )

    # ---------------------------------------------------------
    # Floating pins:
    # warning only
    # ---------------------------------------------------------

    for comp in components:

        for pin_num, pin in comp.pins.items():

            key = (
                comp.ref,
                str(pin_num),
            )

            if key in connected_pins:
                continue

            warnings.append(
                f"{comp.ref}.{pin_num} is not connected"
            )

    return rows, warnings

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
        "--resistance",
        default="1k",
        help="Default resistor value",
    )

    ap.add_argument(
        "--inductance",
        default="1m",
        help="Default inductor value",
    )

    ap.add_argument(
        "--capacitance",
        default="1u",
        help="Default capacitor value",
    )

    ap.add_argument(
        "--diode-model",
        default="Ddefault",
        help="Default diode model name",
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
        args.svg,
        include_layers=layer_set if layer_set else None,
    )

    inkex.errormsg(f"Wires found: {len(wires)}")
    inkex.errormsg(f"Components found: {len(components)}")
    
    for c in components:
        inkex.errormsg(
            f"type={c.type} ref={getattr(c,'ref','?')} pins={len(c.pins)}"
        )

    defaults = {
        "resistance": args.resistance,
        "inductance": args.inductance,
        "capacitance": args.capacitance,
        "diode_model": args.diode_model,
    }

    netlist_lines, net_warnings = make_spice_netlist(
        wires=wires,
        components=components,
        tol=args.tol,
        defaults=defaults,
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
            
            inkex.utils.debug(
                f'text={el.get("id")} token="{token}"'
            )

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
                    
                    inkex.utils.debug(
                        f'component={nearest_component_obj.ref} '
                        f'title="{replacement}"'
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

                    inkex.utils.debug(
                        f'component={nearest_component_obj.ref} '
                        f'label="{replacement}"'
                    )
                
                

            inkex.utils.debug(
                f"token={token} "
                f'nearest_wire="{nearest_wire_obj.element_id if nearest_wire_obj else None}" '
                f'nearest_component="{nearest_component_obj.ref if nearest_component_obj else None}" '
                f"replacement={replacement!r}"
            )

            if not replacement:
                continue

            # replace text
            for child in list(el):

                if local_name(child.tag) == "tspan":

                    child.text = replacement
                    
                    inkex.utils.debug(
                        f'tspan updated to "{child.text}"'
                    )

                    break

            inkex.utils.debug(
                f"Updated {token} -> {replacement}"
            )
 
        
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

            if "." not in new_id and label.lower().startswith("pin"):

                inkex.utils.debug(
                    f"PIN WITHOUT PARENT: "
                    f"label={label} "
                    f"old_id={old_id}"
                )

            inkex.utils.debug(
                f'RENAME old="{old_id}" new="{new_id}"'
            )
            try:

                el.set("id", new_id)
                count += 1

            except ValueError:

                label = el.get(LABEL_ATTR) or ""

                if label:

                    new_label = f"{label}_{old_id}"

                    el.set(LABEL_ATTR, new_label)

                    inkex.utils.debug(
                        f'Duplicate ref "{new_id}" '
                        f'changed label to "{new_label}"'
                    )

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

        inkex.utils.debug(
            f"Copied {updated} labels to ids"
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
            )

            # ---------------------------------------------------------
            # Quick Connectivity Debug
            # ---------------------------------------------------------

            inkex.utils.debug("===== COMPONENT PINS =====")

            for c in components:

                for pin_num, pin in c.pins.items():

                    inkex.utils.debug(
                        f"{c.ref}.{pin_num} "
                        f"a={pin.a} "
                        f"b={pin.b}"
                    )

            inkex.utils.debug("===== WIRES =====")

            for w in wires:

                inkex.utils.debug(
                    f"{w.element_id} "
                    f"start={w.start} "
                    f"end={w.end}"
                )

            defaults = {
                "resistance": self.options.resistance,
                "inductance": self.options.inductance,
                "capacitance": self.options.capacitance,
                "diode_model": self.options.diode_model,
            }

            netlist_lines, net_warnings = make_spice_netlist(
                wires=wires,
                components=components,
                tol=self.options.tol,
                defaults=defaults,
            )

            wire_rows, wire_warnings = make_wirelist(
                wires=wires,
                components=components,
                tol=self.options.tol,
            )
            
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

            # ---------------------------------------------------------
            # Detailed Debug Output
            # ---------------------------------------------------------

            inkex.utils.debug(
                "========== SVG TO SPICE DEBUG =========="
            )

            inkex.utils.debug(
                f"Components detected: {len(components)}"
            )

            for c in components:

                pin_list = sorted(c.pins.keys())

                inkex.utils.debug(
                    f"Component: "
                    f"ref={c.ref} "
                    f"type={c.component_type} "
                    f"group={c.group_id} "
                    f"pins={pin_list}"
                )

                for pin_num, pin in c.pins.items():

                    inkex.utils.debug(
                        f"    Pin {pin_num}: "
                        f"element={pin.element_id} "
                        f"a={pin.a} "
                        f"b={pin.b}"
                    )

            inkex.utils.debug(
                f"Wires detected: {len(wires)}"
            )

            for i, w in enumerate(wires, start=1):

                inkex.utils.debug(
                    f"Wire {i}: "
                    f"id={w.element_id} "
                    f"start={w.start} "
                    f"end={w.end} "
                    f"segments={len(w.segments)}"
                )

            inkex.utils.debug(
                f"Netlist lines generated: {len(netlist_lines)}"
            )

            for line in netlist_lines:

                inkex.utils.debug(
                    f"NET: {line}"
                )

            inkex.utils.debug(
                f"Wirelist rows generated: {len(wire_rows)}"
            )

            inkex.utils.debug(
                "======================================="
            )

        finally:

            try:
                tmp_svg.unlink(missing_ok=True)

            except Exception:
                pass


if __name__ == "__main__":
    SvgToSpice().run()
