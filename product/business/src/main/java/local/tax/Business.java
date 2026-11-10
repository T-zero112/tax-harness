package local.tax;

import com.fasterxml.jackson.databind.ObjectMapper;
import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.security.SecureRandom;
import java.time.Duration;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.http.*;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.web.servlet.mvc.method.annotation.StreamingResponseBody;

@SpringBootApplication
@RestController
public class Business {
    private static final Duration SESSION_TTL = Duration.ofHours(1);
    static final String COOKIE = "tax_product_session";
    private final JdbcTemplate jdbc;
    private final StringRedisTemplate redis;
    private final ObjectMapper json;
    private final Accounts accounts;
    private final boolean secureCookie;
    private final SecureRandom random = new SecureRandom();
    private final HttpClient agent = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(3)).build();

    public Business(JdbcTemplate jdbc, StringRedisTemplate redis, ObjectMapper json, Accounts accounts,
                    @Value("${product.session.secure:false}") boolean secureCookie) {
        this.jdbc = jdbc;
        this.redis = redis;
        this.json = json;
        this.accounts = accounts;
        this.secureCookie = secureCookie;
    }

    public static void main(String[] args) {
        SpringApplication.run(Business.class, args);
    }

    record Credentials(String username, String password) {}
    record Member(String username, String password, String role) {}
    record RoleChange(String role) {}
    enum Access { READ, WRITE, ADMIN }

    @PostMapping("/api/session")
    ResponseEntity<Map<String, String>> login(@RequestBody Credentials credentials,
            @CookieValue(name = COOKIE, required = false) String previous) {
        Accounts.Identity identity = accounts.authenticate(credentials.username(), credentials.password());
        if (validToken(previous)) redis.delete("account-session:" + previous);
        byte[] bytes = new byte[32];
        random.nextBytes(bytes);
        String token = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
        redis.opsForValue().set("account-session:" + token, identity.id() + "|" + identity.tenant(), SESSION_TTL);
        return ResponseEntity.ok().header(HttpHeaders.SET_COOKIE, cookie(token, SESSION_TTL))
                .body(Map.of("tenant_id", identity.tenant(), "username", identity.username()));
    }

    @DeleteMapping("/api/session")
    ResponseEntity<Void> logout(@CookieValue(name = COOKIE, required = false) String token) {
        if (validToken(token)) redis.delete("account-session:" + token);
        return ResponseEntity.noContent().header(HttpHeaders.SET_COOKIE, cookie("", Duration.ZERO)).build();
    }

    @GetMapping("/api/enterprises")
    List<Map<String, Object>> enterprises(@CookieValue(name = COOKIE, required = false) String token) {
        Accounts.Identity identity = identity(token);
        return jdbc.queryForList("""
                select e.enterprise_id, e.name from enterprise e join enterprise_grant g using (tenant_id, enterprise_id)
                where g.tenant_id = ? and g.account_id = ? order by e.enterprise_id
                """, identity.tenant(), identity.id());
    }

    @GetMapping("/api/enterprise-access")
    List<Map<String, Object>> access(@CookieValue(name = COOKIE, required = false) String token) {
        Accounts.Identity identity = identity(token);
        return jdbc.queryForList("select enterprise_id, role from enterprise_grant where tenant_id = ? and account_id = ?",
                identity.tenant(), identity.id());
    }

    @GetMapping("/api/enterprises/{enterpriseId}/members")
    List<Map<String, Object>> members(@CookieValue(name = COOKIE, required = false) String token,
                                      @PathVariable String enterpriseId) {
        Accounts.Identity admin = authorize(token, enterpriseId, Access.ADMIN);
        return jdbc.queryForList("""
                select a.username, g.role from enterprise_grant g join product_account a using (account_id)
                where g.tenant_id = ? and g.enterprise_id = ? order by a.username
                """, admin.tenant(), enterpriseId);
    }

    @PostMapping("/api/enterprises/{enterpriseId}/members")
    Map<String, String> createMember(@CookieValue(name = COOKIE, required = false) String token,
                                     @PathVariable String enterpriseId, @RequestBody Member member) {
        return accounts.createMember(authorize(token, enterpriseId, Access.ADMIN), enterpriseId,
                member.username(), member.password(), member.role());
    }

    @PatchMapping("/api/enterprises/{enterpriseId}/members/{username}")
    ResponseEntity<Void> changeRole(@CookieValue(name = COOKIE, required = false) String token,
                                     @PathVariable String enterpriseId, @PathVariable String username,
                                     @RequestBody RoleChange change) {
        if (change.role() == null) throw new ResponseStatusException(HttpStatus.BAD_REQUEST);
        accounts.updateMember(authorize(token, enterpriseId, Access.ADMIN), enterpriseId, username, change.role());
        return ResponseEntity.noContent().build();
    }

    @PutMapping("/api/enterprises/{enterpriseId}/members/{username}")
    ResponseEntity<Void> grant(@CookieValue(name = COOKIE, required = false) String token,
                               @PathVariable String enterpriseId, @PathVariable String username,
                               @RequestBody RoleChange change) {
        accounts.grantMember(authorize(token, enterpriseId, Access.ADMIN), enterpriseId, username, change.role());
        return ResponseEntity.noContent().build();
    }

    @DeleteMapping("/api/enterprises/{enterpriseId}/members/{username}")
    ResponseEntity<Void> revoke(@CookieValue(name = COOKIE, required = false) String token,
                                 @PathVariable String enterpriseId, @PathVariable String username) {
        accounts.updateMember(authorize(token, enterpriseId, Access.ADMIN), enterpriseId, username, null);
        return ResponseEntity.noContent().build();
    }

    @PostMapping(value = "/api/enterprises/{enterpriseId}/probe", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    ResponseEntity<StreamingResponseBody> probe(@CookieValue(name = COOKIE, required = false) String token,
                                               @PathVariable String enterpriseId) throws Exception {
        String tenant = authorize(token, enterpriseId, Access.READ).tenant();
        HttpRequest request = HttpRequest.newBuilder(URI.create("http://agent:8000/internal/probe"))
                .timeout(Duration.ofSeconds(10))
                .header("Authorization", "Bearer " + System.getenv("PRODUCT_AGENT_TOKEN"))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(json.writeValueAsString(
                        Map.of("tenant_id", tenant, "enterprise_id", enterpriseId))))
                .build();
        HttpResponse<InputStream> upstream;
        try {
            upstream = agent.send(request, HttpResponse.BodyHandlers.ofInputStream());
        } catch (java.io.IOException e) {
            throw new ResponseStatusException(HttpStatus.BAD_GATEWAY, "Agent unavailable");
        }
        if (upstream.statusCode() != 200) {
            upstream.body().close();
            throw new ResponseStatusException(HttpStatus.BAD_GATEWAY, "Agent rejected the probe");
        }
        StreamingResponseBody stream = output -> {
            try (InputStream input = upstream.body()) {
                byte[] buffer = new byte[4096];
                int size;
                while ((size = input.read(buffer)) != -1) {
                    output.write(buffer, 0, size);
                    output.flush();
                }
            }
        };
        return ResponseEntity.ok().contentType(MediaType.TEXT_EVENT_STREAM)
                .cacheControl(CacheControl.noCache()).body(stream);
    }

    private Accounts.Identity identity(String token) {
        String value = validToken(token) ? redis.opsForValue().get("account-session:" + token) : null;
        if (value == null) throw new ResponseStatusException(HttpStatus.UNAUTHORIZED);
        int separator = value.indexOf('|');
        if (separator != 36) throw new ResponseStatusException(HttpStatus.UNAUTHORIZED);
        return accounts.resolve(UUID.fromString(value.substring(0, separator)), value.substring(separator + 1));
    }

    Accounts.Identity authorize(String token, String enterprise, Access access) {
        Accounts.Identity identity = identity(token);
        String role = accounts.role(identity, enterprise);
        if ((access == Access.ADMIN && !role.equals("admin")) || (access == Access.WRITE && role.equals("viewer"))) {
            throw new ResponseStatusException(HttpStatus.FORBIDDEN);
        }
        return identity;
    }

    private static boolean validToken(String token) {
        return token != null && token.matches("[A-Za-z0-9_-]{43}");
    }

    private String cookie(String value, Duration ttl) {
        return ResponseCookie.from(COOKIE, value).httpOnly(true).sameSite("Strict")
                .secure(secureCookie).path("/").maxAge(ttl).build().toString();
    }
}
