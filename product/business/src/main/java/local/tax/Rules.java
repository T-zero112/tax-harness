package local.tax;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.IOException;
import java.net.URI;
import java.net.http.*;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Duration;
import java.time.LocalDate;
import java.time.YearMonth;
import java.util.*;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;

@RestController
@RequestMapping("/api/enterprises/{enterpriseId}/rules")
public class Rules {
    private static final String APPLICABLE = """
        tenant_id = ? and enterprise_id = ? and state = 'published'
        and effective_from <= ? and (effective_until is null or effective_until > ?)
        """;
    private static final String FIELDS = """
        rule_id, code, version, source, clause, content, required_fields, effective_from, effective_until, state
        """;
    private final Business sessions;
    private final JdbcTemplate jdbc;
    private final ObjectMapper json;
    private final HttpClient agent = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();

    public Rules(Business sessions, JdbcTemplate jdbc, ObjectMapper json) {
        this.sessions = sessions;
        this.jdbc = jdbc;
        this.json = json;
    }

    record Citation(UUID rule_id, String code, String version, String source, String clause) {}
    record Rule(Citation citation, String content, List<String> required_fields, LocalDate effective_from,
                LocalDate effective_until, String state) {}
    record Draft(String code, String version, String source, String clause, String content,
                 List<String> required_fields, String effective_from, String effective_until) {}
    record Hit(Rule rule, double semantic_score, boolean exact_match) {}
    record Selection(String status, List<Rule> candidates) {
        Rule selected() { return candidates.getFirst(); }
    }

    @GetMapping
    List<Rule> versions(@CookieValue(name = Business.COOKIE, required = false) String token,
                        @PathVariable String enterpriseId) {
        String tenant = sessions.authorize(token, enterpriseId, Business.Access.READ).tenant();
        lockEnterprise(tenant, enterpriseId, "");
        return jdbc.query("select " + FIELDS + " from invoice_rule where tenant_id = ? and enterprise_id = ? "
                + "order by code, effective_from desc, version, clause", this::readRule, tenant, enterpriseId);
    }

    @GetMapping("/search")
    List<Hit> search(@CookieValue(name = Business.COOKIE, required = false) String token,
                     @PathVariable String enterpriseId, @RequestParam String period, @RequestParam String query)
            throws IOException, InterruptedException {
        String tenant = sessions.authorize(token, enterpriseId, Business.Access.READ).tenant();
        lockEnterprise(tenant, enterpriseId, "");
        if (query.isBlank() || query.length() > 240) throw bad("Invalid search query");
        return rank(tenant, enterpriseId, period(period), query.strip());
    }

    @PostMapping
    Rule create(@CookieValue(name = Business.COOKIE, required = false) String token,
                @PathVariable String enterpriseId, @RequestBody Draft draft) throws IOException, InterruptedException {
        String tenant = sessions.authorize(token, enterpriseId, Business.Access.ADMIN).tenant();
        lockEnterprise(tenant, enterpriseId, "");
        if (!valid(draft.code(), 80) || !valid(draft.version(), 40) || !valid(draft.source(), 120)
                || !valid(draft.clause(), 80) || !valid(draft.content(), 600)
                || draft.required_fields() == null || draft.required_fields().isEmpty()
                || draft.required_fields().stream().anyMatch(f -> !"purpose".equals(f) && !"note".equals(f))
                || new HashSet<>(draft.required_fields()).size() != draft.required_fields().size()) {
            throw bad("Invalid rule fields");
        }
        LocalDate from = period(draft.effective_from());
        LocalDate until = draft.effective_until() == null ? null : period(draft.effective_until());
        if (until != null && !until.isAfter(from)) throw bad("Invalid rule interval");
        String vector = embed(draft.content());
        UUID id = UUID.randomUUID();
        int inserted = jdbc.update("""
                insert into invoice_rule (rule_id,tenant_id,enterprise_id,code,version,source,clause,
                  content,required_fields,effective_from,effective_until,embedding)
                values (?,?,?,?,?,?,?,?,?::jsonb,?,?,?::vector) on conflict do nothing
                """, id, tenant, enterpriseId, draft.code(), draft.version(), draft.source(), draft.clause(),
                draft.content(), json.writeValueAsString(draft.required_fields().stream().sorted().toList()), from, until, vector);
        if (inserted != 1) throw conflict("Rule version already exists");
        return byId(tenant, enterpriseId, id);
    }

    @PostMapping("/{ruleId}/{action:publish|retire}")
    @Transactional
    public Rule transition(@CookieValue(name = Business.COOKIE, required = false) String token,
                           @PathVariable String enterpriseId, @PathVariable UUID ruleId, @PathVariable String action) {
        String tenant = sessions.authorize(token, enterpriseId, Business.Access.ADMIN).tenant();
        lockEnterprise(tenant, enterpriseId, " for update");
        Rule rule = byId(tenant, enterpriseId, ruleId);
        String expected = action.equals("publish") ? "draft" : "published";
        if (!rule.state().equals(expected)) throw conflict("Invalid rule state transition");
        jdbc.update("update invoice_rule set state = ? where tenant_id = ? and enterprise_id = ? and rule_id = ?",
                action.equals("publish") ? "published" : "retired", tenant, enterpriseId, ruleId);
        return byId(tenant, enterpriseId, ruleId);
    }

    Selection select(String tenant, String enterprise, LocalDate date) throws IOException, InterruptedException {
        List<Hit> hits = rank(tenant, enterprise, date, "发票用途和备注完整性");
        return selection(hits.stream().map(Hit::rule).toList());
    }

    Selection checkCurrent(String tenant, String enterprise, LocalDate date) {
        lockEnterprise(tenant, enterprise, " for share");
        return selection(jdbc.query("select " + FIELDS + " from invoice_rule where " + APPLICABLE,
                this::readRule, tenant, enterprise, date, date));
    }

    private Selection selection(List<Rule> rules) {
        if (rules.isEmpty()) return new Selection("rule_missing", rules);
        if (rules.stream().map(Rule::required_fields).distinct().count() > 1
                || rules.stream().map(r -> List.of(r.citation().code(), r.citation().clause())).distinct().count() < rules.size()) {
            return new Selection("rule_conflict", rules);
        }
        return new Selection("ready", rules);
    }

    private List<Hit> rank(String tenant, String enterprise, LocalDate date, String query)
            throws IOException, InterruptedException {
        List<Rule> unindexed = jdbc.query("select " + FIELDS + " from invoice_rule where " + APPLICABLE
                + " and embedding is null", this::readRule, tenant, enterprise, date, date);
        // Seed rules get indexed on first use, after the same scope filter as retrieval.
        for (Rule rule : unindexed) {
            String vector = embed(rule.content());
            jdbc.update("update invoice_rule set embedding = ?::vector where tenant_id = ? and enterprise_id = ? and rule_id = ? and embedding is null",
                    vector, tenant, enterprise, rule.citation().rule_id());
        }
        if (jdbc.queryForObject("select count(*) from invoice_rule where " + APPLICABLE, Integer.class,
                tenant, enterprise, date, date) == 0) return List.of();
        String vector = embed(query);
        return jdbc.query("""
                with eligible as materialized (select * from invoice_rule where %s)
                select %s, 1 - (embedding <=> ?::vector) as semantic_score,
                    strpos(lower(source || ' ' || clause || ' ' || content), lower(?)) > 0 as exact_match
                from eligible order by exact_match desc, semantic_score desc nulls last, rule_id
                """.formatted(APPLICABLE, FIELDS),
                (row, index) -> new Hit(readRule(row, index), row.getDouble("semantic_score"), row.getBoolean("exact_match")),
                tenant, enterprise, date, date, vector, query);
    }

    private Rule byId(String tenant, String enterprise, UUID id) {
        return jdbc.query("select " + FIELDS + " from invoice_rule where tenant_id = ? and enterprise_id = ? and rule_id = ?",
                this::readRule, tenant, enterprise, id).stream().findFirst()
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND));
    }

    private Rule readRule(ResultSet row, int index) throws SQLException {
        List<String> required = new ArrayList<>();
        try {
            json.readTree(row.getString("required_fields")).forEach(node -> required.add(node.asText()));
        } catch (IOException error) {
            throw new SQLException("Invalid rule fields", error);
        }
        return new Rule(new Citation(row.getObject("rule_id", UUID.class), row.getString("code"), row.getString("version"),
                row.getString("source"), row.getString("clause")), row.getString("content"), List.copyOf(required),
                row.getObject("effective_from", LocalDate.class), row.getObject("effective_until", LocalDate.class),
                row.getString("state"));
    }

    private void lockEnterprise(String tenant, String enterprise, String lock) {
        if (jdbc.queryForList("select enterprise_id from enterprise where tenant_id = ? and enterprise_id = ?" + lock,
                String.class, tenant, enterprise).isEmpty()) throw new ResponseStatusException(HttpStatus.NOT_FOUND);
    }

    private String embed(String text) throws IOException, InterruptedException {
        var request = HttpRequest.newBuilder(URI.create("http://agent:8000/internal/embed"))
                .timeout(Duration.ofSeconds(90))
                .header("Authorization", "Bearer " + System.getenv("PRODUCT_AGENT_TOKEN"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json.writeValueAsString(Map.of("text", text))))
                .build();
        try {
            var response = agent.send(request, HttpResponse.BodyHandlers.ofString());
            JsonNode vector = json.readTree(response.body()).path("embedding");
            if (response.statusCode() != 200 || !vector.isArray() || vector.size() != 384) throw new IOException("Invalid embedding");
            double norm = 0;
            for (JsonNode item : vector) {
                if (!item.isNumber() || !Double.isFinite(item.asDouble())) throw new IOException("Invalid embedding");
                norm += item.asDouble() * item.asDouble();
            }
            if (!Double.isFinite(norm) || norm == 0) throw new IOException("Invalid embedding");
            return vector.toString();
        } catch (IOException error) {
            throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "Rule embedding unavailable", error);
        }
    }

    private static boolean valid(String value, int max) {
        return value != null && !value.isBlank() && value.length() <= max;
    }

    private static LocalDate period(String value) {
        try {
            return YearMonth.parse(value).atDay(1);
        } catch (RuntimeException error) {
            throw bad("Period must be YYYY-MM");
        }
    }

    private static ResponseStatusException bad(String message) { return new ResponseStatusException(HttpStatus.BAD_REQUEST, message); }
    private static ResponseStatusException conflict(String message) { return new ResponseStatusException(HttpStatus.CONFLICT, message); }
}
