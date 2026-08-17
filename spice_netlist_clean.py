import re
from typing import Callable, Dict, List, Set, Tuple, Any

try:
    import inkex
except Exception:  # allow unit testing outside Inkscape
    class _Debug:
        @staticmethod
        def debug(msg):
            print(msg)
    class inkex:  # type: ignore
        utils = _Debug()


def make_spice_netlist(
    wires: List[Any],
    components: List[Any],
    tol: float,
    defaults: Dict[str, str],
    *,
    build_wire_nets: Callable,
    pin_touching_nets: Callable,
    split_explicit_owner: Callable,
    first_netlist_line: Callable,
) -> Tuple[List[str], List[str]]:
    """
    Standalone generic SPICE-like netlist generator.

    External dependencies are passed in as keyword-only functions to avoid
    circular imports from svg_to_spice.py.
    """

    inkex.utils.debug("ENTER make_spice_netlist")
    inkex.utils.debug("===== COLLECTED WIRES =====")
    for w in wires:
        inkex.utils.debug(
            f'id="{w.element_id}" title="{w.title}" owner="{getattr(w, "owner", "")}"'
        )

    key_to_net, _ = build_wire_nets(wires, tol)

    lines: List[str] = []
    warnings: List[str] = []

    def not_connected_node_for_pin_id(pin_id: str) -> str:
        return pin_id.replace(".", "_") + "_not_connected"

    def not_connected_node_for_missing_pin(comp_ref: str, pin_name: str) -> str:
        return f"{comp_ref}.{pin_name}".replace(".", "_") + "_not_connected"

    def split_mixed_netlist(text: str) -> Tuple[List[str], List[str]]:
        main_lines: List[str] = []
        subckt_lines: List[str] = []
        in_subckt = False

        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue

            if line.lower().startswith(".subckt"):
                in_subckt = True
                subckt_lines.append(line)
                continue

            if in_subckt:
                subckt_lines.append(line)
                if line.lower().startswith(".ends"):
                    in_subckt = False
                continue

            main_lines.append(line)

        return main_lines, subckt_lines

    def is_auto_net(name: str) -> bool:
        return re.fullmatch(r"N\d+", str(name or "")) is not None

    def merged_external_node_name(nodes: List[str]) -> str:
        unique = sorted({n for n in nodes if n}, key=lambda x: x.lower())
        if not unique:
            return ""

        explicit = [n for n in unique if not is_auto_net(n)]
        if explicit:
            return "_".join(explicit)

        return "_".join(unique)

    def choose_net(comp, pin_name: str, nets: Set[str]) -> str:
        if len(nets) == 1:
            return next(iter(nets))

        explicit = sorted([n for n in nets if not is_auto_net(n)], key=lambda x: x.lower())
        chosen = explicit[0] if explicit else sorted(nets)[0]
        warnings.append(
            f"{comp.ref}: pin {pin_name} touches multiple nets {sorted(nets)}; using {chosen}"
        )
        return chosen

    def raw_node_for_pin(comp, pin_name: str) -> str:
        pin = comp.pins.get(pin_name)

        if pin is None:
            lower = pin_name.lower()
            for key, value in comp.pins.items():
                if key.lower() == lower:
                    pin = value
                    pin_name = key
                    break

        if pin is None:
            node = not_connected_node_for_missing_pin(comp.ref, pin_name)
            warnings.append(f"{comp.ref}: missing pin {pin_name}; using synthetic node {node}")
            return node

        nets = pin_touching_nets(pin, wires, key_to_net, tol)
        inkex.utils.debug(f"{comp.ref}.{pin_name} nets={sorted(nets)}")

        if len(nets) == 0:
            node = not_connected_node_for_pin_id(pin.element_id)
            warnings.append(f"{comp.ref}: pin {pin_name} is not connected; using synthetic node {node}")
            return node

        return choose_net(comp, pin_name, nets)

    external_node_alias: Dict[str, str] = {}
        
    def resolve_alias(net: str) -> str:

        seen = set()

        while net in external_node_alias:

            if net in seen:
                break

            seen.add(net)

            new_net = external_node_alias[net]

            if new_net == net:
                break

            net = new_net

        return net

    def node_for_pin(comp, pin_name: str) -> str:

        raw = raw_node_for_pin(comp, pin_name)

        aliased = resolve_alias(raw)

        inkex.utils.debug(
            f"NODE ALIAS {comp.ref}.{pin_name}: "
            f"raw={raw} alias={aliased}"
        )

        return aliased

    def is_named_internal_wire(wire) -> bool:
        return bool((getattr(wire, "title", "") or "").strip().startswith("#"))

    def instance_subckt_name_from_line(line: str, subckt_names: Set[str]) -> str:
        tokens = line.split()
        if len(tokens) < 2:
            return ""
        if not tokens[0].lower().startswith("x"):
            return ""

        candidate = tokens[-1]
        for name in subckt_names:
            if candidate.lower() == name.lower():
                return name
        return ""

    def pin_groups_joined_by_internal_named_wires(comp, internal_wires: List[Any]) -> List[Set[str]]:
        named_wires = [w for w in internal_wires if is_named_internal_wire(w)]
        if not named_wires:
            return []

        key_to_internal_net, _ = build_wire_nets(named_wires, tol)
        net_to_pins: Dict[str, Set[str]] = {}

        for pin_name, pin in comp.pins.items():
            nets = pin_touching_nets(pin, named_wires, key_to_internal_net, tol)
            for net in nets:
                net_to_pins.setdefault(net, set()).add(pin_name)

        groups: List[Set[str]] = []
        for net, pin_names in sorted(net_to_pins.items()):
            if len(pin_names) < 2:
                continue
            inkex.utils.debug(
                f"INTERNAL PROPAGATION WIRE {comp.ref}: net={net} pins={sorted(pin_names)}"
            )
            groups.append(pin_names)

        return groups


    def pin_groups_joined_by_zero_ohm_resistors(comp) -> List[Set[str]]:
        """
        Find external pins connected together through chains of
        0-ohm resistors inside a .SUBCKT definition.

        Example:

            .SUBCKT BusBar pin1 pin2
            R1 pin1 short 0
            R2 pin2 short 0
            .ENDS

        returns:

            [{"pin1", "pin2"}]
        """

        _, subckt_lines = split_mixed_netlist(comp.netlist)

        graph: Dict[str, Set[str]] = {}

        def add_edge(a: str, b: str):
            graph.setdefault(a, set()).add(b)
            graph.setdefault(b, set()).add(a)

        for line in subckt_lines:

            tokens = line.split()

            if len(tokens) < 4:
                continue

            ref = tokens[0]

            if not ref.upper().startswith("R"):
                continue

            value = tokens[3].upper()

            if value not in {
                "0",
                "0.0",
                "0R",
                "0OHM",
            }:
                continue

            n1 = tokens[1]
            n2 = tokens[2]

            add_edge(n1, n2)

        if not graph:
            return []

        pin_names = set(comp.pins.keys())

        groups = []
        visited = set()

        for start in graph:

            if start in visited:
                continue

            stack = [start]
            component_nodes = set()

            while stack:
                node = stack.pop()

                if node in visited:
                    continue

                visited.add(node)
                component_nodes.add(node)

                stack.extend(graph.get(node, []))

            external_pins = component_nodes & pin_names

            if len(external_pins) >= 2:
                groups.append(external_pins)

        return groups



    def strip_owner_prefix_from_first_token(text: str, comp) -> str:
        owner, local = split_explicit_owner(text)
        if not owner or not local:
            return text

        first = first_netlist_line(text)
        if not first:
            return text

        return re.sub(
            rf"(?im)^(\s*){re.escape(owner)}\.{re.escape(local)}(\b)",
            rf"\1{local}\2",
            text,
            count=1,
        )

    def expand_component_netlist(comp, strip_owner_prefix: bool = True) -> List[str]:
        main_lines, _subckt_lines = split_mixed_netlist(comp.netlist)
        text = "\n".join(main_lines).strip()

        if not text:
            return []

        if strip_owner_prefix:
            text = strip_owner_prefix_from_first_token(text, comp)

        text = re.sub(r"@id", comp.ref, text, flags=re.I)
        text = re.sub(r"@title", comp.title, text, flags=re.I)
        text = re.sub(r"@label", comp.label, text, flags=re.I)
        text = re.sub(r"@description", comp.description, text, flags=re.I)

        pin_by_lower = {name.lower(): name for name in comp.pins}
        inkex.utils.debug(f"{comp.ref} pins={list(comp.pins.keys())}")

        def replace_dollar(match):
            token = match.group(1)
            key = pin_by_lower.get(token.lower())
            inkex.utils.debug(f"{comp.ref} token={token} key={key}")
            if key is None:
                return match.group(0)
            return node_for_pin(comp, key)

        text = re.sub(r"\$([A-Za-z_][A-Za-z0-9_\-]*)\b", replace_dollar, text)

        out_lines: List[str] = []
        for line in text.splitlines():
            if not line.strip():
                continue

            raw_tokens = line.split()
            new_tokens: List[str] = []
            for token in raw_tokens:
                key = pin_by_lower.get(token.lower())
                if key is not None:
                    new_tokens.append(node_for_pin(comp, key))
                else:
                    new_tokens.append(token)
                    
            out_line = " ".join(new_tokens)

            inkex.utils.debug(
                f"NETLIST EXPAND {comp.ref}: {out_line}"
            )

            out_lines.append(out_line)

        return out_lines

    # Find all subckt names from raw .SUBCKT blocks.
    subckt_names: Set[str] = set()
    for comp in components:
        _main_lines, subckt_lines = split_mixed_netlist(comp.netlist)
        for line in subckt_lines:
            m = re.match(r"^\.subckt\s+(\S+)", line, flags=re.I)
            if m:
                subckt_names.add(m.group(1))

    # Build external node aliases from internal named wires.
    for comp in sorted(components, key=lambda c: c.order):
        if getattr(comp, "is_subckt", False):
            continue

        main_lines, _subckt_lines = split_mixed_netlist(comp.netlist)
        if not main_lines:
            continue

        for line in main_lines:
            subckt_name = instance_subckt_name_from_line(line, subckt_names)
            if not subckt_name:
                continue

            internal_wires = [
                w for w in wires
                if getattr(w, "owner", "") in {comp.group_id, comp.ref}
            ]

            for w in wires:
                if w.element_id == "wire1":
                    inkex.utils.debug(
                        f"WIRE1 DEBUG: "
                        f"id={w.element_id} "
                        f"owner={getattr(w,'owner',None)} "
                        f"title={w.title}"
                    )
            if comp.ref == "BB-105":
                inkex.utils.debug(
                    f"BB105 group_id={comp.group_id} ref={comp.ref}"
                )


            inkex.utils.debug(
                f"PROP TEST {comp.ref}: subckt={subckt_name} "
                f"internal_wires={[w.element_id for w in internal_wires]}"
            )


            pin_groups = []

            pin_groups.extend(
                pin_groups_joined_by_internal_named_wires(
                    comp,
                    internal_wires
                )
            )

            pin_groups.extend(
                pin_groups_joined_by_zero_ohm_resistors(
                    comp
                )
            )

            inkex.utils.debug(
                f"ZERO OHM PROP GROUPS {comp.ref}: {pin_groups}"
            )



            inkex.utils.debug(f"PROP GROUPS {comp.ref}: {pin_groups}")

            for pin_group in pin_groups:
                inkex.utils.debug(
                    f"PROP CHECK {comp.ref}: "
                    f"pin_group={pin_group} "
                    f"comp.pins={list(comp.pins.keys())}"
                )
                raw_nodes: List[str] = []
                for pin_name in sorted(pin_group):
                    if pin_name not in comp.pins:
                        continue
                    raw_nodes.append(
                        resolve_alias(
                            raw_node_for_pin(comp, pin_name)
                        )
                    )
                inkex.utils.debug(
                    f"PROP RAW NODES {comp.ref}: {raw_nodes}"
                )
                merged = merged_external_node_name(raw_nodes)
                if not merged:
                    continue

                for raw in raw_nodes:

                    raw = resolve_alias(raw)

                    external_node_alias[raw] = merged

                inkex.utils.debug(
                    f"INSTANCE NODE PROPAGATION {comp.ref}: subckt={subckt_name} "
                    f"pins={sorted(pin_group)} raw_nodes={raw_nodes} merged={merged}"
                )
    inkex.utils.debug("===== ALIAS TABLE =====")

    for k, v in sorted(external_node_alias.items()):
        inkex.utils.debug(f"{k} -> {v}")
        
    # Emit main circuit lines first.
    for comp in sorted(components, key=lambda c: c.order):
        if getattr(comp, "is_subckt", False):
            continue

        main_lines, _subckt_lines = split_mixed_netlist(comp.netlist)
        if not main_lines:
            if not comp.netlist:
                warnings.append(f"{comp.ref}: no netlist text; skipped")
            continue

        lines.extend(expand_component_netlist(comp, strip_owner_prefix=True))

    # Emit raw .SUBCKT definitions after main circuit lines.
    emitted_subckt_blocks: Set[str] = set()
    for comp in sorted(components, key=lambda c: c.order):
        _main_lines, subckt_lines = split_mixed_netlist(comp.netlist)
        if not subckt_lines:
            continue

        block_text = "\n".join(subckt_lines)
        if block_text in emitted_subckt_blocks:
            continue

        emitted_subckt_blocks.add(block_text)
        lines.extend(subckt_lines)

    inkex.utils.debug("EXIT make_spice_netlist")
    return lines, warnings, external_node_alias
