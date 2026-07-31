#!/usr/bin/env python3
# coding: utf-8
"""
Falstad / CircuitJS text import extension for Inkscape.
Version 3:
- Auto origin placement based on the minimum Falstad coordinate in pasted text.
- Manual X and Y offsets can be negative.
- Uses lxml.etree for SVG desc metadata.
- Uses SVG paths for linework for better Inkscape compatibility.
"""

import math
import re
import html
import xml.etree.ElementTree as ET
from lxml import etree
from datetime import datetime

import inkex
from inkex import Group, PathElement, TextElement, Circle, Rectangle
from inkex.paths import Path

TAG_NAMES = {
    "w": "wire", "r": "resistor", "c": "capacitor", "l": "inductor", "g": "ground",
    "R": "voltage source", "v": "voltage source", "i": "current source",
    "d": "diode", "D": "diode", "z": "zener", "s": "switch", "S": "switch",
    "aout": "audio out", "o": "scope",
}


def _num(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def points_from_x_attr(elem):
    raw = elem.attrib.get("x", "")
    vals = [_num(v) for v in raw.replace(",", " ").split()]
    if len(vals) >= 4:
        return vals[0], vals[1], vals[2], vals[3]
    return None


def circuit_bounds(root):
    xs, ys = [], []
    for elem in list(root):
        pts = points_from_x_attr(elem)
        if pts is None:
            continue
        x1, y1, x2, y2 = pts
        xs.extend([x1, x2])
        ys.extend([y1, y2])
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def clean_falstad_text(text):
    if not text:
        return ""
    text = html.unescape(text).strip()
    text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    m = re.search(r"<cir\b[\s\S]*?</cir>", text, flags=re.IGNORECASE)
    if m:
        text = m.group(0)
    return text.strip()


def parse_circuit(text):
    text = clean_falstad_text(text)
    if not text:
        raise inkex.AbortExtension("Paste Falstad/CircuitJS text into the text box first.")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        try:
            root = ET.fromstring("<cir>" + text + "</cir>")
        except ET.ParseError:
            raise inkex.AbortExtension(f"Could not parse Falstad text as XML: {e}")
    if root.tag.lower() != "cir":
        cir = root.find(".//cir")
        if cir is None:
            raise inkex.AbortExtension("No <cir>...</cir> block found in the pasted text.")
        root = cir
    return root, text


def svg_path(d, style, parent):
    p = PathElement()
    p.path = Path(d)
    p.style = style
    parent.append(p)
    return p


def add_line(parent, x1, y1, x2, y2, style):
    return svg_path(f"M {x1:.6g},{y1:.6g} L {x2:.6g},{y2:.6g}", style, parent)


def add_text(parent, text, x, y, size, style):
    t = TextElement(x=str(x), y=str(y))
    t.text = str(text)
    t.style = inkex.Style(dict(style))
    t.style["font-size"] = str(size)
    parent.append(t)
    return t


def unit_geometry(x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-9:
        length = 1.0
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    return length, ux, uy, px, py


def pt(x, y, ux, uy, px, py, along, off):
    return x + ux * along + px * off, y + uy * along + py * off


def path_poly(points):
    if not points:
        return ""
    return "M " + " L ".join(f"{x:.6g},{y:.6g}" for x, y in points)


def add_circle(parent, cx, cy, r, style):
    c = Circle(cx=str(cx), cy=str(cy), r=str(r))
    c.style = style
    parent.append(c)
    return c


def add_rect(parent, x, y, w, h, style):
    r = Rectangle(x=str(x), y=str(y), width=str(w), height=str(h))
    r.style = style
    parent.append(r)
    return r


def label_for(elem):
    tag = elem.tag
    if tag == "r" and "r" in elem.attrib:
        return elem.attrib["r"] + " ohm"
    if tag == "c" and "c" in elem.attrib:
        return elem.attrib["c"] + " F"
    if tag == "l" and "l" in elem.attrib:
        return elem.attrib["l"] + " H"
    if tag == "R":
        bits = []
        if "maxv" in elem.attrib:
            bits.append(elem.attrib["maxv"] + " V")
        if "fr" in elem.attrib:
            bits.append(elem.attrib["fr"] + " Hz")
        return "Source " + ", ".join(bits) if bits else "Voltage source"
    for k in ("v", "f", "fr", "maxv", "name", "label"):
        if k in elem.attrib:
            return elem.attrib[k]
    return TAG_NAMES.get(tag, tag)


def draw_resistor(parent, x1, y1, x2, y2, style):
    L, ux, uy, px, py = unit_geometry(x1, y1, x2, y2)
    lead = min(18, L * 0.22)
    zig_len = max(1.0, L - 2 * lead)
    pts = [(x1, y1), pt(x1, y1, ux, uy, px, py, lead, 0)]
    for i in range(1, 8):
        pts.append(pt(x1, y1, ux, uy, px, py, lead + zig_len * i / 8, 10 if i % 2 else -10))
    pts += [pt(x1, y1, ux, uy, px, py, L - lead, 0), (x2, y2)]
    svg_path(path_poly(pts), style, parent)


def draw_capacitor(parent, x1, y1, x2, y2, style):
    L, ux, uy, px, py = unit_geometry(x1, y1, x2, y2)
    mid, gap, plate = L / 2, 5, 18
    a, b = mid - gap, mid + gap
    add_line(parent, x1, y1, *pt(x1, y1, ux, uy, px, py, a, 0), style)
    add_line(parent, *pt(x1, y1, ux, uy, px, py, b, 0), x2, y2, style)
    add_line(parent, *pt(x1, y1, ux, uy, px, py, a, -plate), *pt(x1, y1, ux, uy, px, py, a, plate), style)
    add_line(parent, *pt(x1, y1, ux, uy, px, py, b, -plate), *pt(x1, y1, ux, uy, px, py, b, plate), style)


def draw_inductor(parent, x1, y1, x2, y2, style):
    L, ux, uy, px, py = unit_geometry(x1, y1, x2, y2)
    lead = min(16, L * 0.2)
    add_line(parent, x1, y1, *pt(x1, y1, ux, uy, px, py, lead, 0), style)
    add_line(parent, *pt(x1, y1, ux, uy, px, py, L - lead, 0), x2, y2, style)
    span = max(1, L - 2 * lead)
    turns = max(3, min(6, int(span / 14)))
    step = span / turns
    start = pt(x1, y1, ux, uy, px, py, lead, 0)
    d = [f"M {start[0]:.6g},{start[1]:.6g}"]
    pos = lead
    for _ in range(turns):
        p1 = pt(x1, y1, ux, uy, px, py, pos + step * 0.25, -8)
        p2 = pt(x1, y1, ux, uy, px, py, pos + step * 0.75, -8)
        p3 = pt(x1, y1, ux, uy, px, py, pos + step, 0)
        d.append(f"C {p1[0]:.6g},{p1[1]:.6g} {p2[0]:.6g},{p2[1]:.6g} {p3[0]:.6g},{p3[1]:.6g}")
        pos += step
    svg_path(" ".join(d), style, parent)


def draw_ground(parent, x1, y1, x2, y2, style):
    L, ux, uy, px, py = unit_geometry(x1, y1, x2, y2)
    add_line(parent, x1, y1, x2, y2, style)
    for i, w in enumerate([22, 14, 7]):
        cx, cy = pt(x2, y2, ux, uy, px, py, 6 + i * 6, 0)
        add_line(parent, cx + px*w/2, cy + py*w/2, cx - px*w/2, cy - py*w/2, style)


def draw_source(parent, x1, y1, x2, y2, style, text_style):
    L, ux, uy, px, py = unit_geometry(x1, y1, x2, y2)
    r = min(18, max(10, L * 0.22))
    mid = pt(x1, y1, ux, uy, px, py, L / 2, 0)
    add_line(parent, x1, y1, *pt(x1, y1, ux, uy, px, py, L/2-r, 0), style)
    add_line(parent, *pt(x1, y1, ux, uy, px, py, L/2+r, 0), x2, y2, style)
    add_circle(parent, mid[0], mid[1], r, style)
    add_text(parent, "+", mid[0]-4, mid[1]-3, 9, text_style)
    add_text(parent, "-", mid[0]+3, mid[1]+8, 9, text_style)


def draw_diode(parent, x1, y1, x2, y2, style):
    L, ux, uy, px, py = unit_geometry(x1, y1, x2, y2)
    lead = min(18, L * 0.25)
    add_line(parent, x1, y1, *pt(x1, y1, ux, uy, px, py, lead, 0), style)
    add_line(parent, *pt(x1, y1, ux, uy, px, py, L-lead, 0), x2, y2, style)
    a = pt(x1, y1, ux, uy, px, py, lead, -12)
    b = pt(x1, y1, ux, uy, px, py, lead, 12)
    c = pt(x1, y1, ux, uy, px, py, L-lead, 0)
    svg_path(f"M {a[0]:.6g},{a[1]:.6g} L {b[0]:.6g},{b[1]:.6g} L {c[0]:.6g},{c[1]:.6g} Z", style, parent)
    add_line(parent, *pt(x1, y1, ux, uy, px, py, L-lead, -13), *pt(x1, y1, ux, uy, px, py, L-lead, 13), style)


def draw_switch(parent, x1, y1, x2, y2, style):
    L, ux, uy, px, py = unit_geometry(x1, y1, x2, y2)
    a = pt(x1, y1, ux, uy, px, py, L * 0.38, 0)
    b = pt(x1, y1, ux, uy, px, py, L * 0.62, 0)
    add_line(parent, x1, y1, *a, style)
    add_line(parent, *b, x2, y2, style)
    add_circle(parent, a[0], a[1], 2.5, style)
    add_circle(parent, b[0], b[1], 2.5, style)
    add_line(parent, *a, *pt(x1, y1, ux, uy, px, py, L * 0.60, -14), style)


def draw_generic(parent, x1, y1, x2, y2, style):
    L, ux, uy, px, py = unit_geometry(x1, y1, x2, y2)
    lead = min(14, L * 0.2)
    add_line(parent, x1, y1, *pt(x1, y1, ux, uy, px, py, lead, 0), style)
    add_line(parent, *pt(x1, y1, ux, uy, px, py, L-lead, 0), x2, y2, style)
    mx, my = pt(x1, y1, ux, uy, px, py, L / 2, 0)
    add_rect(parent, mx - 24, my - 12, 48, 24, style)


def draw_component(parent, elem, style, text_style, show_labels=True, fallback=True):
    p = points_from_x_attr(elem)
    if p is None:
        return False
    x1, y1, x2, y2 = p
    tag = elem.tag
    if tag == "w":
        add_line(parent, x1, y1, x2, y2, style)
    elif tag == "r":
        draw_resistor(parent, x1, y1, x2, y2, style)
    elif tag == "c":
        draw_capacitor(parent, x1, y1, x2, y2, style)
    elif tag == "l":
        draw_inductor(parent, x1, y1, x2, y2, style)
    elif tag == "g":
        draw_ground(parent, x1, y1, x2, y2, style)
    elif tag in ("R", "v", "i"):
        draw_source(parent, x1, y1, x2, y2, style, text_style)
    elif tag in ("d", "D", "z"):
        draw_diode(parent, x1, y1, x2, y2, style)
    elif tag in ("s", "S"):
        draw_switch(parent, x1, y1, x2, y2, style)
    elif fallback:
        draw_generic(parent, x1, y1, x2, y2, style)
    else:
        return False

    if show_labels and tag != "w":
        L, ux, uy, px, py = unit_geometry(x1, y1, x2, y2)
        mx, my = pt(x1, y1, ux, uy, px, py, L / 2, -18)
        add_text(parent, label_for(elem), mx, my, 10, text_style)
    return True


class FalstadImporter(inkex.EffectExtension):
    def add_arguments(self, pars):
        pars.add_argument("--falstad_text", default="")
        pars.add_argument("--scale", type=float, default=1.0)
        pars.add_argument("--auto_origin", type=inkex.Boolean, default=True)
        pars.add_argument("--margin", type=float, default=20.0)
        pars.add_argument("--x_offset", type=float, default=0.0)
        pars.add_argument("--y_offset", type=float, default=0.0)
        pars.add_argument("--stroke_width", type=float, default=2.0)
        pars.add_argument("--show_labels", type=inkex.Boolean, default=True)
        pars.add_argument("--fallback_symbols", type=inkex.Boolean, default=True)
        pars.add_argument("--layer_name", default="Falstad import")

    def effect(self):
        root, original = parse_circuit(self.options.falstad_text)
        bounds = circuit_bounds(root)
        if bounds is None:
            raise inkex.AbortExtension("Parsed the circuit, but no drawable x=\"x1 y1 x2 y2\" coordinates were found.")
        min_x, min_y, max_x, max_y = bounds

        scale = max(0.01, float(self.options.scale))
        manual_x = float(self.options.x_offset)
        manual_y = float(self.options.y_offset)
        margin = float(self.options.margin)

        if bool(self.options.auto_origin):
            # Move the minimum Falstad coordinate to the drawing margin, then apply manual offsets.
            # Example: if min_x=432 and min_y=272 and margin=20, translate=(-412,-252).
            ox = margin - min_x + manual_x
            oy = margin - min_y + manual_y
        else:
            # Manual mode. Negative offsets are allowed because .inx does not clamp these fields.
            ox = manual_x
            oy = manual_y

        style = inkex.Style({
            "stroke": "#000000", "stroke-width": str(self.options.stroke_width),
            "fill": "none", "stroke-linecap": "round", "stroke-linejoin": "round",
        })
        text_style = inkex.Style({
            "font-family": "Arial, Helvetica, sans-serif", "font-size": "10px",
            "fill": "#000000", "stroke": "none",
        })

        layer = Group()
        layer.set(inkex.addNS("groupmode", "inkscape"), "layer")
        layer.set(inkex.addNS("label", "inkscape"), self.options.layer_name or "Falstad import")
        layer.set("transform", f"translate({ox:.6g},{oy:.6g}) scale({scale:.6g})")
        layer.set("data-falstad-min-x", str(min_x))
        layer.set("data-falstad-min-y", str(min_y))
        layer.set("data-falstad-max-x", str(max_x))
        layer.set("data-falstad-max-y", str(max_y))
        layer.set("data-falstad-translate-x", str(ox))
        layer.set("data-falstad-translate-y", str(oy))
        self.svg.get_current_layer().append(layer)

        desc = etree.Element("desc")
        desc.text = (
            "Imported from Falstad/CircuitJS text on " + datetime.now().isoformat(timespec="seconds") + "\n"
            + f"Falstad bounds: min=({min_x},{min_y}) max=({max_x},{max_y}) translate=({ox},{oy}) scale={scale}\n"
            + original
        )
        layer.append(desc)

        drawn = 0
        for elem in list(root):
            if elem.tag.lower() == "o" or points_from_x_attr(elem) is None:
                continue
            g = Group()
            g.set("id", self.svg.get_unique_id("falstad_" + re.sub(r"[^A-Za-z0-9_-]", "_", elem.tag)))
            g.set("data-falstad-tag", elem.tag)
            for k, v in elem.attrib.items():
                g.set("data-falstad-" + re.sub(r"[^A-Za-z0-9_-]", "_", k), str(v))
            layer.append(g)
            if draw_component(g, elem, style, text_style, bool(self.options.show_labels), bool(self.options.fallback_symbols)):
                drawn += 1

        if drawn == 0:
            raise inkex.AbortExtension("Parsed the circuit, but did not find drawable elements with x=\"x1 y1 x2 y2\" coordinates.")


if __name__ == "__main__":
    FalstadImporter().run()
