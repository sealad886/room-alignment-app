from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class ContractTests(unittest.TestCase):
    def test_openapi_contract_has_versioned_route_families_and_command_union(self):
        contract = json.loads((ROOT / "contracts" / "openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(contract["openapi"], "3.1.0")
        paths = contract["paths"]
        for path in (
            "/grants",
            "/libraries/{libraryId}/scans",
            "/libraries/{libraryId}/roots",
            "/libraries/{libraryId}/roots/{rootId}/revoke",
            "/libraries/{libraryId}/time-policy",
            "/libraries/{libraryId}/cluster-jobs",
            "/libraries/{libraryId}/cluster-generations",
            "/cluster-generations/{clusterGenerationId}",
            "/cluster-generations/{clusterGenerationId}/sessions",
            "/cluster-generations/{clusterGenerationId}/events",
            "/cluster-generations/{clusterGenerationId}/facets",
            "/cluster-generations/{clusterGenerationId}/unclustered",
            "/cluster-generations/{clusterGenerationId}/selection-preview",
            "/session-clusters/{clusterId}/memberships",
            "/event-clusters/{clusterId}/memberships",
            "/libraries/{libraryId}/cluster-suggestions",
            "/projects/{projectId}/commands",
            "/projects/{projectId}/commands/delta",
            "/projects/{projectId}/alignment-summary",
            "/projects/{projectId}/timeline-window",
            "/projects/{projectId}/program-at",
            "/projects/{projectId}/alignment-jobs",
            "/projects/{projectId}/render-plans",
            "/jobs/event-token",
            "/events",
            "/artifacts/{artifactId}/manifest",
        ):
            self.assertIn(path, paths)
        self.assertEqual(
            contract["components"]["schemas"]["ProjectCommand"]["$ref"],
            "https://room-alignment.local/contracts/commands.schema.json",
        )
        self.assertEqual(contract["security"], [{"sessionCookie": []}])
        self.assertEqual(contract["components"]["securitySchemes"]["sessionCookie"]["name"], "ra_session")
        self.assertEqual(
            contract["paths"]["/events"]["get"]["security"],
            [{"sessionCookie": [], "eventToken": []}],
        )
        for path_item in contract["paths"].values():
            for method in ("get", "post", "put", "patch", "delete"):
                if method in path_item:
                    self.assertEqual(
                        path_item[method]["responses"]["default"],
                        {"$ref": "#/components/responses/Error"},
                    )
            if "post" in path_item:
                self.assertEqual(
                    path_item["post"]["security"],
                    [{"sessionCookie": [], "csrfToken": []}],
                )
        commands = json.loads((ROOT / "contracts" / "commands.schema.json").read_text(encoding="utf-8"))
        command_types = {
            commands["$defs"][variant["$ref"].split("/")[-1]]["properties"]["commandType"]["const"]
            for variant in commands["allOf"][0]["oneOf"]
        }
        self.assertIn("SetSyncTransform", command_types)
        self.assertIn("SetClipAlignment", command_types)
        self.assertIn("SetTimelineSections", command_types)
        self.assertIn("GenerateProgramDraft", command_types)
        self.assertIn("SetAudioMode", command_types)
        self.assertIn("ReconcileBoundary", command_types)
        self.assertNotIn("InitializeProgram", command_types)

    def test_all_normative_json_contracts_parse_and_are_served(self):
        contract = json.loads((ROOT / "contracts" / "openapi.json").read_text(encoding="utf-8"))
        for name in (
            "api.schema.json",
            "domain.schema.json",
            "commands.schema.json",
            "manifest.schema.json",
            "timeline.schema.json",
        ):
            schema = json.loads((ROOT / "contracts" / name).read_text(encoding="utf-8"))
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertIn("/contracts/{contractName}", contract["paths"])
        self.assertIn("/artifacts/{artifactId}/video", contract["paths"])
        manifest = json.loads((ROOT / "contracts" / "manifest.schema.json").read_text(encoding="utf-8"))
        self.assertIn("manifestCanonicalization", manifest["required"])
        self.assertEqual(
            manifest["properties"]["manifestCanonicalization"]["const"],
            "room-alignment-canonical-json/v1",
        )
        self.assertIsNone(manifest["properties"]["artifact"]["properties"]["manifestSha256"]["const"])
        api_schema = json.loads((ROOT / "contracts" / "api.schema.json").read_text(encoding="utf-8"))
        frame_rate = api_schema["$defs"]["RenderPlanCreate"]["properties"]["frameRate"]
        self.assertEqual(frame_rate, {"type": "number", "minimum": 1, "maximum": 240})
        timeline_schema = json.loads(
            (ROOT / "contracts" / "timeline.schema.json").read_text(encoding="utf-8")
        )
        self.assertIn("ClusterFacets", timeline_schema["$defs"])
        facets_response = contract["paths"][
            "/cluster-generations/{clusterGenerationId}/facets"
        ]["get"]["responses"]["200"]
        self.assertEqual(
            facets_response["content"]["application/json"]["schema"]["$ref"],
            "https://room-alignment.local/contracts/timeline.schema.json#/$defs/ClusterFacets",
        )

    def test_generated_browser_client_is_current(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_api_client.py"), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
