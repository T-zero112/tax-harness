import unittest
from types import SimpleNamespace
import guards

class HistoricReceipt(unittest.TestCase):
    def test_historic_save_does_not_prove_new_proposal_claim(self):
        proof={"tool":"get_operation","result":{"status":"succeeded","data":{"operation":{"state":"confirmed"}}}}
        self.assertEqual(guards.proposal_claim(SimpleNamespace(message="已保存。",claims_proposal=True),[],[proof]),"proposal_claim_without_event")
        self.assertIsNone(guards.proposal_claim(SimpleNamespace(message="已保存。",claims_proposal=False),[],[proof]))
