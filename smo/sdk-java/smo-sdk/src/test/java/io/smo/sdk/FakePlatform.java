package io.smo.sdk;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * A scripted R1 Termination + SME on the JDK's own HTTP server: replies are queued per "METHOD /path"; the last reply
 * of a queue repeats. Every request is recorded. Unscripted routes answer 404.
 */
final class FakePlatform implements AutoCloseable {

    /**
     * One scripted answer: the status, the body (null for none) and extra response headers.
     */
    record Reply(int status, String body, Map<String, String> headers) {
        static Reply of(int status, String body) {
            return new Reply(status, body, Map.of());
        }

        /** A lost answer: the server received the request and closes the connection without answering. */
        static Reply drop() {
            return new Reply(0, null, Map.of());
        }
    }

    /**
     * One request as the fake received it. Header names are lower-cased; {@code path} and {@code query} are the raw, still
     * percent-encoded forms.
     */
    record Seen(String method, String path, String query, Map<String, String> headers, String body) {
        String header(String name) {
            return headers.get(name.toLowerCase());
        }
    }

    private final HttpServer server;
    private final Map<String, Deque<Reply>> script = new HashMap<>();
    final List<Seen> seen = new CopyOnWriteArrayList<>();

    FakePlatform() throws IOException {
        server = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
        server.createContext("/", this::handle);
        server.start();
    }

    String url() {
        return "http://127.0.0.1:" + server.getAddress().getPort();
    }

    /** Queue a reply for "METHOD /path". */
    FakePlatform on(String route, int status, String body) {
        return on(route, Reply.of(status, body));
    }

    synchronized FakePlatform on(String route, Reply reply) {
        script.computeIfAbsent(route, k -> new ArrayDeque<>()).add(reply);
        return this;
    }

    /** The standard happy path: /bootstrap names this server's SME, which enrols and grants a token. */
    FakePlatform withSme(String token, int expiresIn) {
        on("GET /bootstrap", 200, "{\"apiEndpoints\":[{\"apiName\":\"service-apis\",\"tokenEndPoint\":{\"uri\":\"" + url()
                + "/sme/oauth2/token\"}}]}");
        on("POST /sme/invoker-registrations", 201, "{\"apiInvokerId\":\"inv-1\",\"onboardingSecret\":\"s3cret\"}");
        on("POST /sme/oauth2/token", 200, "{\"access_token\":\"" + token + "\",\"token_type\":\"Bearer\",\"expires_in\":" + expiresIn + "}");
        return this;
    }

    /**
     * The recorded requests with exactly this method and path, in arrival order.
     */
    List<Seen> requests(String method, String path) {
        List<Seen> out = new ArrayList<>();
        for (Seen s : seen) {
            if (s.method().equals(method) && s.path().equals(path)) {
                out.add(s);
            }
        }
        return out;
    }

    /**
     * Records the request, then answers with the next scripted reply for "METHOD path" (the last one repeats), or 404 when the
     * route has no script. Runs on the server's thread.
     */
    private void handle(HttpExchange exchange) throws IOException {
        String body = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
        Map<String, String> headers = new HashMap<>();
        exchange.getRequestHeaders().forEach((k, v) -> headers.put(k.toLowerCase(), v.get(0)));
        String method = exchange.getRequestMethod();
        String path = exchange.getRequestURI().getRawPath();
        seen.add(new Seen(method, path, exchange.getRequestURI().getRawQuery(), headers, body));

        Reply reply;
        synchronized (this) {
            Deque<Reply> queue = script.get(method + " " + path);
            reply = queue == null ? Reply.of(404, "{\"title\":\"NOT_FOUND\"}") : queue.size() > 1 ? queue.poll() : queue.peek();
        }
        if (reply.status() == 0) {
            exchange.close();      // no status line: the client sees a broken connection
            return;
        }
        byte[] bytes = reply.body() == null ? new byte[0] : reply.body().getBytes(StandardCharsets.UTF_8);
        reply.headers().forEach((k, v) -> exchange.getResponseHeaders().add(k, v));
        exchange.getResponseHeaders().add("Content-Type", "application/json");
        exchange.sendResponseHeaders(reply.status(), bytes.length == 0 ? -1 : bytes.length);
        if (bytes.length > 0) {
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(bytes);
            }
        }
        exchange.close();
    }

    @Override
    public void close() {
        server.stop(0);
    }

    /** A clock the test moves by hand. */
    static final class TestClock extends Clock {
        private Instant now = Instant.parse("2026-01-01T00:00:00Z");

        void advance(Duration d) {
            now = now.plus(d);
        }

        @Override
        public java.time.ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override
        public Clock withZone(java.time.ZoneId zone) {
            return this;
        }

        @Override
        public Instant instant() {
            return now;
        }
    }
}
