package io.smo.example;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import io.smo.sdk.SdkException;
import io.smo.sdk.SmoSdk;
import java.io.IOException;
import java.time.Clock;
import java.time.Duration;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Function;

/**
 * The smallest rApp that exercises the Java SDK against a running SMO: it
 * <ol>
 *   <li>gets an access token (the SDK enrols an invoker at SME, or uses the one rApp Management issued);</li>
 *   <li>registers the operator API of its instance, where the GUI reaches the page the package declares;</li>
 *   <li>reports a heartbeat to rApp Management every {@code HELLO_HEARTBEAT_SECONDS};</li>
 *   <li>on "Run now" reads a data route (DME types) and an AI route (the registry's models) and records what it saw;</li>
 *   <li>on SIGTERM reports a final heartbeat, clears the operator API and deregisters its own invoker.</li>
 * </ol>
 * It takes no action on the network. Configuration is the environment ({@link Settings#fromEnv}) plus the SDK's own
 * ({@code R1_GATEWAY_URL}, {@code SMO_INVOKER_ID}, ...).
 */
public final class HelloRapp implements AutoCloseable {
    private static final System.Logger LOG = System.getLogger(HelloRapp.class.getName());
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final int KEPT_RUNS = 50;

    /**
     * @param instanceId      the rApp instance this process is ({@code SMO_INSTANCE_ID}, the id rApp Management gave it)
     * @param port            where the operator API listens ({@code PORT}, default 8000)
     * @param operatorApiBase the URL R1 Termination reaches the operator API at ({@code HELLO_OPERATOR_API_BASE}); it must not be loopback
     * @param heartbeat       the heartbeat period ({@code HELLO_HEARTBEAT_SECONDS}, default 30)
     */
    public record Settings(String instanceId, int port, String operatorApiBase, Duration heartbeat) {
        /**
         * Reads the settings from {@code env} (a function, so a test passes a map): {@code SMO_INSTANCE_ID} and
         * {@code HELLO_OPERATOR_API_BASE} are required, {@code PORT} defaults to 8000 and {@code HELLO_HEARTBEAT_SECONDS} to 30.
         *
         * @throws IllegalStateException when a required variable is missing or blank
         * @throws NumberFormatException when {@code PORT} or {@code HELLO_HEARTBEAT_SECONDS} is not a number
         */
        public static Settings fromEnv(Function<String, String> env) {
            String instance = env.apply("SMO_INSTANCE_ID");
            if (instance == null || instance.isBlank()) {
                throw new IllegalStateException("SMO_INSTANCE_ID is required: the id of the rApp instance (POST /rapp-mgmt/instances)");
            }
            int port = Integer.parseInt(or(env.apply("PORT"), "8000"));
            String base = or(env.apply("HELLO_OPERATOR_API_BASE"), "");
            if (base.isBlank()) {
                throw new IllegalStateException("HELLO_OPERATOR_API_BASE is required: the URL the gateway reaches this container at, e.g. http://hello-java-rapp:8000");
            }
            return new Settings(instance, port, base, Duration.ofSeconds(Long.parseLong(or(env.apply("HELLO_HEARTBEAT_SECONDS"), "30"))));
        }

        private static String or(String value, String fallback) {
            return value == null || value.isBlank() ? fallback : value;
        }
    }

    private final SmoSdk sdk;
    private final Settings settings;
    private final Clock clock;
    private final OperatorServer server;
    private final ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "heartbeat");
        t.setDaemon(true);
        return t;
    });

    private final AtomicBoolean closed = new AtomicBoolean();
    private final Deque<ObjectNode> runs = new ArrayDeque<>();
    private String state = "STARTING";
    private long processed;
    private String lastRunAt;
    private int dmeTypes = -1;
    private int models = -1;

    /**
     * Creates the rApp and the operator API server, which binds its port here (port 0 picks a free one). Nothing is served or
     * sent to the platform until {@link #start()}.
     */
    public HelloRapp(SmoSdk sdk, Settings settings, Clock clock) throws IOException {
        this.sdk = sdk;
        this.settings = settings;
        this.clock = clock;
        this.server = new OperatorServer(this, settings.port());
    }

    /**
     * Runs the rApp until SIGTERM: a failed start is logged and ends the JVM with exit code 1.
     */
    public static void main(String[] args) throws Exception {
        Settings settings = Settings.fromEnv(System::getenv);
        HelloRapp app = new HelloRapp(SmoSdk.fromEnv(), settings, Clock.systemUTC());
        Runtime.getRuntime().addShutdownHook(new Thread(app::close, "shutdown"));
        try {
            app.start();
        } catch (RuntimeException e) {
            // the operator API's thread is not a daemon: without the exit a failed start would leave a JVM that serves but is not registered
            LOG.log(System.Logger.Level.ERROR, "could not start: " + e.getMessage(), e);
            System.exit(1);
        }
        Thread.currentThread().join();                // until SIGTERM
    }

    /** Starts the operator API, registers it, and starts the heartbeat. */
    public void start() {
        server.start();
        // The first token is obtained here: the SDK discovers the token endpoint, enrols (or uses the issued invoker) and grants.
        sdk.instances().registerOperatorApi(settings.instanceId(), settings.operatorApiBase());
        setState("RUNNING");
        scheduler.scheduleWithFixedDelay(this::heartbeat, 0, settings.heartbeat().toSeconds(), TimeUnit.SECONDS);
        LOG.log(System.Logger.Level.INFO, "instance {0} registered its operator API at {1}", settings.instanceId(), settings.operatorApiBase());
    }

    /** One heartbeat: the platform keeps the newest self-report of the instance (GET /rapp-mgmt/instances/{id}/performance). */
    void heartbeat() {
        try {
            sdk.instances().reportPerformance(settings.instanceId(), Map.of("heartbeat", clock.instant().toString(), "state", state(), "runs", processedCount()));
        } catch (SdkException e) {
            LOG.log(System.Logger.Level.WARNING, "heartbeat failed: {0}", e.getMessage());     // the next tick tries again; the SDK already retried
        }
    }

    /**
     * A run: reads the data types DME knows and the models AIMgF's registry holds and records the counts. {@code dryRun} is accepted from the
     * operator page and changes nothing here, as the rApp acts on nothing anyway.
     */
    ObjectNode run(boolean dryRun) {
        JsonNode types = sdk.data().listTypes(null);
        JsonNode registered = sdk.models().listModels(100);
        ObjectNode entry = JSON.createObjectNode();
        entry.put("runId", UUID.randomUUID().toString());
        entry.put("startedAt", clock.instant().toString());
        entry.put("result", "ok");
        entry.put("dryRun", dryRun);
        entry.put("dmeTypes", types.size());
        entry.put("models", registered.size());
        synchronized (this) {
            processed++;
            lastRunAt = entry.get("startedAt").asText();
            dmeTypes = types.size();
            models = registered.size();
            runs.addFirst(entry);
            while (runs.size() > KEPT_RUNS) {
                runs.removeLast();
            }
        }
        return entry;
    }

    /**
     * The JSON for {@code GET /instances/{id}/status}: state, mode (always SHADOW), the number of runs, the time of the last run
     * and the counts it saw (-1 before the first run).
     */
    synchronized ObjectNode status() {
        ObjectNode node = JSON.createObjectNode();
        node.put("state", state);
        node.put("mode", "SHADOW");
        node.put("processed", processed);
        node.put("lastRunAt", lastRunAt);
        node.put("dmeTypes", dmeTypes);
        node.put("models", models);
        return node;
    }

    /**
     * The newest recorded runs, at most {@code limit}, newest first, as {@code {"items": [...]}}. Only the last 50 runs are kept.
     */
    synchronized ObjectNode runs(int limit) {
        ArrayNode items = JSON.createArrayNode();
        runs.stream().limit(limit).forEach(items::add);
        ObjectNode node = JSON.createObjectNode();
        node.set("items", items);
        return node;
    }

    int operatorPort() {
        return server.port();
    }

    String instanceId() {
        return settings.instanceId();
    }

    synchronized boolean ready() {
        return "RUNNING".equals(state);
    }

    private synchronized String state() {
        return state;
    }

    private synchronized long processedCount() {
        return processed;
    }

    private synchronized void setState(String value) {
        state = value;
    }

    /** Stops cleanly: a last heartbeat, the operator API forgotten, the invoker this process enrolled deregistered. */
    @Override
    public void close() {
        if (!closed.compareAndSet(false, true)) {
            return;                                   // the shutdown hook and a failed start can both get here
        }
        scheduler.shutdownNow();
        setState("STOPPING");
        try {
            heartbeat();
            sdk.instances().clearOperatorApi(settings.instanceId());
        } catch (SdkException e) {
            LOG.log(System.Logger.Level.WARNING, "could not clear the operator API: {0}", e.getMessage());
        } finally {
            server.stop();
            sdk.close();
        }
    }
}
