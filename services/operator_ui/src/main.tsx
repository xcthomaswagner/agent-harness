import { render } from "preact";
import { App } from "./App";

const mount = document.getElementById("app");
if (!mount) {
  throw new Error("operator-ui: #app mount point missing");
}
render(<App />, mount);
