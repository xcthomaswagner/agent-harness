export {
  parseJsonObject,
  readableError,
  readableErrorText,
} from "../api/errors";

export interface ActionNotice {
  tone: "ok" | "warn" | "err";
  text: string;
}
