"""Pin the CLI surface that ``scripts/reproduce.sh`` parses.

This file exists because of a real, shipped break: the scripts invoked
``agent.generate --count/--swap-at`` and ``fot show --json`` long after those
flags were renamed or before they existed. argparse and click both exited 2, the
scripts hid it behind ``2>/dev/null``, and the advertised "judge path" could not
go green on any machine.

Nothing here touches the network. These are contract tests: if a flag the shell
scripts depend on disappears, a test fails instead of a judge's run.

Options are introspected via click rather than grepped out of ``--help`` --
rendered help is wrapped to the terminal width, so grepping it passes on one
machine and fails on another.
"""

from __future__ import annotations

import typer

from agent.generate import build_parser
from fot.cli import app

# (command, flags scripts/reproduce.sh and scripts/setup.sh rely on)
CLI_CONTRACT = {
    "show": {"--json", "--since"},
    "compare": {"--json", "--since"},
    "counter-proof": {"--json", "--since", "--step"},
    "apply": {"--only"},
    "gauges": {"--since", "--endpoint"},
}

# Flags the setup scripts pass to the trace generator.
GENERATOR_CONTRACT = {
    "--runs",
    "--model",
    "--validate-rate",
    "--out-of-order-rate",
    "--seed",
    "--stub",
    "--fast",
    "--no-otlp",
}


def _click_group():
    return typer.main.get_command(app)


def _opts(command_name: str) -> set[str]:
    cmd = _click_group().commands.get(command_name)
    assert cmd is not None, f"fot has no `{command_name}` command"
    return {opt for param in cmd.params for opt in getattr(param, "opts", [])}


def test_every_command_the_scripts_call_exists():
    available = set(_click_group().commands)
    for name in CLI_CONTRACT:
        assert name in available, f"`fot {name}` is gone; the shell scripts call it"


def test_flags_the_shell_scripts_depend_on_are_present():
    for name, required in CLI_CONTRACT.items():
        missing = required - _opts(name)
        assert not missing, f"`fot {name}` lost {sorted(missing)} — scripts/ pass these"


def test_subcommand_groups_exist():
    """setup.sh runs `fot dashboard apply` and `fot alert apply`."""
    group = _click_group()
    for name in ("dashboard", "alert"):
        sub = group.commands.get(name)
        assert sub is not None, f"`fot {name}` group is gone"
        assert "apply" in getattr(sub, "commands", {}), f"`fot {name} apply` is gone"


def test_compare_defaults_need_no_positional_arguments():
    """setup.sh and reproduce.sh both run a bare `fot compare`."""
    cmd = _click_group().commands["compare"]
    required = [p.name for p in cmd.params if getattr(p, "required", False)]
    assert not required, f"bare `fot compare` would exit 2 on missing {required}"


def test_generator_accepts_every_flag_the_setup_scripts_pass():
    """The exact break this suite was written for."""
    known = {opt for action in build_parser()._actions for opt in action.option_strings}
    missing = GENERATOR_CONTRACT - known
    assert not missing, f"agent.generate lost {sorted(missing)} — scripts/ pass these"


def test_generator_rejects_the_flags_that_used_to_be_passed():
    """--count/--swap-at are gone for good; make the rename explicit."""
    known = {opt for action in build_parser()._actions for opt in action.option_strings}
    assert "--count" not in known
    assert "--swap-at" not in known


def test_default_validate_rate_reproduces_the_published_number():
    """round(125 * rate) must be 80, i.e. the 64.0% the README and video quote."""
    from agent.generate import resolve_rates

    rate, _ = resolve_rates(None, None)
    assert round(125 * rate) == 80, f"rate {rate} gives {round(125 * rate)}/125, not 80"
