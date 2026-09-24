#!/usr/bin/env python3
#     The Timeouter Tool
#     Copyright (C) 2026  Certora Ltd.
#
#     This program is free software: you can redistribute it and/or modify
#     it under the terms of the GNU General Public License as published by
#     the Free Software Foundation, version 3 of the License.
#
#     This program is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.
#
#     You should have received a copy of the GNU General Public License
#     along with this program.  If not, see <https://www.gnu.org/licenses/>.

import io
import atexit
import sys
import os
import re
import shutil
import uuid
import zipfile
from abc import ABC

import httpx
from pathlib import Path
import json
import argparse
import subprocess
import secrets
from datetime import datetime

from rich.console import Console
from certora_login import login
from typing import Final, cast
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache

CERTORA_INTERNAL_ROOT = Path(".certora_internal")
PACKAGE_NAME = "timeouter"
CHUNK_SIZE = 128 * 1024
TIME = 7200
RUN_SOURCE_VALUE = "TIMEOUTER"


class TimeouterError(Exception):
    pass


class Ecosystems(str, Enum):
    EVM = "EVM"
    SOROBAN = "SOROBAN"
    SOLANA = "SOLANA"
    SUI = "SUI"


def load_default_portfolio(ecosystem: Ecosystems) -> list[dict[str, str]]:

    name = f"portfolio.{ecosystem.name}.json"

    # Load from local filesystem (same directory as this script)
    path = Path(__file__).resolve().parent / name
    if not path.is_file():
        raise FileNotFoundError(
            f"JSON file '{name}' not found next to {__file__}"
        )

    with path.open(encoding="utf-8") as f:
        return json.load(f)


# For each ecosystem reruns is a bit different
class Runner(ABC):
    class_prover: str | None = None

    def __init__(self, timeouter: "Timeouter") -> None:
        self.timeouter = timeouter
        self.ecosystem = timeouter.ecosystem
        self.run_conf_content: dict = {}
        self.runs_status: list[dict[str, str]] = []

    @classmethod
    def create(cls, timeouter: "Timeouter") -> "Runner":
        mapping = {
            Ecosystems.EVM: EvmRunner,
            Ecosystems.SOROBAN: SorobanRunner,
            Ecosystems.SOLANA: SolanaRunner,
            Ecosystems.SUI: SuiRunner,
        }
        return mapping[timeouter.ecosystem](timeouter)

    @lru_cache(maxsize=1)
    def get_prover(self) -> str:
        """Set the prover command for the timeouter using an 'or'-chain."""

        class_prover = type(self).class_prover
        if not class_prover:
            raise TimeouterError("class_prover is not set for this Runner subclass.")

        candidate = (self.timeouter.prover_cmd or os.getenv("TIMEOUTER_PROVER") or class_prover)
        resolved = shutil.which(candidate)
        if not resolved:
            raise TimeouterError(f"Prover command '{candidate}' not found or not executable")
        return resolved

    def set_cwd(self) -> None:
        self.timeouter.cwd = self.timeouter.find_cwd_dir()

    def handle_run_conf(self):
        pass


class RustRunner(Runner):

    def handle_run_conf(self):
        if self.ecosystem == Ecosystems.SOLANA:
            extension = ".so"
        elif self.ecosystem == Ecosystems.SOROBAN:
            extension = ".wasm"
        else:
            raise TimeouterError("Error: handle_run_conf called for unsupported ecosystem.")
        try:
            with open(self.timeouter.run_conf, "r", encoding="utf-8") as f:
                self.run_conf_content = json.load(f)
        except FileNotFoundError:
            raise TimeouterError("run.conf file not found.")
        except json.JSONDecodeError:
            raise TimeouterError("Failed to parse JSON in run.conf.")

        self.run_conf_content.pop('build_script', None)
        files = self.run_conf_content.get('files', None)

        if not files:
            exec_files = list(self.timeouter.job_directory.glob(f"*{extension}"))

            if len(exec_files) != 1:
                raise TimeouterError(
                    f"Expected exactly one exec file, found {len(exec_files)}: {[str(f) for f in exec_files]}"
                )
            self.timeouter.log_message(f"Found exec file: {exec_files[0]}")
            self.run_conf_content['files'] = [str(Path(os.path.relpath(exec_files[0], self.timeouter.cwd)))]

        if self.ecosystem == Ecosystems.SOLANA:
            metadata_content = self.timeouter.metadata.get('conf') if self.timeouter.metadata else None
            proj_dir = None
            try:
                proj_dir = self.timeouter.find_proj_dir()
            except FileNotFoundError:
                pass

            # Handle solana_inlining and solana_summaries. These can reach us two ways:
            #
            # 1. Already present in run.conf (from the original conf file or a CLI flag).
            #    Their paths are relative to the original submission cwd, which does not
            #    match the reconstructed source tree, so we re-resolve each entry against
            #    the downloaded sources and rewrite it relative to our run cwd.
            # 2. Coming from a cargo or build script: present in the metadata.conf
            #    (metadata_content) but not in run.conf. If proj_dir is not found that means
            #    we ran with a compiled binary and there is no need to add these attributes.

            for key in ['solana_inlining', 'solana_summaries']:
                if key in self.run_conf_content:
                    files = [self.timeouter.find_file_in_sources(Path(p).name) / Path(p).name
                             for p in self.run_conf_content[key]]
                elif metadata_content and proj_dir and metadata_content.get(key):
                    files = [Path(proj_dir) / f for f in metadata_content[key]]
                else:
                    continue
                self.run_conf_content[key] = \
                    [str(Path(os.path.relpath(f, self.timeouter.cwd))) for f in files]

        with open(self.timeouter.run_conf, "w", encoding="utf-8") as f:
            json.dump(self.run_conf_content, f, indent=2)


class SorobanRunner(RustRunner):
    class_prover = "certoraSorobanProver"


class SolanaRunner(RustRunner):
    class_prover = "certoraSolanaProver"


class EvmRunner(Runner):
    class_prover = "certoraRun"

    def handle_run_conf(self):
        with open(self.timeouter.run_conf, "r", encoding="utf-8") as f:
            self.run_conf_content = json.load(f)


class SuiRunner(Runner):
    class_prover = "certoraSuiProver"

    def set_cwd(self) -> None:
        self.timeouter.cwd = self.timeouter.job_directory


@dataclass
class Timeouter:
    methods: list[str] = field(default_factory=list)
    rule: list[str] = field(default_factory=list)
    download_only: bool = field(init=False)
    default_prover_args: str = field(init=False)
    prover_cmd: str | None = field(init=False)
    sanity_check: str = field(init=False)
    job_url: str = field(init=False)
    group_id: str = field(init=False)
    jid: str = field(init=False)
    uid: str = field(init=False)
    disable_local_typechecking: bool = field(init=False)
    more_cli_flags: list[str] | None = field(init=False)

    domain: str = field(init=False)
    runs_directory: Path = field(init=False)
    source_dir: Path = field(init=False)
    group_url: str = field(init=False, default="")
    server: str = field(init=False)
    log_buffer: io.StringIO = field(init=False, default_factory=io.StringIO)
    portfolio: list[dict[str, str]] = field(init=False)
    portfolio_json: Path = field(init=False)
    dump_portfolio: str = field(init=False)
    msg: str = field(init=False)
    job_directory: Path = field(init=False)
    cwd: Path = field(init=False)
    run_conf: Path = field(init=False)

    ecosystem: Ecosystems = field(init=False)
    metadata: dict = field(init=False)
    runner: Runner = field(init=False)

    def parse_job_url(self) -> tuple[str, str, str]:
        template: Final = re.compile(r"https://(vaas-stg\.certora\.com|prover\.certora\.com)/output/([^/]*)/([^/?]*)")
        match = template.match(self.job_url)
        if not match:
            raise ValueError(f"Could not parse the run URL {self.job_url}")
        # returns domain, uid, jid
        return match.group(1), match.group(2), match.group(3)

    def set_paths(self) -> None:
        try:
            if not self.job_url:
                raise ValueError("job_url is not set")
            self.domain, self.uid, self.jid = self.parse_job_url()
            if not getattr(self, 'job_directory', None):
                self.job_directory = CERTORA_INTERNAL_ROOT / f"timeouter_{self.jid}"
            self.job_directory = self.job_directory.resolve()
            self.source_dir = self.job_directory / "repro"
            self.runs_directory = self.job_directory / "runs"
        except Exception as e:
            raise TimeouterError(f"Error setting paths: {e}")

    @staticmethod
    def parse_options() -> 'Timeouter':
        parser = argparse.ArgumentParser(description="Tool for rerunning certora jobs from source")

        parser.add_argument("-m", "--methods", nargs="+", action='append',
                            help="Run portfolio only on listed methods", default=[])

        parser.add_argument("-r", "--rule", nargs="+", action='append',
                            help="Run portfolio only on listed rule", default=[])

        parser.add_argument("-a", "--default_prover_args",
                            default='', help="Use listed prover args in every run")

        parser.add_argument("-p", "--prover", dest="prover_cmd",
                            help="Use the command given for running the certora prover client. "
                                 "If not set, use the command specified in the environment variable TIMEOUTER_PROVER "
                                 "else if certora CLI package is installed, based on job's ecosystem use "
                                 "certoraRun/certoraSolanaProver/certoraSorobanProver, else use the appropriate "
                                 "locally installed script certoraRun.py/certoraSolanaProver.py/certoraSorobanProver.py"
                                 " if found in PATH.")

        choices = ["production", "staging"]
        parser.add_argument("-s", "--server", default="production", choices=choices,
                            help="Server for running the prover options are 'staging' or 'production' (the default)")

        choices = ["none", "basic", "advanced"]
        parser.add_argument("--sanity_check",  default="basic", choices=choices,
                            help="Sanity check level (default: basic)")

        parser.add_argument("-j", "--portfolio_json",
                            help="Path to a JSON file with a runs portfolio overriding the default.")

        parser.add_argument("-o", "--job_directory", type=Path,
                            help="Output directory for downloaded job sources"
                                 "(default: <job_id> in .certora_internal directory)")

        parser.add_argument("--msg",
                            help="Additional message to add to each run in the portfolio",)

        parser.add_argument("--download_only", action="store_true",
                            help="Do not run, only download the job (default: false)")

        parser.add_argument("--disable_local_typechecking", action="store_true",
                            help="Pass --disable_local_typechecking to the prover")

        # in most cases the positional arguments job_url is required, but for dumping portfolio it is not
        parser.add_argument("job_url", nargs="?",
                            help="Certora job URL for rerunning, the job sources will first be "
                                 "downloaded if they do not exist.")

        parser.add_argument("--dump_portfolio",
                            help="Ecosystem name to dump the default portfolio to standard output "
                                 "and exit (e.g., EVM, SOROBAN)")

        parser.add_argument("--more_cli_flags", nargs=argparse.REMAINDER,
                            help="Additional flags to pass to the prover")

        try:
            args = parser.parse_args()
        except SystemExit:
            raise
        except Exception as e:
            raise TimeouterError(f"Unexpected error parsing arguments: {e}")

        # initialize the Timeouter instance with parsed arguments
        instance = Timeouter()
        for k, v in vars(args).items():
            setattr(instance, k, v)

        # flatten lists of lists for 'methods' and 'rule' otherwise they will be lists of lists
        instance.methods = [x for group in instance.methods for x in group]
        instance.rule = [x for group in instance.rule for x in group]

        return instance

    def download_job_zip(self) -> None:

        if self.job_directory.exists():
            self.log_message(f"Directory {self.job_directory} already exists, skipping.")
            return

        api_url = f"https://{self.domain}/v1/domain/jobs/{self.jid}/f/inputs"
        credentials = login()
        try:
            resp = httpx.get(
                api_url,
                cookies={name: str(value) for name, value in credentials.items()},
                follow_redirects=True,
                timeout=httpx.Timeout(300.0, read=300.0)  # 5*60 = 5 minutes
            )
            resp.raise_for_status()  # will raise exception for HTTP errors
        except httpx.HTTPError as e:
            raise TimeouterError(f"Failed to download job: {e}")
        self.job_directory.mkdir(parents=True)

        zip_file = self.job_directory / f"{self.jid}.zip"
        with open(zip_file, "wb") as f:
            f.write(resp.content)

        try:
            with zipfile.ZipFile(zip_file, "r") as zip_ref:
                for member in zip_ref.namelist():
                    if (
                            member.startswith(".certora_sources/")
                            or member.startswith("build/")
                            or member == ".certora_metadata.json"
                            or member.endswith(".so")
                            or member.endswith(".wasm")
                    ):
                        zip_ref.extract(member, self.job_directory)
        except zipfile.BadZipFile as e:
            raise TimeouterError(f"Invalid zip file: {e}")

        (self.job_directory / ".certora_sources").rename(self.source_dir)

    def run_portfolio_run(self, conf: dict) -> None:

        print(f"Running {conf['msg']}: {conf['flags']}")
        cmd = [self.runner.get_prover(), os.path.relpath(self.run_conf, self.cwd),  # run.conf relative to cwd
               "--smt_timeout", str(TIME),
               "--global_timeout", str(TIME),
               "--prover_args", f"{self.default_prover_args} {conf['flags']}",
               "--group_id", self.group_id,
               "--rule_sanity", self.sanity_check,
               "--server", self.server,
#               "--run_source", RUN_SOURCE_VALUE
               ]
        msgs = [conf["msg"]]
        if self.runner.run_conf_content and self.runner.run_conf_content.get("msg"):
            msgs.append(self.runner.run_conf_content.get("msg"))
        if getattr(self, "msg", None):
            msgs.append(self.msg)
        cmd.extend(["--msg", ":".join(msgs)])

        if self.ecosystem in (Ecosystems.EVM, Ecosystems.SUI):
            if self.methods:
                cmd.append("--method")
                cmd.extend(self.methods)

        if self.rule:
            cmd.append("--rule")
            cmd.extend(self.rule)

        if self.disable_local_typechecking:
            cmd.append("--disable_local_typechecking")

        if self.more_cli_flags:
            cmd.extend(self.more_cli_flags)

        cmd_str = ' '.join(cast(list[str], cmd))
        self.log_message(f"Running: {cmd_str}")
        try:
            result = subprocess.run(cast(list[str], cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, cwd=self.cwd)
        except OSError as e:
            raise TimeouterError(f"Failed to run prover: {e}")

        self.log_message(f"\n\nReturn code {conf['msg']}: {result.returncode}")
        if result.stdout:
            self.log_message(f"\n\nstdout {conf['msg']}:\n{result.stdout}")
        if result.stderr:
            self.log_message(f"\n\nstderr {conf['msg']}:\n{result.stderr}")

        if result.returncode != 0:
            raise TimeouterError("Run failed: " + cmd_str + "\n\n" + result.stderr)

        match = re.search(r"Follow your job and see verification results at\s*(\S+)", result.stdout)
        if match:
            url = match.group(1)
            self.runner.runs_status.append({"conf": conf['msg'], "url": url})
            if not self.group_url:
                match_url = re.match(r'^https?://[^/]+/', url)
                if match_url:
                    self.group_url = match_url.group(0) + "?groupIds=" + self.group_id
                else:
                    raise TimeouterError("Could not extract domain from job URL.")
        else:
            raise RuntimeError("Could not find job URL in output")

    def run_portfolio(self) -> None:
        self.run_conf = self.source_dir / "run.conf"
        self.runner = Runner.create(self)
        self.runner.set_cwd()

        self.log_message("prover command: " + str(self.prover_cmd))
        self.group_id = str(uuid.uuid4())
        self.log_message("group_id: " + str(self.group_id))

        self.runner.handle_run_conf()  # the runner may modify run.conf as needed

        assert len(self.portfolio) > 0

        self.run_portfolio_run(self.portfolio[0])  # run first to prevent unnecessary runs if error occurs

        for conf in self.portfolio[1:]:
            self.run_portfolio_run(conf)

        # Write runs to runs_file as JSON
        runs_json_file = self.runs_directory / (f"{datetime.now().strftime('%y_%m_%d_%H_%M_%S')}_"
                                                f"{secrets.randbelow(1_000_000):06d}{secrets.token_hex(2)}")

        runs_json_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(runs_json_file, "w") as f:
                json.dump(self.runner.runs_status, f, indent=2)
        except OSError as e:
            print(f"Failed to write {runs_json_file}: {e}", file=sys.stderr)

        latest_link = self.job_directory / "latest_run.json"
        try:
            if latest_link.exists() or latest_link.is_symlink():
                latest_link.unlink()
            latest_link.symlink_to(runs_json_file)
        except OSError as e:
            raise TimeouterError(
                f"Failed to create latest_run.json symlink "
                f"({latest_link} → {runs_json_file}): {e}"
            ) from e

        console = Console()
        console.print(f"[link={self.group_url}]All runs can be viewed in {self.group_url}[/link]")

    def write_log_file(self) -> None:

        log_file = self.job_directory / "log.txt"
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "w") as f:
                f.write(self.log_buffer.getvalue())
        except OSError as e:
            print(f"Could not open log file {log_file} for writing: {e}")

    def log_message(self, message: str) -> None:
        self.log_buffer.write(message + "\n")

    def find_cwd_dir(self) -> Path:
        return self.find_file_in_sources(".cwd")

    def find_proj_dir(self) -> Path:
        return self.find_file_in_sources(".project_directory")

    def find_file_in_sources(self, filename: str) -> Path:
        for root, dirs, files in os.walk(self.source_dir):
            # Skip prover scratch dirs, which hold nested copies of the sources from
            # previous runs and would make basename lookups ambiguous.
            dirs[:] = [d for d in dirs if d != ".certora_internal"]
            if filename in files:
                return Path(root)
        raise FileNotFoundError(f"{filename} file not found in source directory")

    def validate_portfolio(self) -> None:
        if not isinstance(self.portfolio, list):
            raise ValueError("Portfolio must be a list.")
        for i, item in enumerate(self.portfolio):
            if not isinstance(item, dict):
                raise ValueError(f"Portfolio item at index {i} is not a dictionary.")
            if set(item.keys()) != {"flags", "msg"}:
                raise ValueError(f"Portfolio item at index {i} must have keys 'flags' and 'msg'.")
            if not all(isinstance(item[k], str) for k in ("flags", "msg")):
                raise ValueError(f"Portfolio item at index {i} must have string values for 'flags' and 'msg'.")

    def get_portfolio(self) -> None:
        """
        Loads the portfolio configuration for the current run.

        - If `self.portfolio_json` is set, loads the portfolio from the specified JSON file.
        - Otherwise, loads the default portfolio for the current ecosystem.
        - If the `--dump_portfolio` argument is provided, prints the default portfolio for the specified ecosystem
          to standard output and exits.
        """
        try:
            if self.portfolio_json:
                with open(self.portfolio_json, "r") as f:
                    self.portfolio = json.load(f)
            else:
                self.portfolio = load_default_portfolio(self.ecosystem)

        except Exception as e:
            raise TimeouterError(f"Failed to load portfolio: {e}")

        self.validate_portfolio()

    def get_ecosystem(self) -> None:
        metadata_path = self.job_directory / ".certora_metadata.json"
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                self.metadata = json.load(f)
            ecosystem_str = self.metadata.get("ecosystem", '')

            # if ecosystem is not set in .certora_metadata.json, fetch from the job on the server
            if not ecosystem_str:
                credentials = login()
                resp = httpx.get(
                    f"https://{self.domain}/v1/domain/jobs/{self.jid}",
                    cookies={name: str(value) for name, value in credentials.items()},
                    follow_redirects=True,
                    timeout=httpx.Timeout(300.0, read=300.0)
                )
                resp.raise_for_status()
                job_info = json.loads(resp.content.decode("utf-8"))
                ecosystem_str = job_info.get("ecosystem", 'EVM')

            # Convert to enum and validate
            try:
                self.ecosystem = Ecosystems(ecosystem_str)
            except ValueError:
                raise TimeouterError(
                    f"Unknown ecosystem '{ecosystem_str}'. Valid values: {[e.name for e in Ecosystems]}"
                )

        except FileNotFoundError:
            raise TimeouterError(f"Metadata file '{metadata_path}' not found.")
        except json.JSONDecodeError:
            raise TimeouterError(f"Failed to parse JSON in '{metadata_path}'.")
        except httpx.HTTPError as e:
            raise TimeouterError(f"Failed to fetch job info: {e}")

    def handle_dump_portfolio(self):
        ecosystem = getattr(self, "dump_portfolio", None)
        if ecosystem:
            portfolio = load_default_portfolio(Ecosystems(ecosystem.upper()))
            print(json.dumps(portfolio, indent=2))
        else:
            raise RuntimeError("Ecosystem should be set by argparse. This code path should not be reached.")


def main():
    try:
        # read options from command line
        timeouter = Timeouter.parse_options()

        # handle dump portfolio and exit
        if timeouter.dump_portfolio:
            timeouter.handle_dump_portfolio()
            exit(0)

        # expecting now that job_url is set
        timeouter.set_paths()   # set the paths from job_url
        atexit.register(timeouter.write_log_file)

        # if job directory exists, no need to download
        if timeouter.job_directory.exists():
            timeouter.log_message(f"Directory {timeouter.job_directory} already exists, skipping.")
        else:
            timeouter.download_job_zip()

        # from the metadata, get the ecosystem (EVM, SOROBAN, SOLANA, SUI)
        timeouter.get_ecosystem()

        # get the portfolio either from user input or for the resource file portfolio.<ecosystem>.json
        timeouter.get_portfolio()

        # run the portfolio unless download_only is set
        if not timeouter.download_only:
            timeouter.run_portfolio()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
