package io.smo.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import java.net.http.HttpClient;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * Covers the MSAC credential of {@link DataClient#mediateAction} ({@code SEC-15.6b}): it goes in the {@code X-MSAC-Credential}
 * header of {@code POST /dme/actions} only when given, never in the body, an {@link SdkException} or a {@code toString}, it is
 * kept next to the {@code Idempotency-Key} when the SDK repeats the call, and the header name is the one the committed DME
 * OpenAPI document declares. The SDK reads no environment variable for it (nothing in {@code DataClient} or {@code MsacCredential}
 * calls {@code System.getenv}; a test cannot set one). Uses {@link FakePlatform}. Run: cd smo/sdk-java && ./mvnw -B -ntp -C verify (JDK 21; CI job sdk-java).
 */
class MsacCredentialTest {
    private static final String SECRET = "s3cr3t-msac-credential-value";
    private static final List<Map<String, Object>> CHANGES = List.of(Map.of("managedElementRef", "me-1"));
    private static final String ROUTE = "POST /dme/actions";

    private final FakePlatform.TestClock clock = new FakePlatform.TestClock();

    private DataClient data(FakePlatform fake) {
        SmoConfig config = SmoConfig.of(fake.url()).withRetry(new RetryPolicy(4, Duration.ofMillis(100), Duration.ofSeconds(1), false, RetryPolicy.defaults().retryableStatuses()));
        return new DataClient(new R1Client(config, HttpClient.newHttpClient(), clock, d -> { }));
    }

    /**
     * Pins that the header carries the credential when one is given, is absent when it is null or the overload without one is used, and that the call is POST /dme/actions.
     */
    @Test
    void theHeaderIsSentOnlyWhenACredentialIsGiven() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on(ROUTE, 200, "{\"status\":\"FORWARDED\"}");
            DataClient data = data(fake);
            assertEquals("FORWARDED", data.mediateAction("ident-1", CHANGES, null, null, null, null, MsacCredential.of(SECRET)).get("status").asText());
            data.mediateAction("ident-1", CHANGES, null, null, null, null, null);
            data.mediateAction("ident-1", CHANGES, null, null, null, null);

            List<FakePlatform.Seen> posts = fake.requests("POST", "/dme/actions");
            assertEquals(3, posts.size());
            assertEquals(SECRET, posts.get(0).header("X-MSAC-Credential"));
            assertNull(posts.get(1).header("X-MSAC-Credential"));
            assertNull(posts.get(2).header("X-MSAC-Credential"));
            assertNotNull(posts.get(0).header("Idempotency-Key"));
        }
    }

    /**
     * Pins that the body has the action's fields, leaves out the unset optional ones, and does not contain the credential.
     */
    @Test
    void theCredentialIsNotInTheBody() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on(ROUTE, 200, "{}");
            data(fake).mediateAction("ident-1", CHANGES, null, "role-a", null, Map.of("rationale", "x"), MsacCredential.of(SECRET));

            String body = fake.requests("POST", "/dme/actions").get(0).body();
            assertFalse(body.contains(SECRET), body);
            assertFalse(body.toLowerCase().contains("credential"), body);
            JsonNode sent = Json.MAPPER.readTree(body);
            assertEquals("ident-1", sent.get("requestedBy").asText());
            assertEquals("role-a", sent.get("msacRole").asText());
            assertEquals(1, sent.get("changes").size());
            assertEquals("x", sent.get("decision").get("rationale").asText());
            assertFalse(sent.has("scope"), body);
            assertFalse(sent.has("sourceContext"), body);
        }
    }

    /**
     * Pins that neither the exception of a refusal, nor its message, nor the credential object's toString shows the secret.
     */
    @Test
    void theCredentialIsNotInAnExceptionOrAToString() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on(ROUTE, 403, "{\"detail\":{\"title\":\"MSAC_ACCESS_DENIED\",\"status\":403}}");
            MsacCredential credential = MsacCredential.of(SECRET);
            SdkException e = assertThrows(SdkException.class, () -> data(fake).mediateAction("ident-1", CHANGES, null, null, null, null, credential));

            assertEquals(403, e.status());
            assertFalse(e.toString().contains(SECRET));
            assertFalse(String.valueOf(e.body()).contains(SECRET));
            assertFalse(credential.toString().contains(SECRET));
            assertFalse(List.of(credential).toString().contains(SECRET));
        }
    }

    /**
     * Pins that the repeat after a 503 and the repeat after CONCURRENT_MODIFICATION send the same credential and the same Idempotency-Key as the first try.
     */
    @Test
    void theCredentialIsKeptNextToTheIdempotencyKeyOnARepeat() throws Exception {
        String race = "{\"detail\":{\"title\":\"CONCURRENT_MODIFICATION\",\"status\":409}}";
        for (int first : new int[] {503, 409}) {
            try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
                fake.on(ROUTE, first, first == 409 ? race : "{}").on(ROUTE, 200, "{\"status\":\"FORWARDED\"}");
                data(fake).mediateAction("ident-1", CHANGES, null, null, null, null, MsacCredential.of(SECRET));

                List<FakePlatform.Seen> posts = fake.requests("POST", "/dme/actions");
                assertEquals(2, posts.size(), "status " + first);
                assertNotNull(posts.get(0).header("Idempotency-Key"));
                assertEquals(posts.get(0).header("Idempotency-Key"), posts.get(1).header("Idempotency-Key"));
                assertEquals(SECRET, posts.get(0).header("X-MSAC-Credential"));
                assertEquals(SECRET, posts.get(1).header("X-MSAC-Credential"));
            }
        }
    }

    /**
     * Pins that a credential must have a value: a null one is refused when it is built, not sent as a missing header.
     */
    @Test
    void aNullCredentialValueIsRefused() {
        assertThrows(NullPointerException.class, () -> MsacCredential.of(null));
    }

    /**
     * Pins that {@link MsacCredential#HEADER} is a header parameter of POST /actions in docs/openapi/dme.json.
     */
    @Test
    void theHeaderNameIsTheOneTheDmeContractDeclares() throws Exception {
        Path file = Path.of(System.getProperty("smo.openapi.dir", "../../docs/openapi")).resolve("dme.json");
        JsonNode operation = Json.MAPPER.readTree(Files.readString(file)).at("/paths/~1actions/post");
        boolean declared = false;
        for (JsonNode parameter : operation.path("parameters")) {
            declared |= "header".equals(parameter.path("in").asText()) && MsacCredential.HEADER.equals(parameter.path("name").asText());
        }
        assertTrue(declared, "docs/openapi/dme.json does not declare " + MsacCredential.HEADER + " on POST /actions");
    }
}
