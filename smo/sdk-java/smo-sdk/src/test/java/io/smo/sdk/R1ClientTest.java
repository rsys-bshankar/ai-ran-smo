package io.smo.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import java.net.http.HttpClient;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;

/**
 * Covers {@link R1Client}: the bearer token and its refresh on a 401, retries and backoff, the Idempotency-Key of a POST,
 * the one repeat of a lost write race, query encoding and offboarding on close. It uses {@link FakePlatform} for the platform
 * and injects the clock and the sleeper, so no test waits. No network beyond loopback. Run: cd smo/sdk-java && ./mvnw -B -ntp -C verify (JDK 21; CI job sdk-java).
 */
class R1ClientTest {
    private final List<Duration> sleeps = new ArrayList<>();
    private final FakePlatform.TestClock clock = new FakePlatform.TestClock();

    private R1Client client(FakePlatform fake) {
        SmoConfig config = SmoConfig.of(fake.url()).withRetry(new RetryPolicy(4, Duration.ofMillis(100), Duration.ofSeconds(1), false, RetryPolicy.defaults().retryableStatuses()));
        return new R1Client(config, HttpClient.newHttpClient(), clock, sleeps::add);
    }

    /**
     * Pins that the token is sent as a bearer and a {items, ...} answer is returned as its list.
     */
    @Test
    void sendsTheBearerTokenAndDecodesTheBody() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on("GET /dme/dme-types", 200, "{\"items\":[{\"id\":\"a\"},{\"id\":\"b\"}],\"total\":2,\"limit\":100,\"offset\":0}");
            JsonNode types = new DataClient(client(fake)).listTypes(null);

            assertTrue(types.isArray(), "{items,...} is unwrapped to the list");
            assertEquals(2, types.size());
            assertEquals("Bearer tok", fake.requests("GET", "/dme/dme-types").get(0).header("Authorization"));
            assertEquals("application/json", fake.requests("GET", "/dme/dme-types").get(0).header("Accept"));
        }
    }

    /**
     * Pins that an object whose {@code items} is not a list is not unwrapped.
     */
    @Test
    void anObjectWithoutItemsIsReturnedAsIs() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on("GET /aimgf/models/m1", 200, "{\"modelId\":\"m1\",\"items\":\"not a list\"}");
            assertEquals("m1", new ModelsClient(client(fake)).getModel("m1").get("modelId").asText());
        }
    }

    /**
     * Pins that a token SME has stopped accepting is replaced and the call sent again with the new one.
     */
    @Test
    void a401RefreshesTheTokenOnceAndRepeatsTheCall() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("old", 3600)) {
            fake.on("POST /sme/oauth2/token", 200, "{\"access_token\":\"new\",\"expires_in\":3600}")
                    .on("GET /dme/dme-types", 401, "{\"title\":\"UNAUTHORIZED\"}")
                    .on("GET /dme/dme-types", 200, "[]");
            JsonNode result = new DataClient(client(fake)).listTypes(null);

            assertTrue(result.isArray());
            List<FakePlatform.Seen> calls = fake.requests("GET", "/dme/dme-types");
            assertEquals(2, calls.size());
            assertEquals("Bearer old", calls.get(0).header("Authorization"));
            assertEquals("Bearer new", calls.get(1).header("Authorization"));
        }
    }

    /**
     * Pins that only one fresh token is tried: a 401 that persists is thrown, not looped on.
     */
    @Test
    void aSecond401IsFinal() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 3600)) {
            fake.on("GET /dme/dme-types", 401, "{\"title\":\"UNAUTHORIZED\"}");
            SdkException e = assertThrows(SdkException.class, () -> new DataClient(client(fake)).listTypes(null));
            assertEquals(401, e.status());
            assertEquals(2, fake.requests("GET", "/dme/dme-types").size(), "one fresh token, one more try, no loop");
        }
    }

    /**
     * Pins the waits between repeats: the backoff doubles, and a Retry-After replaces the computed wait.
     */
    @Test
    void transientStatusesAreRetriedWithBackoffAndRetryAfter() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on("GET /aimgf/models", 503, "{}")
                    .on("GET /aimgf/models", new FakePlatform.Reply(429, "{}", Map.of("Retry-After", "2")))
                    .on("GET /aimgf/models", 502, "{}")
                    .on("GET /aimgf/models", 200, "{\"items\":[]}");
            JsonNode models = new ModelsClient(client(fake)).listModels(10);

            assertEquals(0, models.size());
            assertEquals(4, fake.requests("GET", "/aimgf/models").size());
            assertEquals(List.of(Duration.ofMillis(100), Duration.ofSeconds(2), Duration.ofMillis(400)), sleeps);
        }
    }

    /**
     * Pins that after the last attempt the final answer's status is thrown, with one wait fewer than attempts.
     */
    @Test
    void whenTheAttemptsRunOutTheLastAnswerIsTheError() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on("GET /aimgf/models", 503, "{\"title\":\"UNAVAILABLE\"}");
            SdkException e = assertThrows(SdkException.class, () -> new ModelsClient(client(fake)).listModels(10));
            assertEquals(503, e.status());
            assertEquals(4, fake.requests("GET", "/aimgf/models").size());
            assertEquals(3, sleeps.size());
        }
    }

    /**
     * Pins that a 4xx is final after one try and the exception carries the status and the body.
     */
    @Test
    void aClientErrorIsNotRetriedAndIsMappedWithItsBody() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on("GET /aimgf/models/nope", 404, "{\"title\":\"MODEL_NOT_FOUND\",\"status\":404}");
            SdkException e = assertThrows(SdkException.class, () -> new ModelsClient(client(fake)).getModel("nope"));
            assertEquals(404, e.status());
            assertTrue(e.isClientError());
            assertTrue(e.body().contains("MODEL_NOT_FOUND"));
            assertEquals(1, fake.requests("GET", "/aimgf/models/nope").size());
            assertTrue(sleeps.isEmpty());
        }
    }

    /**
     * Pins that a transport failure is repeated, then thrown with status 0 and no body.
     */
    @Test
    void unreachableGatewayIsRetriedThenSurfacesAsAnSdkExceptionWithStatusZero() throws Exception {
        String dead;
        try (FakePlatform gone = new FakePlatform()) {
            dead = gone.url();
        }
        R1Client r1 = new R1Client(SmoConfig.of(dead).withRetry(new RetryPolicy(3, Duration.ofMillis(10), Duration.ofMillis(10), false, java.util.Set.of())),
                HttpClient.newHttpClient(), clock, sleeps::add);
        SdkException e = assertThrows(SdkException.class, () -> r1.get("/dme/dme-types", null));
        assertEquals(0, e.status());
        assertNull(e.body());
        assertEquals(2, sleeps.size(), "three attempts, two waits");
    }

    /**
     * Pins that a POST has a generated key that its repeat reuses, so the platform can recognise the repeat, and that the JSON body is sent.
     */
    @Test
    void everyPostCarriesAnIdempotencyKeyThatItsRepeatsReuse() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on("POST /dme/data-jobs", 503, "{}").on("POST /dme/data-jobs", 201, "{\"dataJobId\":\"j1\"}");
            JsonNode job = new DataClient(client(fake)).createDataJob("t1", "PULL", "HTTP", "me", null, null, null);

            assertEquals("j1", job.get("dataJobId").asText());
            List<FakePlatform.Seen> posts = fake.requests("POST", "/dme/data-jobs");
            assertEquals(2, posts.size());
            String key = posts.get(0).header("Idempotency-Key");
            assertNotNull(key);
            assertEquals(32, key.length());
            assertEquals(key, posts.get(1).header("Idempotency-Key"));
            JsonNode sent = Json.MAPPER.readTree(posts.get(0).body());
            assertEquals("t1", sent.get("dmeTypeId").asText());
            assertTrue(sent.get("productionJobDefinition").isObject());
            assertEquals("application/json", posts.get(0).header("Content-Type"));
        }
    }

    /**
     * Pins that a key the caller supplies is sent instead of a generated one.
     */
    @Test
    void aCallersOwnIdempotencyKeyWins() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on("POST /dme/data-jobs", 201, "{}");
            client(fake).request("POST", "/dme/data-jobs", null, Map.of("a", 1), Map.of("Idempotency-Key", "mine")).ok();
            assertEquals("mine", fake.requests("POST", "/dme/data-jobs").get(0).header("Idempotency-Key"));
        }
    }

    /**
     * Pins that only a 409 CONCURRENT_MODIFICATION is repeated, once, with the same key; another 409 is final.
     */
    @Test
    void aLostWriteRaceIsSentOnceMoreWithTheSameKeyButOtherConflictsAreFinal() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            String race = "{\"detail\":{\"title\":\"CONCURRENT_MODIFICATION\",\"status\":409}}";
            fake.on("POST /rapp-mgmt/instances/i1/performance", 409, race).on("POST /rapp-mgmt/instances/i1/performance", 200, "{\"status\":\"recorded\"}")
                    .on("POST /rapp-mgmt/instances/i2/terminate", 409, "{\"detail\":{\"title\":\"LIFECYCLE_ILLEGAL_TRANSITION\"}}");
            InstancesClient instances = new InstancesClient(client(fake));

            assertEquals("recorded", instances.reportPerformance("i1", Map.of("k", 1)).get("status").asText());
            List<FakePlatform.Seen> posts = fake.requests("POST", "/rapp-mgmt/instances/i1/performance");
            assertEquals(2, posts.size());
            assertEquals(posts.get(0).header("Idempotency-Key"), posts.get(1).header("Idempotency-Key"));

            SdkException e = assertThrows(SdkException.class, () -> instances.terminate("i2"));
            assertEquals(409, e.status());
            assertEquals(1, fake.requests("POST", "/rapp-mgmt/instances/i2/terminate").size());
        }
    }

    /**
     * Pins that a GET answered with CONCURRENT_MODIFICATION is thrown at once.
     */
    @Test
    void aReadIsNeverRepeatedForAConflict() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on("GET /rapp-mgmt/instances/i1", 409, "{\"detail\":{\"title\":\"CONCURRENT_MODIFICATION\"}}");
            assertThrows(SdkException.class, () -> new InstancesClient(client(fake)).get("i1"));
            assertEquals(1, fake.requests("GET", "/rapp-mgmt/instances/i1").size());
        }
    }

    /**
     * Pins that a 204 reads as a missing JSON node and that query values are URL-encoded.
     */
    @Test
    void a204IsAMissingNodeAndQueryValuesAreEncodedWithNullsLeftOut() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on("DELETE /rapp-mgmt/instances/i1/operator-api", 204, null).on("GET /dme/data-jobs", 200, "{\"items\":[]}");
            R1Client r1 = client(fake);
            new InstancesClient(r1).clearOperatorApi("i1");
            assertTrue(r1.delete("/rapp-mgmt/instances/i1/operator-api", null).ok().isMissingNode());

            new DataClient(r1).listDataJobs("a b&c", null);
            assertEquals("dme_type_id=a+b%26c", fake.requests("GET", "/dme/data-jobs").get(0).query());
        }
    }

    /**
     * Pins the query string of a null or empty map, a repeated key for a collection, and a null value left out.
     */
    @Test
    void queryStringHandlesCollectionsAndEmpty() {
        assertEquals("", R1Client.queryString(null));
        assertEquals("", R1Client.queryString(Map.of()));
        Map<String, Object> q = new java.util.LinkedHashMap<>();
        q.put("x", List.of(1, 2));
        q.put("y", null);
        assertEquals("?x=1&x=2", R1Client.queryString(q));
    }

    /**
     * Pins that closing the client deregisters the invoker the SDK enrolled.
     */
    @Test
    void closeOffboardsTheSelfEnrolledInvoker() throws Exception {
        try (FakePlatform fake = new FakePlatform().withSme("tok", 300)) {
            fake.on("DELETE /sme/invoker-registrations/inv-1", 204, null).on("GET /aimgf/models", 200, "[]");
            R1Client r1 = client(fake);
            new ModelsClient(r1).listModels(1);
            r1.close();
            assertEquals(1, fake.requests("DELETE", "/sme/invoker-registrations/inv-1").size());
        }
    }
}
