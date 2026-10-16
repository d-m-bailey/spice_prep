# spice_prep

`spice_prep` prepares SPICE netlists for Netgen LVS by resolving
parameterized subcircuits. It creates specialized subcircuit definitions for
the resolved parameter values, allowing equivalent instances to share a
definition and leaving the resulting netlist ready for LVS.
