package local.tax;

import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.server.ResponseStatusException;

@RestControllerAdvice
public class AgentErrors {
    @ExceptionHandler({org.springframework.http.converter.HttpMessageNotReadableException.class,
            org.springframework.web.method.annotation.MethodArgumentTypeMismatchException.class})
    ResponseEntity<?> invalidInput(Exception error) {
        return ResponseEntity.badRequest().body(Map.of("error",Map.of(
                "code","invalid_request","message","Invalid request body or identifier")));
    }

    @ExceptionHandler(ResponseStatusException.class)
    ResponseEntity<?> status(ResponseStatusException error) {
        String reason=error.getReason();
        String code=reason!=null && reason.matches("[a-z_]+") ? reason : "http_"+error.getStatusCode().value();
        return ResponseEntity.status(error.getStatusCode()).body(Map.of("error",Map.of(
                "code",code,"message",reason==null ? "Request rejected" : reason)));
    }
}
