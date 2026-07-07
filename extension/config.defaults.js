// Filled automatically when you download from Dashboard → Setup.
// Overwritten in the zip with DASHBOARD_BASE_URL from the server.
// `var` (not `const`): Gmail's SPA re-fires tab "complete" repeatedly, so the
// service worker re-injects this file into the same world multiple times.
// `const` throws "already declared" on re-injection; `var` redeclaration is a
// harmless no-op.
var RECON_DEFAULT_SERVER_URL = "https://recon.vaidikedu.com";
