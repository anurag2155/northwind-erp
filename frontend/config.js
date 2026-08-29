// Where the console looks for its API. Chosen by hostname so the same file is
// correct locally and on Render, with no build step to get wrong: an earlier
// render.yaml substitution produced "https://northwind-erp-api" -- the service
// name without its domain -- and the deployed console could not reach anything.
// A ?api=<url> query parameter still overrides this at runtime.
window.ERP_BASE = location.hostname.endsWith("onrender.com")
  ? "https://northwind-erp-api.onrender.com"
  : "http://127.0.0.1:8080";
