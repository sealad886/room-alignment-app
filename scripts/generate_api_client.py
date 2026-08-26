from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contracts" / "openapi.json"
TARGET = ROOT / "web" / "api-client.js"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}


def operations() -> dict[str, dict[str, object]]:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    shared_parameters = contract.get("components", {}).get("parameters", {})

    def resolve_parameter(value: dict[str, object]) -> dict[str, object]:
        reference = value.get("$ref")
        if not reference:
            return value
        prefix = "#/components/parameters/"
        if not isinstance(reference, str) or not reference.startswith(prefix):
            raise ValueError(f"Unsupported parameter reference: {reference}")
        return shared_parameters[reference.removeprefix(prefix)]

    result: dict[str, dict[str, object]] = {}
    for path, path_item in contract["paths"].items():
        for method, operation in path_item.items():
            if method not in HTTP_METHODS:
                continue
            operation_id = operation.get("operationId")
            if not operation_id or operation_id in result:
                raise ValueError(f"Every operation needs a unique operationId: {method.upper()} {path}")
            parameters = [
                resolve_parameter(value)
                for value in [*path_item.get("parameters", []), *operation.get("parameters", [])]
            ]
            path_parameters = [
                str(value["name"])
                for value in parameters
                if value.get("in") == "path"
            ]
            query_parameters = {
                str(value["name"]): bool(value.get("required"))
                for value in parameters
                if value.get("in") == "query"
            }
            declared_in_path = {
                part[1:-1] for part in path.split("/") if part.startswith("{") and part.endswith("}")
            }
            if declared_in_path != set(path_parameters):
                raise ValueError(f"Path parameters are not fully declared: {method.upper()} {path}")
            response_content = operation.get("responses", {}).get("200", {}).get("content", {})
            result[operation_id] = {
                "method": method.upper(),
                "path": path,
                "pathParameters": path_parameters,
                "queryParameters": query_parameters,
                "binary": any(
                    value.get("schema", {}).get("format") == "binary"
                    for value in response_content.values()
                ),
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
      const {{binary, ...requestOptions}} = options;
      const response = await fetch(`${{this.base}}${{path}}`, {{...requestOptions, headers, credentials: "same-origin"}});
      const contentType = response.headers.get("Content-Type") || "";
      const payload = contentType.includes("json")
        ? await response.json()
        : (binary ? await response.blob() : await response.text());
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
      const supplied = parameters.query || {{}};
      const allowed = new Set(Object.keys(operation.queryParameters));
      for (const name of Object.keys(supplied)) {{
        if (!allowed.has(name)) throw new Error(`Unknown query parameter ${{name}} for ${{operationId}}`);
      }}
      for (const [name, required] of Object.entries(operation.queryParameters)) {{
        if (required && (supplied[name] === undefined || supplied[name] === null)) {{
          throw new Error(`Missing query parameter ${{name}} for ${{operationId}}`);
        }}
      }}
      const unexpected = Object.keys(parameters).filter(name => name !== "query" && !operation.pathParameters.includes(name));
      if (unexpected.length) throw new Error(`Unknown parameter ${{unexpected[0]}} for ${{operationId}}`);
      const query = new URLSearchParams(supplied).toString();
      if (query) path += `?${{query}}`;
      const options = {{method: operation.method}};
      if (body !== undefined) options.body = JSON.stringify(body);
      options.binary = operation.binary;
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
