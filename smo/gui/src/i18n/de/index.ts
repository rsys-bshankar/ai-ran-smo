/** German: the messages translated so far; a key it lacks is shown in English (`../index.tsx`). */
import type { MessageKey } from "../en";
import { common } from "./common";
import { preferences } from "./preferences";

export const de: Partial<Record<MessageKey, string>> = { ...common, ...preferences };
