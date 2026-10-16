# spice_prep specification

## Purpose

`spice_prep` transforms a SPICE netlist into a format suitable for Netgen LVS.
One of its responsibilities is resolving parameters in parameterized
subcircuits.

## Parameterized subcircuit specialization

For a subcircuit that declares parameters and is instantiated with parameter
overrides:

- Resolve references to those parameters in the subcircuit body using the
  values supplied by the instantiation.
- Substitute resolved parameter values inside device-value expressions,
  including quoted expressions, and evaluate those expressions to numeric
  values. Subcircuit defaults provide the values unless an instance overrides
  them; instance overrides take precedence throughout that specialized
  subcircuit body.
- Treat every token in a subcircuit declaration or a defined subcircuit
  instance that has the form `name=value` as a parameter, regardless of the
  parameter name.
- Emit a specialized subcircuit definition whose body contains the resolved
  values and whose interface does not declare the resolved parameters.
- Rewrite the instance to refer to the specialized subcircuit and omit its
  parameter overrides.
- Preserve the instance name and its node connections.
- Preserve input continuation lines in the output; remove parameter tokens
  without flattening or otherwise rewriting the continuation-line layout.

An `X` instance whose target name has no subcircuit definition is a device, not
a subcircuit instance. Leave it unchanged and do not resolve its parameter-like
tokens.

Parameter resolution may span multiple subcircuit hierarchy levels. Resolve
parameter values in the context of the complete hierarchy, propagating values
through parent and child instantiations as needed before specializing a
subcircuit. Start traversal from subcircuits that are not instantiated by
another defined subcircuit, so hierarchies contained entirely within
subcircuit definitions are processed even when there are no instances outside
those definitions.

Instantiations that resolve to the same subcircuit definition and the same
parameter values may share one specialized subcircuit definition. Instantiations
with different resolved parameter values must refer to distinct specialized
definitions. The naming scheme is implementation-defined; numeric suffixes are
one acceptable option. Generated names must not collide with other subcircuit
names.

When a parameterized subcircuit has exactly one distinct resolved
specialization, use the original subcircuit name for its specialized definition,
even if multiple instances share it.

For example, if `amp_dpc`'s default `rl` value is `4k`, the expression
`L='max((rl-526)/470.9,0.5)'` in its body becomes
`L=7.37736249734551`. An instance override such as `rl=8k` instead produces a
specialization whose expression evaluates to `L=15.8717349755787`.

Numeric expressions support SPICE SI suffixes `a`, `f`, `p`, `n`, `u`, `m`,
`k`, `Meg`, `g`, and `t`, plus the `max()` and `sqrt()` functions used in the
provided netlist.

## Example

Input:

```spice
.subckt example A B R=17
R0 A B R
.ends

X0 IN OUT example R=20
X1 IN OUT example R=30
```

One valid output is:

```spice
.subckt example_1 A B
R0 A B 20
.ends
.subckt example_2 A B
R0 A B 30
.ends
X0 IN OUT example_1
X1 IN OUT example_2
```

The generated suffixes need not be numeric. If both instances instead supply
`R=20`, they may both refer to the same specialized definition.

## Hierarchical resolution test case

This case verifies that a parameter is propagated through an intermediate
subcircuit and that instances with the same resolved values can share a
specialized definition.

Before:

```spice
.subckt leaf A B R=1
R0 A B R
.ends
.subckt wrapper A B P=2
XLEAF A B leaf R={P}
.ends

X0 IN OUT wrapper P=20
X1 IN2 OUT2 wrapper P=20
```

After:

```spice
.subckt leaf A B
R0 A B 20
.ends
.subckt wrapper A B
XLEAF A B leaf
.ends

X0 IN OUT wrapper
X1 IN2 OUT2 wrapper
```

Parameter values can refer to values in the calling subcircuit, so resolution
must follow instance overrides through nested hierarchy. Basic arithmetic
expressions in parameter values are evaluated when their operands resolve.
An override whose name is not declared by the target subcircuit is an error.

## Program and tests

Run the transformer with an input file and output path:

```sh
python3 spice_prep.py input.sp -o output.sp
```

With no input path, the program reads standard input and writes the transformed
netlist to standard output.

Run the regression tests with:

```sh
python3 -m unittest -v test_spice_prep
```

## Specification maintenance

Keep this document aligned with the implemented netlist transformation
behavior. Update it whenever parameterized-subcircuit specialization behavior
or its requirements change.
