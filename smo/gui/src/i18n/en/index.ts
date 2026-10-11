/** English, the source language: every message the console has. Every other language is checked against these keys and falls back to them. */
import { common } from "./common";
import { preferences } from "./preferences";

export const en = { ...common, ...preferences } as const;

/** A message's key. */
export type MessageKey = keyof typeof en;
