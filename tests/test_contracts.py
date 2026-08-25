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
        commands = contract["components"]["schemas"]["ProjectCommand"]["properties"]["commandType"]["enum"]
        self.assertIn("SetSyncTransform", commands)
        self.assertIn("SetAudioMode", commands)
        self.assertIn("ReconcileBoundary", commands)

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
