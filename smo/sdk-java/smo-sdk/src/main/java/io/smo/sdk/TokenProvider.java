package io.smo.sdk;

import com.fasterxml.jackson.databind.JsonNode;
import java.security.SecureRandom;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

/**
 * The process's OAuth2 identity at SME and its cached access token: the Java counterpart of the Python client's
 * {@code _ModuleIdentity} ({@code shared/smo_shared/r1_client.py}).
 *
 * <ol>
 *   <li><b>Discover</b> the token endpoint from R1 Termination's {@code GET /bootstrap} (sending {@code X-Bootstrap-Key}
 *       when configured). Once.</li>
 *   <li><b>Enrol</b> an API invoker at SME ({@code POST /invoker-registrations}) unless the configuration pins one
 *       ({@code SMO_INVOKER_ID} and {@code SMO_INVOKER_SECRET}, as issued by rApp Management). A module presents its
 *       enrollment secret in {@code X-SMO-Enrollment}; a rApp presents none, and SME records it as a rApp.</li>
 *   <li><b>Grant</b>: {@code POST /oauth2/token} with {@code client_credentials} and the scope of the identity kind.</li>
 *   <li><b>Cache</b> the token until 30 s before it expires, then fetch another; {@link #token(boolean)} with
 *       {@code refresh=true} discards it first ({@link R1Client} does that once on a 401).</li>
 * </ol>
 *
 * <p>Differences from the Python client, on purpose: failing to obtain a token throws an {@link SdkException} (Python logs
 * and sends the call without a token, so the caller sees the gateway's 401); an invoker that was <em>pinned</em> is never
 * replaced by a fresh enrolment when SME no longer knows it (a replacement would not be the identity rApp Management issued
 * for the instance), the error is raised instead. A self-enrolled invoker is enrolled afresh once when SME answers
 * {@code invalid_client}.
 */
public final class TokenProvider {
    private static final long EXPIRY_MARGIN_SECONDS = 30;
    private static final String TOKEN_PATH = "/oauth2/token";

    private final SmoConfig config;
    private final HttpCaller http;
    private final Clock clock;
    private final SecureRandom random = new SecureRandom();
    private final Object lock = new Object();

    private String tokenEndpoint;
    private String invokerId;
    private String invokerSecret;
    private boolean selfEnrolled;
    private String token;
    private Instant expiresAt = Instant.MIN;

    /**
     * The identity starts as the one pinned in {@code config} (null when none is, which means the first token enrols one).
     * {@code clock} is injected so that tests move time by hand.
     */
    TokenProvider(SmoConfig config, HttpCaller http, Clock clock) {
        this.config = config;
        this.http = http;
        this.clock = clock;
        this.invokerId = config.invokerId();
        this.invokerSecret = config.invokerSecret();
    }

    /** A valid bearer token, from the cache or freshly granted. */
    public String token() {
        return token(false);
    }

    /** As {@link #token()}; with {@code refresh} a cached token is discarded first. */
    public String token(boolean refresh) {
        synchronized (lock) {
            if (!refresh && token != null && clock.instant().isBefore(expiresAt)) {
                return token;
            }
            token = null;
            String endpoint = discover();
            if (invokerId == null) {
                enrol(endpoint);
            }
            R1Response grant = grant(endpoint);
            if (grant.status() == 400 && selfEnrolled && "invalid_client".equals(grant.json().path("error").asText())) {
                enrol(endpoint);      // SME has forgotten the invoker (purged, or a fresh database): enrol afresh, once
                grant = grant(endpoint);
            }
            if (!grant.isSuccess()) {
                throw new SdkException(grant.status(), grant.body());
            }
            JsonNode body = grant.json();
            String accessToken = body.path("access_token").asText("");
            if (accessToken.isEmpty()) {
                throw new SdkException("SME's token answer has no access_token");
            }
            long lifetime = body.path("expires_in").asLong(60);
            token = accessToken;
            expiresAt = clock.instant().plus(Duration.ofSeconds(Math.max(lifetime - EXPIRY_MARGIN_SECONDS, 1)));
            return token;
        }
    }

    /** The invoker id in use, null before the first token was asked for. */
    public String invokerId() {
        synchronized (lock) {
            return invokerId;
        }
    }

    /**
     * Deregisters the invoker this process enrolled for itself. A pinned identity belongs to rApp Management and is
     * left alone. Returns whether an invoker was removed; a failure is not thrown (an orphan registration is only
     * clutter, SME's stale-invoker purge removes it).
     */
    public boolean offboard() {
        synchronized (lock) {
            if (!selfEnrolled || invokerId == null || tokenEndpoint == null) {
                return false;
            }
            try {
                Map<String, String> headers = new LinkedHashMap<>();
                if (token != null) {
                    headers.put("Authorization", "Bearer " + token);
                }
                R1Response response = http.send("DELETE", smeBase(tokenEndpoint) + "/invoker-registrations/" + invokerId, headers, null,
                        config.retry());
                if (response.isSuccess() || response.status() == 404) {
                    invokerId = null;
                    invokerSecret = null;
                    selfEnrolled = false;
                    token = null;
                    return true;
                }
            } catch (SdkException e) {
                // best effort
            }
            return false;
        }
    }

    /**
     * Returns SME's token endpoint, asking R1 Termination's {@code GET /bootstrap} the first time only: the first entry of
     * {@code apiEndpoints} that has a {@code tokenEndPoint.uri} wins. The uri is used as given (it is not checked to end in
     * {@code /oauth2/token}). Throws {@link SdkException} for an error status, or when no entry names an endpoint. The caller holds {@code lock}.
     */
    private String discover() {
        if (tokenEndpoint == null) {
            Map<String, String> headers = new LinkedHashMap<>();
            if (config.bootstrapKey() != null) {
                headers.put("X-Bootstrap-Key", config.bootstrapKey());
            }
            R1Response response = http.send("GET", config.gatewayUrl() + "/bootstrap", headers, null, config.retry());
            if (!response.isSuccess()) {
                throw new SdkException(response.status(), response.body());
            }
            for (JsonNode endpoint : response.json().path("apiEndpoints")) {
                String uri = endpoint.path("tokenEndPoint").path("uri").asText("");
                if (!uri.isEmpty()) {
                    tokenEndpoint = uri;
                    break;
                }
            }
            if (tokenEndpoint == null) {
                throw new SdkException("/bootstrap names no token endpoint");
            }
        }
        return tokenEndpoint;
    }

    /**
     * Registers a new API invoker at SME and keeps the id and secret it returns as the identity, marked as self-enrolled. The
     * request carries an opaque label in place of a public key; a module identity also sends its enrollment secret in
     * {@code X-SMO-Enrollment}. Every registration mints a new invoker, so a repeat after a lost answer would leave an orphan
     * and an identity nobody holds (SEC-15.13): the call carries an {@code Idempotency-Key} generated once for this registration
     * (all its repeats send the same one, a re-enrolment gets a new one) and is repeated only on 429 and 503, which prove SME did
     * not run it; a transport failure, 502 or 504 is raised. SME does not store an answer for this route (it would hold the
     * onboarding secret), so today the key is a promise to a platform that may and the limit on repeats is what keeps it single. Throws {@link SdkException} for an error status or an answer without
     * an id and secret. The caller holds {@code lock}.
     */
    private void enrol(String endpoint) {
        // An opaque label, not a PEM key: this client authenticates with its onboarding secret (see the Python client).
        byte[] salt = new byte[6];
        random.nextBytes(salt);
        String label = "smo-" + (config.module() ? "module" : "rapp") + ":" + config.name() + ":"
                + Base64.getUrlEncoder().withoutPadding().encodeToString(salt);
        Map<String, String> headers = new LinkedHashMap<>();
        if (config.module() && config.enrollmentSecret() != null) {
            headers.put("X-SMO-Enrollment", config.enrollmentSecret());
        }
        headers.put("Idempotency-Key", UUID.randomUUID().toString().replace("-", ""));
        R1Response response = http.send("POST", smeBase(endpoint) + "/invoker-registrations", headers,
                Json.write(Map.of("apiInvokerPublicKey", label)), config.retry(), false);
        if (!response.isSuccess()) {
            throw new SdkException(response.status(), response.body());
        }
        JsonNode body = response.json();
        String id = body.path("apiInvokerId").asText("");
        String secret = body.path("onboardingSecret").asText("");
        if (id.isEmpty() || secret.isEmpty()) {
            throw new SdkException("SME's invoker registration answer has no apiInvokerId / onboardingSecret");
        }
        invokerId = id;
        invokerSecret = secret;
        selfEnrolled = true;
    }

    /**
     * Sends the {@code client_credentials} grant for the identity's scope and returns the answer unchecked: {@link #token(boolean)}
     * decides what a non-success status means. The caller holds {@code lock}.
     */
    private R1Response grant(String endpoint) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("grant_type", "client_credentials");
        body.put("client_id", invokerId);
        body.put("client_secret", invokerSecret);
        body.put("scope", config.scope());
        return http.send("POST", endpoint, Map.of(), Json.write(body), config.retry());
    }

    private static String smeBase(String endpoint) {
        int at = endpoint.lastIndexOf(TOKEN_PATH);
        return at < 0 ? endpoint : endpoint.substring(0, at);
    }
}
