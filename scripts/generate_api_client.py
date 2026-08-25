from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contracts" / "openapi.json"
TARGET = ROOT / "web" / "api-client.js"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def operations() -> dict[str, dict[str, object]]:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    result: dict[str, dict[str, object]] = {}
    for path, path_item in contract["paths"].items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            operation_id = operation.get("operationId")
            if not operation_id or operation_id in result:
                raise ValueError(f"Every operation needs a unique operationId: {method.upper()} {path}")
            result[operation_id] = {
                "method": method.upper(),
                "path": path,
                "pathParameters": re.findall(r"\{([^}]+)\}", path),
            }
    return dict(sorted(result.items()))


def generated_client() -> str:
    operation_json = json.dumps(operations(), indent=2, sort_keys=True)
    return f'''// Generated from contracts/openapi.json by scripts/generate_api_client.py. Do not edit.
(() => {{
  const OPERATIONS = {operation_json};

  class APIError extends Error {{
    constructor(error, status) {{
      super(error?.message || `Request failed: ${{status}}`);
      this.name = "APIError";
      this.code = error?.code || "INTERNAL_ERROR";
      this.status = status;
      this.retryable = Boolean(error?.retryable);
      this.details = error?.details || {{}};
    }}
  }}

  class RoomAlignmentAPIClient {{
    constructor(base = "/api/v1") {{ this.base = base; this.csrf = null; }}

    async request(path, options = {{}}) {{
      const headers = {{"Accept": "application/json", ...(options.headers || {{}})}};
      if (options.body !== undefined) headers["Content-Type"] = "application/json";
      if (options.method && options.method !== "GET" && this.csrf) headers["X-CSRF-Token"] = this.csrf;
      const response = await fetch(`${{this.base}}${{path}}`, {{...options, headers, credentials: "same-origin"}});
      const contentType = response.headers.get("Content-Type") || "";
      const payload = contentType.includes("json") ? await response.json() : await response.text();
      if (!response.ok) throw new APIError(payload?.error, response.status);
      return payload;
    }}

    async invoke(operationId, parameters = {{}}, body = undefined) {{
      const operation = OPERATIONS[operationId];
      if (!operation) throw new Error(`Unknown API operation: ${{operationId}}`);
      let path = operation.path;
      for (const name of operation.pathParameters) {{
        if (parameters[name] === undefined || parameters[name] === null) {{
          throw new Error(`Missing path parameter ${{name}} for ${{operationId}}`);
        }}
        path = path.replace(`{{${{name}}}}`, encodeURIComponent(String(parameters[name])));
      }}
      const query = new URLSearchParams(parameters.query || {{}}).toString();
      if (query) path += `?${{query}}`;
      const options = {{method: operation.method}};
      if (body !== undefined) options.body = JSON.stringify(body);
      const value = await this.request(path, options);
      if (operationId === "getSession") this.csrf = value.csrfToken;
      return value;
    }}
  }}

  for (const operationId of Object.keys(OPERATIONS)) {{
    RoomAlignmentAPIClient.prototype[operationId] = function(parameters = {{}}, body = undefined) {{
      return this.invoke(operationId, parameters, body);
    }};
  }}

  window.RoomAlignmentAPI = {{APIError, RoomAlignmentAPIClient, OPERATIONS}};
}})();
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = generated_client()
    if args.check:
        if not TARGET.exists() or TARGET.read_text(encoding="utf-8") != expected:
            print(f"Generated client is stale: {TARGET}", file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
