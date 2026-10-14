package io.smo.sdk;

import com.fasterxml.jackson.databind.JsonNode;
import java.util.LinkedHashMap;
import java.util.Map;

/** Base of the namespace clients: one {@link Route} in, the decoded JSON (or an {@link SdkException}) out. */
abstract class NamespaceClient {
    private final R1Client r1;

    NamespaceClient(R1Client r1) {
        this.r1 = r1;
    }

    final JsonNode call(Route route, Map<String, ?> query, Object body, Object... pathArgs) {
        return callWith(route, query, body, Map.of(), pathArgs);
    }

    /** As {@link #call}, with extra request headers. */
    final JsonNode callWith(Route route, Map<String, ?> query, Object body, Map<String, String> headers, Object... pathArgs) {
        return r1.request(route.method(), route.path(pathArgs), query, body, headers).ok();
    }

    /** A map of the given key/value pairs without the entries whose value is null. */
    static Map<String, Object> params(Object... keysAndValues) {
        Map<String, Object> map = new LinkedHashMap<>();
        for (int i = 0; i < keysAndValues.length; i += 2) {
            if (keysAndValues[i + 1] != null) {
                map.put((String) keysAndValues[i], keysAndValues[i + 1]);
            }
        }
        return map;
    }
}
