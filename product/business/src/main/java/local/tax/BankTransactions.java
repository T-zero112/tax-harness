package local.tax;

import java.time.LocalDate;
import java.time.YearMonth;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.List;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;

@RestController
@RequestMapping("/api/enterprises/{enterpriseId}/bank-transactions")
public class BankTransactions {
    private final Business sessions;
    private final JdbcTemplate jdbc;

    public BankTransactions(Business sessions, JdbcTemplate jdbc) {
        this.sessions = sessions;
        this.jdbc = jdbc;
    }

    record Transaction(String transaction_id, LocalDate transaction_date, String direction,
                       String amount_cents, String counterparty, String note) {}
    record Transactions(String enterprise_id, String accounting_period, List<Transaction> records,
                        boolean has_more) {}

    @GetMapping
    Transactions list(@CookieValue(name = Business.COOKIE, required = false) String token,
                      @PathVariable String enterpriseId, @RequestParam String period,
                      @RequestParam(name = "transaction_id", required = false) String transactionId,
                      @RequestParam(defaultValue = "0") int offset) {
        String tenant = sessions.authorize(token, enterpriseId, Business.Access.READ).tenant();
        LocalDate month;
        try {
            if (!period.matches("[0-9]{4}-[0-9]{2}") || period.startsWith("0000")) throw invalid();
            month = YearMonth.parse(period).atDay(1);
        } catch (DateTimeParseException error) {
            throw invalid();
        }
        if ((transactionId != null && !transactionId.matches("[A-Za-z0-9_-]{1,64}")) || offset < 0) throw invalid();

        List<Object> parameters = new ArrayList<>(List.of(tenant, enterpriseId, month));
        String sql = """
                select transaction_id, transaction_date, direction, amount_cents, counterparty, note
                from product_bank_transaction where tenant_id = ? and enterprise_id = ? and accounting_period = ?
                """;
        if (transactionId != null) {
            sql += " and transaction_id = ?";
            parameters.add(transactionId);
        }
        parameters.add(offset);
        List<Transaction> records = jdbc.query(sql + " order by transaction_date, transaction_id limit 101 offset ?",
                (row, index) -> new Transaction(row.getString("transaction_id"),
                        row.getObject("transaction_date", LocalDate.class), row.getString("direction"),
                        row.getString("amount_cents"), row.getString("counterparty"), row.getString("note")),
                parameters.toArray());
        boolean more = records.size() > 100;
        return new Transactions(enterpriseId, period, more ? records.subList(0, 100) : records, more);
    }

    private static ResponseStatusException invalid() {
        return new ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid transaction filters");
    }
}
