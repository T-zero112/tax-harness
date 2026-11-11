package local.tax;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.LocalDate;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;

@RestController
@RequestMapping("/api/enterprises/{enterpriseId}/invoices/{invoiceId}")
public class Invoices {
    private static final String SCOPE = "tenant_id = ? and enterprise_id = ? and invoice_id = ?";
    private final Business sessions;
    private final JdbcTemplate jdbc;
    private final ObjectMapper json;
    private final Rules rules;
    private final ChatScopes chatScopes;
    private final AgentOperations operations;
    private final TransactionTemplate transaction;
    private final HttpClient agent = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();

    public Invoices(Business sessions, JdbcTemplate jdbc, ObjectMapper json, Rules rules,
                    ChatScopes chatScopes, AgentOperations operations, PlatformTransactionManager manager) {
        this.sessions = sessions;
        this.jdbc = jdbc;
        this.json = json;
        this.rules = rules;
        this.chatScopes = chatScopes;
        this.operations = operations;
        this.transaction = new TransactionTemplate(manager);
    }

    record Invoice(String enterprise_id, String invoice_id, String purpose, String note, long revision,
                   LocalDate accounting_period, String amount_cents, String seller, String item_name) {}
    record Proposal(UUID proposal_id, long expected_revision, JsonNode changes, String rule_version, JsonNode rule_ref) {}
    record Receipt(UUID proposal_id, UUID idempotency_key, String actor, String rule_version,
                   JsonNode before, JsonNode after, String saved_at, JsonNode rule_ref, UUID operation_id) {}

    @GetMapping
    Invoice get(@CookieValue(name = Business.COOKIE, required = false) String token,
                @PathVariable String enterpriseId, @PathVariable String invoiceId) {
        return invoice(sessions.authorize(token, enterpriseId, Business.Access.READ).tenant(), enterpriseId, invoiceId, false);
    }

    @PostMapping("/review")
    JsonNode review(@CookieValue(name = Business.COOKIE, required = false) String token,
                    @PathVariable String enterpriseId, @PathVariable String invoiceId) throws Exception {
        String tenant = sessions.authorize(token, enterpriseId, Business.Access.READ).tenant();
        Invoice current = invoice(tenant, enterpriseId, invoiceId, false);
        Rules.Selection selection = rules.select(tenant, enterpriseId, current.accounting_period());
        if (!selection.status().equals("ready")) return manual(tenant, current, selection);
        return assess(current, json.createObjectNode(), selection.selected());
    }

    @PostMapping("/proposals")
    JsonNode propose(@CookieValue(name = Business.COOKIE, required = false) String token,
                     @PathVariable String enterpriseId, @PathVariable String invoiceId,
                     @RequestBody JsonNode body) throws Exception {
        return proposeForChat(token, enterpriseId, invoiceId, body, null);
    }

    JsonNode proposeForChat(String token, String enterpriseId, String invoiceId, JsonNode body,
                            ChatScopes.Lease lease) throws Exception {
        return proposeForChat(token,enterpriseId,invoiceId,body,lease,null);
    }

    JsonNode proposeForChat(String token, String enterpriseId, String invoiceId, JsonNode body,
                            ChatScopes.Lease lease, UUID operation) throws Exception {
        String tenant = sessions.authorize(token, enterpriseId, Business.Access.WRITE).tenant();
        Invoice current = invoice(tenant, enterpriseId, invoiceId, false);
        validateChanges(body);
        Rules.Selection selection = rules.select(tenant, enterpriseId, current.accounting_period());
        if (!selection.status().equals("ready")) return manual(tenant, current, selection);
        Rules.Rule rule = selection.selected();
        JsonNode result = assess(current, body, rule);
        // The Agent may echo user-provided values. Java verifies every field before persisting.
        JsonNode changes = result.path("changes");
        if (!changes.equals(body)) {
            throw new ResponseStatusException(HttpStatus.BAD_GATEWAY, "Invalid Agent suggestion");
        }
        UUID id = UUID.randomUUID();
        JsonNode reference = json.valueToTree(rule.citation());
        String version = rule.citation().code() + ":" + rule.citation().version();
        transaction.executeWithoutResult(status -> {
            if (lease != null) operations.checkProposalTurn(lease);
            if (lease != null) chatScopes.check(lease, true);
            sessions.authorize(token, enterpriseId, Business.Access.WRITE);
            jdbc.update("""
                insert into invoice_proposal (proposal_id, tenant_id, enterprise_id, invoice_id,
                    expected_revision, changes, rule_version, rule_ref, chat_scope_id, chat_generation,before_record)
                values (?, ?, ?, ?, ?, ?::jsonb, ?, ?::jsonb, ?, ?,?::jsonb)
                """, id, tenant, enterpriseId, invoiceId, current.revision(), changes.toString(), version,
                    reference.toString(), lease == null ? null : lease.scope_id(), lease == null ? null : lease.generation(),
                    json.valueToTree(current).toString());
            if (lease != null) operations.attachProposal(lease,operation,id);
        });
        return json.valueToTree(new Proposal(id, current.revision(), changes, version, reference));
    }

    @PostMapping("/proposals/{proposalId}/confirm")
    public Receipt confirm(@CookieValue(name = Business.COOKIE, required = false) String token,
                           @PathVariable String enterpriseId, @PathVariable String invoiceId,
                           @PathVariable UUID proposalId, @RequestHeader("Idempotency-Key") UUID key,
                           @RequestHeader(name="X-Chat-Scope",required=false) UUID chatScope,
                           @RequestBody JsonNode body) throws Exception {
        Accounts.Identity actor = sessions.authorize(token, enterpriseId, Business.Access.WRITE);
        if (!body.isObject() || body.size() != 1 || !body.path("confirmed").isBoolean()
                || !body.path("confirmed").booleanValue()) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Explicit confirmation required");
        }
        UUID operation=operations.prepareConfirmation(token,enterpriseId,invoiceId,proposalId,key,chatScope);
        try {
            return transaction.execute(status -> confirmTransaction(token,enterpriseId,invoiceId,proposalId,key,chatScope,actor,operation));
        } catch (RuntimeException error) {
            operations.failed(operation);
            throw error;
        }
    }

    private Receipt confirmTransaction(String token,String enterpriseId,String invoiceId,UUID proposalId,UUID key,
                                       UUID chatScope,Accounts.Identity actor,UUID operation) {
        String tenant=actor.tenant();
        operations.lockConfirmation(operation,proposalId,chatScope);
        chatScopes.checkProposal(proposalId,actor,token);
        sessions.authorize(token,enterpriseId,Business.Access.WRITE);
        Invoice before = invoice(tenant, enterpriseId, invoiceId, true);
        Receipt previous = receipt(tenant, enterpriseId, invoiceId, key);
        if (previous != null) {
            if (!previous.proposal_id().equals(proposalId)) throw conflict("Idempotency key already used");
            return previous;
        }
        List<Proposal> proposals = jdbc.query("select proposal_id, expected_revision, changes, rule_version, rule_ref "
                + "from invoice_proposal where " + SCOPE + " and proposal_id = ?", (row, index) ->
                new Proposal(row.getObject("proposal_id", UUID.class), row.getLong("expected_revision"),
                        parse(row.getString("changes")), row.getString("rule_version"), parse(row.getString("rule_ref"))),
                tenant, enterpriseId, invoiceId, proposalId);
        if (proposals.isEmpty()) throw new ResponseStatusException(HttpStatus.NOT_FOUND);
        Proposal proposal = proposals.getFirst();
        if (before.revision() != proposal.expected_revision()) throw conflict("Invoice version changed");
        validateChanges(proposal.changes());
        Rules.Selection applicable = rules.checkCurrent(tenant, enterpriseId, before.accounting_period());
        if (!applicable.status().equals("ready") || applicable.candidates().stream()
                .noneMatch(rule -> json.valueToTree(rule.citation()).equals(proposal.rule_ref()))) {
            throw conflict("Applicable rule changed; review again");
        }

        String purpose = proposal.changes().has("purpose") ? proposal.changes().get("purpose").asText() : before.purpose();
        String note = proposal.changes().has("note") ? proposal.changes().get("note").asText() : before.note();
        jdbc.update("update product_invoice set purpose = ?, note = ?, revision = revision + 1 where " + SCOPE,
                purpose, note, tenant, enterpriseId, invoiceId);
        Invoice after = invoice(tenant, enterpriseId, invoiceId, false);
        if (after.revision() != before.revision() + 1 || !java.util.Objects.equals(after.purpose(), purpose)
                || !java.util.Objects.equals(after.note(), note)) {
            throw new IllegalStateException("Invoice readback did not match the confirmed proposal");
        }
        int inserted = jdbc.update("""
                insert into invoice_audit (tenant_id, idempotency_key, enterprise_id, invoice_id,
                    proposal_id, actor, rule_version, before_record, after_record, rule_ref,operation_id)
                values (?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?::jsonb, ?::jsonb,?) on conflict do nothing
                """, tenant, key, enterpriseId, invoiceId, proposalId, actor.username(), proposal.rule_version(),
                json.valueToTree(before).toString(), json.valueToTree(after).toString(), proposal.rule_ref().toString(),operation);
        if (inserted != 1) throw conflict("Confirmation or idempotency key already used");
        Receipt result=receipt(tenant, enterpriseId, invoiceId, key);
        operations.confirmed(operation,result);
        return result;
    }

    @GetMapping("/audit")
    List<Receipt> audit(@CookieValue(name = Business.COOKIE, required = false) String token,
                        @PathVariable String enterpriseId, @PathVariable String invoiceId) {
        String tenant = sessions.authorize(token, enterpriseId, Business.Access.READ).tenant();
        invoice(tenant, enterpriseId, invoiceId, false);
        return jdbc.query("select * from invoice_audit where " + SCOPE + " order by saved_at desc",
                (row, index) -> auditRow(row), tenant, enterpriseId, invoiceId);
    }

    private Receipt receipt(String tenant, String enterprise, String invoice, UUID key) {
        return jdbc.query("select * from invoice_audit where " + SCOPE + " and idempotency_key = ?",
                (row, index) -> auditRow(row), tenant, enterprise, invoice, key).stream().findFirst().orElse(null);
    }

    private Receipt auditRow(java.sql.ResultSet row) throws java.sql.SQLException {
        return new Receipt(row.getObject("proposal_id", UUID.class), row.getObject("idempotency_key", UUID.class),
                row.getString("actor"), row.getString("rule_version"), parse(row.getString("before_record")),
                parse(row.getString("after_record")), row.getString("saved_at"), parse(row.getString("rule_ref")),
                row.getObject("operation_id",UUID.class));
    }

    private Invoice invoice(String tenant, String enterprise, String invoice, boolean lock) {
        return jdbc.query("select * from product_invoice where " + SCOPE + (lock ? " for update" : ""),
                (row, index) -> new Invoice(row.getString("enterprise_id"), row.getString("invoice_id"),
                        row.getString("purpose"), row.getString("note"), row.getLong("revision"),
                        row.getObject("accounting_period", LocalDate.class), row.getString("amount_cents"),
                        row.getString("seller"), row.getString("item_name")),
                tenant, enterprise, invoice).stream().findFirst()
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND));
    }

    private JsonNode manual(String tenant, Invoice invoice, Rules.Selection selection) {
        UUID id = UUID.randomUUID();
        JsonNode candidates = json.valueToTree(selection.candidates().stream().map(Rules.Rule::citation).toList());
        jdbc.update("""
                insert into invoice_rule_review (review_id, tenant_id, enterprise_id, invoice_id,
                    invoice_revision, accounting_period, reason, candidates)
                values (?, ?, ?, ?, ?, ?, ?, ?::jsonb)
                """, id, tenant, invoice.enterprise_id(), invoice.invoice_id(), invoice.revision(),
                invoice.accounting_period(), selection.status(), candidates.toString());
        return json.valueToTree(Map.of("status", "manual_review", "reason", selection.status(),
                "review_id", id, "candidates", candidates));
    }

    private void validateChanges(JsonNode changes) {
        if (!changes.isObject() || changes.isEmpty()) throw new ResponseStatusException(HttpStatus.BAD_REQUEST);
        changes.fields().forEachRemaining(field -> {
            if (!List.of("purpose", "note").contains(field.getKey()) || !field.getValue().isTextual()
                    || field.getValue().asText().isBlank() || field.getValue().asText().length() > 500) {
                throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid invoice field or value");
            }
        });
    }

    private JsonNode assess(Invoice current, JsonNode changes, Rules.Rule rule) throws Exception {
        HttpRequest request = HttpRequest.newBuilder(URI.create("http://agent:8000/internal/invoice-review"))
                .timeout(Duration.ofSeconds(10))
                .header("Authorization", "Bearer " + System.getenv("PRODUCT_AGENT_TOKEN"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json.writeValueAsString(Map.of(
                        "invoice", current, "changes", changes, "required_fields", rule.required_fields()))))
                .build();
        try {
            var response = agent.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != 200) throw new ResponseStatusException(HttpStatus.BAD_GATEWAY);
            var result = (com.fasterxml.jackson.databind.node.ObjectNode) json.readTree(response.body());
            result.put("status", "ready");
            result.put("rule_version", rule.citation().code() + ":" + rule.citation().version());
            result.put("rule_description", rule.content());
            result.set("rule_ref", json.valueToTree(rule.citation()));
            return result;
        } catch (java.io.IOException e) {
            throw new ResponseStatusException(HttpStatus.BAD_GATEWAY, "Agent unavailable", e);
        }
    }

    private JsonNode parse(String value) {
        if (value == null) return null;
        try {
            return json.readTree(value);
        } catch (java.io.IOException error) {
            throw new IllegalStateException("Invalid persisted JSON", error);
        }
    }

    private static ResponseStatusException conflict(String message) {
        return new ResponseStatusException(HttpStatus.CONFLICT, message);
    }
}
