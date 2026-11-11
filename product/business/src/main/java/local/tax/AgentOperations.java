package local.tax;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.*;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;

@RestController
public class AgentOperations {
    private final AgentTasks tasks;
    private final Business sessions;
    private final ChatScopes scopes;
    private final JdbcTemplate jdbc;
    private final ObjectMapper json;
    private final TransactionTemplate transaction;

    public AgentOperations(AgentTasks tasks,Business sessions,ChatScopes scopes,JdbcTemplate jdbc,
                           ObjectMapper json,PlatformTransactionManager manager) {
        this.tasks=tasks;
        this.sessions=sessions;
        this.scopes=scopes;
        this.jdbc=jdbc;
        this.json=json;
        transaction=new TransactionTemplate(manager);
    }

    UUID beginProposal(ChatScopes.Lease lease,String invoice) {
        return transaction.execute(status -> {
            sessions.authorize(lease.session(),lease.enterprise_id(),Business.Access.WRITE);
            tasks.checkTurn(lease,true);
            tasks.noUnknown(lease.task_id(),null);
            var task=tasks.lock(lease.task_id());
            if (task.get("object_id")==null || !invoice.equals(task.get("object_id")))
                throw AgentTasks.conflict("proposal_target_unverified");
            var old=jdbc.queryForList("select operation_id,state from agent_operation where task_id=? and turn_id=? and kind='proposal'",
                    lease.task_id(),lease.turn_id());
            if (!old.isEmpty()) throw AgentTasks.conflict("proposal_already_attempted");
            UUID id=UUID.randomUUID();
            jdbc.update("insert into agent_operation(operation_id,task_id,turn_id,kind,state,object_id) values (?,?,?,'proposal','pending',?)",
                    id,lease.task_id(),lease.turn_id(),invoice);
            return id;
        });
    }

    void checkProposalTurn(ChatScopes.Lease lease) { tasks.checkTurn(lease,true); }

    void attachProposal(ChatScopes.Lease lease,UUID operation,UUID proposal) {
        tasks.checkTurn(lease,true);
        jdbc.update("update invoice_proposal set task_id=? where proposal_id=?",lease.task_id(),proposal);
        jdbc.update("update agent_operation set state='confirmed',proposal_id=?,result=?::jsonb,updated_at=now() "
                + "where operation_id=? and state='pending'",proposal,
                json.valueToTree(Map.of("proposal_id",proposal)).toString(),operation);
        jdbc.update("update agent_task set proposal_id=?,question=null,status='waiting_user' where task_id=?",
                proposal,lease.task_id());
    }

    void failed(UUID operation) {
        if (operation==null) return;
        transaction.executeWithoutResult(status -> jdbc.update(
                "update agent_operation set state='failed',updated_at=now() where operation_id=? and state='pending'",operation));
    }

    UUID prepareConfirmation(String cookie,String enterprise,String invoice,UUID proposal,UUID key,UUID scopeId) {
        return transaction.execute(status -> {
            var actor=sessions.authorize(cookie,enterprise,Business.Access.WRITE);
            var rows=jdbc.queryForList("select task_id from invoice_proposal where proposal_id=? and tenant_id=? "
                    + "and enterprise_id=? and invoice_id=? and task_id is not null",proposal,actor.tenant(),enterprise,invoice);
            if (rows.isEmpty()) return null;
            UUID taskId=(UUID)rows.getFirst().get("task_id");
            var task=tasks.owned(cookie,enterprise,taskId,true);
            var replay=jdbc.queryForList("select operation_id,state,proposal_id from agent_operation "
                    + "where task_id=? and kind='confirmation' and idempotency_key=?",taskId,key);
            if (!replay.isEmpty() && !proposal.equals(replay.getFirst().get("proposal_id")))
                throw AgentTasks.conflict("idempotency_conflict");
            if (!replay.isEmpty() && replay.getFirst().get("state").equals("confirmed"))
                return (UUID)replay.getFirst().get("operation_id");
            validateConfirmation(task,proposal,scopeId);
            tasks.noUnknown(taskId,null);
            if (!replay.isEmpty()) {
                jdbc.update("update agent_operation set state='pending',updated_at=now() where operation_id=?",
                        replay.getFirst().get("operation_id"));
                return (UUID)replay.getFirst().get("operation_id");
            }
            UUID id=UUID.randomUUID();
            jdbc.update("insert into agent_operation(operation_id,task_id,kind,state,object_id,proposal_id,idempotency_key)"
                    + " values (?,?,'confirmation','pending',?,?,?)",id,taskId,invoice,proposal,key);
            return id;
        });
    }

    void lockConfirmation(UUID operation,UUID proposal,UUID scopeId) {
        if (operation==null) return;
        var op=jdbc.queryForMap("select task_id,state from agent_operation where operation_id=?",operation);
        var task=tasks.lock((UUID)op.get("task_id"));
        if (!op.get("state").equals("confirmed")) {
            validateConfirmation(task,proposal,scopeId);
            tasks.noUnknown((UUID)task.get("task_id"),operation);
        }
    }

    private void validateConfirmation(Map<String,Object> task,UUID proposal,UUID scope) {
        if (!Set.of("waiting_user","blocked").contains(task.get("status")) || !proposal.equals(task.get("proposal_id"))
                || scope==null || !scope.equals(task.get("bound_scope_id")))
            throw AgentTasks.conflict("resume_required");
        if (task.get("active_turn_id")!=null && task.get("lease_until")!=null
                && ((java.sql.Timestamp)task.get("lease_until")).toInstant().isAfter(java.time.Instant.now()))
            throw AgentTasks.conflict("conversation_busy");
    }

    void confirmed(UUID operation,Object receipt) {
        if (operation==null) return;
        var row=jdbc.queryForMap("select task_id from agent_operation where operation_id=?",operation);
        jdbc.update("update agent_operation set state='confirmed',result=?::jsonb,updated_at=now() where operation_id=?",
                json.valueToTree(receipt).toString(),operation);
        jdbc.update("update agent_task set status='succeeded',question=null,revision=revision+1,updated_at=now() where task_id=?",
                row.get("task_id"));
    }

    @PostMapping("/api/enterprises/{enterprise}/chat/tasks/{task}/operations/{operation}/reconcile")
    Object reconcile(@CookieValue(name=Business.COOKIE,required=false) String cookie,
                     @PathVariable String enterprise,@PathVariable UUID task,@PathVariable UUID operation) {
        return transaction.execute(status -> {
            var current=tasks.owned(cookie,enterprise,task,true);
            if (current.get("active_turn_id")!=null && current.get("lease_until")!=null
                    && ((java.sql.Timestamp)current.get("lease_until")).toInstant().isAfter(java.time.Instant.now()))
                throw AgentTasks.conflict("conversation_busy");
            var rows=jdbc.queryForList("select * from agent_operation where operation_id=? and task_id=? for update",operation,task);
            if (rows.isEmpty()) throw AgentTasks.missing();
            var op=rows.getFirst();
            if (!Set.of("unknown","pending").contains(op.get("state")))
                return Map.of("operation_id",operation,"state",op.get("state"));
            // Waiting for the invoice lock proves an earlier transaction is no longer in flight.
            jdbc.queryForList("select invoice_id from product_invoice where tenant_id=? and enterprise_id=? and invoice_id=? for update",
                    current.get("tenant_id"),enterprise,op.get("object_id"));
            var audits=jdbc.queryForList("select proposal_id from invoice_audit where tenant_id=? and idempotency_key=?",
                    current.get("tenant_id"),op.get("idempotency_key"));
            boolean saved=op.get("kind").equals("confirmation") && !audits.isEmpty()
                    && audits.getFirst().get("proposal_id").equals(op.get("proposal_id"));
            boolean proposal=op.get("kind").equals("proposal") && op.get("proposal_id")!=null
                    && jdbc.queryForObject("select count(*) from invoice_proposal where proposal_id=? and task_id=?",
                            Integer.class,op.get("proposal_id"),task)==1;
            String outcome=saved || proposal ? "confirmed" : "failed";
            if (Set.of("unknown","pending").contains(op.get("state")))
                jdbc.update("update agent_operation set state=?,updated_at=now() where operation_id=?",outcome,operation);
            if (saved)
                jdbc.update("update agent_task set status='succeeded',revision=revision+1,updated_at=now() where task_id=?",task);
            else if (jdbc.queryForObject("select count(*) from agent_operation where task_id=? and state in ('pending','unknown')",
                    Integer.class,task)==0)
                jdbc.update("update agent_task set status='waiting_user',revision=revision+1,updated_at=now() where task_id=? and status='blocked'",task);
            return Map.of("operation_id",operation,"state",outcome);
        });
    }
}
