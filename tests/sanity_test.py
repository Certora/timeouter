import shutil
import subprocess
import sys
import unittest
import json
from pathlib import Path
import os

PACKAGE_NAME = "timeouter"
PACKAGE_SRC = Path(__file__).resolve().parent / ".."
WORK_DIR = Path(__file__).resolve().parent / "work"

# An EVM run
EVM_JOB_URL = "https://vaas-stg.certora.com/output/69614/fed1f795b86846fd9f72efa3a8581fba"

SCRIPT_DIR = Path(__file__).resolve().parent


def write_portfolio(content: list[dict[str, str]]):
    with open(Path.cwd() / 'portfolio.json', 'w') as f:
        json.dump(content, f, indent=2)



def run_timeouter(flags=None, timeouter_exec: str = "timeouter",
                  portfolio: str = str(SCRIPT_DIR / "single_entry_portfolio.json"),
                  prover: str = "",
                  job_url: str = EVM_JOB_URL) -> subprocess.CompletedProcess:
    """
    Do not pass --portfolio_json in flags, it will be overridden by the portfolio argument.
    """
    if flags is None:
        flags = []
    if prover:
        flags.extend(["--prover", prover])
    flags.extend(["--portfolio_json", portfolio])

    cmd = [timeouter_exec] + [job_url] + flags
    result = subprocess.run(cmd, cwd=WORK_DIR, capture_output=True, text=True)
    return result


class TestSetup(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if WORK_DIR.exists():
            shutil.rmtree(WORK_DIR)
        WORK_DIR.mkdir()

        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", PACKAGE_NAME],
            cwd=WORK_DIR,
            check=False,
        )

        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(PACKAGE_SRC)],
            cwd=WORK_DIR,
            check=True,
        )




    def test_package_import_and_version(self):
        result = subprocess.run([sys.executable, "-c", "import timeouter;"], cwd=WORK_DIR,
                                capture_output=True, text=True, check=True)


class TestFlags(unittest.TestCase):

    def setUp(self):
        if WORK_DIR.exists():
            shutil.rmtree(WORK_DIR)
        WORK_DIR.mkdir(parents=True)

    def test_dump_portfolio_sui(self):
        result = run_timeouter(["--dump_portfolio", "SUI"])
        expected_file = PACKAGE_SRC / "packages" / "timeouter" / "portfolio.SUI.json"
        self.assertEqual(result.returncode, 0)
        with open(expected_file, encoding="utf-8") as f:
            expected_content = f.read()
        self.assertIn(expected_content, result.stdout)

    def test_dump_portfolio_bad(self):
        result = run_timeouter(["--dump_portfolio", "BAD"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("'BAD' is not a valid Ecosystems", result.stderr)

    def test_dump_portfolio_no_value(self):
        result = run_timeouter(["--dump_portfolio"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("error", result.stderr.lower())

    def test_default_run(self):
        result = run_timeouter()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("error", result.stderr.lower())

    def test_download_fails(self):
        job_url = EVM_JOB_URL + "non_existent_suffix" # Invalid URL to trigger failure
        result = run_timeouter(["--download_only"], job_url=job_url)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Failed to download job", result.stderr)


    def test_portfolios(self):
        write_portfolio([{"flags": " -t 6000", "msg": "prover_arg with a matching CLI flag"}])
        result = run_timeouter(portfolio="portfolio.json")
        self.assertEqual(result.returncode, 1)
        write_portfolio([{"flags_error": "-acSoft 6", "msg": "bad key"}])
        result = run_timeouter(portfolio="portfolio_does_not_exist.json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Failed to load portfolio", result.stderr)


if __name__ == "__main__":
    unittest.main()
