import unittest
import executor
from model_adapter import DECISION,Requirement

class Feedback(unittest.TestCase):
    def test_original_guards_and_feedback(self):
        req=Requirement(requirement_id="fixed",kind="invoice_total",filters={})
        cases=[
          ({"kind":"ask","message":"请补充资料。","requirements":[]},True,"requirements_fixed"),
          ({"kind":"ask","message":"请补充资料。","requirements":[{"requirement_id":"fixed","kind":"rules","filters":{}}]},False,"duplicate_requirements"),
          ({"kind":"answer","message":"核实。","answer_items":[]},True,"answer_items_incomplete"),
          ({"kind":"answer","message":"核实。","answer_items":[{"requirement_id":"fixed","evidence_ids":["invented"],"value_key":"invoice_cents"}]},True,"evidence_mismatch")]
        for data,registered,code in cases:
            state={"decision":DECISION.validate_python(data),"requirements":[req],"registered":registered,"evidence":[],"body":{"message":"发票汇总"},"memory":{"pending":None,"trace":{"events":[]}}}
            result=executor.validate(state)
            self.assertEqual(result["feedback"]["code"],code)
            self.assertEqual(result["requirements"],[req])
            self.assertTrue(result["feedback"]["recommended_next_tools"])
            self.assertIn("reason",result["feedback"])
