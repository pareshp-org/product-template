#!/usr/bin/env python3
"""Automated Verification and Seeded Defect Execution Harness.

Fulfills MasterSpec Section 31.1 & Section 31.2:
- "A verification contract that cannot fail is not a contract."
- Executes unit tests in verification/test_endpoints.py
- Executes seeded defect test verification/test_seeded_defect.py in baseline mode (must pass)
- Executes seeded defect test with INJECT_DEFECT=1 (must fail closed to confirm sensitivity)
- Records execution outcome into verification/seeded-results.json adhering to the schema
  expected by tools/evidence/seeded_defect.py
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def get_product_id(product_root: Path) -> str:
    product_yaml = product_root / "product.yaml"
    if product_yaml.exists():
        try:
            import yaml

            data = yaml.safe_load(product_yaml.read_text(encoding="utf-8"))
            pid = data.get("identity", {}).get("product_id")
            if pid:
                return str(pid)
        except Exception:
            try:
                text = product_yaml.read_text(encoding="utf-8")
                match = re.search(r"product_id:\s*([^\s\n\r]+)", text)
                if match:
                    return match.group(1).strip("'\"")
            except Exception:
                pass
    return product_root.name


def get_seeded_case_info(verification_dir: Path) -> tuple[str, str]:
    contract_yaml = verification_dir / "contract.yaml"
    case_id = "SDC-001"
    description = "Seeded defect injection via INJECT_DEFECT=1 env var; contract must fail closed"
    if contract_yaml.exists():
        try:
            import yaml

            data = yaml.safe_load(contract_yaml.read_text(encoding="utf-8"))
            sdc = data.get("seeded_defect_case", {})
            if sdc.get("id"):
                case_id = str(sdc["id"])
            if sdc.get("description"):
                description = str(sdc["description"])
        except Exception:
            try:
                text = contract_yaml.read_text(encoding="utf-8")
                sdc_match = re.search(r"seeded_defect_case:(.+?)(?:\n\w|\Z)", text, re.DOTALL)
                if sdc_match:
                    sdc_text = sdc_match.group(1)
                    id_match = re.search(r"\bid:\s*([^\s\n\r]+)", sdc_text)
                    if id_match:
                        case_id = id_match.group(1).strip("'\"")
            except Exception:
                pass
    return case_id, description


def run_command(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def main() -> int:
    parser = argparse.ArgumentParser(description="Run product contract verification and seeded defect harness.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to output seeded-results.json (default: verification/seeded-results.json)",
    )
    args = parser.parse_args()

    verification_dir = Path(__file__).resolve().parent
    product_root = verification_dir.parent
    product_id = get_product_id(product_root)
    case_id, case_description = get_seeded_case_info(verification_dir)

    results_file = args.output or (verification_dir / "seeded-results.json")

    print("============================================================")
    print(f"Contract Verification Harness: {product_id}")
    print(f"Root: {product_root}")
    print("============================================================")

    # Base environment
    base_env = os.environ.copy()
    base_env.pop("INJECT_DEFECT", None)

    # 1. Execute unit tests in verification/test_endpoints.py
    endpoints_test_path = verification_dir / "test_endpoints.py"
    if endpoints_test_path.exists():
        print(f"\n[Step 1/3] Running endpoint tests: {endpoints_test_path.name}")
        cmd = [sys.executable, str(endpoints_test_path)]
        code, stdout, stderr = run_command(cmd, product_root, base_env)
        print(stdout, end="")
        print(stderr, end="", file=sys.stderr)
        if code != 0:
            print(f"[FAIL] Endpoint verification tests failed with exit code {code}", file=sys.stderr)
            return 1
        print("[PASS] Endpoint verification tests passed.")
    else:
        print(f"\n[Step 1/3] Warning: {endpoints_test_path.name} not found, skipping endpoint tests.")

    # 2. Execute seeded defect test in baseline mode (must pass)
    seeded_test_path = verification_dir / "test_seeded_defect.py"
    if not seeded_test_path.exists():
        print(f"[ERROR] Seeded defect test file not found: {seeded_test_path}", file=sys.stderr)
        return 1

    print(f"\n[Step 2/3] Running seeded defect test in baseline mode (must pass): {seeded_test_path.name}")
    code, stdout, stderr = run_command([sys.executable, str(seeded_test_path)], product_root, base_env)
    print(stdout, end="")
    print(stderr, end="", file=sys.stderr)
    if code != 0:
        print(f"[FAIL] Baseline seeded defect test failed with exit code {code} (expected 0)", file=sys.stderr)
        return 1
    print("[PASS] Baseline seeded defect test passed as expected.")

    # 3. Execute seeded defect test with INJECT_DEFECT=1 (must fail closed)
    print("\n[Step 3/3] Running seeded defect test with INJECT_DEFECT=1 (must fail closed)")
    injected_env = base_env.copy()
    injected_env["INJECT_DEFECT"] = "1"
    code, stdout, stderr = run_command([sys.executable, str(seeded_test_path)], product_root, injected_env)
    print(stdout, end="")
    print(stderr, end="", file=sys.stderr)

    # Invariant: A contract that cannot fail is not a contract.
    # The injected defect test MUST fail (exit code != 0).
    if code != 0:
        print(f"[PASS] Injected defect correctly caught! Test failed closed with exit code {code}.")
        passed_flag = False
        outcome_str = "validation_error_raised"
        overall_success = True
    else:
        print(
            "[FATAL] Contract sensitivity failure: test unexpectedly passed (exit code 0) when INJECT_DEFECT=1! (SIG-18, Section 31.2)",
            file=sys.stderr,
        )
        passed_flag = True
        outcome_str = "no_error_raised"
        overall_success = False

    # 4. Record execution outcome into verification/seeded-results.json
    results_data = {
        "product_id": product_id,
        "seeded_cases": [
            {
                "id": case_id,
                "description": case_description,
                "passed": passed_flag,
                "outcome": outcome_str,
            }
        ],
    }

    try:
        with open(results_file, "w", encoding="utf-8") as f:
            json.dump(results_data, f, indent=2)
            f.write("\n")
        print(f"\n[INFO] Recorded seeded results to {results_file}")
    except Exception as e:
        print(f"[ERROR] Failed to write seeded-results.json: {e}", file=sys.stderr)
        return 1

    if not overall_success:
        print("[FAIL] Contract verification failed: seeded defect was not detected!", file=sys.stderr)
        return 2

    print("============================================================")
    print(f"[SUCCESS] All verification stages completed successfully for {product_id}")
    print("============================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
