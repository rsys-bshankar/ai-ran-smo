/**
 * What the onboarding screens (MGT-14.6) decide without a screen: the wording of each onboarding state, which actions a state allows (the ones RAN NF OAM's
 * state machine allows, so a button is never offered that the backend would refuse as an illegal transition), and the template form turned into the request
 * body RAN NF OAM validates (the same limits, so a mistake is shown before the call). Who asked (`requestedBy`) is never sent: the GUI backend sets it.
 * Used by Configuration → Element onboarding (pages/configuration/sections/OnboardingTemplates.tsx, ElementOnboarding.tsx, OnboardingDialogs.tsx).
 * Software campaigns (MGT-15.5 to 15.7) have one set of rules, on the Software page (pages/software/data/form.ts, data/types.ts).
 * Pure functions only, no network and no React, so lifecycle.test.ts covers it without a screen.
 * A limit changed in RAN NF OAM's validation must be changed here as well, or the form accepts what the backend then refuses.
 * Words come from the catalog (`i18n/en/lib.ts`, `lifecycle.*`, GUI-6.4): the checks take a last `t` (English when omitted).
 */

import type { OnboardingStatus, OnboardingTemplate, TemplateChange } from "../api/types";
import type { TKey, Translator } from "../i18n";
import { ENGLISH } from "./domain";

/** What each onboarding state means: the catalog key of the sentence (`lifecycle.onboarding.*`, shown with `t`). */
export const ONBOARDING_MEANING: Record<OnboardingStatus, TKey> = {
  DISCOVERED: "lifecycle.onboarding.DISCOVERED",
  NO_TEMPLATE: "lifecycle.onboarding.NO_TEMPLATE",
  TEMPLATE_SELECTED: "lifecycle.onboarding.TEMPLATE_SELECTED",
  APPLYING: "lifecycle.onboarding.APPLYING",
  ONBOARDED: "lifecycle.onboarding.ONBOARDED",
  FAILED: "lifecycle.onboarding.FAILED",
};

// ---------------------------------------------------------------- onboarding of an element

/** Which buttons an element's onboarding row offers. Applying is possible once a template is selected, and again after it succeeded or failed; selecting (again)
 * is possible wherever the template is not being written. */
export function onboardingActions(status: OnboardingStatus): { apply: boolean; select: boolean } {
  return { apply: status === "TEMPLATE_SELECTED" || status === "ONBOARDED" || status === "FAILED", select: status !== "APPLYING" };
}

// ---------------------------------------------------------------- templates

export const TEMPLATE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
const OPERATIONS = ["merge", "replace", "create", "delete", "remove"];

/** The template editor's fields as the form holds them: every field is text or a flag, and `changes` is the JSON the person edits, not yet parsed. */
export interface TemplateForm {
  name: string; entityType: string; vendorName: string; description: string; softwareBaseline: string;
  requireBaseline: boolean; autoApply: boolean; enabled: boolean; changes: string;
}

export const STARTER_CHANGES = JSON.stringify([{ managedFunctionRef: "GNBDUFunction=1", attributeChanges: { administrativeState: "UNLOCKED" }, operation: "merge" }], null, 2);

export function blankTemplate(): TemplateForm {
  return { name: "", entityType: "", vendorName: "", description: "", softwareBaseline: "", requireBaseline: false, autoApply: false, enabled: true, changes: STARTER_CHANGES };
}

/** Turns a stored template into the editor's fields (null becomes a blank, the changes become indented JSON), so editing a template and saving it unchanged sends back what it held. */
export function templateForm(t: OnboardingTemplate): TemplateForm {
  return {
    name: t.name, entityType: t.entityType, vendorName: t.vendorName ?? "", description: t.description ?? "", softwareBaseline: t.softwareBaseline ?? "",
    requireBaseline: t.requireBaseline, autoApply: t.autoApply, enabled: t.enabled, changes: JSON.stringify(t.changes, null, 2),
  };
}

/** The changes of a template from the editor's JSON: a non-empty list of up to 200 changes, each an object with `attributeChanges` (an object), optionally a
 * `managedFunctionRef` and an `operation`; a change names no `managedElementRef` (the element is the new element's own). */
export function parseChanges(text: string, t: Translator = ENGLISH): { ok: true; value: TemplateChange[] } | { ok: false; error: string } {
  let parsed: unknown;
  try { parsed = JSON.parse(text); } catch (e) { return { ok: false, error: t("lifecycle.changes.notJson", { error: (e as Error).message }) }; }
  if (!Array.isArray(parsed) || parsed.length === 0) return { ok: false, error: t("lifecycle.changes.notList") };
  if (parsed.length > 200) return { ok: false, error: t("lifecycle.changes.tooMany") };
  const out: TemplateChange[] = [];
  for (const [i, c] of parsed.entries()) {
    const n = i + 1;
    if (c === null || typeof c !== "object" || Array.isArray(c)) return { ok: false, error: t("lifecycle.change.notObject", { n }) };
    const o = c as Record<string, unknown>;
    if ("managedElementRef" in o) return { ok: false, error: t("lifecycle.change.elementRef", { n }) };
    const unknown = Object.keys(o).filter((k) => !["managedFunctionRef", "attributeChanges", "operation"].includes(k));
    if (unknown.length) return { ok: false, error: t("lifecycle.change.unknownField", { n, field: unknown[0] }) };
    const attrs = o.attributeChanges ?? {};
    if (attrs === null || typeof attrs !== "object" || Array.isArray(attrs)) return { ok: false, error: t("lifecycle.change.attributes", { n }) };
    const operation = o.operation ?? "merge";
    if (typeof operation !== "string" || !OPERATIONS.includes(operation)) return { ok: false, error: t("lifecycle.change.operation", { n, list: OPERATIONS.join(", ") }) };
    const fn = o.managedFunctionRef;
    if (fn !== undefined && fn !== null && (typeof fn !== "string" || fn.length > 500)) return { ok: false, error: t("lifecycle.change.functionRef", { n }) };
    out.push({ ...(typeof fn === "string" && fn ? { managedFunctionRef: fn } : {}), attributeChanges: attrs as Record<string, unknown>, operation });
  }
  return { ok: true, value: out };
}

/** The body of PUT /ran-nf-oam/onboarding-templates/{name}, or the problem to show. `editing` leaves the name alone (it is the key). */
export function templatePayload(form: TemplateForm, editing: boolean, t: Translator = ENGLISH): { ok: true; name: string; body: Record<string, unknown> } | { ok: false; error: string } {
  const name = form.name.trim();
  if (!editing && !TEMPLATE_NAME.test(name)) return { ok: false, error: t("lifecycle.template.name") };
  if (!form.entityType.trim()) return { ok: false, error: t("lifecycle.template.entityType") };
  const baseline = form.softwareBaseline.trim();
  if (form.requireBaseline && !baseline) return { ok: false, error: t("lifecycle.template.baseline") };
  const changes = parseChanges(form.changes, t);
  if (!changes.ok) return changes;
  return {
    ok: true, name,
    body: {
      entityType: form.entityType.trim(), vendorName: form.vendorName.trim() || null, description: form.description.trim() || null, changes: changes.value,
      softwareBaseline: baseline || null, requireBaseline: form.requireBaseline, autoApply: form.autoApply, enabled: form.enabled,
    },
  };
}
