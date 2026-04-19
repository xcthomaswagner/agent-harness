import { render } from "preact";
import { App } from "./App";
import { installGlobalLinkInterceptor } from "./router";
import { applyStoredTheme } from "./theme";

applyStoredTheme();
installGlobalLinkInterceptor();

const mount = document.getElementById("app");
if (!mount) {
  throw new Error("operator-ui: #app mount point missing");
}
render(<App />, mount);
