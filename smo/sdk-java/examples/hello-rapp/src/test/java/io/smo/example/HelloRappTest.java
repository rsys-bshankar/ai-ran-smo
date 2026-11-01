package io.smo.example;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import io.smo.sdk.RetryPolicy;
import io.smo.sdk.SmoConfig;
import io.smo.sdk.SmoSdk;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

/** The example rApp against a stand-in for R1 Termination, SME, DME, AIMgF and rApp Management. */
class HelloRappTest {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final String INSTANCE = "11111111-2222-3333-4444-555555555555";

    private HttpServer platform;
    private final List<String> calls = new CopyOnWriteArrayList<>();
    private final Map<String, String> bodies = new ConcurrentHashMap<>();
    private HelloRapp app;
    private final HttpClient http = HttpClient.newHttpClient();

    /**
     * Starts the stand-in platform on a free loopback port and builds the rApp against it (no retries, operator API on a free
     * port, a one-hour heartbeat period, so a test sees the first beat and the one at close). The platform records every call and body and answers 401 to a
     * call without the issued token.
     */
    @BeforeEach
    void startPlatform() throws Exception {
        platform = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
        String base = "http://127.0.0.1:" + platform.getAddress().getPort();
        Map<String, String> answers = Map.of(
                "GET /bootstrap", "{\"apiEndpoints\":[{\"tokenEndPoint\":{\"uri\":\"" + base + "/sme/oauth2/token\"}}]}",
                "POST /sme/invoker-registrations", "{\"apiInvokerId\":\"inv\",\"onboardingSecret\":\"sec\"}",
                "POST /sme/oauth2/token", "{\"access_token\":\"tok\",\"expires_in\":300}",
                "PUT /rapp-mgmt/instances/" + INSTANCE + "/operator-api", "{\"operatorApiBase\":\"x\"}",
                "POST /rapp-mgmt/instances/" + INSTANCE + "/performance", "{\"status\":\"recorded\"}",
                "GET /dme/dme-types", "{\"items\":[{\"id\":\"a\"},{\"id\":\"b\"},{\"id\":\"c\"}],\"total\":3}",
                "GET /aimgf/models", "{\"items\":[{\"id\":\"m\"}],\"total\":1}");
        platform.createContext("/", exchange -> {
            String key = exchange.getRequestMethod() + " " + exchange.getRequestURI().getPath();
            // the body first: a test that waits for the call (awaitCall) then reads its body must find it
            bodies.put(key, new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            calls.add(key);
            if (!key.startsWith("GET /bootstrap") && !key.startsWith("POST /sme/") && !"Bearer tok".equals(exchange.getRequestHeaders().getFirst("Authorization"))
                    && !key.startsWith("DELETE /sme/")) {
                exchange.sendResponseHeaders(401, -1);
            } else if (key.startsWith("DELETE ")) {
                exchange.sendResponseHeaders(204, -1);
            } else if (answers.containsKey(key)) {
                byte[] out = answers.get(key).getBytes(StandardCharsets.UTF_8);
                exchange.sendResponseHeaders(key.startsWith("POST /sme/invoker") ? 201 : 200, out.length);
                exchange.getResponseBody().write(out);
            } else {
                exchange.sendResponseHeaders(404, -1);
            }
            exchange.close();
        });
        platform.start();

        SmoConfig config = SmoConfig.of(base).withRetry(RetryPolicy.none()).withName("hello-test");
        app = new HelloRapp(new SmoSdk(config), new HelloRapp.Settings(INSTANCE, 0, "http://hello-java-rapp:8000", Duration.ofHours(1)), Clock.systemUTC());
    }

    @AfterEach
    void stopPlatform() {
        platform.stop(0);
    }

    /**
     * Calls the rApp's own operator API.
     */
    private HttpResponse<String> call(String method, String path, String body) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + app.operatorPort() + path)).timeout(Duration.ofSeconds(10));
        b.method(method, body == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(body));
        return http.send(b.build(), HttpResponse.BodyHandlers.ofString());
    }

    /**
     * Waits up to five seconds for the platform to have received {@code key} ("METHOD path"), since the heartbeat runs on another thread.
     */
    private void awaitCall(String key) throws InterruptedException {
        for (int i = 0; i < 100 && !calls.contains(key); i++) {
            Thread.sleep(50);
        }
        assertTrue(calls.contains(key), "expected " + key + " in " + calls);
    }

    /**
     * Pins that start registers the operator API base, sends a heartbeat with state RUNNING, and the health routes answer 200.
     */
    @Test
    void startRegistersTheOperatorApiAndSendsAHeartbeat() throws Exception {
        app.start();

        JsonNode put = JSON.readTree(bodies.get("PUT /rapp-mgmt/instances/" + INSTANCE + "/operator-api"));
        assertEquals("http://hello-java-rapp:8000", put.get("operatorApiBase").asText());
        awaitCall("POST /rapp-mgmt/instances/" + INSTANCE + "/performance");
        JsonNode beat = JSON.readTree(bodies.get("POST /rapp-mgmt/instances/" + INSTANCE + "/performance"));
        assertEquals("RUNNING", beat.get("state").asText());
        assertEquals(200, call("GET", "/ready", null).statusCode());
        assertEquals(200, call("GET", "/live", null).statusCode());
        app.close();
    }

    /**
     * Pins that a run reads DME and AIMgF's registry, answers 202 with the counts, and the status and runs routes show it.
     */
    @Test
    void aRunReadsADataRouteAndAnAiRouteAndTheOperatorPageSeesIt() throws Exception {
        app.start();
        HttpResponse<String> run = call("POST", "/instances/" + INSTANCE + "/run", "{\"dryRun\":true}");
        assertEquals(202, run.statusCode());
        JsonNode entry = JSON.readTree(run.body());
        assertEquals(3, entry.get("dmeTypes").asInt());
        assertEquals(1, entry.get("models").asInt());
        assertTrue(entry.get("dryRun").asBoolean());

        JsonNode status = JSON.readTree(call("GET", "/instances/" + INSTANCE + "/status", null).body());
        assertEquals("RUNNING", status.get("state").asText());
        assertEquals(1, status.get("processed").asInt());
        assertEquals(3, status.get("dmeTypes").asInt());

        JsonNode runs = JSON.readTree(call("GET", "/instances/" + INSTANCE + "/runs?limit=5", null).body());
        assertEquals(1, runs.get("items").size());
        assertEquals(entry.get("runId").asText(), runs.get("items").get(0).get("runId").asText());
        app.close();
    }

    /**
     * Pins that another instance's id or an unknown path is 404 and a wrong method is 405.
     */
    @Test
    void routesOfAnotherInstanceAndWrongMethodsAreRefused() throws Exception {
        app.start();
        assertEquals(404, call("GET", "/instances/other/status", null).statusCode());
        assertEquals(404, call("GET", "/nope", null).statusCode());
        assertEquals(405, call("GET", "/instances/" + INSTANCE + "/run", null).statusCode());
        assertEquals(405, call("POST", "/instances/" + INSTANCE + "/status", "{}").statusCode());
        app.close();
    }

    /**
     * Pins that a platform failure during a run is answered as 502 PLATFORM_ERROR and the rApp keeps serving.
     */
    @Test
    void aPlatformFailureDuringARunIsA502NotACrash() throws Exception {
        app.start();
        platform.removeContext("/");
        platform.createContext("/", exchange -> {
            exchange.sendResponseHeaders(503, -1);
            exchange.close();
        });
        HttpResponse<String> run = call("POST", "/instances/" + INSTANCE + "/run", null);
        assertEquals(502, run.statusCode());
        assertTrue(run.body().contains("PLATFORM_ERROR"));
        app.close();
    }

    /**
     * Pins that closing withdraws the operator API registration and deregisters the invoker the SDK enrolled.
     */
    @Test
    void closeClearsTheOperatorApiAndDeregistersTheInvoker() throws Exception {
        app.start();
        awaitCall("POST /rapp-mgmt/instances/" + INSTANCE + "/performance");
        app.close();
        assertTrue(calls.contains("DELETE /rapp-mgmt/instances/" + INSTANCE + "/operator-api"));
        assertTrue(calls.contains("DELETE /sme/invoker-registrations/inv"));
    }

    /**
     * Pins the settings: defaults, and an error when the instance id or the operator API base is missing.
     */
    @Test
    void settingsComeFromTheEnvironmentAndTheRequiredOnesAreRequired() {
        HelloRapp.Settings s = HelloRapp.Settings.fromEnv(Map.of("SMO_INSTANCE_ID", "i", "HELLO_OPERATOR_API_BASE", "http://h:8000", "PORT", "9000")::get);
        assertEquals(9000, s.port());
        assertEquals(Duration.ofSeconds(30), s.heartbeat());
        assertThrows(IllegalStateException.class, () -> HelloRapp.Settings.fromEnv(Map.of("HELLO_OPERATOR_API_BASE", "http://h")::get));
        assertThrows(IllegalStateException.class, () -> HelloRapp.Settings.fromEnv(Map.of("SMO_INSTANCE_ID", "i")::get));
        assertFalse(s.toString().isEmpty());
    }
}
