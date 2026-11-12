package local.tax;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;

@Service
public class RecentOperations {
    private final Business sessions;
    private final JdbcTemplate jdbc;
    private final ObjectMapper json;
    public RecentOperations(Business sessions,JdbcTemplate jdbc,ObjectMapper json) {
        this.sessions=sessions;this.jdbc=jdbc;this.json=json;
    }
    List<Map<String,Object>> read(ChatScopes.Lease lease,String invoice,int limit) {
        var actor=sessions.authorize(lease.session(),lease.enterprise_id(),Business.Access.READ);
        if (limit<1 || limit>5 || invoice!=null && !invoice.matches("[A-Za-z0-9_-]{1,64}")) throw AgentTasks.bad();
        var rows=jdbc.queryForList("""
            select p.proposal_id,p.invoice_id,p.changes::text as changes,
                coalesce(a.operation_id,o.operation_id) as operation_id,
                case when a.proposal_id is not null then 'confirmed'
                     when o.state in ('unknown','pending') then o.state
                     when p.chat_generation<>s.generation then 'expired'
                     else 'pending_confirmation' end as state,
                coalesce(a.saved_at,o.updated_at,p.created_at) as occurred_at
            from invoice_proposal p join agent_task t on t.task_id=p.task_id
            join agent_chat_scope s on s.scope_id=p.chat_scope_id
            join product_invoice i on i.tenant_id=p.tenant_id and i.enterprise_id=p.enterprise_id and i.invoice_id=p.invoice_id
            left join invoice_audit a on a.proposal_id=p.proposal_id and a.tenant_id=p.tenant_id
            left join lateral (select operation_id,state,updated_at from agent_operation where proposal_id=p.proposal_id
                order by updated_at desc limit 1) o on true
            where p.tenant_id=? and p.enterprise_id=? and t.account_id=? and s.account_id=?
                and i.accounting_period=? and (?::text is null or p.invoice_id=?)
            order by coalesce(a.saved_at,o.updated_at,p.created_at) desc,p.proposal_id desc limit ?
            """,actor.tenant(),lease.enterprise_id(),actor.id(),actor.id(),ChatScopes.month(lease.period()),invoice,invoice,limit);
        rows.forEach(row->{try{row.put("changes",json.readTree(row.get("changes").toString()));}
            catch(Exception error){throw new IllegalStateException(error);}});
        return rows;
    }
}
