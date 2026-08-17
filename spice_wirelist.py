import re, inkex
from typing import Callable, Dict, List, Set, Tuple, Any
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from svg_to_spice import Wire, Component
from netlist_common import build_wire_nets,  pin_touching_nets, endpoint_pins, net_for_wire_segment
from svg_helpers import point_key, point_on_segment, point_to_segment_distance, dist




def make_wirelist(
    wires: List["Wire"],
    components: List["Component"],
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


        if wire.element_id == "wire281111111":

            inkex.utils.debug(
                f"WIRELIST DEBUG {wire.element_id}: "
                f"start_hits="
                f"{[p.component + '.' + str(p.pin_number) for p in start_hits]} "
                f"end_hits="
                f"{[p.component + '.' + str(p.pin_number) for p in end_hits]}"
            )
                    
        if wire.element_id == "wire281111111":

            inkex.utils.debug(
                f"WIRE START = {wire.start}"
            )

            inkex.utils.debug(
                f"WIRE END = {wire.end}"
            )

            for comp in components:

                for pin_name, pin in comp.pins.items():

                    d1 = dist(wire.start, pin.a)

                    d2 = dist(wire.end, pin.a)

                    if d1 < 50 or d2 < 50:

                        inkex.utils.debug(
                            f"NEARBY PIN "
                            f"{comp.ref}.{pin_name} "
                            f"pin={pin.a} "
                            f"d_start={d1:.2f} "
                            f"d_end={d2:.2f}"
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
