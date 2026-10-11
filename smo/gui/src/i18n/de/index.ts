/** German: the messages translated so far; a key it lacks is shown in English (`../index.tsx`). */
import type { MessageKey } from "../en";
import { common } from "./common";
import { admin } from "./admin";
import { alarms } from "./alarms";
import { approvals } from "./approvals";
import { components } from "./components";
import { dashboard } from "./dashboard";
import { decisions } from "./decisions";
import { element } from "./element";
import { exports } from "./exports";
import { kit } from "./kit";
import { login } from "./login";
import { preferences } from "./preferences";
import { rappDetail } from "./rapp-detail";
import { security } from "./security";
import { shell } from "./shell";
import { software } from "./software";
import { topology } from "./topology";

export const de: Partial<Record<MessageKey, string>> = { ...common, ...admin, ...alarms, ...approvals, ...components, ...dashboard, ...decisions, ...element, ...exports, ...kit, ...login, ...preferences, ...rappDetail, ...security, ...shell, ...software, ...topology };
