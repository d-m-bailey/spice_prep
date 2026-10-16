import unittest

from spice_prep import transform_netlist


class TransformNetlistTests(unittest.TestCase):
    def test_distinct_parameter_values_create_distinct_subcircuits(self) -> None:
        source = """.subckt example A B R=17
R0 A B R
.ends

X0 IN OUT example R=20
X1 IN OUT example R=30
"""
        self.assertEqual(
            transform_netlist(source),
            """.subckt example_1 A B
R0 A B 20
.ends
.subckt example_2 A B
R0 A B 30
.ends

X0 IN OUT example_1
X1 IN OUT example_2
""",
        )

    def test_identical_parameters_share_one_subcircuit(self) -> None:
        source = """.subckt example A B R=17
R0 A B R
.ends
X0 IN OUT example R=20
X1 IN OUT example R=20
"""
        self.assertEqual(
            transform_netlist(source),
            """.subckt example A B
R0 A B 20
.ends
X0 IN OUT example
X1 IN OUT example
""",
        )

    def test_single_instantiation_keeps_original_name(self) -> None:
        source = """.subckt example A B R=17
R0 A B R
.ends
X0 IN OUT example R=20
"""
        result = transform_netlist(source)
        self.assertIn(".subckt example A B\nR0 A B 20", result)
        self.assertIn("X0 IN OUT example\n", result)
        self.assertNotIn("R=20", result)

    def test_parameters_resolve_through_multiple_hierarchy_levels(self) -> None:
        source = """.subckt leaf A B R=1
R0 A B R
.ends
.subckt middle A B P=2
XLEAF A B leaf R={P}
.ends
.subckt top A B VALUE=3
XMID A B middle P={VALUE}
.ends
X0 IN OUT top VALUE=20
X1 IN OUT top VALUE=30
"""
        result = transform_netlist(source)
        self.assertIn(".subckt leaf_1 A B\nR0 A B 20\n.ends", result)
        self.assertIn(".subckt leaf_2 A B\nR0 A B 30\n.ends", result)
        self.assertIn("XLEAF A B leaf_1", result)
        self.assertIn("XLEAF A B leaf_2", result)
        self.assertIn("XMID A B middle_1", result)
        self.assertIn("XMID A B middle_2", result)
        self.assertIn("X0 IN OUT top_1", result)
        self.assertIn("X1 IN OUT top_2", result)

    def test_arithmetic_parameter_expressions_are_evaluated(self) -> None:
        source = """.subckt example A B R=1
R0 A B R
.ends
X0 IN OUT example R={10 + 5}
"""
        result = transform_netlist(source)
        self.assertIn("R0 A B 15", result)

    def test_parameter_defaults_can_reference_later_defaults(self) -> None:
        source = """.subckt example A B R={BASE * 2} BASE=10
R0 A B R
.ends
X0 IN OUT example
"""
        result = transform_netlist(source)
        self.assertIn("R0 A B 20", result)

    def test_unknown_parameter_is_reported(self) -> None:
        source = """.subckt example A B R=1
R0 A B R
.ends
X0 IN OUT example BAD=2
"""
        with self.assertRaisesRegex(ValueError, "Unknown parameter"):
            transform_netlist(source)

    def test_continuations_are_preserved_and_all_parameters_removed(self) -> None:
        source = """.subckt example A B
+ R=1 C=2 EXTRA
R0 A B R
C0 A B C
.ends
X0 IN OUT example
+ R=20 C=30 EXTRA
"""
        self.assertEqual(
            transform_netlist(source),
            """.subckt example A B
+ EXTRA
R0 A B 20
C0 A B 30
.ends
X0 IN OUT example
+ EXTRA
""",
        )

    def test_missing_subcircuit_instance_is_left_as_device(self) -> None:
        source = """.subckt example A B R=1
XDEVICE A B missing R=R
R0 A B R
.ends
X0 IN OUT example R=20
"""
        result = transform_netlist(source)
        self.assertIn("XDEVICE A B missing R=R", result)
        self.assertIn("R0 A B 20", result)
        self.assertNotIn("XDEVICE A B missing 20", result)

    def test_specializes_hierarchy_reachable_from_root_subcircuit(self) -> None:
        source = """.subckt chip IN OUT
XCHILD IN OUT child R=20 C=30
.ends
.subckt child A B R=1 C=2
R0 A B R
C0 A B C
.ends
"""
        self.assertEqual(
            transform_netlist(source),
            """.subckt chip IN OUT
XCHILD IN OUT child
.ends
.subckt child A B
R0 A B 20
C0 A B 30
.ends
""",
        )

    def test_resolves_parameters_inside_quoted_device_expressions(self) -> None:
        source = """.subckt amp_dpc inp inn nb outp outn VDD VSS w=20 l=0.15 rl=4k wt=6 mt=6 mm=0 cs=0.6p
XRl_p outn VDD VSS sky130_fd_pr__res_high_po_0p69 L='max((rl-526)/470.9,0.5)' mult=1 m=1
.ends
XDEFAULT a b c d e VDD VSS amp_dpc
XOVERRIDE a b c d e VDD VSS amp_dpc rl=8k
"""
        result = transform_netlist(source)
        self.assertIn(
"L=7.37736249734551 mult=1 m=1",
result,
        )
        self.assertIn(
"L=15.8717349755787 mult=1 m=1",
result,
        )
        self.assertNotIn("max((rl-526)", result)
        self.assertIn("XDEFAULT a b c d e VDD VSS amp_dpc_1", result)
        self.assertIn("XOVERRIDE a b c d e VDD VSS amp_dpc_2", result)

    def test_evaluates_sqrt_with_si_suffixes_in_quotes(self) -> None:
        source = """.subckt example A B C=4p
CC A B cap W='sqrt(C/1p)'
.ends
X0 IN OUT example
"""
        result = transform_netlist(source)
        self.assertIn("W=2", result)
        self.assertNotIn("sqrt", result)

    def test_evaluates_meg_suffix_and_parameter_reference(self) -> None:
        source = """.subckt example A B R=4k
R0 A B resistor W='sqrt(R/1Meg)'
.ends
X0 IN OUT example
"""
        result = transform_netlist(source)
        self.assertIn("W=0.0632455532033676", result)

    def test_generated_names_avoid_existing_subcircuit_names(self) -> None:
        source = """.subckt example A B R=1
R0 A B R
.ends
.subckt example_1 A B
R1 A B 1
.ends
X0 IN OUT example R=2
X1 IN OUT example R=3
"""
        result = transform_netlist(source)
        self.assertIn(".subckt example_2 A B", result)
        self.assertIn(".subckt example_3 A B", result)
        self.assertIn(".subckt example_1 A B\nR1 A B 1", result)


if __name__ == "__main__":
    unittest.main()
