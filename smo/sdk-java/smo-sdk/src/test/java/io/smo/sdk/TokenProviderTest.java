package io.smo.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import java.net.http.HttpClient;
import java.time.Duration;
import org.junit.jupiter.api.Test;

/**
 * Covers {@link TokenProvider}: discovery, enrolment, the grant and its scope, caching until 30 s before expiry, refresh,
 * pinned and self-enrolled identities, errors, and offboarding. It talks to {@link FakePlatform} with the retry policy off
 * unless a test says otherwise. No network beyond loopback. Run: cd smo/sdk-java && ./mvnw -B -ntp -C verify (JDK 21; CI job sdk-java).
 */
class TokenProviderTest {

    /**
     * Builds a provider over a real {@link HttpCaller} whose sleeper does nothing and the given clock.
     */
    private static TokenProvider provider(SmoConfig config, FakePlatform.TestClock clock) {
        HttpCaller http = new HttpCaller(HttpClient.newHttpClient(), Duration.ofSeconds(5), d -> { });
        return new TokenProvider(config, http, clock);
    }

    /**
     * A config for the fake, with no retry and the name "unit".
     */
    private static SmoConfig config(FakePlatform fake) {
        return SmoConfig.of(fake.url()).withRetry(RetryPolicy.none()).withName("unit");
    }

    /**
     * Pins the first-token sequence: bootstrap with the key, enrolment without an enrollment secret, then a client_credentials grant for scope smo-rapp.
     */
    @Test
    void enrolsThenGrantsARappToken() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok-1", 300)) {
            TokenProvider tokens = provider(config(fake).withBootstrapKey("bk"), new FakePlatform.TestClock());

            assertEquals("tok-1", tokens.token());
            assertEquals("inv-1", tokens.invokerId());

            assertEquals("bk", fake.requests("GET", "/bootstrap").get(0).header("X-Bootstrap-Key"));
            FakePlatform.Seen enrol = fake.requests("POST", "/sme/invoker-registrations").get(0);
            JsonNode enrolBody = Json.MAPPER.readTree(enrol.body());
            assertTrue(enrolBody.get("apiInvokerPublicKey").asText().startsWith("smo-rapp:unit:"));
            assertNull(enrol.header("X-SMO-Enrollment"), "a rApp presents no enrollment secret");
            JsonNode grant = Json.MAPPER.readTree(fake.requests("POST", "/sme/oauth2/token").get(0).body());
            assertEquals("client_credentials", grant.get("grant_type").asText());
            assertEquals("inv-1", grant.get("client_id").asText());
            assertEquals("s3cret", grant.get("client_secret").asText());
            assertEquals("smo-rapp", grant.get("scope").asText());
        }
    }

    /**
     * Pins that the module identity sends its enrollment secret and asks for the internal scope.
     */
    @Test
    void aModuleAsksForTheInternalScopeAndPresentsItsEnrollmentSecret() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            SmoConfig module = SmoConfig.fromEnv(k -> switch (k) {
                case "R1_GATEWAY_URL" -> fake.url();
                case "SMO_IDENTITY_KIND" -> "module";
                case "SMO_ENROLLMENT_SECRET" -> "enroll-me";
                default -> null;
            }).withRetry(RetryPolicy.none());
            provider(module, new FakePlatform.TestClock()).token();

            assertEquals("enroll-me", fake.requests("POST", "/sme/invoker-registrations").get(0).header("X-SMO-Enrollment"));
            assertEquals("smo-internal", Json.MAPPER.readTree(fake.requests("POST", "/sme/oauth2/token").get(0).body()).get("scope").asText());
        }
    }

    /**
     * Pins the cache lifetime (expiry minus 30 s) and that a renewal reuses the endpoint and the invoker.
     */
    @Test
    void cachesUntilThirtySecondsBeforeExpiryThenGrantsAgain() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok-a", 100)) {
            fake.on("POST /sme/oauth2/token", 200, "{\"access_token\":\"tok-b\",\"expires_in\":100}");
            FakePlatform.TestClock clock = new FakePlatform.TestClock();
            TokenProvider tokens = provider(config(fake), clock);

            assertEquals("tok-a", tokens.token());
            clock.advance(Duration.ofSeconds(69));
            assertEquals("tok-a", tokens.token(), "still inside lifetime minus the 30 s margin");
            assertEquals(1, fake.requests("POST", "/sme/oauth2/token").size());

            clock.advance(Duration.ofSeconds(2));
            assertEquals("tok-b", tokens.token(), "past the margin: a new grant");
            assertEquals(2, fake.requests("POST", "/sme/oauth2/token").size());
            assertEquals(1, fake.requests("POST", "/sme/invoker-registrations").size(), "the invoker is enrolled once");
            assertEquals(1, fake.requests("GET", "/bootstrap").size(), "the token endpoint is discovered once");
        }
    }

    /**
     * Pins that asking with refresh obtains a new token even when the cached one is valid.
     */
    @Test
    void refreshDiscardsTheCachedToken() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok-a", 3600)) {
            fake.on("POST /sme/oauth2/token", 200, "{\"access_token\":\"tok-b\",\"expires_in\":3600}");
            TokenProvider tokens = provider(config(fake), new FakePlatform.TestClock());
            assertEquals("tok-a", tokens.token());
            assertEquals("tok-b", tokens.token(true));
            assertEquals("tok-b", tokens.token());
        }
    }

    /**
     * Pins that an invoker from the configuration is used as is and neither registered nor deregistered.
     */
    @Test
    void aPinnedIdentityIsNeverEnrolledOrOffboarded() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            TokenProvider tokens = provider(config(fake).withIdentity("issued-id", "issued-secret"), new FakePlatform.TestClock());
            tokens.token();

            assertTrue(fake.requests("POST", "/sme/invoker-registrations").isEmpty());
            assertEquals("issued-id", Json.MAPPER.readTree(fake.requests("POST", "/sme/oauth2/token").get(0).body()).get("client_id").asText());
            assertFalse(tokens.offboard());
            assertTrue(fake.requests("DELETE", "/sme/invoker-registrations/issued-id").isEmpty());
        }
    }

    /**
     * Pins that invalid_client for an invoker the SDK enrolled leads to one new enrolment and a retried grant.
     */
    @Test
    void aForgottenSelfEnrolledInvokerIsEnrolledAfreshOnce() throws Exception {
        try (FakePlatform fake = new FakePlatform()) {
            fake.on("GET /bootstrap", 200, "{\"apiEndpoints\":[{\"tokenEndPoint\":{\"uri\":\"" + fake.url() + "/sme/oauth2/token\"}}]}")
                    .on("POST /sme/invoker-registrations", 201, "{\"apiInvokerId\":\"old\",\"onboardingSecret\":\"o\"}")
                    .on("POST /sme/invoker-registrations", 201, "{\"apiInvokerId\":\"new\",\"onboardingSecret\":\"n\"}")
                    .on("POST /sme/oauth2/token", 200, "{\"access_token\":\"t1\",\"expires_in\":31}")
                    .on("POST /sme/oauth2/token", 400, "{\"error\":\"invalid_client\",\"error_description\":\"invoker not registered\"}")
                    .on("POST /sme/oauth2/token", 200, "{\"access_token\":\"t2\",\"expires_in\":300}");
            FakePlatform.TestClock clock = new FakePlatform.TestClock();
            TokenProvider tokens = provider(config(fake), clock);

            assertEquals("t1", tokens.token());
            clock.advance(Duration.ofSeconds(5));          // lifetime 31 s minus the margin leaves 1 s
            assertEquals("t2", tokens.token());
            assertEquals("new", tokens.invokerId());
            assertEquals(2, fake.requests("POST", "/sme/invoker-registrations").size());
        }
    }

    /**
     * Pins that invalid_client for a pinned invoker is thrown and no replacement is enrolled.
     */
    @Test
    void aPinnedIdentitySmeRefusesIsAnErrorNotAReplacement() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on("POST /sme/oauth2/token", 400, "{\"error\":\"invalid_client\"}");
            TokenProvider tokens = provider(config(fake).withIdentity("issued-id", "bad"), new FakePlatform.TestClock());
            // the scripted queue answers the 200 first (withSme); drain it
            tokens.token();
            SdkException e = assertThrows(SdkException.class, () -> tokens.token(true));
            assertEquals(400, e.status());
            assertTrue(fake.requests("POST", "/sme/invoker-registrations").isEmpty());
        }
    }

    /**
     * Pins that a refused grant is thrown with SME's status and body.
     */
    @Test
    void aGrantSmeRefusesIsReportedWithItsStatusAndBody() throws Exception {
        try (FakePlatform fake = new FakePlatform()) {
            fake.on("GET /bootstrap", 200, "{\"apiEndpoints\":[{\"tokenEndPoint\":{\"uri\":\"" + fake.url() + "/sme/oauth2/token\"}}]}")
                    .on("POST /sme/invoker-registrations", 201, "{\"apiInvokerId\":\"i\",\"onboardingSecret\":\"s\"}")
                    .on("POST /sme/oauth2/token", 400, "{\"error\":\"invalid_scope\"}");
            TokenProvider tokens = provider(config(fake), new FakePlatform.TestClock());
            SdkException e = assertThrows(SdkException.class, tokens::token);
            assertEquals(400, e.status());
            assertTrue(e.body().contains("invalid_scope"));
        }
    }

    /**
     * Pins that a bootstrap answer without a token endpoint is an error.
     */
    @Test
    void aBootstrapThatNamesNoTokenEndpointIsAnError() throws Exception {
        try (FakePlatform fake = new FakePlatform()) {
            fake.on("GET /bootstrap", 200, "{\"apiEndpoints\":[{\"apiName\":\"x\"}]}");
            assertThrows(SdkException.class, () -> provider(config(fake), new FakePlatform.TestClock()).token());
        }
    }

    /**
     * Pins that the token calls obey the retry policy: a 503 from /bootstrap is repeated.
     */
    @Test
    void transientErrorsDuringEnrolmentAreRetried() throws Exception {
        try (FakePlatform fake = new FakePlatform()) {
            fake.on("GET /bootstrap", 503, "{}")
                    .on("GET /bootstrap", 200, "{\"apiEndpoints\":[{\"tokenEndPoint\":{\"uri\":\"" + fake.url() + "/sme/oauth2/token\"}}]}")
                    .on("POST /sme/invoker-registrations", 201, "{\"apiInvokerId\":\"i\",\"onboardingSecret\":\"s\"}")
                    .on("POST /sme/oauth2/token", 200, "{\"access_token\":\"ok\",\"expires_in\":300}");
            SmoConfig retrying = SmoConfig.of(fake.url()).withRetry(new RetryPolicy(3, Duration.ofMillis(1), Duration.ofMillis(1), false, RetryPolicy.defaults().retryableStatuses()));
            assertEquals("ok", provider(retrying, new FakePlatform.TestClock()).token());
            assertEquals(2, fake.requests("GET", "/bootstrap").size());
        }
    }

    /**
     * Pins that offboarding deletes the self-enrolled invoker with the current token, forgets it, and a second call does nothing.
     */
    @Test
    void offboardDeregistersASelfEnrolledInvoker() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on("DELETE /sme/invoker-registrations/inv-1", 204, null);
            TokenProvider tokens = provider(config(fake), new FakePlatform.TestClock());
            tokens.token();

            assertTrue(tokens.offboard());
            assertEquals("Bearer tok", fake.requests("DELETE", "/sme/invoker-registrations/inv-1").get(0).header("Authorization"));
            assertNull(tokens.invokerId());
            assertFalse(tokens.offboard(), "nothing left to remove");
        }
    }

    /**
     * Pins SEC-15.13: the enrolment POST carries an Idempotency-Key of 32 hex digits, and the repeat after a 503 (which proves
     * SME did not run it) sends the same key, so a platform that stores the first answer would serve the repeat from it.
     */
    @Test
    void theEnrolmentCarriesOneIdempotencyKeyThatItsRepeatsReuse() throws Exception {
        try (FakePlatform fake = new FakePlatform()) {
            fake.on("GET /bootstrap", 200, "{\"apiEndpoints\":[{\"tokenEndPoint\":{\"uri\":\"" + fake.url() + "/sme/oauth2/token\"}}]}")
                    .on("POST /sme/invoker-registrations", 503, "{}")
                    .on("POST /sme/invoker-registrations", 201, "{\"apiInvokerId\":\"i\",\"onboardingSecret\":\"s\"}")
                    .on("POST /sme/oauth2/token", 200, "{\"access_token\":\"ok\",\"expires_in\":300}");
            SmoConfig retrying = SmoConfig.of(fake.url()).withRetry(new RetryPolicy(3, Duration.ofMillis(1), Duration.ofMillis(1), false, RetryPolicy.defaults().retryableStatuses()));
            provider(retrying, new FakePlatform.TestClock()).token();

            var posts = fake.requests("POST", "/sme/invoker-registrations");
            assertEquals(2, posts.size());
            String key = posts.get(0).header("Idempotency-Key");
            assertTrue(key != null && key.matches("[0-9a-f]{32}"), "key: " + key);
            assertEquals(key, posts.get(1).header("Idempotency-Key"));
        }
    }

    /**
     * Pins SEC-15.13: when the enrolment's answer is lost (the connection breaks after SME received it) the call is not repeated,
     * because SME stores no answer for this route and a repeat would register a second invoker; the error reaches the caller.
     */
    @Test
    void aLostEnrolmentAnswerIsNotRepeated() throws Exception {
        try (FakePlatform fake = new FakePlatform()) {
            fake.on("GET /bootstrap", 200, "{\"apiEndpoints\":[{\"tokenEndPoint\":{\"uri\":\"" + fake.url() + "/sme/oauth2/token\"}}]}")
                    .on("POST /sme/invoker-registrations", FakePlatform.Reply.drop())
                    .on("POST /sme/invoker-registrations", 201, "{\"apiInvokerId\":\"i\",\"onboardingSecret\":\"s\"}");
            SmoConfig retrying = SmoConfig.of(fake.url()).withRetry(new RetryPolicy(3, Duration.ofMillis(1), Duration.ofMillis(1), false, RetryPolicy.defaults().retryableStatuses()));
            TokenProvider tokens = provider(retrying, new FakePlatform.TestClock());

            assertThrows(SdkException.class, tokens::token);
            assertEquals(1, fake.requests("POST", "/sme/invoker-registrations").size(), "one registration attempt, not a second one");
        }
    }

    /**
     * Pins that a 504 on the enrolment (the gateway lost SME's answer, which may have run) is final and reaches the caller with its status.
     */
    @Test
    void aGatewayTimeoutOnTheEnrolmentIsFinal() throws Exception {
        try (FakePlatform fake = new FakePlatform()) {
            fake.on("GET /bootstrap", 200, "{\"apiEndpoints\":[{\"tokenEndPoint\":{\"uri\":\"" + fake.url() + "/sme/oauth2/token\"}}]}")
                    .on("POST /sme/invoker-registrations", 504, "{\"title\":\"GATEWAY_TIMEOUT\"}")
                    .on("POST /sme/invoker-registrations", 201, "{\"apiInvokerId\":\"i\",\"onboardingSecret\":\"s\"}");
            SmoConfig retrying = SmoConfig.of(fake.url()).withRetry(new RetryPolicy(3, Duration.ofMillis(1), Duration.ofMillis(1), false, RetryPolicy.defaults().retryableStatuses()));

            SdkException e = assertThrows(SdkException.class, () -> provider(retrying, new FakePlatform.TestClock()).token());
            assertEquals(504, e.status());
            assertEquals(1, fake.requests("POST", "/sme/invoker-registrations").size());
        }
    }

    /**
     * Pins that a fresh enrolment (after SME forgot the invoker) is a new logical registration with a new key.
     */
    @Test
    void aReEnrolmentUsesANewIdempotencyKey() throws Exception {
        try (FakePlatform fake = new FakePlatform()) {
            fake.on("GET /bootstrap", 200, "{\"apiEndpoints\":[{\"tokenEndPoint\":{\"uri\":\"" + fake.url() + "/sme/oauth2/token\"}}]}")
                    .on("POST /sme/invoker-registrations", 201, "{\"apiInvokerId\":\"old\",\"onboardingSecret\":\"o\"}")
                    .on("POST /sme/oauth2/token", 400, "{\"error\":\"invalid_client\"}")
                    .on("POST /sme/oauth2/token", 200, "{\"access_token\":\"t\",\"expires_in\":300}");
            provider(config(fake), new FakePlatform.TestClock()).token();
            var posts = fake.requests("POST", "/sme/invoker-registrations");
            assertEquals(2, posts.size());
            assertTrue(!posts.get(0).header("Idempotency-Key").equals(posts.get(1).header("Idempotency-Key")));
        }
    }
}
