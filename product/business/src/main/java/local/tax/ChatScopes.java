package local.tax;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.LocalDate;
import java.time.YearMonth;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;

@Service
public class ChatScopes {
    record Lease(String session, UUID scope_id, String tenant_id, String username,
                 String enterprise_id, String period, long generation) {}
    private final Business sessions;
    private final JdbcTemplate jdbc;
    private final StringRedisTemplate redis;
    private final ObjectMapper json;

    public ChatScopes(Business sessions, JdbcTemplate jdbc, StringRedisTemplate redis, ObjectMapper json) {
        this.sessions = sessions;
        this.jdbc = jdbc;
        this.redis = redis;
        this.json = json;
    }

    static LocalDate month(String period) {
        try {
            if (period == null || !period.matches("[0-9]{4}-(0[1-9]|1[0-2])") || period.startsWith("0000"))
                throw new IllegalArgumentException();
            return YearMonth.parse(period).atDay(1);
        } catch (RuntimeException error) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Period must be YYYY-MM");
        }
    }

    @Transactional
    public Lease activate(String session, UUID scopeId, String enterprise, String period) {
        Accounts.Identity actor = sessions.authorize(session, enterprise, Business.Access.READ);
        LocalDate date = month(period);
        jdbc.update("""
            insert into agent_chat_scope(scope_id,account_id,tenant_id,browser_session,enterprise_id,accounting_period)
            values (?,?,?,?,?,?) on conflict do nothing
            """, scopeId, actor.id(), actor.tenant(), sessionDigest(session), enterprise, date);
        Map<String,Object> scope = lock(scopeId);
        if (!scope.get("account_id").equals(actor.id()) || !scope.get("browser_session").equals(sessionDigest(session)))
            throw new ResponseStatusException(HttpStatus.NOT_FOUND);
        if (!scope.get("enterprise_id").equals(enterprise) || !scope.get("accounting_period").toString().equals(date.toString())) {
            jdbc.update("""
                update agent_chat_scope set enterprise_id=?, accounting_period=?, generation=generation+1,updated_at=now()
                where scope_id=?
                """, enterprise, date, scopeId);
            scope = lock(scopeId);
        }
        return new Lease(session,scopeId,actor.tenant(),actor.username(),enterprise,period,
                ((Number)scope.get("generation")).longValue());
    }

    String issue(Lease lease) throws Exception {
        String token = UUID.randomUUID().toString() + UUID.randomUUID();
        redis.opsForValue().set("agent-call:" + token,json.writeValueAsString(lease),Duration.ofMinutes(15));
        return token;
    }

    void release(String token) { redis.delete("agent-call:" + token); }

    Lease resolve(String authorization, String token) throws Exception {
        String expected = "Bearer " + System.getenv("PRODUCT_AGENT_TOKEN");
        if (!MessageDigest.isEqual(authorization.getBytes(StandardCharsets.UTF_8),expected.getBytes(StandardCharsets.UTF_8)))
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED);
        String value = token == null ? null : redis.opsForValue().get("agent-call:" + token);
        if (value == null) throw new ResponseStatusException(HttpStatus.UNAUTHORIZED);
        Lease lease = json.readValue(value,Lease.class);
        Accounts.Identity actor = sessions.authorize(lease.session(),lease.enterprise_id(),Business.Access.READ);
        if (!actor.tenant().equals(lease.tenant_id()) || !actor.username().equals(lease.username()))
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED);
        check(lease, false);
        return lease;
    }

    void check(Lease lease, boolean lock) {
        List<Map<String,Object>> scopes = jdbc.queryForList("select generation,enterprise_id,accounting_period "
                + "from agent_chat_scope where scope_id=? and browser_session=?" + (lock ? " for update" : ""),
                lease.scope_id(),sessionDigest(lease.session()));
        if (scopes.isEmpty() || ((Number)scopes.getFirst().get("generation")).longValue()!=lease.generation()
                || !scopes.getFirst().get("enterprise_id").equals(lease.enterprise_id())
                || !scopes.getFirst().get("accounting_period").toString().equals(month(lease.period()).toString()))
            throw new ResponseStatusException(HttpStatus.CONFLICT,"Chat scope changed; start another turn");
    }

    @Transactional
    public void invalidate(Lease lease) {
        check(lease,true);
        jdbc.update("update agent_chat_scope set generation=generation+1,updated_at=now() where scope_id=?",lease.scope_id());
    }

    void checkProposal(UUID proposal, Accounts.Identity actor, String session) {
        List<Map<String,Object>> bindings = jdbc.queryForList("select chat_scope_id,chat_generation from invoice_proposal "
                + "where proposal_id=? and tenant_id=? and chat_scope_id is not null",proposal,actor.tenant());
        if (bindings.isEmpty()) return;
        Map<String,Object> binding = bindings.getFirst();
        Map<String,Object> scope = lock((UUID)binding.get("chat_scope_id"));
        if (!scope.get("account_id").equals(actor.id()) || !scope.get("browser_session").equals(sessionDigest(session)))
            throw new ResponseStatusException(HttpStatus.NOT_FOUND);
        boolean saved = jdbc.queryForObject("select count(*) from invoice_audit where tenant_id=? and proposal_id=?",
                Integer.class,actor.tenant(),proposal)>0;
        if (!saved && !scope.get("generation").equals(binding.get("chat_generation")))
            throw new ResponseStatusException(HttpStatus.CONFLICT,"Proposal scope expired; review again");
    }

    private Map<String,Object> lock(UUID id) {
        return jdbc.queryForList("select * from agent_chat_scope where scope_id=? for update",id).stream().findFirst()
                .orElseThrow(()->new ResponseStatusException(HttpStatus.NOT_FOUND));
    }

    private static String sessionDigest(String session) {
        try {
            return java.util.HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                    .digest(session.getBytes(StandardCharsets.UTF_8)));
        } catch (java.security.NoSuchAlgorithmException error) { throw new IllegalStateException(error); }
    }
}
