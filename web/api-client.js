// Generated from contracts/openapi.json by scripts/generate_api_client.py. Do not edit.
(() => {
  const OPERATIONS = {
  "applyProjectCommand": {
    "method": "POST",
    "path": "/projects/{projectId}/commands",
    "pathParameters": [
      "projectId"
    ]
  },
  "attestReview": {
    "method": "POST",
    "path": "/render-plans/{planId}/review",
    "pathParameters": [
      "planId"
    ]
  },
  "cancelJob": {
    "method": "POST",
    "path": "/jobs/{jobId}/cancel",
    "pathParameters": [
      "jobId"
    ]
  },
  "cancelScan": {
    "method": "POST",
    "path": "/scans/{scanId}/cancel",
    "pathParameters": [
      "scanId"
    ]
  },
  "createEventToken": {
    "method": "POST",
    "path": "/jobs/event-token",
    "pathParameters": []
  },
  "createGrant": {
    "method": "POST",
    "path": "/grants",
    "pathParameters": []
  },
  "createLibrary": {
    "method": "POST",
    "path": "/libraries",
    "pathParameters": []
  },
  "createProject": {
    "method": "POST",
    "path": "/projects",
    "pathParameters": []
  },
  "createRenderPlan": {
    "method": "POST",
    "path": "/projects/{projectId}/render-plans",
    "pathParameters": [
      "projectId"
    ]
  },
  "getArtifact": {
    "method": "GET",
    "path": "/artifacts/{artifactId}",
    "pathParameters": [
      "artifactId"
    ]
  },
  "getArtifactVideo": {
    "method": "GET",
    "path": "/artifacts/{artifactId}/video",
    "pathParameters": [
      "artifactId"
    ]
  },
  "getCompiledProgram": {
    "method": "GET",
    "path": "/projects/{projectId}/program",
    "pathParameters": [
      "projectId"
    ]
  },
  "getJob": {
    "method": "GET",
    "path": "/jobs/{jobId}",
    "pathParameters": [
      "jobId"
    ]
  },
  "getJsonSchemaContract": {
    "method": "GET",
    "path": "/contracts/{contractName}",
    "pathParameters": [
      "contractName"
    ]
  },
  "getManifest": {
    "method": "GET",
    "path": "/artifacts/{artifactId}/manifest",
    "pathParameters": [
      "artifactId"
    ]
  },
  "getMedia": {
    "method": "GET",
    "path": "/media/{mediaId}",
    "pathParameters": [
      "mediaId"
    ]
  },
  "getOpenApiContract": {
    "method": "GET",
    "path": "/openapi.json",
    "pathParameters": []
  },
  "getProgramAt": {
    "method": "GET",
    "path": "/projects/{projectId}/program-at",
    "pathParameters": [
      "projectId"
    ]
  },
  "getProject": {
    "method": "GET",
    "path": "/projects/{projectId}",
    "pathParameters": [
      "projectId"
    ]
  },
  "getProjectRevision": {
    "method": "GET",
    "path": "/projects/{projectId}/revisions/{revision}",
    "pathParameters": [
      "projectId",
      "revision"
    ]
  },
  "getRenderPlan": {
    "method": "GET",
    "path": "/render-plans/{planId}",
    "pathParameters": [
      "planId"
    ]
  },
  "getScan": {
    "method": "GET",
    "path": "/scans/{scanId}",
    "pathParameters": [
      "scanId"
    ]
  },
  "getSession": {
    "method": "GET",
    "path": "/session",
    "pathParameters": []
  },
  "getSystem": {
    "method": "GET",
    "path": "/system",
    "pathParameters": []
  },
  "listClusterSuggestions": {
    "method": "GET",
    "path": "/libraries/{libraryId}/cluster-suggestions",
    "pathParameters": [
      "libraryId"
    ]
  },
  "listGrants": {
    "method": "GET",
    "path": "/grants",
    "pathParameters": []
  },
  "listLibraries": {
    "method": "GET",
    "path": "/libraries",
    "pathParameters": []
  },
  "listMedia": {
    "method": "GET",
    "path": "/libraries/{libraryId}/media",
    "pathParameters": [
      "libraryId"
    ]
  },
  "listProjects": {
    "method": "GET",
    "path": "/projects",
    "pathParameters": []
  },
  "listProvenanceResolutions": {
    "method": "GET",
    "path": "/media/{mediaId}/provenance/resolutions",
    "pathParameters": [
      "mediaId"
    ]
  },
  "listSuggestions": {
    "method": "GET",
    "path": "/projects/{projectId}/suggestions",
    "pathParameters": [
      "projectId"
    ]
  },
  "resolveProvenance": {
    "method": "POST",
    "path": "/media/{mediaId}/provenance/resolutions",
    "pathParameters": [
      "mediaId"
    ]
  },
  "revokeGrant": {
    "method": "POST",
    "path": "/grants/{grantId}/revoke",
    "pathParameters": [
      "grantId"
    ]
  },
  "startAlignmentAnalysis": {
    "method": "POST",
    "path": "/projects/{projectId}/alignment-jobs",
    "pathParameters": [
      "projectId"
    ]
  },
  "startClusterAnalysis": {
    "method": "POST",
    "path": "/libraries/{libraryId}/cluster-jobs",
    "pathParameters": [
      "libraryId"
    ]
  },
  "startRender": {
    "method": "POST",
    "path": "/render-plans/{planId}/render",
    "pathParameters": [
      "planId"
    ]
  },
  "startScan": {
    "method": "POST",
    "path": "/libraries/{libraryId}/scans",
    "pathParameters": [
      "libraryId"
    ]
  },
  "streamEvents": {
    "method": "GET",
    "path": "/events",
    "pathParameters": []
  },
  "updateLibraryTimePolicy": {
    "method": "POST",
    "path": "/libraries/{libraryId}/time-policy",
    "pathParameters": [
      "libraryId"
    ]
  }
};

  class APIError extends Error {
    constructor(error, status) {
      super(error?.message || `Request failed: ${status}`);
      this.name = "APIError";
      this.code = error?.code || "INTERNAL_ERROR";
      this.status = status;
      this.retryable = Boolean(error?.retryable);
      this.details = error?.details || {};
    }
  }

  class RoomAlignmentAPIClient {
    constructor(base = "/api/v1") { this.base = base; this.csrf = null; }

    async request(path, options = {}) {
      const headers = {"Accept": "application/json", ...(options.headers || {})};
      if (options.body !== undefined) headers["Content-Type"] = "application/json";
      if (options.method && options.method !== "GET" && this.csrf) headers["X-CSRF-Token"] = this.csrf;
      const response = await fetch(`${this.base}${path}`, {...options, headers, credentials: "same-origin"});
      const contentType = response.headers.get("Content-Type") || "";
      const payload = contentType.includes("json") ? await response.json() : await response.text();
      if (!response.ok) throw new APIError(payload?.error, response.status);
      return payload;
    }

    async invoke(operationId, parameters = {}, body = undefined) {
      const operation = OPERATIONS[operationId];
      if (!operation) throw new Error(`Unknown API operation: ${operationId}`);
      let path = operation.path;
      for (const name of operation.pathParameters) {
        if (parameters[name] === undefined || parameters[name] === null) {
          throw new Error(`Missing path parameter ${name} for ${operationId}`);
        }
        path = path.replace(`{${name}}`, encodeURIComponent(String(parameters[name])));
      }
      const query = new URLSearchParams(parameters.query || {}).toString();
      if (query) path += `?${query}`;
      const options = {method: operation.method};
      if (body !== undefined) options.body = JSON.stringify(body);
      const value = await this.request(path, options);
      if (operationId === "getSession") this.csrf = value.csrfToken;
      return value;
    }
  }

  for (const operationId of Object.keys(OPERATIONS)) {
    RoomAlignmentAPIClient.prototype[operationId] = function(parameters = {}, body = undefined) {
      return this.invoke(operationId, parameters, body);
    };
  }

  window.RoomAlignmentAPI = {APIError, RoomAlignmentAPIClient, OPERATIONS};
})();
