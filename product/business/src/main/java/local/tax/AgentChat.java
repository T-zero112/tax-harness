package local.tax;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.*;
import org.springframework.http.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

@RestController
public class AgentChat {
    private final Business sessions;
    private final ChatScopes scopes;
    private final Invoices invoices;
    private final Rules rules;
    private final JdbcTemplate jdbc;
    private final ObjectMapper json;
    private final AgentTasks tasks;
    private final AgentOperations operations;
    private final AgentRedaction redaction;
    private final RecentOperations recent;
    private final HttpClient agent = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();

    public AgentChat(Business sessions, ChatScopes scopes, Invoices invoices, Rules rules,
                     JdbcTemplate jdbc, ObjectMapper json, AgentTasks tasks, AgentOperations operations, AgentRedaction redaction, RecentOperations recent) {
        this.sessions=sessions; this.scopes=scopes; this.invoices=invoices;
        this.rules=rules; this.jdbc=jdbc; this.json=json;
        this.tasks=tasks;
        this.operations=operations;
        this.redaction=redaction;
        this.recent=recent;
    }

    record Turn(UUID session_id, String period, String message, UUID task_id) {}
    record Tool(String call_token, JsonNode arguments) {}

    @PostMapping("/api/enterprises/{enterprise}/chat/scope")
    Map<String,Object> scope(@CookieValue(name=Business.COOKIE,required=false) String cookie,
                            @PathVariable String enterprise, @RequestBody Turn body) {
        if (body.session_id()==null) throw bad();
        var lease=scopes.activate(cookie,body.session_id(),enterprise,body.period());
        return Map.of("session_id",lease.scope_id(),"generation",lease.generation());
    }

    @PostMapping(value="/api/enterprises/{enterprise}/chat",produces=MediaType.TEXT_EVENT_STREAM_VALUE)
    ResponseEntity<StreamingResponseBody> turn(@CookieValue(name=Business.COOKIE,required=false) String cookie,
            @PathVariable String enterprise,@RequestBody Turn body) throws Exception {
        if (body.session_id()==null || body.message()==null || body.message().isBlank() || body.message().length()>4000)
            throw bad();
        var lease=tasks.begin(cookie,enterprise,body.period(),body.task_id(),body.session_id(),body.message());
        String callToken=scopes.issue(lease);
        var renewal=tasks.renew(lease);
        var payload=Map.of("session_id",body.session_id(),"task_id",lease.task_id(),"turn_id",lease.turn_id(),
                "scope",Map.of("tenant_id",lease.tenant_id(),"username",lease.username(),"enterprise_id",enterprise,
                    "period",body.period(),"generation",lease.generation()),
                "message",redaction.text(body.message(),cookie,callToken),"call_token",callToken,"enterprises",sessions.enterprises(cookie));
        HttpResponse<InputStream> response;
        try {
            var request=HttpRequest.newBuilder(URI.create("http://agent:8000/internal/chat/turn"))
                .timeout(Duration.ofSeconds(780)).header("Authorization","Bearer "+System.getenv("PRODUCT_AGENT_TOKEN"))
                .header("Content-Type","application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json.writeValueAsString(payload))).build();
            response=agent.send(request,HttpResponse.BodyHandlers.ofInputStream());
        } catch (Exception error) {
            renewal.cancel(false);
            tasks.abort(lease,"agent_unavailable");
            tasks.release(lease);
            scopes.release(callToken);
            throw new ResponseStatusException(HttpStatus.BAD_GATEWAY,"Agent unavailable");
        }
        if (response.statusCode()!=200) {
            response.body().close();
            renewal.cancel(false);
            tasks.abort(lease,"agent_rejected");
            tasks.release(lease);
            scopes.release(callToken);
            throw new ResponseStatusException(response.statusCode()==503 ? HttpStatus.SERVICE_UNAVAILABLE:HttpStatus.BAD_GATEWAY,
                response.statusCode()==503 ? "Model is not configured":"Agent unavailable");
        }
        StreamingResponseBody stream=output -> {
            try (InputStream input=response.body()) {
                byte[] buffer=new byte[4096];
                int size;
                while ((size=input.read(buffer))!=-1) { output.write(buffer,0,size); output.flush(); }
            } finally {
                renewal.cancel(false);
                tasks.abort(lease,"stream_interrupted");
                tasks.release(lease);
                scopes.release(callToken);
            }
        };
        return ResponseEntity.ok().contentType(MediaType.TEXT_EVENT_STREAM).cacheControl(CacheControl.noCache()).body(stream);
    }

    @PostMapping("/internal/agent/{tool}")
    Object tool(@RequestHeader(value="Authorization",defaultValue="") String authorization,
                @PathVariable String tool,@RequestBody Tool request) throws Exception {
        ChatScopes.Lease lease=scopes.resolve(authorization,request.call_token(),!tool.equals("trace"));
        JsonNode args=request.arguments();
        if (args==null || !args.isObject()) throw bad();
        if (tool.equals("trace")) {
            tasks.finish(lease,args,request.call_token());
            tasks.release(lease);
            return Map.of("status","succeeded","data",Map.of("stored",true));
        }
        tasks.checkTurn(lease,false);
        if (tool.equals("context")) return Map.of("status","succeeded","data",tasks.context(lease));
        if (tool.equals("invalidate_scope")) {
            scopes.invalidate(lease);
            return Map.of("status","succeeded","data",Map.of("invalidated",true));
        }
        String tenant=lease.tenant_id(), enterprise=lease.enterprise_id(), cookie=lease.session();
        var period=ChatScopes.month(lease.period());
        Object data;
        String invoice=args.path("invoice_id").asText("");
        if (Set.of("get_invoice","check_purpose","create_proposal","get_operation").contains(tool)) {
            if (!invoice.matches("[A-Za-z0-9_-]{1,64}")) throw bad();
            var current=invoices.get(cookie,enterprise,invoice);
            if (!current.accounting_period().equals(period)) throw new ResponseStatusException(HttpStatus.NOT_FOUND);
            data=switch (tool) {
                case "get_invoice" -> Map.of("record",current);
                case "check_purpose" -> invoices.review(cookie,enterprise,invoice);
                case "create_proposal" -> createProposal(lease,invoice,args,current);
                default -> operation(lease,invoice,args.path("proposal_id").asText());
            };
        } else if (tool.equals("list_recent_operations")) {
            data=Map.of("operations",recent.read(lease,args.hasNonNull("invoice_id")?invoice:null,args.path("limit").asInt(5)));
        } else if (tool.equals("search_rules")) {
            data=Map.of("rules",rules.search(cookie,enterprise,lease.period(),args.path("query").asText("发票用途")));
        } else if (tool.equals("list_invoices")) {
            List<Object> params=new ArrayList<>(List.of(tenant,enterprise,period));
            String where="tenant_id=? and enterprise_id=? and accounting_period=?";
            for (String field:List.of("invoice_id","seller","item_name")) {
                if (args.hasNonNull(field)) {
                    String value=args.path(field).asText();
                    if (value.isBlank() || value.length()>120) throw bad();
                    where+=" and "+field+(field.equals("invoice_id") ? "=?" : " ilike ? escape '!'");
                    params.add(field.equals("invoice_id") ? value : "%"+value.replace("!","!!").replace("%","!%").replace("_","!_")+"%");
                }
            }
            var totals=jdbc.queryForMap("select count(*) as count, count(*) filter(where amount_cents is null) as unknown_amounts,"
                +"coalesce(sum(amount_cents),0)::text as invoice_cents from product_invoice where "+where,params.toArray());
            var records=jdbc.queryForList("select invoice_id,accounting_period,purpose,note,revision,amount_cents::text as amount_cents,"
                +"seller,item_name from product_invoice where "+where+" order by invoice_id limit 101",params.toArray());
            data=Map.of("records",records.size()>100 ? records.subList(0,100):records,"has_more",records.size()>100,"summary",totals);
        } else if (tool.equals("list_bank_transactions")) {
            List<Object> params=new ArrayList<>(List.of(tenant,enterprise,period));
            String where="tenant_id=? and enterprise_id=? and accounting_period=?";
            if (args.hasNonNull("direction")) {
                String direction=args.path("direction").asText();
                if (!Set.of("income","expense").contains(direction)) throw bad();
                where+=" and direction=?"; params.add(direction);
            }
            if (args.hasNonNull("transaction_id")) { where+=" and transaction_id=?"; params.add(args.path("transaction_id").asText()); }
            var totals=jdbc.queryForMap("select coalesce(sum(amount_cents) filter(where direction='income'),0)::text as income_cents,"
                +"coalesce(sum(amount_cents) filter(where direction='expense'),0)::text as expense_cents"
                +" from product_bank_transaction where "+where,params.toArray());
            var rows=jdbc.queryForList("select transaction_id,direction,amount_cents::text as amount_cents,counterparty,note"
                +" from product_bank_transaction where "+where+" order by transaction_id limit 101",params.toArray());
            data=Map.of("records",rows.size()>100 ? rows.subList(0,100):rows,"has_more",rows.size()>100,"summary",totals);
        } else throw new ResponseStatusException(HttpStatus.BAD_REQUEST,"Unknown tool");
        Map<String,Object> result=new LinkedHashMap<>();
        result.put("status","succeeded");
        result.put("data",data);
        result.put("error",null);
        result.put("object_id",invoice);
        String operationId=tool.equals("create_proposal") ? json.valueToTree(data).path("operation_id").asText(null)
                : tool.equals("get_operation") ? json.valueToTree(data).path("operation").path("operation_id").asText(null) : null;
        result.put("operation_id",operationId);
        result.put("scope",Map.of("enterprise_id",enterprise,"period",lease.period()));
        return result;
    }

    private Object createProposal(ChatScopes.Lease lease,String invoice,JsonNode args,Invoices.Invoice current) throws Exception {
        UUID operation=operations.beginProposal(lease,invoice);
        try {
            JsonNode proposal=invoices.proposeForChat(lease.session(),lease.enterprise_id(),invoice,args.path("changes"),lease,operation);
            if (proposal.path("status").asText().equals("manual_review")) operations.failed(operation);
            return Map.of("proposal",proposal,"before",current,"invoice_id",invoice,"operation_id",operation);
        } catch (Exception error) {
            operations.failed(operation);
            throw error;
        }
    }

    private Object operation(ChatScopes.Lease lease,String invoice,String proposal) {
        UUID id;
        try { id=UUID.fromString(proposal); } catch (RuntimeException error) { throw bad(); }
        var actor=sessions.authorize(lease.session(),lease.enterprise_id(),Business.Access.READ);
        var rows=jdbc.queryForList("select p.proposal_id, p.expected_revision, p.changes, "
                +"coalesce(a.operation_id,(select o.operation_id from agent_operation o where o.proposal_id=p.proposal_id "
                +"and o.kind='proposal' order by o.created_at limit 1)) as operation_id, "
                +"case when a.proposal_id is not null then 'confirmed' when p.chat_scope_id is not null and "
                +"p.chat_generation<>s.generation then 'expired' else 'pending' end as state "
                +"from invoice_proposal p join agent_task t on t.task_id=p.task_id left join invoice_audit a on p.proposal_id=a.proposal_id "
                +"left join agent_chat_scope s on p.chat_scope_id=s.scope_id "
                +"where p.tenant_id=? and p.enterprise_id=? and p.invoice_id=? and p.proposal_id=? and t.account_id=?",
                lease.tenant_id(),lease.enterprise_id(),invoice,id,actor.id());
        if (rows.isEmpty()) throw new ResponseStatusException(HttpStatus.NOT_FOUND);
        return Map.of("operation",rows.getFirst(),"record",invoices.get(lease.session(),lease.enterprise_id(),invoice));
    }

    private static ResponseStatusException bad() { return new ResponseStatusException(HttpStatus.BAD_REQUEST,"Invalid chat request"); }
}
