package local.tax;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.util.*;
import java.util.concurrent.*;
import java.util.regex.Pattern;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.data.redis.core.script.DefaultRedisScript;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;
import jakarta.annotation.PreDestroy;

@RestController
public class AgentTasks {
    private static final String BASE = "/api/enterprises/{enterprise}/chat/tasks";
    private static final String ACTIVE = "status in ('running','waiting_user','blocked')";
    private final Business sessions;
    private final ChatScopes scopes;
    private final JdbcTemplate jdbc;
    private final ObjectMapper json;
    private final AgentRedaction redaction;
    private final StringRedisTemplate redis;
    private final TransactionTemplate transaction;
    private final ScheduledExecutorService renewal = Executors.newScheduledThreadPool(2);
    private static final DefaultRedisScript<Long> RENEW = new DefaultRedisScript<>(
        "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('expire',KEYS[1],30) else return 0 end",Long.class);
    private static final DefaultRedisScript<Long> RELEASE = new DefaultRedisScript<>(
        "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end",Long.class);

    public AgentTasks(Business sessions, ChatScopes scopes, JdbcTemplate jdbc, ObjectMapper json,
                      AgentRedaction redaction, StringRedisTemplate redis, PlatformTransactionManager manager) {
        this.sessions = sessions;
        this.scopes = scopes;
        this.jdbc = jdbc;
        this.json = json;
        this.redaction = redaction;
        this.redis = redis;
        transaction = new TransactionTemplate(manager);
    }

    @PreDestroy
    void stop() { renewal.shutdownNow(); }

    record Revision(long expected_revision, String object_id, String question) {}
    record Resume(UUID session_id, String period, long expected_revision) {}

    @GetMapping(BASE + "/active")
    Object active(@CookieValue(name=Business.COOKIE,required=false) String cookie, @PathVariable String enterprise,
                  @RequestParam String period) {
        var actor = sessions.authorize(cookie,enterprise,Business.Access.READ);
        ChatScopes.month(period);
        var rows = jdbc.queryForList("select * from agent_task where tenant_id=? and account_id=? and enterprise_id=? and "
                + ACTIVE,actor.tenant(),actor.id(),enterprise);
        return rows.isEmpty() ? Map.of("task",json.nullNode()) : view(rows.getFirst(),period);
    }

    @GetMapping(BASE + "/{id}")
    Object detail(@CookieValue(name=Business.COOKIE,required=false) String cookie, @PathVariable String enterprise,
                  @PathVariable UUID id, @RequestParam String period) {
        return view(owned(cookie,enterprise,id,false),period);
    }

    @GetMapping(BASE + "/{id}/traces")
    Object traces(@CookieValue(name=Business.COOKIE,required=false) String cookie, @PathVariable String enterprise,
                  @PathVariable UUID id) {
        owned(cookie,enterprise,id,false);
        return jdbc.queryForList("select turn_id,status,steps,summary::text as summary,model_name,duration_ms,context_bytes "
                + "from agent_trace where task_id=? order by created_at,turn_id",id).stream().map(row -> {
                    row.put("summary",parse(row.get("summary").toString()));
                    return row;
                }).toList();
    }

    @PostMapping(BASE + "/{id}/correct")
    @Transactional
    public Object correct(@CookieValue(name=Business.COOKIE,required=false) String cookie,
                          @PathVariable String enterprise, @PathVariable UUID id, @RequestBody Revision body) {
        var row = owned(cookie,enterprise,id,true);
        mutable(row,body.expected_revision());
        if (body.object_id()!=null && !body.object_id().matches("[A-Za-z0-9_-]{1,64}")) throw bad();
        if (body.question()!=null && body.question().length()>2000) throw bad();
        if (body.object_id()==null && body.question()==null) throw bad();
        if (body.object_id()!=null && jdbc.queryForObject(
                "select count(*) from product_invoice where tenant_id=? and enterprise_id=? and invoice_id=? and accounting_period=?",
                Integer.class,row.get("tenant_id"),enterprise,body.object_id(),row.get("accounting_period"))==0)
            throw missing();
        invalidateProposal(row);
        String object=body.object_id()==null ? (String)row.get("object_id") : body.object_id();
        String question=body.question()==null ? (String)row.get("question") : redaction.text(body.question(),cookie);
        if (body.object_id()!=null && !Objects.equals(body.object_id(),row.get("object_id")) && body.question()==null)
            question="请补充目标发票的核验信息。";
        jdbc.update("update agent_task set object_id=?,question=?,proposal_id=null,revision=revision+1,"
                + "context_since=now(),status='waiting_user',updated_at=now() where task_id=?",
                object,question,id);
        return view(owned(cookie,enterprise,id,false),row.get("accounting_period").toString().substring(0,7));
    }

    @PostMapping(BASE + "/{id}/cancel")
    @Transactional
    public Object cancel(@CookieValue(name=Business.COOKIE,required=false) String cookie,
                         @PathVariable String enterprise,@PathVariable UUID id,@RequestBody Revision body) {
        var row = owned(cookie,enterprise,id,true);
        mutable(row,body.expected_revision());
        invalidateProposal(row);
        jdbc.update("update agent_task set status='cancelled',revision=revision+1,updated_at=now() where task_id=?",id);
        return Map.of("status","cancelled","task_id",id);
    }

    @PostMapping(BASE + "/{id}/resume")
    @Transactional
    public Object resume(@CookieValue(name=Business.COOKIE,required=false) String cookie,
                         @PathVariable String enterprise,@PathVariable UUID id,@RequestBody Resume body) {
        // Keep the same lock order as turn/proposal/confirmation: task, then scope, then invoice.
        var row = owned(cookie,enterprise,id,true);
        mutable(row,body.expected_revision());
        if (body.session_id()==null || !row.get("accounting_period").toString().equals(ChatScopes.month(body.period()).toString()))
            throw conflict("period_mismatch");
        var lease = scopes.activate(cookie,body.session_id(),enterprise,body.period());
        jdbc.update("update agent_task set bound_scope_id=?,revision=revision+1,updated_at=now() where task_id=?",
                lease.scope_id(),id);
        if (row.get("proposal_id")!=null)
            jdbc.update("update invoice_proposal set chat_scope_id=?,chat_generation=? where proposal_id=? and task_id=?",
                    lease.scope_id(),lease.generation(),row.get("proposal_id"),id);
        return view(owned(cookie,enterprise,id,false),body.period());
    }

    Map<String,Object> owned(String cookie,String enterprise,UUID id,boolean lock) {
        var actor = sessions.authorize(cookie,enterprise,Business.Access.READ);
        return jdbc.queryForList("select * from agent_task where task_id=? and tenant_id=? and account_id=? and enterprise_id=?"
                + (lock ? " for update" : ""),id,actor.tenant(),actor.id(),enterprise).stream().findFirst().orElseThrow(AgentTasks::missing);
    }

    Map<String,Object> lock(UUID id) {
        return jdbc.queryForList("select * from agent_task where task_id=? for update",id).stream().findFirst()
                .orElseThrow(AgentTasks::missing);
    }

    private boolean busy(Map<String,Object> row) {
        return row.get("active_turn_id")!=null && row.get("lease_until")!=null
                && ((java.sql.Timestamp)row.get("lease_until")).toInstant().isAfter(Instant.now());
    }

    void noUnknown(UUID task, UUID except) {
        if (jdbc.queryForObject("select count(*) from agent_operation where task_id=? and state in ('pending','unknown') "
                + "and (?::uuid is null or operation_id<>?::uuid)",Integer.class,task,except,except)>0)
            throw conflict("operation_unknown");
    }

    private void mutable(Map<String,Object> row,long expected) {
        if (!Set.of("running","waiting_user","blocked").contains(row.get("status"))) throw conflict("task_closed");
        if (((Number)row.get("revision")).longValue()!=expected) throw conflict("revision_conflict");
        if (busy(row)) throw conflict("conversation_busy");
        recoverExpired(row);
        noUnknown((UUID)row.get("task_id"),null);
    }

    private void recoverExpired(Map<String,Object> row) {
        if (row.get("active_turn_id")==null || busy(row)) return;
        jdbc.update("update agent_trace set status='incomplete',completed_at=now() where turn_id=? and status='running'",
                row.get("active_turn_id"));
        jdbc.update("update agent_operation set state='unknown',updated_at=now() where task_id=? and state='pending'",
                row.get("task_id"));
        jdbc.update("update agent_task set active_turn_id=null,lease_until=null,status=case when exists "
                + "(select 1 from agent_operation where task_id=? and state='unknown') then 'blocked' else 'waiting_user' end "
                + "where task_id=?",row.get("task_id"),row.get("task_id"));
    }

    private void invalidateProposal(Map<String,Object> row) {
        if (row.get("proposal_id")!=null)
            jdbc.update("update invoice_proposal set chat_generation=-1 where proposal_id=? and task_id=?",
                    row.get("proposal_id"),row.get("task_id"));
    }

    ChatScopes.Lease begin(String cookie,String enterprise,String period,UUID requested,UUID scopeId,String message) {
        UUID turn = UUID.randomUUID();
        ChatScopes.Lease lease = transaction.execute(status -> {
            var actor = sessions.authorize(cookie,enterprise,Business.Access.READ);
            // Serialize task allocation even when there is no task row to lock yet.
            jdbc.queryForList("select enterprise_id from enterprise where tenant_id=? and enterprise_id=? for update",
                    actor.tenant(),enterprise);
            Map<String,Object> task;
            if (requested!=null) task=owned(cookie,enterprise,requested,true);
            else {
                var active = jdbc.queryForList("select * from agent_task where tenant_id=? and account_id=? and enterprise_id=? and "
                        + ACTIVE + " for update",actor.tenant(),actor.id(),enterprise);
                if (active.isEmpty()) {
                    UUID id = UUID.randomUUID();
                    jdbc.update("insert into agent_task(task_id,account_id,tenant_id,enterprise_id,accounting_period,status) "
                            + "values (?,?,?,?,?,'waiting_user')",id,actor.id(),actor.tenant(),enterprise,ChatScopes.month(period));
                    task=lock(id);
                } else task=active.getFirst();
            }
            if (!Set.of("running","waiting_user","blocked").contains(task.get("status"))) throw conflict("task_closed");
            if (busy(task)) throw conflict("conversation_busy");
            recoverExpired(task);
            boolean readOnly=!task.get("accounting_period").toString().equals(ChatScopes.month(period).toString());
            if (!readOnly && task.get("proposal_id")!=null && !scopeId.equals(task.get("bound_scope_id")))
                throw conflict("resume_required");
            var scoped=scopes.activate(cookie,scopeId,enterprise,period);
            UUID id=(UUID)task.get("task_id");
            String target=target(redaction.text(message,cookie));
            if (!readOnly && target!=null && task.get("object_id")!=null && !target.equals(task.get("object_id")))
                throw conflict("target_correction_required");
            if (!readOnly && target!=null)
                jdbc.update("update agent_task set object_id=? where task_id=?",target,id);
            jdbc.update("update agent_task set active_turn_id=?,lease_until=now()+interval '30 seconds',"
                    + "bound_scope_id=case when ? then bound_scope_id else ? end,"
                    + "status=case when ? then status when status='blocked' then status else 'running' end where task_id=?",
                    turn,readOnly,scoped.scope_id(),readOnly,id);
            UUID messageId=UUID.randomUUID();
            jdbc.update("insert into agent_message(message_id,task_id,turn_id,role,content,scope_generation,accounting_period,task_context)"
                    + " values (?,?,?,'user',?,?,?,?)",messageId,id,turn,redaction.text(message,cookie),scoped.generation(),
                    ChatScopes.month(period),!readOnly);
            jdbc.update("insert into agent_trace(turn_id,task_id,status) values (?,?,'running')",turn,id);
            return scoped.forTurn(id,turn,readOnly);
        });
        if (!Boolean.TRUE.equals(redis.opsForValue().setIfAbsent(key(lease),turn.toString(),Duration.ofSeconds(30)))) {
            abort(lease,"conversation_busy");
            throw conflict("conversation_busy");
        }
        return lease;
    }

    ScheduledFuture<?> renew(ChatScopes.Lease lease) {
        return renewal.scheduleAtFixedRate(() -> {
            try {
                Long owned=redis.execute(RENEW,List.of(key(lease)),lease.turn_id().toString());
                if (Long.valueOf(1).equals(owned))
                    jdbc.update("update agent_task set lease_until=now()+interval '30 seconds' where task_id=? and active_turn_id=?",
                            lease.task_id(),lease.turn_id());
            } catch (RuntimeException ignored) { /* Fencing rejects further writes when renewal is lost. */ }
        },8,8,TimeUnit.SECONDS);
    }

    void checkTurn(ChatScopes.Lease lease,boolean write) {
        if (lease.task_id()==null || lease.turn_id()==null) throw conflict("turn_required");
        var row=lock(lease.task_id());
        if (!lease.turn_id().equals(row.get("active_turn_id")) || !busy(row)
                || !lease.turn_id().toString().equals(redis.opsForValue().get(key(lease))))
            throw conflict("turn_expired");
        if (write && lease.read_only()) throw conflict("period_read_only");
    }

    Object context(ChatScopes.Lease lease) {
        var row=owned(lease.session(),lease.enterprise_id(),lease.task_id(),false);
        List<Map<String,Object>> history=history(row,lease.period(),lease.turn_id());
        var pending=lease.read_only() ? List.<Map<String,Object>>of() : jdbc.queryForList("""
            select m.content from agent_message m where m.task_id=? and m.role='user' and m.task_context
            and m.created_at>=? and not exists (select 1 from agent_memory_invalidation i where i.message_id=m.message_id)
            order by m.created_at desc,message_id desc limit 8
            """,lease.task_id(),row.get("context_since"));
        List<String> values=new ArrayList<>(pending.stream().map(m->m.get("content").toString()).toList());
        Collections.reverse(values);
        if (lease.read_only())
            values.add(jdbc.queryForObject("select content from agent_message where turn_id=? and role='user'",
                    String.class,lease.turn_id()));
        var memories=jdbc.queryForList("""
            select memory_id,revision,source_message_id,content from agent_memory
            where account_id=? and tenant_id=? and active and (enterprise_id=? or enterprise_id is null)
            order by created_at,memory_id
            """,row.get("account_id"),row.get("tenant_id"),lease.enterprise_id());
        Map<String,Object> out=new LinkedHashMap<>();
        out.put("task",taskJson(row));
        out.put("matches_period",!lease.read_only());
        out.put("history",history);
        out.put("pending",lease.read_only() ? null : row.get("object_id"));
        out.put("pending_messages",values);
        out.put("proposal",lease.read_only() ? null : proposalPointer(row));
        out.put("memories",memories);
        out.put("message_id",jdbc.queryForObject("select message_id from agent_message where turn_id=? and role='user'",
                UUID.class,lease.turn_id()));
        return redaction.clean(out,lease.session());
    }

    List<Map<String,Object>> history(Map<String,Object> task,String period,UUID excludeTurn) {
        var rows=jdbc.queryForList("""
            select m.message_id,m.role,m.content,m.turn_id from agent_message m join agent_task t using(task_id)
            where t.account_id=? and t.tenant_id=? and t.enterprise_id=? and m.accounting_period=?
            and m.task_context and (?::uuid is null or m.turn_id<>?::uuid) and not exists (
                select 1 from agent_memory_invalidation i join agent_message source on source.message_id=i.message_id
                join agent_memory pref using(memory_id)
                where source.turn_id=m.turn_id and pref.account_id=t.account_id and pref.tenant_id=t.tenant_id)
            order by m.created_at desc,m.message_id desc limit 8
            """,task.get("account_id"),task.get("tenant_id"),task.get("enterprise_id"),ChatScopes.month(period),excludeTurn,excludeTurn);
        Collections.reverse(rows);
        return rows;
    }

    @Transactional
    public void finish(ChatScopes.Lease lease,JsonNode body,String callToken) {
        Map<String,Object> task=lock(lease.task_id());
        if (!lease.turn_id().equals(task.get("active_turn_id"))) return;
        String status=body.path("status").asText("incomplete");
        if (!Set.of("succeeded","needs_input","pending_confirmation","incomplete").contains(status)) status="incomplete";
        String reply=redaction.text(body.path("reply").asText("本轮未完成，请重新核实。"),lease.session(),callToken);
        if (reply.length()>64000) throw bad();
        JsonNode clean=redaction.clean(body,lease.session(),callToken);
        if (clean.toString().length()>1000000) throw bad();
        jdbc.update("insert into agent_message(message_id,task_id,turn_id,role,content,scope_generation,accounting_period,task_context)"
                + " values (?,?,?,'assistant',?,?,?,?) on conflict(turn_id,role) do nothing",
                UUID.randomUUID(),lease.task_id(),lease.turn_id(),reply,lease.generation(),ChatScopes.month(lease.period()),!lease.read_only());
        jdbc.update("update agent_trace set status=?,steps=?,summary=?::jsonb,model_name=?,duration_ms=?,context_bytes=?,completed_at=now() "
                + "where turn_id=? and status='running'",status,Math.min(6,Math.max(0,body.path("steps").asInt())),
                clean.toString(),redaction.text(body.path("model_name").asText()),Math.max(0,body.path("duration_ms").asLong()),
                Math.max(0,body.path("context_bytes").asInt()),lease.turn_id());
        if (!lease.read_only()) {
            boolean unknown=jdbc.queryForObject("select count(*) from agent_operation where task_id=? and state in ('pending','unknown')",
                    Integer.class,lease.task_id())>0;
            String next=unknown ? "blocked" : status.equals("succeeded") && task.get("question")==null && task.get("proposal_id")==null
                    ? "succeeded" : "waiting_user";
            String question=task.get("question")==null && status.equals("needs_input") ? reply : (String)task.get("question");
            if (status.equals("pending_confirmation")) question=null;
            jdbc.update("update agent_task set status=?,question=?,revision=revision+1,updated_at=now() where task_id=?",
                    next,question,lease.task_id());
        }
        jdbc.update("update agent_task set active_turn_id=null,lease_until=null where task_id=? and active_turn_id=?",
                lease.task_id(),lease.turn_id());
    }

    void abort(ChatScopes.Lease lease,String code) {
        transaction.executeWithoutResult(status -> finish(lease,json.valueToTree(Map.of(
                "status","incomplete","reply","本轮中断，待办已保留。","guard_codes",List.of(code))),""));
    }

    void release(ChatScopes.Lease lease) {
        redis.execute(RELEASE,List.of(key(lease)),lease.turn_id().toString());
    }

    private String key(ChatScopes.Lease lease) { return "agent-task-lock:"+lease.task_id(); }

    private Object view(Map<String,Object> row,String period) {
        Map<String,Object> out=new LinkedHashMap<>();
        out.put("task",taskJson(row));
        out.put("matches_period",row.get("accounting_period").toString().equals(ChatScopes.month(period).toString()));
        out.put("messages",history(row,period,null));
        out.put("proposal",proposalSummary(row));
        out.put("requires_resume",row.get("proposal_id")!=null && jdbc.queryForObject(
                "select count(*) from invoice_proposal p join agent_chat_scope s on s.scope_id=p.chat_scope_id "
                + "where p.proposal_id=? and p.chat_generation=s.generation and p.chat_scope_id=?",
                Integer.class,row.get("proposal_id"),row.get("bound_scope_id"))==0);
        out.put("unfinished_operations",jdbc.queryForList("select operation_id,kind,state,object_id,proposal_id,idempotency_key "
                + "from agent_operation where task_id=? and state in ('pending','unknown') order by created_at",row.get("task_id")));
        return out;
    }

    private Object taskJson(Map<String,Object> row) {
        var copy=new LinkedHashMap<>(row);
        copy.remove("lease_until");
        copy.remove("active_turn_id");
        copy.remove("context_since");
        copy.put("accounting_period",row.get("accounting_period").toString().substring(0,7));
        return copy;
    }

    private Object proposalPointer(Map<String,Object> task) {
        return task.get("proposal_id")==null ? null : Map.of("invoice_id",task.get("object_id"),"proposal_id",task.get("proposal_id"));
    }

    Object proposalSummary(Map<String,Object> task) {
        if (task.get("proposal_id")==null) return null;
        var proposals=jdbc.queryForList("select proposal_id,expected_revision,changes::text as changes,rule_ref::text as rule_ref,"
                + "before_record::text as before_record "
                + "from invoice_proposal where proposal_id=? and task_id=?",task.get("proposal_id"),task.get("task_id"));
        if (proposals.isEmpty()) return null;
        var proposal=proposals.getFirst();
        proposal.put("changes",parse(proposal.get("changes").toString()));
        proposal.put("rule_ref",parse(proposal.get("rule_ref").toString()));
        Object before=proposal.remove("before_record");
        if (before!=null) before=parse(before.toString());
        else before=jdbc.queryForMap("select purpose,note,revision from product_invoice where tenant_id=? and enterprise_id=? and invoice_id=?",
                    task.get("tenant_id"),task.get("enterprise_id"),task.get("object_id"));
        return Map.of("invoice_id",task.get("object_id"),"before",before,"proposal",proposal);
    }

    static String target(String text) {
        var matcher=Pattern.compile("(?<![A-Za-z0-9_-])[A-Za-z]+[0-9]+(?:-[A-Za-z0-9_-]+)?(?![A-Za-z0-9_-])").matcher(text);
        Set<String> ids=new HashSet<>();
        while (matcher.find()) if (!matcher.group().matches("(?i)C[0-9]+")) ids.add(matcher.group());
        return ids.size()==1 ? ids.iterator().next() : null;
    }

    JsonNode parse(String value) {
        try { return json.readTree(value); } catch (Exception error) { throw new IllegalStateException(error); }
    }
    static ResponseStatusException conflict(String code) { return new ResponseStatusException(HttpStatus.CONFLICT,code); }
    static ResponseStatusException missing() { return new ResponseStatusException(HttpStatus.NOT_FOUND); }
    static ResponseStatusException bad() { return new ResponseStatusException(HttpStatus.BAD_REQUEST,"Invalid task request"); }
}
