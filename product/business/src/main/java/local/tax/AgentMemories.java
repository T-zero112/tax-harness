package local.tax;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.*;

@RestController
public class AgentMemories {
    private static final String BASE="/api/enterprises/{enterprise}/chat/memories";
    private final Business sessions;
    private final JdbcTemplate jdbc;
    private final AgentRedaction redaction;

    public AgentMemories(Business sessions,JdbcTemplate jdbc,AgentRedaction redaction) {
        this.sessions=sessions;
        this.jdbc=jdbc;
        this.redaction=redaction;
    }

    record MemoryInput(String content,UUID source_message_id,boolean user_wide,long expected_revision) {}

    @GetMapping(BASE)
    Object list(@CookieValue(name=Business.COOKIE,required=false) String cookie,@PathVariable String enterprise) {
        var actor=sessions.authorize(cookie,enterprise,Business.Access.READ);
        return jdbc.queryForList("select * from agent_memory where account_id=? and tenant_id=? and active "
                + "and (enterprise_id=? or enterprise_id is null) order by created_at,memory_id",actor.id(),actor.tenant(),enterprise);
    }

    @PostMapping(BASE)
    @Transactional
    public Object create(@CookieValue(name=Business.COOKIE,required=false) String cookie,
                         @PathVariable String enterprise,@RequestBody MemoryInput body) {
        var actor=sessions.authorize(cookie,enterprise,Business.Access.READ);
        content(body.content());
        source(actor,enterprise,body.source_message_id());
        UUID id=UUID.randomUUID();
        jdbc.update("insert into agent_memory(memory_id,account_id,tenant_id,enterprise_id,content,source_message_id)"
                + " values (?,?,?,?,?,?)",id,actor.id(),actor.tenant(),body.user_wide()?null:enterprise,
                redaction.text(body.content(),cookie),body.source_message_id());
        audit(id,actor.id(),"created",1,body,cookie);
        return owned(actor,enterprise,id,false);
    }

    @PostMapping(BASE+"/{id}/correct")
    @Transactional
    public Object correct(@CookieValue(name=Business.COOKIE,required=false) String cookie,
                          @PathVariable String enterprise,@PathVariable UUID id,@RequestBody MemoryInput body) {
        content(body.content());
        return revise(cookie,enterprise,id,body,false);
    }

    @DeleteMapping(BASE+"/{id}")
    @Transactional
    public Object delete(@CookieValue(name=Business.COOKIE,required=false) String cookie,
                         @PathVariable String enterprise,@PathVariable UUID id,@RequestBody MemoryInput body) {
        return revise(cookie,enterprise,id,body,true);
    }

    @GetMapping(BASE+"/{id}/audit")
    Object audit(@CookieValue(name=Business.COOKIE,required=false) String cookie,
                 @PathVariable String enterprise,@PathVariable UUID id) {
        var actor=sessions.authorize(cookie,enterprise,Business.Access.READ);
        owned(actor,enterprise,id,false);
        return jdbc.queryForList("select event_id,action,revision,payload::text as payload,created_at "
                + "from agent_memory_audit where memory_id=? order by revision",id);
    }

    private Object revise(String cookie,String enterprise,UUID id,MemoryInput body,boolean deleting) {
        var actor=sessions.authorize(cookie,enterprise,Business.Access.READ);
        var row=owned(actor,enterprise,id,true);
        if (!Boolean.TRUE.equals(row.get("active")) || ((Number)row.get("revision")).longValue()!=body.expected_revision())
            throw AgentTasks.conflict("revision_conflict");
        if (body.source_message_id()!=null) source(actor,enterprise,body.source_message_id());
        long revision=body.expected_revision()+1;
        jdbc.update("insert into agent_memory_invalidation(memory_id,message_id,revision) values (?,?,?)",
                id,row.get("source_message_id"),revision);
        jdbc.update("update agent_memory set content=?,active=?,revision=?,source_message_id=? where memory_id=?",
                deleting ? "" : redaction.text(body.content(),cookie),!deleting,revision,
                body.source_message_id()==null ? row.get("source_message_id") : body.source_message_id(),id);
        audit(id,actor.id(),deleting ? "deleted":"corrected",revision,body,cookie);
        return owned(actor,enterprise,id,false);
    }

    private Map<String,Object> owned(Accounts.Identity actor,String enterprise,UUID id,boolean lock) {
        return jdbc.queryForList("select * from agent_memory where memory_id=? and account_id=? and tenant_id=? "
                + "and (enterprise_id=? or enterprise_id is null)"+(lock?" for update":""),
                id,actor.id(),actor.tenant(),enterprise).stream().findFirst().orElseThrow(AgentTasks::missing);
    }

    private void source(Accounts.Identity actor,String enterprise,UUID id) {
        if (id==null || jdbc.queryForObject("select count(*) from agent_message m join agent_task t using(task_id) "
                + "where m.message_id=? and m.role='user' and t.account_id=? and t.tenant_id=? and t.enterprise_id=?",
                Integer.class,id,actor.id(),actor.tenant(),enterprise)!=1) throw AgentTasks.missing();
    }

    private void content(String content) {
        if (content==null || content.isBlank() || content.length()>1000) throw AgentTasks.bad();
    }

    private void audit(UUID id,UUID actor,String action,long revision,Object body,String cookie) {
        JsonNode clean=redaction.clean(body,cookie);
        jdbc.update("insert into agent_memory_audit(event_id,memory_id,actor_id,action,revision,payload) values (?,?,?,?,?,?::jsonb)",
                UUID.randomUUID(),id,actor,action,revision,clean.toString());
    }
}
