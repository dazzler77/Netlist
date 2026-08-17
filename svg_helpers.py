#!/usr/bin/env python3
from __future__ import annotations
import xml.etree.ElementTree as ET
import inkex
from typing import Dict, Iterable, List, Optional, Set, Tuple
import csv
import math
import re
import sys
# ---------------------------------------------------------------------
# SVG helpers
# ---------------------------------------------------------------------

Point = Tuple[float, float]
Matrix = Tuple[float, float, float, float, float, float]

NUMBER = r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?"
PATH_TOKEN_RE = re.compile(rf"[MmLlHhVvZz]|{NUMBER}")
TRANSFORM_RE = re.compile(r"(matrix|translate|scale|rotate)\s*\(([^)]*)\)")

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
    
def get_title(el):
    """
    Return text from the first child <title> element.
    """

    for child in el:

        if local_name(child.tag) == "title":

            return (child.text or "").strip()

    return ""

def get_description(el):
    """
    Return text from the first child <desc> element.
    """

    for child in el:

        if local_name(child.tag) == "desc":

            return (child.text or "").strip()

    return ""

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

def get_match_value(el, source):
    if source == "id":
        return element_name(el)

    if source == "label":
        return get_label(el)

    if source == "title":
        return get_title(el)

    if source == "description":
        return get_description(el)

    return ""

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
 
def pin_relative_path(
    pin_el: ET.Element,
    component_group: ET.Element,
    parent,
):
    parts = []

    cur = pin_el

    while cur is not None and cur is not component_group:

        name = element_name(cur)

        if name:
            parts.append(name)

        cur = parent.get(cur)

    parts.reverse()

    return ".".join(parts)

 
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


