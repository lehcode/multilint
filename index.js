// OpenCode V2 loads a plugin directory through its root index.js and ignores package.json, so a
// `plugins` entry pointing at this package's directory needs this file; the bundle stays in dist/.
export { default } from "./dist/index.js";
