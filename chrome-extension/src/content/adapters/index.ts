// chrome-extension/src/content/adapters/index.ts
// Importing the adapter modules registers them (ADAPTERS.push at import time).
// Greenhouse, Workday, Lever and Jobvite keep their own modules; `common`
// registers the rest.
import "./greenhouse";
import "./workday";
import "./lever";
import "./jobvite";
import "./common";
export { getAdapter, resolveAdapter, ADAPTERS } from "./registry";
export type { SiteAdapter, FieldContext, AnswerContext, FillContext, AdapterFillResult } from "./types";
export {
  classifyWithAdapter,
  resolveAnswerWithAdapter,
  tryAdapterOperation,
  runAdapterOperations,
} from "./apply";
