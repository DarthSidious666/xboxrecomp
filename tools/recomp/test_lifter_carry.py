import unittest

from .disasm import BasicBlock, Instruction, Operand
from .lifter import Lifter, lift_basic_block


class CarryLifterTest(unittest.TestCase):
    def test_neg_carry_feeds_adjacent_sbb(self):
        neg = Instruction(0, 2, "neg", "esi", "f7de")
        neg.operands = [Operand(type="reg", reg="esi")]
        sbb = Instruction(2, 2, "sbb", "esi, esi", "19f6")
        sbb.operands = [
            Operand(type="reg", reg="esi"),
            Operand(type="reg", reg="esi"),
        ]

        lifted, _ = lift_basic_block(
            Lifter(), BasicBlock(start=0, instructions=[neg, sbb]))
        generated = "\n".join(lifted)

        self.assertIn("_cf = (esi != 0);", generated)
        self.assertIn(
            "esi = _cf ? 0xFFFFFFFF : 0; /* sbb self (CF extend) */",
            generated,
        )

    def test_neg_carry_feeds_adjacent_adc(self):
        neg = Instruction(0, 2, "neg", "eax", "f7d8")
        neg.operands = [Operand(type="reg", reg="eax")]
        adc = Instruction(2, 3, "adc", "edx, 0", "83d200")
        adc.operands = [
            Operand(type="reg", reg="edx"),
            Operand(type="imm", imm=0),
        ]

        lifted, _ = lift_basic_block(
            Lifter(), BasicBlock(start=0, instructions=[neg, adc]))
        generated = "\n".join(lifted)

        self.assertIn("_cf = (eax != 0);", generated)
        self.assertIn("edx = edx + 0 + _cf; /* adc */", generated)


if __name__ == "__main__":
    unittest.main()
