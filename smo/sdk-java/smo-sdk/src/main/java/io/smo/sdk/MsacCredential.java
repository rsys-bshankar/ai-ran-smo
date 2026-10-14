package io.smo.sdk;

import java.util.Objects;

/**
 * The credential of an MSAC Identity ({@code SEC-15.6b}), to be passed to {@link DataClient#mediateAction}. A class of its own
 * so that the secret is not a plain {@code String} an rApp might log by accident: {@link #toString()} never shows it. The SDK
 * puts it in the {@code X-MSAC-Credential} header of that one call and nowhere else, and reads no environment variable for
 * it; the rApp passes in what it holds from wherever it keeps secrets.
 */
public final class MsacCredential {
    /** The header that carries it; RAN NF OAM reads the same name ({@code SEC-15.6}). */
    public static final String HEADER = "X-MSAC-Credential";

    private final String value;

    private MsacCredential(String value) {
        this.value = value;
    }

    /** The credential {@code value} (not null; an empty one is sent as given, and the platform refuses it). */
    public static MsacCredential of(String value) {
        return new MsacCredential(Objects.requireNonNull(value, "value"));
    }

    String value() {
        return value;
    }

    /** Never the secret. */
    @Override
    public String toString() {
        return "MsacCredential[redacted]";
    }
}
