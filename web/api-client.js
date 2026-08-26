// Generated from contracts/openapi.json by scripts/generate_api_client.py. Do not edit.
(() => {
  const OPERATIONS = {
  "addLibraryRoot": {
    "binary": false,
    "method": "POST",
    "path": "/libraries/{libraryId}/roots",
    "pathParameters": [
      "libraryId"
    ],
    "queryParameters": {}
  },
  "applyProjectCommand": {
    "binary": false,
    "method": "POST",
    "path": "/projects/{projectId}/commands",
    "pathParameters": [
      "projectId"
    ],
    "queryParameters": {
      "preview": false
    }
  },
  "applyProjectDeltaCommand": {
    "binary": false,
    "method": "POST",
    "path": "/projects/{projectId}/commands/delta",
    "pathParameters": [
      "projectId"
    ],
    "queryParameters": {
      "preview": false
    }
  },
  "attestReview": {
    "binary": false,
    "method": "POST",
    "path": "/render-plans/{planId}/review",
    "pathParameters": [
      "planId"
    ],
    "queryParameters": {}
  },
  "cancelJob": {
    "binary": false,
    "method": "POST",
    "path": "/jobs/{jobId}/cancel",
    "pathParameters": [
      "jobId"
    ],
    "queryParameters": {}
  },
  "cancelScan": {
    "binary": false,
    "method": "POST",
    "path": "/scans/{scanId}/cancel",
    "pathParameters": [
      "scanId"
    ],
    "queryParameters": {}
  },
  "createEventToken": {
    "binary": false,
    "method": "POST",
    "path": "/jobs/event-token",
    "pathParameters": [],
    "queryParameters": {}
  },
  "createGrant": {
    "binary": false,
    "method": "POST",
    "path": "/grants",
    "pathParameters": [],
    "queryParameters": {}
  },
  "createLibrary": {
    "binary": false,
    "method": "POST",
    "path": "/libraries",
    "pathParameters": [],
    "queryParameters": {}
  },
  "createProject": {
    "binary": false,
    "method": "POST",
    "path": "/projects",
    "pathParameters": [],
    "queryParameters": {}
  },
  "createRenderPlan": {
    "binary": false,
    "method": "POST",
    "path": "/projects/{projectId}/render-plans",
    "pathParameters": [
      "projectId"
    ],
    "queryParameters": {}
  },
  "getAlignmentSummary": {
    "binary": false,
    "method": "GET",
    "path": "/projects/{projectId}/alignment-summary",
    "pathParameters": [
      "projectId"
    ],
    "queryParameters": {}
  },
  "getApplicationSettings": {
    "binary": false,
    "method": "GET",
    "path": "/settings",
    "pathParameters": [],
    "queryParameters": {}
  },
  "getArtifact": {
    "binary": false,
    "method": "GET",
    "path": "/artifacts/{artifactId}",
    "pathParameters": [
      "artifactId"
    ],
    "queryParameters": {}
  },
  "getArtifactVideo": {
    "binary": true,
    "method": "GET",
    "path": "/artifacts/{artifactId}/video",
    "pathParameters": [
      "artifactId"
    ],
    "queryParameters": {}
  },
  "getAudioWaveform": {
    "binary": false,
    "method": "GET",
    "path": "/media/{mediaId}/waveform",
    "pathParameters": [
      "mediaId"
    ],
    "queryParameters": {
      "endSourceUs": false,
      "maxPoints": false,
      "startSourceUs": false
    }
  },
  "getClusterFacets": {
    "binary": false,
    "method": "GET",
    "path": "/cluster-generations/{clusterGenerationId}/facets",
    "pathParameters": [
      "clusterGenerationId"
    ],
    "queryParameters": {}
  },
  "getClusterGeneration": {
    "binary": false,
    "method": "GET",
    "path": "/cluster-generations/{clusterGenerationId}",
    "pathParameters": [
      "clusterGenerationId"
    ],
    "queryParameters": {}
  },
  "getCompiledProgram": {
    "binary": false,
    "method": "GET",
    "path": "/projects/{projectId}/program",
    "pathParameters": [
      "projectId"
    ],
    "queryParameters": {}
  },
  "getJob": {
    "binary": false,
    "method": "GET",
    "path": "/jobs/{jobId}",
    "pathParameters": [
      "jobId"
    ],
    "queryParameters": {}
  },
  "getJsonSchemaContract": {
    "binary": false,
    "method": "GET",
    "path": "/contracts/{contractName}",
    "pathParameters": [
      "contractName"
    ],
    "queryParameters": {}
  },
  "getManifest": {
    "binary": false,
    "method": "GET",
    "path": "/artifacts/{artifactId}/manifest",
    "pathParameters": [
      "artifactId"
    ],
    "queryParameters": {}
  },
  "getMedia": {
    "binary": false,
    "method": "GET",
    "path": "/media/{mediaId}",
    "pathParameters": [
      "mediaId"
    ],
    "queryParameters": {}
  },
  "getMediaPreview": {
    "binary": true,
    "method": "GET",
    "path": "/media/{mediaId}/preview",
    "pathParameters": [
      "mediaId"
    ],
    "queryParameters": {}
  },
  "getOpenApiContract": {
    "binary": false,
    "method": "GET",
    "path": "/openapi.json",
    "pathParameters": [],
    "queryParameters": {}
  },
  "getProgramAt": {
    "binary": false,
    "method": "GET",
    "path": "/projects/{projectId}/program-at",
    "pathParameters": [
      "projectId"
    ],
    "queryParameters": {
      "outputUs": false
    }
  },
  "getProject": {
    "binary": false,
    "method": "GET",
    "path": "/projects/{projectId}",
    "pathParameters": [
      "projectId"
    ],
    "queryParameters": {}
  },
  "getProjectPreparation": {
    "binary": false,
    "method": "GET",
    "path": "/projects/{projectId}/preparation",
    "pathParameters": [
      "projectId"
    ],
    "queryParameters": {}
  },
  "getProjectRevision": {
    "binary": false,
    "method": "GET",
    "path": "/projects/{projectId}/revisions/{revision}",
    "pathParameters": [
      "projectId",
      "revision"
    ],
    "queryParameters": {}
  },
  "getRenderPlan": {
    "binary": false,
    "method": "GET",
    "path": "/render-plans/{planId}",
    "pathParameters": [
      "planId"
    ],
    "queryParameters": {}
  },
  "getScan": {
    "binary": false,
    "method": "GET",
    "path": "/scans/{scanId}",
    "pathParameters": [
      "scanId"
    ],
    "queryParameters": {}
  },
  "getSession": {
    "binary": false,
    "method": "GET",
    "path": "/session",
    "pathParameters": [],
    "queryParameters": {}
  },
  "getSystem": {
    "binary": false,
    "method": "GET",
    "path": "/system",
    "pathParameters": [],
    "queryParameters": {}
  },
  "getTimelineSectionProposal": {
    "binary": false,
    "method": "GET",
    "path": "/projects/{projectId}/timeline-section-proposal",
    "pathParameters": [
      "projectId"
    ],
    "queryParameters": {
      "gapMode": false
    }
  },
  "getTimelineWindow": {
    "binary": false,
    "method": "GET",
    "path": "/projects/{projectId}/timeline-window",
    "pathParameters": [
      "projectId"
    ],
    "queryParameters": {
      "endAlignedUs": true,
      "lane": false,
      "resolutionUs": true,
      "startAlignedUs": true
    }
  },
  "listAlignmentProposalSets": {
    "binary": false,
    "method": "GET",
    "path": "/projects/{projectId}/alignment-proposal-sets",
    "pathParameters": [
      "projectId"
    ],
    "queryParameters": {}
  },
  "listClusterGenerations": {
    "binary": false,
    "method": "GET",
    "path": "/libraries/{libraryId}/cluster-generations",
    "pathParameters": [
      "libraryId"
    ],
    "queryParameters": {
      "cursor": false,
      "limit": false
    }
  },
  "listClusterSuggestions": {
    "binary": false,
    "method": "GET",
    "path": "/libraries/{libraryId}/cluster-suggestions",
    "pathParameters": [
      "libraryId"
    ],
    "queryParameters": {}
  },
  "listEventClusters": {
    "binary": false,
    "method": "GET",
    "path": "/cluster-generations/{clusterGenerationId}/events",
    "pathParameters": [
      "clusterGenerationId"
    ],
    "queryParameters": {
      "cursor": false,
      "endUs": false,
      "limit": false,
      "rootId": false,
      "sessionId": false,
      "sourceCandidateId": false,
      "startUs": false,
      "warning": false
    }
  },
  "listEventMemberships": {
    "binary": false,
    "method": "GET",
    "path": "/event-clusters/{clusterId}/memberships",
    "pathParameters": [
      "clusterId"
    ],
    "queryParameters": {
      "cursor": false,
      "limit": false
    }
  },
  "listGrants": {
    "binary": false,
    "method": "GET",
    "path": "/grants",
    "pathParameters": [],
    "queryParameters": {}
  },
  "listLibraries": {
    "binary": false,
    "method": "GET",
    "path": "/libraries",
    "pathParameters": [],
    "queryParameters": {}
  },
  "listLibraryRoots": {
    "binary": false,
    "method": "GET",
    "path": "/libraries/{libraryId}/roots",
    "pathParameters": [
      "libraryId"
    ],
    "queryParameters": {}
  },
  "listMedia": {
    "binary": false,
    "method": "GET",
    "path": "/libraries/{libraryId}/media",
    "pathParameters": [
      "libraryId"
    ],
    "queryParameters": {
      "cursor": false,
      "generation": false,
      "limit": false
    }
  },
  "listProjects": {
    "binary": false,
    "method": "GET",
    "path": "/projects",
    "pathParameters": [],
    "queryParameters": {}
  },
  "listProvenanceResolutions": {
    "binary": false,
    "method": "GET",
    "path": "/media/{mediaId}/provenance/resolutions",
    "pathParameters": [
      "mediaId"
    ],
    "queryParameters": {
      "field": false
    }
  },
  "listSessionClusters": {
    "binary": false,
    "method": "GET",
    "path": "/cluster-generations/{clusterGenerationId}/sessions",
    "pathParameters": [
      "clusterGenerationId"
    ],
    "queryParameters": {
      "cursor": false,
      "endUs": false,
      "limit": false,
      "rootId": false,
      "sourceCandidateId": false,
      "startUs": false,
      "warning": false
    }
  },
  "listSessionMemberships": {
    "binary": false,
    "method": "GET",
    "path": "/session-clusters/{clusterId}/memberships",
    "pathParameters": [
      "clusterId"
    ],
    "queryParameters": {
      "cursor": false,
      "limit": false
    }
  },
  "listSuggestions": {
    "binary": false,
    "method": "GET",
    "path": "/projects/{projectId}/suggestions",
    "pathParameters": [
      "projectId"
    ],
    "queryParameters": {}
  },
  "listUnclusteredMemberships": {
    "binary": false,
    "method": "GET",
    "path": "/cluster-generations/{clusterGenerationId}/unclustered",
    "pathParameters": [
      "clusterGenerationId"
    ],
    "queryParameters": {
      "cursor": false,
      "limit": false
    }
  },
  "previewProjectSelection": {
    "binary": false,
    "method": "POST",
    "path": "/cluster-generations/{clusterGenerationId}/selection-preview",
    "pathParameters": [
      "clusterGenerationId"
    ],
    "queryParameters": {}
  },
  "resolveProvenance": {
    "binary": false,
    "method": "POST",
    "path": "/media/{mediaId}/provenance/resolutions",
    "pathParameters": [
      "mediaId"
    ],
    "queryParameters": {}
  },
  "revokeGrant": {
    "binary": false,
    "method": "POST",
    "path": "/grants/{grantId}/revoke",
    "pathParameters": [
      "grantId"
    ],
    "queryParameters": {}
  },
  "revokeLibraryRoot": {
    "binary": false,
    "method": "POST",
    "path": "/libraries/{libraryId}/roots/{rootId}/revoke",
    "pathParameters": [
      "libraryId",
      "rootId"
    ],
    "queryParameters": {}
  },
  "startAlignmentAnalysis": {
    "binary": false,
    "method": "POST",
    "path": "/projects/{projectId}/alignment-jobs",
    "pathParameters": [
      "projectId"
    ],
    "queryParameters": {}
  },
  "startClusterAnalysis": {
    "binary": false,
    "method": "POST",
    "path": "/libraries/{libraryId}/cluster-jobs",
    "pathParameters": [
      "libraryId"
    ],
    "queryParameters": {}
  },
  "startRender": {
    "binary": false,
    "method": "POST",
    "path": "/render-plans/{planId}/render",
    "pathParameters": [
      "planId"
    ],
    "queryParameters": {}
  },
  "startScan": {
    "binary": false,
    "method": "POST",
    "path": "/libraries/{libraryId}/scans",
    "pathParameters": [
      "libraryId"
    ],
    "queryParameters": {}
  },
  "streamEvents": {
    "binary": false,
    "method": "GET",
    "path": "/events",
    "pathParameters": [],
    "queryParameters": {
      "after": false,
      "token": true
    }
  },
  "updateApplicationSettings": {
    "binary": false,
    "method": "PUT",
    "path": "/settings",
    "pathParameters": [],
    "queryParameters": {}
  },
  "updateLibraryTimePolicy": {
    "binary": false,
    "method": "POST",
    "path": "/libraries/{libraryId}/time-policy",
    "pathParameters": [
      "libraryId"
    ],
    "queryParameters": {}
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
      const {binary, ...requestOptions} = options;
      const response = await fetch(`${this.base}${path}`, {...requestOptions, headers, credentials: "same-origin"});
      const contentType = response.headers.get("Content-Type") || "";
      const payload = contentType.includes("json")
        ? await response.json()
        : (binary ? await response.blob() : await response.text());
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
      const supplied = parameters.query || {};
      const allowed = new Set(Object.keys(operation.queryParameters));
      for (const name of Object.keys(supplied)) {
        if (!allowed.has(name)) throw new Error(`Unknown query parameter ${name} for ${operationId}`);
      }
      for (const [name, required] of Object.entries(operation.queryParameters)) {
        if (required && (supplied[name] === undefined || supplied[name] === null)) {
          throw new Error(`Missing query parameter ${name} for ${operationId}`);
        }
      }
      const unexpected = Object.keys(parameters).filter(name => name !== "query" && !operation.pathParameters.includes(name));
      if (unexpected.length) throw new Error(`Unknown parameter ${unexpected[0]} for ${operationId}`);
      const query = new URLSearchParams(supplied).toString();
      if (query) path += `?${query}`;
      const options = {method: operation.method};
      if (body !== undefined) options.body = JSON.stringify(body);
      options.binary = operation.binary;
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
