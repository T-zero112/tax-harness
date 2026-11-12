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


class SchemaAndRequirements(unittest.TestCase):
    def test_tool_enum_and_specific_argument_schema(self):
        from model_adapter import DECISION,decision_schema
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):DECISION.validate_python({"kind":"tool","tool":"ask","arguments":{}})
        branches=decision_schema(True)["$defs"]["ToolDecision"]["oneOf"]
        bank=next(b for b in branches if b["properties"]["tool"]["const"]=="list_bank_transactions")
        self.assertEqual(bank["properties"]["arguments"]["properties"]["direction"]["anyOf"][0]["enum"],["income","expense"])
        self.assertNotIn("requirements",bank["properties"])
    def test_program_registration_and_hints(self):
        req=guards.required_by_text("请查发票合计、银行收入、银行支出及规则")
        self.assertEqual({r.kind for r in req},{"invoice_total","income_total","expense_total","rules"})
        self.assertEqual(len(guards.next_tools(req,[],{})),4)
        for code in ["evidence_mismatch","requirements_fixed","duplicate_requirements","answer_items_incomplete"]:
            f=guards.feedback(code,req,[],{})
            self.assertTrue(f["reason"]);self.assertEqual(len(f["fixed_requirements"]),4)
    def test_operation_not_invoice_evidence(self):
        from model_adapter import Requirement,AnswerItem
        req=Requirement(requirement_id="op",kind="operation",filters={"invoice_id":"F001"})
        proof={"evidence_id":"e","tool":"get_operation","arguments":{"invoice_id":"F001","proposal_id":"p"},"result":{"status":"succeeded","data":{"operation":{"proposal_id":"p","state":"confirmed"}}}}
        item=AnswerItem(requirement_id="op",evidence_ids=["e"],value_key="operation")
        self.assertIn("confirmed",guards.render([req],[item],[proof]))
        proof["tool"]="get_invoice"
        with self.assertRaises(ValueError):guards.render([req],[item],[proof])


class ModelTelemetry(unittest.TestCase):
    def test_failure_codes_and_latency(self):
        import model_adapter as m
        import httpx
        from unittest.mock import patch
        for failure,code in [(httpx.ReadTimeout("timeout"),"model_timeout"),(httpx.ConnectError("connection"),"model_http_error"),(ValueError("schema"),"model_invalid_output")]:
            telemetry={}
            with patch.object(m,"_decide",side_effect=failure):
                with self.assertRaises(type(failure)):m.decide([],telemetry)
            self.assertEqual(telemetry["error_code"],code)
            self.assertGreaterEqual(telemetry["duration_ms"],0)


    def test_http_error_evidence_is_redacted(self):
        import model_adapter as m
        import httpx,os,tempfile
        from pathlib import Path
        from unittest.mock import patch
        key="test-only-credential-not-a-real-key"
        with tempfile.TemporaryDirectory() as folder:
            client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(400,json={"error":key})))
            settings={"PRODUCT_MODEL_BASE_URL":"https://unit.test/v1","PRODUCT_MODEL_NAME":"test","PRODUCT_MODEL_API_KEY":key,"PRODUCT_MODEL_STRUCTURED":"none","PRODUCT_AGENT_EVIDENCE_DIR":folder}
            record={}
            with patch.dict(os.environ,settings),patch.object(m.httpx,"Client",return_value=client):
                with self.assertRaises(httpx.HTTPStatusError):m.decide([],record)
            self.assertEqual(record["error_code"],"model_http_error")
            self.assertEqual(record["http_status"],400)
            self.assertNotIn(key,next(Path(folder).glob("*.json")).read_text())


    def test_malformed_envelopes_are_invalid_output(self):
        import model_adapter as m
        import httpx,os
        from unittest.mock import patch
        settings={"PRODUCT_MODEL_BASE_URL":"https://unit.test/v1","PRODUCT_MODEL_NAME":"test","PRODUCT_MODEL_API_KEY":"","PRODUCT_MODEL_STRUCTURED":"none","PRODUCT_AGENT_EVIDENCE_DIR":""}
        for payload in ([],{}, {"choices":[]},{"choices":[None]},{"choices":[{"message":None}]}):
            client=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=payload)))
            record={}
            with patch.dict(os.environ,settings),patch.object(m.httpx,"Client",return_value=client):
                with self.assertRaises(ValueError):m.decide([],record)
            self.assertEqual(record["error_code"],"model_invalid_output")


    def test_provider_schema_keeps_proposal_field_allowlist(self):
        import model_adapter as m,json
        schema=m.decision_schema()
        self.assertNotIn("propertyNames",json.dumps(schema))
        changes=schema["$defs"]["ProposeDecision"]["properties"]["arguments"]["properties"]["changes"]
        self.assertEqual(set(changes["properties"]),{"purpose","note"})
        self.assertFalse(changes["additionalProperties"])
        self.assertEqual(changes["minProperties"],1)
        for value in changes["properties"].values():self.assertEqual(value["maxLength"],500)
