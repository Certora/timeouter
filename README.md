# Timeouter

Timeouter runs a Certora Prover job again, with a portfolio of about 10 solver configurations. A portfolio is a list of Prover flag sets. The Certora team selected these configurations because they often solve rules that time out.

Timeouter works like the `rerun rule from report` button in the Certora cloud report. It downloads the sources of a source run, which is the job that timed out. Then it submits one run to the cloud for each configuration in the portfolio.

The portfolio is bigger than the one that the report button uses. Also, Timeouter submits the runs from your local disk, not from the cloud. Because of this:

1. You control what runs in the cloud.
2. Your local environment can be different from the environment that sent the source run. If it is, you must change the run configuration to match your environment.

## Prerequisites

Install the Certora CLI. Timeouter runs the command of the CLI that matches the ecosystem of the job: `certoraRun`, `certoraSolanaProver`, `certoraSorobanProver` or `certoraSuiProver`. It finds the command in your `PATH`.

To use a different command, for example a local build of the Certora Prover, give it with `--prover` or in the `TIMEOUTER_PROVER` environment variable.

You also need a Certora account.

## Installation

We recommend [uv](https://docs.astral.sh/uv/). uv installs each tool in its own virtual environment.

```bash
# Install uv (only if you do not have it)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install timeouter
uv tool install git+https://github.com/Certora/timeouter.git

# Upgrade to the latest main
uv tool upgrade timeouter

# Uninstall
uv tool uninstall timeouter
```

The installation also installs `httpx`, `rich` and `certora-login`.

You can also install with `pip`:

```bash
pip install git+https://github.com/Certora/timeouter.git
```

## Usage

```
timeouter "<job report url>" <options>
```

Timeouter downloads the job sources and the rest of the input ZIP file from the job report URL. It stores them in `.certora_internal/timeouter_<job id>` in the current directory. If the sources are already there, Timeouter uses them and does not download them again.

Sometimes Timeouter opens a login page. Log in with your Certora account to continue.

To use your own portfolio instead of the default one, give a JSON file with `--portfolio_json`. To see the default portfolio for an ecosystem, run `timeouter --dump_portfolio EVM`. The ecosystems are `EVM`, `SOLANA`, `SOROBAN` and `SUI`.

To see all options, run `timeouter --help`.
