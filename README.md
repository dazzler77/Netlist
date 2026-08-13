# SVG to SPICE Netlist Inkscape Extension

This extension extracts electrical connectivity from an SVG schematic and generates:

- SPICE-like netlist (`.cir`)
- Wirelist (`_wirelist.csv`)
- On-sheet net labels (`#node`, `#wire`, etc.)
- Hierarchical subcircuits (`.SUBCKT`)
- Connector and busbar node propagation

---

# Basic Workflow

## 1. Draw the schematic

Create:

- Components (groups)
- Pins
- Wires

The extension determines connectivity from geometry.

Pins must physically touch wires within the configured tolerance.

---

## 2. Components

A component is normally an SVG group containing:

- Component graphics
- Pin objects
- Optional title/description
- Component values are stored in the netlist definition in the description.

Example:

```spice
Netlist=R1 $Pin1 $Pin2 10k
```

Supported placeholders:

| Placeholder | Meaning |
|------------|----------|
| `$Pin1` | node connected to Pin1 |
| `$Pin2` | node connected to Pin2 |
| `@id` | component reference |
| `@title` | SVG title |
| `@label` | Inkscape label |
| `@description` | SVG description |

Example:

```spice
@id $Pin1 $Pin2 10k
```

becomes:

```spice
R1 28102 28117 10k
```

---

# Pins

Pins are identified using the configured pin regex.

Typical naming:

```text
pin1
pin2
pinA
pinB
LD1
LD2
```

Pin names are preserved exactly and may be referenced by:

```spice
$pin1
$LD1
$A
```

---

# Wires

Wires are detected using the configured wire regex.

Example:

```text
wire1
wire2
wire20
```

or

```text
BB-105.wire1
```

for internal wires within a component.

---

# On-Sheet Text Tags

Text objects can contain special titles.

## Wire Tags

| Tag | Result |
|------|---------|
| `#id` | wire identifier |
| `#node` | node name |
| `#_node` | node name without leading N |
| `#wire` | connection summary |

Example:

```text
#node
```

might display:

```text
12345
```

---

## Component Tags

| Tag | Result |
|------|---------|
| `@id` | component reference |
| `@title` | SVG title |
| `@label` | Inkscape label |

---

# Subcircuits

Subcircuits can be embedded directly in the component description.

Example:

```spice
XBusbar1 pin1 pin2 BusBar

.SUBCKT BusBar pin1 pin2
R1 pin1 short 0
R2 pin2 short 0
.ENDS BusBar
```

The first line becomes the component instance.

The `.SUBCKT` block is emitted only once.

---

# Busbars and Connectors

Node propagation can occur inside a subcircuit.

This allows connectors, terminal blocks, busbars and links to pass node names through the device.

## Method 1 - Internal Wire

Inside the component SVG:

```text
#12345 wire
```

connecting:

```text
pin1 ---- wire ---- pin2
```

The node is propagated between pins.

---

## Method 2 - 0 Ohm Resistors, Recommended

Example:

```spice
.SUBCKT BusBar pin1 pin2

R1 pin1 short 0
R2 pin2 short 0

.ENDS BusBar
```

The extension recognises the zero-ohm path and treats:

```text
pin1
pin2
```

as electrically joined for node propagation.

Result:

```spice
XBusbar1 12345 12345 BusBar
```

while preserving the internal subcircuit:

```spice
.SUBCKT BusBar pin1 pin2
R1 pin1 short 0
R2 pin2 short 0
.ENDS BusBar
```

Internal node names are not altered.

---

# Connectivity Rules

A connection exists when:

- wire endpoint touches pin
- wire overlaps pin geometry
- wire segments intersect
- connectivity is within the configured tolerance

If a pin is not connected, a synthetic node is generated:

```text
Component_Pin1_not_connected
```

or

```text
N001
```

depending on context.

---

# Outputs

## Netlist

Example:

```spice
* SVG extracted SPICE-like netlist

V1 12345 54321 DC 2V

XBusbar1 12345 12345 BusBar

.SUBCKT BusBar pin1 pin2
R1 pin1 short 0
R2 pin2 short 0
.ENDS BusBar

.end
```

---

## Wirelist

Example:

```csv
wire_id,from_component,from_pin,to_component,to_pin,net
wire20,V1,Pin1,BusB,pin1,28102
```

The wirelist reflects propagated node aliases, so displayed `#node` text, CSV output and generated netlist all use the same final node names.

---

# Tips

- Move wire ends close to pins if connectivity is not detected.
- Use `$PinName` placeholders instead of hard-coded node names.
- Use 0 ohm subcircuits for connectors and busbars.
- Use `#node` labels to quickly verify connectivity on the schematic.
- Search the generated netlist for `Not_Connected` or `Nxxx` nets when debugging connectivity issues.
