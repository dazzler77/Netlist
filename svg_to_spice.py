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


def point_key(p: Point, tol: float) -> Tuple[int, int]:
    return (
        round(p[0] / tol),
        round(p[1] / tol),
    )


def point_on_segment(p: Point, a: Point, b: Point, tol: float) -> bool:
    ax, ay = a
    bx, by = b
    px, py = p

    dx = bx - ax
    dy = by - ay
    length2 = dx * dx + dy * dy

    if length2 <= tol * tol:
        return dist(p, a) <= tol

    t = ((px - ax) * dx + (py - ay) * dy) / length2

    if t < -tol or t > 1 + tol:
        return False

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

def group_component_type(el: ET.Element) -> Optional[str]:
    """
    A component is a group with Title or Inkscape label:

        resistor
        inductor
        capacitor
        diode

    Example:

        <g id="R1">
          <title>resistor</title>
          ...
        </g>
    """
    if local_name(el.tag) != "g":
        return None

    candidates = [
        child_title(el),
        inkscape_label(el),
    ]

    for text in candidates:
        key = text.strip().lower()

        if key in COMPONENT_ALIASES:
            return COMPONENT_ALIASES[key]

    return None


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


def build_wire_nets(
    wires: List[Wire],
    tol: float,
) -> Tuple[Dict[Tuple[int, int], str], DSU]:
    """
    Any wires/paths touching each other become the same net.

    This includes:
        - endpoint to endpoint
        - endpoint touching another segment
        - crossing/intersection of two wire segments
        - overlapping straight segments where endpoints lie on each other
    """
    segments = all_wire_segments(wires)
    dsu = DSU()
    points: List[Point] = []

    for _, s in segments:
        points.append(s.a)
        points.append(s.b)

    # Crossing intersections.
    for i, (_, s1) in enumerate(segments):
        for _, s2 in segments[i + 1:]:
            p = segment_intersection(s1.a, s1.b, s2.a, s2.b, tol)

            if p is not None:
                points.append(p)

    # T-junctions and endpoint-on-segment cases.
    for p in list(points):
        for _, s in segments:
            if point_on_segment(p, s.a, s.b, tol):
                points.append(p)

    for p in points:
        dsu.add(point_key(p, tol))

    for _, s in segments:
        ax, ay = s.a
        bx, by = s.b
        dx = bx - ax
        dy = by - ay
        length2 = dx * dx + dy * dy

        if length2 <= tol * tol:
            continue

        on_this: List[Tuple[float, Tuple[int, int]]] = []

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

        for (_, k1), (_, k2) in zip(on_this, on_this[1:]):
            dsu.union(k1, k2)

    roots = sorted({dsu.find(k) for k in dsu.parent})

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
    prefix = str(COMPONENT_TYPES[ctype]["prefix"])

    if ref.upper().startswith(prefix):
        return ref

    return f"{prefix}_{ref}"


def component_value(
    ctype: str,
    defaults: Dict[str, str],
) -> str:
    arg = str(COMPONENT_TYPES[ctype]["default_arg"])
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

    for comp in sorted(components, key=lambda c: c.ref):
        extra_pins = sorted(set(comp.pins) - {"1", "2"})

        if extra_pins:
            warnings.append(
                f"{comp.ref}: {comp.component_type} only supports pin 1 and pin 2. "
                f"Extra pins ignored: {extra_pins}"
            )

        pin1 = comp.pins.get("1")
        pin2 = comp.pins.get("2")

        if pin1 is None or pin2 is None:
            warnings.append(
                f"{comp.ref}: {comp.component_type} must have pin 1 and pin 2. "
                f"Found pins: {sorted(comp.pins)}"
            )
            continue

        pin1_nets = pin_touching_nets(pin1, wires, key_to_net, tol)
        pin2_nets = pin_touching_nets(pin2, wires, key_to_net, tol)

        if len(pin1_nets) != 1:
            warnings.append(
                f"{comp.ref}: pin 1 must touch exactly one net. "
                f"Found: {sorted(pin1_nets) or 'none'}"
            )
            continue

        if len(pin2_nets) != 1:
            warnings.append(
                f"{comp.ref}: pin 2 must touch exactly one net. "
                f"Found: {sorted(pin2_nets) or 'none'}"
            )
            continue

        net1 = next(iter(pin1_nets))
        net2 = next(iter(pin2_nets))

        if net1 == net2:
            warnings.append(
                f"{comp.ref}: pin 1 and pin 2 are connected to the same net {net1}"
            )

        value = component_value(comp.component_type, defaults)

        lines.append(
            f"{spice_ref(comp.ref, comp.component_type)} {net1} {net2} {value}"
        )

        if comp.component_type == "diode":
            used_diode_models.add(value)

    for model in sorted(used_diode_models):
        if model == defaults["diode_model"]:
            lines.append(f".model {model} D")

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

    A wire is listed only when:
        - the wire object has one start endpoint and one end endpoint
        - the start endpoint touches exactly one component pin
        - the end endpoint touches exactly one component pin

    This is separate from the netlist. The netlist joins all touching wires
    into nodes. The wirelist keeps direct physical wire objects.
    """
    rows: List[Dict[str, str]] = []
    warnings: List[str] = []

    key_to_net, _ = build_wire_nets(wires, tol)

    for wire in sorted(wires, key=lambda w: w.element_id):
        start_hits = endpoint_pins(wire.start, components, tol)
        end_hits = endpoint_pins(wire.end, components, tol)

        if not start_hits and not end_hits:
            continue

        if len(start_hits) != 1 or len(end_hits) != 1:
            warnings.append(
                f"wire {wire.element_id}: wirelist requires exactly one component pin "
                f"at each endpoint. "
                f"Start: {[p.component + '.' + p.pin_number for p in start_hits] or 'none'}, "
                f"End: {[p.component + '.' + p.pin_number for p in end_hits] or 'none'}"
            )
            continue

        a = start_hits[0]
        b = end_hits[0]

        if a.component == b.component and a.pin_number == b.pin_number:
            warnings.append(
                f"wire {wire.element_id}: both endpoints touch the same pin "
                f"{a.component}.{a.pin_number}; skipped"
            )
            continue

        net = ""

        if wire.segments:
            net = net_for_wire_segment(wire.segments[0], key_to_net, tol) or ""

        rows.append(
            {
                "wire_id": wire.element_id,
                "from_component": a.component,
                "from_pin": a.pin_number,
                "to_component": b.component,
                "to_pin": b.pin_number,
                "net": net,
            }
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

    def effect(self):
        import tempfile
        from pathlib import Path
        import xml.etree.ElementTree as ET

        # Current SVG document in memory
        root = self.document.getroot()
        tree = ET.ElementTree(root)

        # Save a temp SVG for your existing parser
        tmp_svg = Path(tempfile.mkstemp(suffix=".svg")[1])
        try:
            tree.write(tmp_svg)

            wires, components = collect_svg(tmp_svg)

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

            # IMPORTANT:
            # self.options.input_file -> temp file passed by Inkscape
            # self.svg_path() + self.svg.name -> original saved SVG location/name
            svg_dir = self.svg_path()
            svg_name = self.svg.name

            if not svg_dir or not svg_name:
                inkex.errormsg("Please save the SVG before running this extension.")
                return

            svg_path = Path(svg_dir) / svg_name
            output_folder = svg_path.parent
            base = svg_path.stem

            # Save beside the actual SVG being edited
            out_path = output_folder / f"{base}.cir"
            wire_out_path = output_folder / f"{base}_wirelist.csv"

            with out_path.open("w", encoding="utf-8") as f:
                f.write("* SVG extracted SPICE-like netlist\n")
                for line in netlist_lines:
                    f.write(line + "\n")
                f.write(".end\n")

            write_wirelist_csv(wire_rows, wire_out_path)

            inkex.utils.debug(f"Wrote netlist: {out_path}")
            inkex.utils.debug(f"Wrote wirelist: {wire_out_path}")

            for warning in net_warnings + wire_warnings:
                inkex.errormsg("Warning: " + warning)

        finally:
            # Tidy up temp file
            try:
                tmp_svg.unlink(missing_ok=True)
            except Exception:
                pass


if __name__ == "__main__":
    SvgToSpice().run()
