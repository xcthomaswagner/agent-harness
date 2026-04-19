import { render } from "preact";
import { App } from "./App";
import { installGlobalLinkInterceptor } from "./router";
import { applyStoredPrefs } from "./theme";

applyStoredPrefs();
installGlobalLinkInterceptor();

const mount = document.getElementById("app");
if (!mount) {
  throw new Error("operator-ui: #app mount point missing");
}
render(<App />, mount);
