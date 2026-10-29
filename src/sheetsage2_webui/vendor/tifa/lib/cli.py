from collections.abc import Callable
from typing import TypeVar

import click

T = TypeVar("T")


def csv_list(factory: Callable[[str], T] = str):
    """Click callback that parses a comma-separated string into a list.

    ``factory`` converts each element (default: ``str``).
    Returns ``None`` when the value is ``None``, otherwise a ``list``.
    """
    # noinspection PyUnusedLocal
    def _parse(ctx, param, value):
        if value is None:
            return None
        return [factory(x.strip()) for x in value.split(",")]
    return _parse


def csv_set(factory: Callable[[str], T] = str):
    """Click callback that parses a comma-separated string into a set.

    ``factory`` converts each element (default: ``str``).
    Returns ``None`` when the value is ``None``, otherwise a ``set``.
    """
    # noinspection PyUnusedLocal
    def _parse(ctx, param, value):
        if value is None:
            return None
        return {factory(x.strip()) for x in value.split(",")}
    return _parse


class DefaultGroup(click.Group):
    """A Click group with a default subcommand that handles all arguments
    not explicitly targeting another subcommand.

    The group itself has no options; every argument is forwarded to the
    routing logic in ``invoke``:

    - No args or ``--help``: shows the default command's options plus any
      other visible (non-hidden) subcommands.
    - First arg is a known subcommand name (other than ``_``): dispatches
      to that subcommand with the remaining args.
    - First arg is an option (starts with ``-``) or any unknown token:
      redirects to the default ``_`` command with all args unchanged.

    Use ``default_command`` to register the default, or add a command
    named ``_`` directly --- it will automatically be marked *hidden* and
    become the default.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._default_cmd = None

    def default_command(self, *args, **kwargs):
        """Register a hidden ``_`` command as the default for this group."""
        kwargs.setdefault("hidden", True)
        return self.command("_", *args, **kwargs)

    def add_command(self, cmd: click.Command, name: str | None = None) -> None:
        name = name or cmd.name
        if name == "_":
            cmd.hidden = True
            self._default_cmd = "_"
        super().add_command(cmd, name)

    def parse_args(self, ctx, args):
        """Forward all arguments to subcommand routing unconditionally."""
        ctx.args = list(args)

    def invoke(self, ctx: click.Context) -> None:
        if ctx.args and ctx.args[0] == "--help":
            self._show_default_help(ctx)
            return None

        first = ctx.args[0] if ctx.args else None
        if first in self.commands and first != "_":
            cmd_name = first
            cmd_args = ctx.args[1:]
        elif self._default_cmd:
            cmd_name = self._default_cmd
            cmd_args = ctx.args
        else:
            return super().invoke(ctx)

        cmd = self.commands[cmd_name]
        if cmd_name == "_":
            sub_ctx = cmd.make_context(ctx.info_name, list(cmd_args))
        else:
            sub_ctx = cmd.make_context(cmd_name, list(cmd_args), parent=ctx)
        with ctx:
            return cmd.invoke(sub_ctx)

    def _show_default_help(self, ctx: click.Context) -> None:
        """Show the default command's options plus the group's subcommand listing."""
        cmd = self.commands[self._default_cmd]
        help_ctx = click.Context(cmd, info_name=ctx.info_name)
        help_text = cmd.get_help(help_ctx)

        visible = [
            name for name, c in self.commands.items()
            if name != "_" and not c.hidden
        ]
        if visible:
            formatter = click.HelpFormatter()
            with formatter.section("Commands"):
                for name in sorted(visible):
                    c = self.commands[name]
                    formatter.write_dl([(name, c.short_help or c.help or "")])
            help_text += "\n\n" + formatter.getvalue().rstrip()

        click.echo(help_text)
