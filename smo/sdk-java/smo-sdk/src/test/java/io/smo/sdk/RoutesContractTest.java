package io.smo.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.fasterxml.jackson.databind.JsonNode;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.Set;
import org.junit.jupiter.api.Test;

/**
 * The SDK's routes against the committed OpenAPI documents ({@code docs/openapi/<module>.json}, the same files the platform's own
 * tests keep equal to each service's live schema). This is the build-time link between the hand-written clients and the R1
 * contract: a route that no longer exists, a query parameter the route does not declare, or a required body property the
 * client does not send fails here. (The generated-client alternative was rejected; see {@code sdk-java/README.md}, 1.5.)
 */
class RoutesContractTest {
    private static final Path DIR = Path.of(System.getProperty("smo.openapi.dir", "../../docs/openapi"));

    /** "/dme/data-jobs/{}" is the module "dme" and the path "/data-jobs/{}" in docs/openapi/dme.json. */
    private static String moduleOf(Route route) {
        return route.template().split("/")[1];
    }

    /**
     * Rewrites every {@code {name}} placeholder of an OpenAPI path to {@code {}}, the form {@link Route} templates use.
     */
    private static String normalise(String path) {
        return path.replaceAll("\\{[^}]*}", "{}");
    }

    /**
     * Finds the OpenAPI operation for {@code route} in {@code docs/openapi/<module>.json} (matching the path with its
     * placeholders normalised, and the method). Throws {@link AssertionError} when the document, the path or the method is missing.
     */
    private static JsonNode operation(Route route) throws IOException {
        String module = moduleOf(route);
        Path file = DIR.resolve(module + ".json");
        assertTrue(Files.exists(file), "no OpenAPI document for module " + module + " at " + file.toAbsolutePath());
        JsonNode spec = Json.MAPPER.readTree(Files.readString(file));
        String wanted = route.template().substring(module.length() + 1);
        for (var path : spec.get("paths").properties()) {
            if (normalise(path.getKey()).equals(wanted)) {
                JsonNode op = path.getValue().get(route.method().toLowerCase());
                if (op != null) {
                    return op;
                }
            }
        }
        throw new AssertionError(route.method() + " " + route.template() + " is not in docs/openapi/" + module + ".json");
    }

    /**
     * Pins that every route in {@link Routes} exists, with its method, in the committed OpenAPI documents.
     */
    @Test
    void everyRouteTheSdkCallsExistsInTheCommittedContract() throws IOException {
        for (Route route : Routes.ALL) {
            operation(route);
        }
    }

    /**
     * Pins that the SDK sends no query parameter the contract does not declare.
     */
    @Test
    void everyQueryParameterTheSdkSendsIsDeclared() throws IOException {
        for (Route route : Routes.ALL) {
            Set<String> declared = new HashSet<>();
            for (JsonNode parameter : operation(route).path("parameters")) {
                if ("query".equals(parameter.path("in").asText())) {
                    declared.add(parameter.get("name").asText());
                }
            }
            Set<String> unknown = new HashSet<>(route.query());
            unknown.removeAll(declared);
            assertTrue(unknown.isEmpty(), route.method() + " " + route.template() + " sends query parameters the contract does not declare: " + unknown);
        }
    }

    /**
     * Pins that each route with a typed body sends every required property and no property the schema does not have.
     */
    @Test
    void everyRequiredBodyPropertyIsSent() throws IOException {
        int checked = 0;
        for (Route route : Routes.ALL) {
            JsonNode schema = operation(route).path("requestBody").path("content").path("application/json").path("schema");
            String ref = schema.path("$ref").asText("");
            if (ref.isEmpty()) {
                continue;                                  // a free-form body: nothing to compare
            }
            JsonNode model = Json.MAPPER.readTree(Files.readString(DIR.resolve(moduleOf(route) + ".json")))
                    .at("/components/schemas/" + ref.substring(ref.lastIndexOf('/') + 1));
            Set<String> required = new HashSet<>();
            model.path("required").forEach(r -> required.add(r.asText()));
            Set<String> properties = new HashSet<>();
            model.path("properties").fieldNames().forEachRemaining(properties::add);

            Set<String> missing = new HashSet<>(required);
            missing.removeAll(route.bodyFields());
            assertTrue(missing.isEmpty(), route.method() + " " + route.template() + " does not send required properties " + missing);
            Set<String> unknown = new HashSet<>(route.bodyFields());
            unknown.removeAll(properties);
            assertTrue(unknown.isEmpty(), route.method() + " " + route.template() + " sends properties the contract does not have: " + unknown);
            checked++;
        }
        assertTrue(checked >= 2, "the data-job and operator-api bodies are compared");
    }

    /**
     * Pins that the check itself fails for an unknown path and for a known path with a method it lacks.
     */
    @Test
    void aRouteThatIsNotInTheContractIsCaught() {
        assertThrows(AssertionError.class, () -> operation(Route.get("/dme/no-such-route")));
        assertThrows(AssertionError.class, () -> operation(Route.delete("/dme/dme-types")));
    }

    /**
     * Pins that a path argument becomes one encoded segment and a wrong argument count, or an empty argument, is refused.
     */
    @Test
    void pathExpansionEncodesOneSegmentAndChecksTheArgumentCount() {
        assertEquals("/dme/data-jobs/a%2Fb%20c", Routes.DME_JOB_GET.path("a/b c"));
        assertEquals("/dme/data-jobs/x/status", Routes.DME_JOB_STATUS.path("x"));
        assertThrows(IllegalArgumentException.class, () -> Routes.DME_JOB_GET.path());
        assertThrows(IllegalArgumentException.class, () -> Routes.DME_JOB_GET.path("a", "b"));
        assertThrows(IllegalArgumentException.class, () -> Routes.DME_JOB_GET.path(""));
        assertEquals("/dme/dme-types", Routes.DME_TYPES.path());
    }
}
