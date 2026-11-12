import unittest
from types import SimpleNamespace
import guards

class Claims(unittest.TestCase):
    def test_claims_without_gateway_event(self):
        for text in ["已按您要求生成建议", "已为您准备好修改", "建议已提交", "已更新为办公费", "修改已完成"]:
            with self.subTest(text=text):
                self.assertEqual(guards.proposal_claim(SimpleNamespace(message=text,claims_proposal=False),[]),"proposal_claim_without_event")
    def test_structured_claim_cannot_hide_behind_other_text(self):
        self.assertEqual(guards.proposal_claim(SimpleNamespace(message="请继续",claims_proposal=True),[]),"proposal_claim_without_event")
    def test_verified_proposal(self):
        event={"event":"proposal","data":{"proposal":{"proposal_id":"p"},"operation_id":"o"}}
        d=SimpleNamespace(message="建议已提交",claims_proposal=True)
        self.assertIsNone(guards.proposal_claim(d,[event]))
        self.assertIsNone(guards.safe_reply(d.message,[],[event]))
        self.assertEqual(guards.safe_reply("已保存",[],[event]),"proposal_evidence_missing")
    def test_no_claim_and_no_forged_event(self):
        self.assertIsNone(guards.proposal_claim(SimpleNamespace(message="尚未生成提案，请补充备注",claims_proposal=False),[]))
        self.assertEqual(guards.proposal_claim(SimpleNamespace(message="已生成建议",claims_proposal=False),[{"event":"tool_result","data":{"status":"failed"}}]),"proposal_claim_without_event")

if __name__=="__main__":unittest.main()
