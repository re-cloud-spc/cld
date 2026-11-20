"""Console output and interactive input helpers.

`rich` is optional; everything degrades to plain text (with the inline [tag]
markup stripped) when it isn't importable, so callers can keep using the markup
convention unconditionally.
"""

# rich is optional; degrade to plain text if absent.
try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    _console = Console()
    _RICH = True
except ImportError:
    _console = None
    _RICH = False


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def out(msg="", wrap=True):
    """wrap=False: never hard-wrap (for copy-pasteable commands)."""
    if _RICH:
        _console.print(msg, soft_wrap=not wrap)
    else:
        # Strip the most common rich markup so plain mode stays readable.
        for tag in ("[bold]", "[/bold]", "[red]", "[/red]", "[green]",
                    "[/green]", "[yellow]", "[/yellow]", "[cyan]", "[/cyan]",
                    "[dim]", "[/dim]"):
            msg = msg.replace(tag, "")
        print(msg)


def header(title):
    out()
    out(f"[bold cyan]== {title} ==[/bold cyan]")


def warn(msg):
    out(f"[yellow]! {msg}[/yellow]")


def err(msg):
    out(f"[red]ERROR: {msg}[/red]")


def render_table(title, columns, rows):
    """rows: list of lists (str-able), columns: list of header strings."""
    if not rows:
        out(f"[dim]({title}: none found)[/dim]")
        return
    if _RICH:
        t = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold")
        for c in columns:
            t.add_column(str(c))
        for r in rows:
            t.add_row(*[str(x) for x in r])
        _console.print(t)
    else:
        print(f"\n{title}")
        widths = [len(str(c)) for c in columns]
        for r in rows:
            for i, x in enumerate(r):
                widths[i] = max(widths[i], len(str(x)))
        fmt = "  ".join("{:<%d}" % w for w in widths)
        print(fmt.format(*columns))
        print(fmt.format(*["-" * w for w in widths]))
        for r in rows:
            print(fmt.format(*[str(x) for x in r]))


# --------------------------------------------------------------------------- #
# Input
# --------------------------------------------------------------------------- #
class Abort(Exception):
    """The operator asked to quit at a prompt (q/quit/cancel, or Ctrl-D).

    Raised out of the prompt helpers and caught once in cli.main(), so no caller
    needs a "did they cancel?" branch and a half-built spec can never leak past a
    cancelled prompt. Rollback prompts must map this to False, never to a delete
    -- see confirm_destructive().
    """


_QUIT = ("q", "quit", "cancel")


def _ask(prompt):
    """input() with the two universal escapes wired in: Ctrl-D and q/quit/cancel.

    Every numeric/menu prompt funnels through here so that no prompt can trap the
    operator (rc3 #116). Free-text input uses _ask_text() instead, which does not
    treat 'q' as a keyword.
    """
    try:
        raw = input(prompt).strip()
    except EOFError:            # Ctrl-D
        raise Abort()
    if raw.lower() in _QUIT:
        raise Abort()
    return raw


def _ask_text(prompt):
    """input() for free text: Ctrl-D aborts, but 'q' is a legitimate value
    (a VM may legitimately be named 'q'), so no keyword matching here."""
    try:
        return input(prompt).strip()
    except EOFError:            # Ctrl-D
        raise Abort()


def choose(prompt, items, label=lambda x: str(x), allow_none=False,
           none_label="(none / skip)", default_index=None):
    """Zero-based single-select menu (0 = first item). Returns the item or None.

    The none-option has no number: it is Enter only, so no digit can mean
    "skip" in one menu and "pick" in another."""
    if not items and not allow_none:
        return None
    out()
    if allow_none:
        out(f"  [dim]Enter[/dim]) {none_label}")
    for i, it in enumerate(items):
        mark = " [dim](default)[/dim]" if default_index == i else ""
        out(f"  [cyan]{i}[/cyan]) {label(it)}{mark}")
    skip = (", Enter to skip" if allow_none
            else (", Enter for the default" if default_index is not None else ""))
    hint = f"enter a number 0-{len(items) - 1}{skip}, or q to quit"
    while True:
        raw = _ask(f"{prompt} > ")
        if raw == "" and default_index is not None:
            return items[default_index]
        # Enter picks the none-option when one is offered; without it (cloud,
        # server, flavor, image, network) the choice is required and Enter is
        # still rejected below -- silently defaulting a flavor or image would be
        # a worse failure than re-prompting. `q` is always a way out.
        if raw == "" and allow_none:
            return None
        if raw.isdigit() and int(raw) < len(items):
            return items[int(raw)]
        warn(hint)


def choose_multi(prompt, items, label=lambda x: str(x), none_label="(none)"):
    """Zero-based multi-select. Reads e.g. `0,2` or `0 2`; returns the chosen
    items in the order typed (deduped). Enter returns []."""
    if not items:
        return []
    out()
    out(f"  [dim]Enter[/dim]) {none_label}")
    for i, it in enumerate(items):
        out(f"  [cyan]{i}[/cyan]) {label(it)}")
    return [items[i] for i in choose_indices(prompt, len(items))]


def choose_indices(prompt, count):
    """Zero-based multi-select against a numbered list already printed. Reads
    '0,2' or '0 2'; Enter = none. -> unique indices in the order typed (the
    first SSH key becomes Nova's key_name, so order matters). Re-prompts on
    anything out of range; q / Ctrl-D abort like every prompt."""
    hint = (f"enter numbers 0-{count - 1} separated by commas/spaces, Enter "
            "for none, or q to quit")
    while True:
        raw = _ask(f"{prompt} (e.g. 0,2; Enter = none) > ")
        if raw == "":
            return []
        tokens = [t for t in raw.replace(",", " ").split() if t]
        if not all(t.isdigit() for t in tokens) or any(int(t) >= count for t in tokens):
            warn(hint)
            continue
        return list(dict.fromkeys(int(t) for t in tokens))


def prompt_int(prompt, minimum=None, maximum=None, default=None):
    while True:
        suffix = f" [default {default}]" if default is not None else ""
        raw = _ask(f"{prompt}{suffix} > ")
        if raw == "" and default is not None:
            return default
        if not raw.lstrip("-").isdigit():
            warn("enter an integer"
                 + (", Enter for the default" if default is not None else "")
                 + ", or q to quit")
            continue
        n = int(raw)
        if minimum is not None and n < minimum:
            warn(f"must be >= {minimum}")
            continue
        if maximum is not None and n > maximum:
            warn(f"must be <= {maximum}")
            continue
        return n


def prompt_str(prompt, default=None, required=True):
    """Free text. Deliberately does NOT treat 'q' as quit -- a VM may legitimately
    be named 'q' -- so Ctrl-C / Ctrl-D are the escapes here, and the hint says so."""
    while True:
        suffix = f" [default {default}]" if default else ""
        raw = _ask_text(f"{prompt}{suffix} > ")
        if raw == "" and default is not None:
            return default
        if raw == "" and not required:
            return None
        if raw:
            return raw
        warn("a value is required; Ctrl-C or Ctrl-D to quit")


def confirm(prompt, default=False):
    d = "Y/n" if default else "y/N"
    raw = _ask(f"{prompt} [{d}] > ").lower()
    if raw == "":
        return default
    return raw in ("y", "yes")


def confirm_destructive(prompt, default=False):
    """confirm() for delete/rollback prompts: an Abort (q/quit/Ctrl-D) resolves to
    False, never to a delete.

    Quitting must decline the cleanup and leave the resource, matching the
    default=False on every destructive path. No keystroke -- including a panic
    Ctrl-D -- may ever destroy anything (rc3 #112).
    """
    try:
        return confirm(prompt, default=default)
    except Abort:
        out("[dim]cancelled; leaving the resource in place[/dim]")
        return False


def gb(value):
    try:
        return f"{float(value):,.0f}" if value is not None else "?"
    except (TypeError, ValueError):
        return "?"
