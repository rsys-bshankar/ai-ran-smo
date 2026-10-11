/** English, the source language: every message the console has. Every other language is checked against these keys and falls back to them. */
import { common } from "./common";
import { admin } from "./admin";
import { aiml } from "./aiml";
import { alarms } from "./alarms";
import { approvals } from "./approvals";
import { components } from "./components";
import { configuration } from "./configuration";
import { dashboard } from "./dashboard";
import { data } from "./data";
import { decisions } from "./decisions";
import { element } from "./element";
import { exports } from "./exports";
import { flows } from "./flows";
import { infrastructure } from "./infrastructure";
import { intents } from "./intents";
import { kit } from "./kit";
import { kpis } from "./kpis";
import { lib } from "./lib";
import { login } from "./login";
import { preferences } from "./preferences";
import { rappDetail } from "./rapp-detail";
import { rapps } from "./rapps";
import { safeguards } from "./safeguards";
import { security } from "./security";
import { shell } from "./shell";
import { software } from "./software";
import { topology } from "./topology";

export const en = { ...common, ...admin, ...aiml, ...alarms, ...approvals, ...components, ...configuration, ...dashboard, ...data, ...decisions, ...element, ...exports, ...flows, ...infrastructure, ...intents, ...kit, ...kpis, ...lib, ...login, ...preferences, ...rappDetail, ...rapps, ...safeguards, ...security, ...shell, ...software, ...topology } as const;

/** A message's key. */
export type MessageKey = keyof typeof en;
