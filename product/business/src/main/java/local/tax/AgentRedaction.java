package local.tax;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.stereotype.Service;
import java.util.Set;

@Service
public class AgentRedaction {
    private final ObjectMapper json;
    private static final Set<String> PRIVATE_KEYS = Set.of(
            "authorization","cookie","call_token","api_key","password","agent_token","session");

    public AgentRedaction(ObjectMapper json) { this.json = json; }

    String text(String value, String... ephemeral) {
        if (value == null) return null;
        String result = value;
        for (var entry : System.getenv().entrySet()) {
            if (entry.getValue().length() >= 8 && entry.getKey().matches(".*(TOKEN|PASSWORD|API_KEY).*"))
                result = result.replace(entry.getValue(), "[redacted]");
        }
        for (String secret : ephemeral)
            if (secret != null && secret.length() >= 8) result = result.replace(secret, "[redacted]");
        return result.replaceAll("(?i)Bearer\\s+[A-Za-z0-9._~+/-]+", "Bearer [redacted]")
                .replaceAll("tax_product_session=[A-Za-z0-9_-]+", "tax_product_session=[redacted]");
    }

    JsonNode clean(Object value, String... ephemeral) {
        JsonNode node = json.valueToTree(value);
        return cleanNode(node, ephemeral);
    }

    private JsonNode cleanNode(JsonNode node, String[] ephemeral) {
        if (node.isTextual()) return json.getNodeFactory().textNode(text(node.asText(), ephemeral));
        if (node.isObject()) {
            ObjectNode out = json.createObjectNode();
            node.fields().forEachRemaining(field -> out.set(field.getKey(),
                    PRIVATE_KEYS.contains(field.getKey().toLowerCase())
                            ? json.getNodeFactory().textNode("[redacted]") : cleanNode(field.getValue(), ephemeral)));
            return out;
        }
        if (node.isArray()) {
            var out = json.createArrayNode();
            node.forEach(item -> out.add(cleanNode(item, ephemeral)));
            return out;
        }
        return node;
    }
}
