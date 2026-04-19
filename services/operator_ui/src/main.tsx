import { render } from "preact";
import { App } from "./App";
import { applyStoredTheme } from "./theme";

applyStoredTheme();

const mount = document.getElementById("app");
if (!mount) {
  throw new Error("operator-ui: #app mount point missing");
}
render(<App />, mount);
