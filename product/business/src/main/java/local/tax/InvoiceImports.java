package local.tax;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.YearMonth;
import java.util.*;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;
import org.springframework.web.server.ResponseStatusException;

@RestController
public class InvoiceImports {
    private static final int MAX_BYTES = 2 * 1024 * 1024;
    private static final String BASE = "/api/enterprises/{enterpriseId}/imports";
    private static final String TOKEN = System.getenv("PRODUCT_IMPORT_TOKEN");
    private final Business sessions;
    private final JdbcTemplate jdbc;
    private final ObjectMapper json;
    private final HttpClient gateway = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();

    public InvoiceImports(Business sessions, JdbcTemplate jdbc, ObjectMapper json) {
        this.sessions = sessions;
        this.jdbc = jdbc;
        this.json = json;
    }

    record Job(UUID job_id, String file_name, String file_sha256, String state, JsonNode preview,
               Integer imported_count, String created_at, String imported_at) {}
    record Stored(Job job, String objectKey, UUID attempt) {}

    @PostMapping(BASE)
    Job upload(@CookieValue(name = Business.COOKIE, required = false) String token,
               @PathVariable String enterpriseId, @RequestParam("file") MultipartFile file) throws Exception {
        String tenant = sessions.authorize(token, enterpriseId, Business.Access.WRITE).tenant();
        authorizeScope(tenant, enterpriseId);
        String name = file.getOriginalFilename();
        if (name == null || !name.toLowerCase(Locale.ROOT).endsWith(".xlsx") || name.length() > 160
                || name.contains("/") || name.contains("\\") || name.chars().anyMatch(Character::isISOControl)) {
            throw bad("Invalid Excel file name");
        }
        if (file.isEmpty() || file.getSize() > MAX_BYTES) throw bad("File size out of range");
        byte[] data = file.getBytes();
        String digest = HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(data));
        List<Stored> existing = jdbc.query("""
                select * from invoice_import where tenant_id = ? and enterprise_id = ? and file_sha256 = ?
                """, this::row, tenant, enterpriseId, digest);
        if (!existing.isEmpty()) return existing.getFirst().job();
        String key = tenant + "/" + enterpriseId + "/" + digest + ".xlsx";
        request("PUT", "/objects/" + key, data, "application/octet-stream", 200);
        UUID id = UUID.randomUUID();
        UUID attempt = UUID.randomUUID();
        int count = jdbc.update("""
                insert into invoice_import (job_id, tenant_id, enterprise_id, file_name, file_sha256, object_key, attempt, state)
                values (?, ?, ?, ?, ?, ?, ?, 'queued') on conflict (tenant_id, enterprise_id, file_sha256) do nothing
                """, id, tenant, enterpriseId, name, digest, key, attempt);
        if (count == 0) {
            return jdbc.query("""
                    select * from invoice_import where tenant_id = ? and enterprise_id = ? and file_sha256 = ?
                    """, this::row, tenant, enterpriseId, digest).getFirst().job();
        }
        dispatch(id, attempt, key);
        return find(tenant, enterpriseId, id, false).job();
    }

    @GetMapping(BASE)
    List<Job> list(@CookieValue(name = Business.COOKIE, required = false) String token,
                   @PathVariable String enterpriseId) {
        String tenant = sessions.authorize(token, enterpriseId, Business.Access.READ).tenant();
        authorizeScope(tenant, enterpriseId);
        return jdbc.query("""
                select * from invoice_import where tenant_id = ? and enterprise_id = ?
                order by created_at desc limit 30
                """, this::row, tenant, enterpriseId).stream().map(Stored::job).toList();
    }

    @GetMapping(BASE + "/{id}")
    Job get(@CookieValue(name = Business.COOKIE, required = false) String token,
            @PathVariable String enterpriseId, @PathVariable UUID id) {
        return find(sessions.authorize(token, enterpriseId, Business.Access.READ).tenant(), enterpriseId, id, false).job();
    }

    @PostMapping(BASE + "/{id}/retry")
    Job retry(@CookieValue(name = Business.COOKIE, required = false) String token,
              @PathVariable String enterpriseId, @PathVariable UUID id) {
        String tenant = sessions.authorize(token, enterpriseId, Business.Access.WRITE).tenant();
        Stored previous = find(tenant, enterpriseId, id, false);
        UUID attempt = UUID.randomUUID();
        int updated = jdbc.update("""
                update invoice_import set attempt = ? where tenant_id = ? and enterprise_id = ?
                and job_id = ? and state = 'queued' and attempt = ?
                """, attempt, tenant, enterpriseId, id, previous.attempt());
        if (updated == 1) dispatch(id, attempt, previous.objectKey());
        return find(tenant, enterpriseId, id, false).job();
    }

    @PostMapping("/internal/imports/{id}/{attempt}")
    Map<String, Boolean> result(@PathVariable UUID id, @PathVariable UUID attempt,
                                @RequestHeader("Authorization") String authorization, @RequestBody JsonNode body) {
        if (!MessageDigest.isEqual(authorization.getBytes(StandardCharsets.UTF_8),
                ("Bearer " + TOKEN).getBytes(StandardCharsets.UTF_8))) {
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED);
        }
        JsonNode rows = body.path("rows");
        JsonNode errors = body.path("errors");
        if (!rows.isArray() || !errors.isArray() || rows.size() + errors.size() > 1000
                || (rows.isEmpty() && errors.isEmpty())) throw bad("Invalid parser result");
        validateRows(rows);
        for (JsonNode error : errors) {
            if (!error.path("row").isIntegralNumber() || error.path("row").asInt() < 1
                    || !error.path("message").isTextual() || error.path("message").asText().length() > 200) {
                throw bad("Invalid row error");
            }
        }
        jdbc.update("""
                update invoice_import set state = ?, preview = ?::jsonb
                where job_id = ? and attempt = ? and state = 'queued'
                """, errors.isEmpty() ? "ready" : "invalid", body.toString(), id, attempt);
        return Map.of("accepted", true);
    }

    @PostMapping(BASE + "/{id}/confirm")
    @Transactional
    public Job confirm(@CookieValue(name = Business.COOKIE, required = false) String token,
                       @PathVariable String enterpriseId, @PathVariable UUID id,
                       @RequestBody JsonNode body) {
        String tenant = sessions.authorize(token, enterpriseId, Business.Access.WRITE).tenant();
        if (body.size() != 1 || !body.path("confirmed").isBoolean() || !body.path("confirmed").booleanValue()) {
            throw bad("Explicit confirmation required");
        }
        Job job = find(tenant, enterpriseId, id, true).job();
        if (job.state().equals("imported")) return job;
        if (!job.state().equals("ready")) throw conflict("Import is not ready");
        JsonNode rows = job.preview().path("rows");
        validateRows(rows);
        // A stable lock order also covers overlapping batches.
        var ordered = new ArrayList<JsonNode>();
        rows.forEach(ordered::add);
        ordered.sort(Comparator.comparing(row -> row.path("invoice_id").asText()));
        for (JsonNode row : ordered) {
            int count = jdbc.update("""
                    insert into product_invoice (tenant_id, enterprise_id, invoice_id, accounting_period, purpose, note,
                        amount_cents,seller,item_name)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?) on conflict do nothing
                    """, tenant, enterpriseId, row.path("invoice_id").asText(),
                    YearMonth.parse(row.path("accounting_period").asText()).atDay(1),
                    row.path("purpose").asText(null), row.path("note").asText(null),
                    row.hasNonNull("amount_cents") ? Long.valueOf(row.path("amount_cents").asText()) : null,
                    row.path("seller").asText(null),row.path("item_name").asText(null));
            if (count != 1) throw conflict("Invoice already exists");
        }
        jdbc.update("""
                update invoice_import set state = 'imported', imported_count = ?, imported_at = now()
                where tenant_id = ? and enterprise_id = ? and job_id = ?
                """, rows.size(), tenant, enterpriseId, id);
        return find(tenant, enterpriseId, id, false).job();
    }

    private void validateRows(JsonNode rows) {
        Set<String> invoices = new HashSet<>();
        for (JsonNode row : rows) {
            if (row.hasNonNull("amount_cents") && (!row.get("amount_cents").isTextual()
                    || !row.get("amount_cents").asText().matches("[0-9]{1,18}"))) throw bad("Invalid amount");
            for (String name : List.of("seller","item_name")) {
                if (row.hasNonNull(name) && (!row.get(name).isTextual() || row.get(name).asText().length()>120
                        || row.get(name).asText().indexOf(0)>=0)) throw bad("Invalid invoice detail");
            }
            String invoice = row.path("invoice_id").asText("");
            String period = row.path("accounting_period").asText("");
            if (!row.path("invoice_id").isTextual() || !invoice.matches("[A-Za-z0-9_-]{1,64}")
                    || !invoices.add(invoice) || !row.path("accounting_period").isTextual()
                    || !period.matches("[0-9]{4}-(0[1-9]|1[0-2])") || period.startsWith("0000")
                    || !row.path("row").isIntegralNumber() || row.path("row").asInt() < 2
                    || !validText(row.get("purpose")) || !validText(row.get("note"))) throw bad("Invalid invoice row");
        }
    }

    private boolean validText(JsonNode value) {
        return value != null && (value.isNull() || value.isTextual()
                && value.asText().codePointCount(0, value.asText().length()) <= 500 && value.asText().indexOf(0) < 0);
    }

    private void authorizeScope(String tenant, String enterprise) {
        if (jdbc.queryForList("select enterprise_id from enterprise where tenant_id = ? and enterprise_id = ?",
                String.class, tenant, enterprise).isEmpty()) throw new ResponseStatusException(HttpStatus.NOT_FOUND);
    }

    private Stored find(String tenant, String enterprise, UUID id, boolean lock) {
        return jdbc.query("select * from invoice_import where tenant_id = ? and enterprise_id = ? and job_id = ?"
                + (lock ? " for update" : ""), this::row, tenant, enterprise, id).stream().findFirst()
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND));
    }

    private Stored row(java.sql.ResultSet row, int index) throws java.sql.SQLException {
        String preview = row.getString("preview");
        try {
            return new Stored(new Job(row.getObject("job_id", UUID.class), row.getString("file_name"),
                    row.getString("file_sha256"), row.getString("state"), preview == null ? null : json.readTree(preview),
                    row.getObject("imported_count", Integer.class), row.getString("created_at"), row.getString("imported_at")),
                    row.getString("object_key"), row.getObject("attempt", UUID.class));
        } catch (java.io.IOException error) {
            throw new java.sql.SQLException("Invalid stored preview", error);
        }
    }

    private void dispatch(UUID id, UUID attempt, String key) {
        try {
            request("POST", "/jobs", json.writeValueAsBytes(Map.of("job_id", id, "attempt", attempt, "object_key", key)),
                    "application/json", 202);
        } catch (Exception error) {
            // The durable queued job remains visible and can be re-enqueued.
            if (error instanceof InterruptedException) Thread.currentThread().interrupt();
            org.slf4j.LoggerFactory.getLogger(InvoiceImports.class).warn("Dispatch unavailable for job {}", id);
        }
    }

    private void request(String method, String path, byte[] body, String type, int status) throws InterruptedException {
        var request = HttpRequest.newBuilder(URI.create("http://importer:8000" + path))
                .timeout(Duration.ofSeconds(30)).header("Authorization", "Bearer " + TOKEN)
                .header("Content-Type", type).method(method, HttpRequest.BodyPublishers.ofByteArray(body)).build();
        try {
            if (gateway.send(request, HttpResponse.BodyHandlers.discarding()).statusCode() != status) {
                throw new java.io.IOException("Unexpected importer response");
            }
        } catch (java.io.IOException error) {
            throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "Import service unavailable", error);
        }
    }

    private static ResponseStatusException bad(String message) { return new ResponseStatusException(HttpStatus.BAD_REQUEST, message); }
    private static ResponseStatusException conflict(String message) { return new ResponseStatusException(HttpStatus.CONFLICT, message); }
}
