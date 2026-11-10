package local.tax;

import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.*;
import javax.crypto.SecretKeyFactory;
import javax.crypto.spec.PBEKeySpec;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.http.HttpStatus;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.web.server.ResponseStatusException;

@Component
public class Accounts implements ApplicationRunner {
    private static final int ITERATIONS = 600_000;
    private static final SecureRandom RANDOM = new SecureRandom();
    private final JdbcTemplate jdbc;

    public Accounts(JdbcTemplate jdbc) { this.jdbc = jdbc; }

    record Identity(UUID id, String username, String tenant) {}
    record Login(Identity identity, String hash) {}

    @Override
    @Transactional
    public void run(ApplicationArguments args) {
        bootstrap("alpha", "PRODUCT_ALPHA_PASSWORD", "admin");
        bootstrap("beta", "PRODUCT_BETA_PASSWORD", "viewer");
    }

    private void bootstrap(String username, String key, String role) {
        if (Boolean.TRUE.equals(jdbc.queryForObject(
                "select exists(select 1 from product_account where username = ?)", Boolean.class, username))) return;
        String password = System.getenv(key);
        if (password == null || password.isBlank()) throw new IllegalStateException("Missing bootstrap password");
        UUID id = UUID.randomUUID();
        jdbc.update("insert into product_account values (?, ?, ?, true)", id, username, hash(password));
        jdbc.update("insert into tenant_membership values (?, ?)", username, id);
        jdbc.update("""
                insert into enterprise_grant (tenant_id, enterprise_id, account_id, role)
                select tenant_id, enterprise_id, ?, ? from enterprise where tenant_id = ?
                """, id, role, username);
    }

    Identity authenticate(String username, String password) {
        if (!validUsername(username) || password == null || password.length() > 256) throw unauthorized();
        List<Login> matches = jdbc.query("""
                select a.account_id, a.username, a.password_hash, m.tenant_id
                from product_account a join tenant_membership m using (account_id)
                where a.username = ? and a.enabled
                """, (r, i) -> new Login(new Identity(r.getObject(1, UUID.class), r.getString(2), r.getString(4)), r.getString(3)), username);
        Login found = matches.size() == 1 ? matches.getFirst() : null;
        String[] stored = found == null ? null : found.hash().split(":");
        byte[] salt = stored == null ? new byte[16] : Base64.getDecoder().decode(stored[0]);
        byte[] actual = derive(password, salt);
        if (found == null || !MessageDigest.isEqual(actual, Base64.getDecoder().decode(stored[1]))) throw unauthorized();
        return found.identity();
    }

    Identity resolve(UUID account, String tenant) {
        return jdbc.query("""
                select a.account_id, a.username, m.tenant_id
                from product_account a join tenant_membership m using (account_id)
                where a.account_id = ? and m.tenant_id = ? and a.enabled
                """, (r, i) -> new Identity(r.getObject(1, UUID.class), r.getString(2), r.getString(3)), account, tenant)
                .stream().findFirst().orElseThrow(Accounts::unauthorized);
    }

    String role(Identity identity, String enterprise) {
        String lock = TransactionSynchronizationManager.isActualTransactionActive() ? " for share" : "";
        return jdbc.queryForList("""
                select role from enterprise_grant where tenant_id = ? and enterprise_id = ? and account_id = ?
                """ + lock, String.class, identity.tenant(), enterprise, identity.id()).stream().findFirst()
                .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND));
    }

    @Transactional
    public Map<String, String> createMember(Identity admin, String enterprise, String username, String password, String role) {
        if (!validUsername(username) || password == null || password.length() < 12 || password.length() > 256
                || password.isBlank()) throw new ResponseStatusException(HttpStatus.BAD_REQUEST, "Invalid account credentials");
        validateRole(role);
        UUID account = UUID.randomUUID();
        if (jdbc.update("insert into product_account values (?, ?, ?, true) on conflict (username) do nothing",
                account, username, hash(password)) != 1) {
            throw new ResponseStatusException(HttpStatus.CONFLICT, "Username already exists");
        }
        jdbc.update("insert into tenant_membership values (?, ?)", admin.tenant(), account);
        jdbc.update("insert into enterprise_grant values (?, ?, ?, ?)", admin.tenant(), enterprise, account, role);
        return Map.of("username", username, "role", role);
    }

    void updateMember(Identity admin, String enterprise, String username, String role) {
        if (role != null) validateRole(role);
        int updated = role == null
                ? jdbc.update("""
                  delete from enterprise_grant where tenant_id = ? and enterprise_id = ? and role <> 'admin'
                  and account_id = (select account_id from product_account where username = ?)
                  """, admin.tenant(), enterprise, username)
                : jdbc.update("""
                  update enterprise_grant set role = ? where tenant_id = ? and enterprise_id = ? and role <> 'admin'
                  and account_id = (select account_id from product_account where username = ?)
                  """, role, admin.tenant(), enterprise, username);
        if (updated != 1) throw new ResponseStatusException(HttpStatus.NOT_FOUND);
    }

    void grantMember(Identity admin, String enterprise, String username, String role) {
        validateRole(role);
        List<UUID> ids = jdbc.queryForList("""
                select a.account_id from product_account a join tenant_membership m using (account_id)
                where a.username = ? and m.tenant_id = ? and a.enabled
                """, UUID.class, username, admin.tenant());
        if (ids.isEmpty()) throw new ResponseStatusException(HttpStatus.NOT_FOUND);
        if (jdbc.update("insert into enterprise_grant values (?, ?, ?, ?) on conflict do nothing",
                admin.tenant(), enterprise, ids.getFirst(), role) != 1) {
            throw new ResponseStatusException(HttpStatus.CONFLICT, "Account already has an enterprise grant");
        }
    }

    private static boolean validUsername(String username) {
        return username != null && username.matches("[a-z0-9][a-z0-9_-]{2,63}");
    }

    private static void validateRole(String role) {
        if (!"viewer".equals(role) && !"editor".equals(role)) throw new ResponseStatusException(HttpStatus.BAD_REQUEST);
    }

    private static String hash(String password) {
        byte[] salt = new byte[16];
        RANDOM.nextBytes(salt);
        return Base64.getEncoder().encodeToString(salt) + ":" + Base64.getEncoder().encodeToString(derive(password, salt));
    }

    private static byte[] derive(String password, byte[] salt) {
        var spec = new PBEKeySpec(password.toCharArray(), salt, ITERATIONS, 256);
        try {
            return SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(spec).getEncoded();
        } catch (java.security.GeneralSecurityException error) {
            throw new IllegalStateException(error);
        } finally {
            spec.clearPassword();
        }
    }

    private static ResponseStatusException unauthorized() { return new ResponseStatusException(HttpStatus.UNAUTHORIZED); }
}
