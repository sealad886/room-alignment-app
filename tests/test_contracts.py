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
            "/libraries/{libraryId}/time-policy",
            "/libraries/{libraryId}/cluster-jobs",
            "/libraries/{libraryId}/cluster-suggestions",
            "/projects/{projectId}/commands",
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
        for path_item in contract["paths"].values():
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
        self.assertIn("SetAudioMode", command_types)
        self.assertIn("ReconcileBoundary", command_types)

    def test_all_normative_json_contracts_parse_and_are_served(self):
        contract = json.loads((ROOT / "contracts" / "openapi.json").read_text(encoding="utf-8"))
        for name in ("api.schema.json", "domain.schema.json", "commands.schema.json", "manifest.schema.json"):
            schema = json.loads((ROOT / "contracts" / name).read_text(encoding="utf-8"))
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertIn("/contracts/{contractName}", contract["paths"])
        self.assertIn("/artifacts/{artifactId}/video", contract["paths"])

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
