// @vitest-environment jsdom
/** Tests of the Preferences page (pages/preferences) in the user's language (GUI-6.3): a user whose saved language is German sees the page in
 * German, choosing English previews it at once, and saving sends the language with the other preferences. Uses the fake BFF of
 * `src/testing/bff.tsx` under the real `AuthProvider` and `ThemeProvider`. Run: `npx vitest run src/pages/preferences` from smo/gui. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "../../../auth/AuthContext";
import { DEFAULT_PREFERENCES } from "../../../data/preferences";
import { ThemeProvider } from "../../../shell/ThemeProvider";
import { fakeBff, mountWith } from "../../../testing/bff";
import { byText, cleanup, click, settle } from "../../../testing/dom";
import { Preferences } from "../index";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
beforeEach(() => { document.body.innerHTML = ""; window.localStorage.clear(); });

/** A BFF whose user saved German; the PUT echoes what it was sent. */
function bff() {
  return fakeBff({
    "GET /me": { username: "ana", role: "operator", csrfToken: "c", local: true, totpEnrolled: true },
    "GET /me/preferences": { ...DEFAULT_PREFERENCES, language: "de" },
    "PUT /me/preferences": (call: { body: unknown }) => call.body,
  });
}

const page = () => mountWith(<AuthProvider><ThemeProvider><Preferences /></ThemeProvider></AuthProvider>, { at: "/preferences" });

describe("Preferences in the user's language", () => {
  // The saved language is German: the title, a section and the start-page list are German, and <html lang> is "de".
  it("shows the page in the saved language", async () => {
    bff();
    const { container } = await page();
    await settle(8);
    expect(container.querySelector("h1")?.textContent).toBe("Einstellungen");
    expect(container.textContent).toContain("Angemeldet als ana");
    expect(container.textContent).toContain("Schriftgröße");
    expect([...container.querySelectorAll("option")].map((o) => o.textContent)).toContain("Alarme");
    expect(document.documentElement.lang).toBe("de");
  });

  // Choosing English previews the page in English before it is saved, and Save sends `language: "en"`.
  it("previews a chosen language and saves it", async () => {
    const calls = bff();
    const { container } = await page();
    await settle(8);
    await click(byText(container, "button", /^English/)!);
    expect(container.querySelector("h1")?.textContent).toBe("Preferences");
    expect(container.textContent).toContain("Previewing unsaved changes.");
    await click(byText(container, "button", "Save preferences")!);
    await settle(4);
    const put = calls.find((c) => c.method === "PUT" && c.path === "/me/preferences");
    expect((put?.body as { language?: string })?.language).toBe("en");
  });
});
