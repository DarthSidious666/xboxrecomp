import unittest
from pathlib import Path

from .disasm import BasicBlock, Instruction, Operand
from .lifter import Lifter
from .translator import FunctionTranslator


class FpuLifterTest(unittest.TestCase):
    def test_stack_register_load_duplicates_the_old_top(self):
        instruction = Instruction(0, 2, "fld", "st(0)", "d9c0")
        instruction.operands = [Operand(type="reg", reg="st(0)")]

        self.assertEqual(
            Lifter().lift_instruction(instruction),
            ["fp_push(fp_st(0)); /* fld st(0) */"],
        )

    def test_fst_does_not_pop_and_fstp_does(self):
        operand = Operand(type="mem", mem_base="esp", mem_disp=0x18,
                          mem_size=4)
        store = Instruction(0, 4, "fst", "dword ptr [esp + 0x18]", "")
        store.operands = [operand]
        store_pop = Instruction(
            0, 4, "fstp", "dword ptr [esp + 0x18]", "")
        store_pop.operands = [operand]

        self.assertEqual(
            Lifter().lift_instruction(store),
            ["MEMF(esp + 0x18) = (float)fp_top(); /* fst */"],
        )
        self.assertEqual(
            Lifter().lift_instruction(store_pop),
            ["MEMF(esp + 0x18) = (float)fp_top(); fp_pop(); /* fstp */"],
        )

    def test_qword_integer_conversion_uses_signed_64_bit_storage(self):
        operand = Operand(type="mem", mem_base="esp", mem_disp=0x10,
                          mem_size=8)
        store = Instruction(
            0, 4, "fistp", "qword ptr [esp + 0x10]", "")
        store.operands = [operand]
        load = Instruction(0, 4, "fild", "qword ptr [esp + 0x10]", "")
        load.operands = [operand]

        self.assertEqual(
            Lifter().lift_instruction(store),
            ["SMEM64(esp + 0x10) = (int64_t)llrint(fp_top()); "
             "fp_pop(); /* fistp */"],
        )
        self.assertEqual(
            Lifter().lift_instruction(load),
            ["fp_push((double)SMEM64(esp + 0x10)); /* fild */"],
        )

    def test_memory_arithmetic_updates_st0_without_popping(self):
        instruction = Instruction(
            0, 6, "fdiv", "dword ptr [0x00123456]", "")
        instruction.operands = [
            Operand(type="mem", mem_disp=0x00123456, mem_size=4),
        ]

        self.assertEqual(
            Lifter().lift_instruction(instruction),
            ["fp_top() /= MEMF(0x123456); "
             "/* fdiv dword ptr [0x00123456] */"],
        )

    def test_register_arithmetic_updates_st0_without_popping(self):
        instruction = Instruction(0, 2, "fmul", "st(1)", "d8c9")
        instruction.operands = [Operand(type="reg", reg="st(1)")]

        self.assertEqual(
            Lifter().lift_instruction(instruction),
            ["fp_top() *= fp_st(1); /* fmul st(1) */"],
        )

    def test_translated_functions_share_runtime_fpu_state(self):
        start = 0x00010000
        instructions = [
            Instruction(start, 2, "fld", "st(0)", "d9c0"),
            Instruction(start + 2, 1, "ret", "", "c3"),
        ]
        instructions[0].operands = [Operand(type="reg", reg="st(0)")]
        block = BasicBlock(start=start, instructions=instructions)
        translator = FunctionTranslator(
            b"\0", {start: {"_addr": start, "end": start + 3}})
        translator._read_func_bytes = lambda _start, _end: b"\xd9\xc0\xc3"
        translator.disasm.disassemble_function = (
            lambda _raw, _start, _end: instructions)
        translator.disasm.build_basic_blocks = (
            lambda _instructions, _start, _end, extra_leaders=None: [block])

        generated = translator.translate_function(
            start, {"_addr": start, "end": start + 3})

        self.assertIn("g_fp_stack", generated)
        self.assertIn("g_fp_top", generated)
        self.assertNotIn("double _fp_stack[8]", generated)
        self.assertNotIn("int _fp_top = 0", generated)

    def test_runtime_template_exports_shared_fpu_state(self):
        root = Path(__file__).resolve().parents[2]
        runtime_types = (
            root / "templates" / "runtime" / "recomp_types.h"
        ).read_text()
        main = (
            root / "templates" / "new-game" / "src" / "main.c"
        ).read_text()

        self.assertIn("extern double g_fp_stack[8];", runtime_types)
        self.assertIn("extern uint32_t g_fp_top;", runtime_types)
        self.assertIn("#define SMEM64(addr)", runtime_types)
        self.assertIn("double g_fp_stack[8];", main)
        self.assertIn("uint32_t g_fp_top;", main)


if __name__ == "__main__":
    unittest.main()
