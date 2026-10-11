/** GUI-6.4: no page gains words written in the code instead of the catalog. Every area (a page, the shell, the shared components and kit) is held
 * to its count in `literals-baseline.json`; an area that reached 0 stays at 0, and a count that fell must be written down (`node
 * scripts/i18n-literals.mjs --write`) so it cannot rise again. `node scripts/i18n-literals.mjs <area>` lists an area's literals.
 * Run: `npx vitest run src/i18n`. */
import { describe, expect, it } from "vitest";

// @ts-expect-error -- a plain ES module script with no type declarations
import { areas, baseline as readBaseline, counts, literalsIn } from "../../scripts/i18n-literals.mjs";

const baseline = readBaseline() as Record<string, number>;
const now = counts() as Record<string, number>;

describe("words written in the code (GUI-6.4)", () => {
  // Every area is in the baseline and no area has more literals than it allows; a new page starts at 0.
  it("never grow past the baseline", () => {
    for (const area of areas() as string[]) {
      const allowed = baseline[area] ?? 0;
      expect(now[area], `${area} has ${now[area]} literals, its baseline is ${allowed}: ${JSON.stringify((literalsIn(area) as unknown[]).slice(0, 5))}`)
        .toBeLessThanOrEqual(allowed);
    }
  });

  // A count that fell is written down, and the baseline names no area that is gone.
  it("keep the baseline exact", () => {
    expect(now).toEqual(baseline);
  });

  // The pages translated so far have no literal at all.
  it("leave the translated pages with none", () => {
    expect(now["pages/preferences"]).toBe(0);
  });
});
