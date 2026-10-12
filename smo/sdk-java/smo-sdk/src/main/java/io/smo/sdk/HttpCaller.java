package io.smo.sdk;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * One HTTP exchange with retry: the only place the SDK touches {@link HttpClient}. A transport failure or a status in
 * the {@link RetryPolicy} is sent again after the policy's delay; whatever is left when the attempts run out is returned
 * (a status) or thrown (a transport failure, as an {@link SdkException} with status 0).
 */
final class HttpCaller {

    /** Waits; a test replaces it to record the delays instead of sleeping. */
    @FunctionalInterface
    interface Sleeper {
        /**
         * Waits for {@code duration}. Throws {@link InterruptedException} when the thread is interrupted while waiting.
         */
        void sleep(Duration duration) throws InterruptedException;
    }

    /** Statuses after which the call certainly did not run (the gateway's rate limit, an unavailable service): safe to repeat for any call. */
    private static final Set<Integer> NOT_RUN_STATUSES = Set.of(429, 503);

    private final HttpClient http;
    private final Duration timeout;
    private final Sleeper sleeper;

    HttpCaller(HttpClient http, Duration timeout, Sleeper sleeper) {
        this.http = http;
        this.timeout = timeout;
        this.sleeper = sleeper;
    }

    /**
     * Sends one request and repeats it per {@code retry}.
     *
     * <p>{@code headers} are sent as given (the caller adds {@code Authorization}); {@code Accept: application/json} is always
     * added, and {@code Content-Type: application/json} when {@code jsonBody} is not null. A transport failure is repeated
     * whatever the policy's statuses, and so is a status the policy lists, for every method: this class does not know which
     * calls are safe to repeat (R1Client makes a POST safe by adding an {@code Idempotency-Key}; the token calls of
     * {@link TokenProvider} send none). The overload with {@code repeatWhenUnsure=false} is for a call that is not safe to repeat
     * when its answer may have been lost.
     *
     * @return the answer of the last attempt, with whatever status it has (a 4xx or 5xx is returned, not thrown)
     * @throws SdkException with status 0 when the last attempt fails in transport, or the thread is interrupted
     */
    R1Response send(String method, String url, Map<String, String> headers, String jsonBody, RetryPolicy retry) {
        return send(method, url, headers, jsonBody, retry, true);
    }

    /**
     * As {@link #send(String, String, Map, String, RetryPolicy)}; with {@code repeatWhenUnsure} false only the answers that
     * prove the server did not run the call are repeated (429 and 503, as the Go SDK does for its enrolment), and a transport
     * failure or any other status in the policy (502, 504: the gateway lost the upstream's answer) is final. This is for a call
     * that creates something and that the platform cannot answer a second time from a stored first answer: the repeat would create
     * a second one (SEC-15.13, the invoker registration).
     */
    R1Response send(String method, String url, Map<String, String> headers, String jsonBody, RetryPolicy retry, boolean repeatWhenUnsure) {
        HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(url)).timeout(timeout);
        headers.forEach(builder::header);
        if (jsonBody != null) {
            builder.header("Content-Type", "application/json");
        }
        builder.header("Accept", "application/json");
        builder.method(method, jsonBody == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(jsonBody));
        HttpRequest request = builder.build();

        for (int attempt = 1; ; attempt++) {
            Duration retryAfter = null;
            try {
                HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
                if (attempt >= retry.maxAttempts() || !retry.retriesStatus(response.statusCode())
                        || (!repeatWhenUnsure && !NOT_RUN_STATUSES.contains(response.statusCode()))) {
                    return new R1Response(response.statusCode(), response.body());
                }
                retryAfter = retryAfterOf(response);
            } catch (IOException e) {
                if (attempt >= retry.maxAttempts() || !repeatWhenUnsure) {
                    throw new SdkException(method + " " + url + " failed: " + e, e);
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new SdkException(method + " " + url + " was interrupted", e);
            }
            pause(retry.delayBefore(attempt + 1, retryAfter));
        }
    }

    /**
     * Waits {@code delay} through the sleeper; a zero delay does not call it. An interrupt restores the thread's interrupt flag
     * and is thrown as an {@link SdkException}.
     */
    private void pause(Duration delay) {
        if (delay.isZero()) {
            return;
        }
        try {
            sleeper.sleep(delay);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new SdkException("interrupted while waiting to retry", e);
        }
    }

    /** {@code Retry-After} in seconds; the HTTP-date form is ignored (the computed backoff is used instead). */
    private static Duration retryAfterOf(HttpResponse<?> response) {
        Optional<String> value = response.headers().firstValue("Retry-After");
        if (value.isEmpty()) {
            return null;
        }
        try {
            long seconds = Long.parseLong(value.get().trim());
            return seconds < 0 ? null : Duration.ofSeconds(seconds);
        } catch (NumberFormatException e) {
            return null;
        }
    }
}
