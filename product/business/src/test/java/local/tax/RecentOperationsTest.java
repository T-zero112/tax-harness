package local.tax;
import static org.mockito.Mockito.*;
import static org.junit.jupiter.api.Assertions.*;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.JdbcTemplate;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.UUID;
import java.time.LocalDate;
class RecentOperationsTest {
 @Test void limitsAndOwnershipComeFromSession() {
  var sessions=mock(Business.class);var jdbc=mock(JdbcTemplate.class);var actor=UUID.randomUUID();
  when(sessions.authorize("test-session","C001",Business.Access.READ)).thenReturn(new Accounts.Identity(actor,"alpha","alpha"));
  var recent=new RecentOperations(sessions,jdbc,new ObjectMapper());
  var lease=new ChatScopes.Lease("test-session",UUID.randomUUID(),"alpha","alpha","C001","2026-09",1,UUID.randomUUID(),UUID.randomUUID(),false);
  recent.read(lease,null,5);
  verify(jdbc).queryForList(contains("t.account_id=?"),eq("alpha"),eq("C001"),eq(actor),eq(actor),eq(LocalDate.of(2026,9,1)),isNull(),isNull(),eq(5));
  assertThrows(org.springframework.web.server.ResponseStatusException.class,()->recent.read(lease,null,6));
 }
}
