"""Build a Mermaid flowchart of module import dependencies.

Scans every `.py` under `modules/` plus `main.py` and `owner.py` and emits a
Mermaid `flowchart LR` graph that visualises how the modules depend on each
other. Read on demand by the owner observability page so the graph is always
in sync with the real code.

Nodes are grouped by directory (modules, owner, scripts) so the layout does
not explode into a hairball. A small-render mode hides transitive noise from
the diagram (only first-level imports per module).
"""
import ast
import re
from pathlib import Path


# Modules of the project: keys are stable ids used in the Mermaid graph, values
# are the human label. Anything outside this set is treated as external.
_PROJECT_MODULES = {
    "main": "main.py",
    "config": "config.py",
    "database": "database.py",
    "modules.techs": "modules/techs.py",
    "modules.trucks": "modules/trucks.py",
    "modules.warehouses": "modules/warehouses.py",
    "modules.parts": "modules/parts.py",
    "modules.packages": "modules/packages.py",
    "modules.inventory": "modules/inventory.py",
    "modules.distributors": "modules/distributors.py",
    "modules.excel_import": "modules/excel_import.py",
    "modules.pwa": "modules/pwa.py",
    "modules.email_poller": "modules/email_poller.py",
    "modules.email_service": "modules/email_service.py",
    "modules.email_builders": "modules/email_builders.py",
    "modules.email_reporter": "modules/email_reporter.py",
    "modules.analytics": "modules/analytics.py",
    "modules.logger": "modules/logger.py",
    "modules.name_matcher": "modules/name_matcher.py",
    "modules.pdf_extractor": "modules/pdf_extractor.py",
    "modules.reorder": "modules/reorder.py",
    "modules.events": "modules/events.py",
    "modules.owner": "modules/owner.py",
    "modules.demo": "modules/demo.py",
    "scripts.deps_mermaid": "scripts/deps_mermaid.py",
}


def _canonical(name):
    """Map a raw import name to a project module id if it matches."""
    if not name:
        return None
    if name in _PROJECT_MODULES:
        return name
    # `from modules.foo import x` -> take the package root
    if name.startswith("modules."):
        full = name
        if full in _PROJECT_MODULES:
            return full
        # Parent (modules.foo -> modules.__init__, drop)
        return None
    return None


def _safe_id(label):
    """Mermaid node id: ascii, no dashes-of-hell."""
    return re.sub(r"[^A-Za-z0-9_]", "_", label)


def _scan_imports(path: Path):
    """Yield canonical module ids that this file imports, including
    `from modules.foo import bar` and `import modules.foo`."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError):
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                cid = _canonical(alias.name)
                if cid:
                    out.append(cid)
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` or `from .foo import bar` -> resolve the parent.
            mod = node.module or ""
            if node.level and node.level > 0:
                # Relative; resolve against this file's module path.
                rel = path.relative_to(path.parents[2])  # repo root
                parts = list(rel.parts)
                # Drop the file basename to get the package path.
                pkg_parts = parts[:-1]
                # Apply `level` upward steps.
                if node.level > len(pkg_parts):
                    continue
                parent = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                base = ".".join(parent)
                if mod:
                    full = f"{base}.{mod}" if base else mod
                else:
                    full = base
                cid = _canonical(full)
                if cid:
                    out.append(cid)
                continue
            cid = _canonical(mod)
            if cid:
                out.append(cid)
            # `from modules import techs, parts` -> map each to `modules.<name>`.
            if mod == "modules":
                for alias in node.names:
                    sub = f"modules.{alias.name}"
                    cid2 = _canonical(sub)
                    if cid2:
                        out.append(cid2)
    # Dedupe, preserve order.
    seen = set()
    unique = []
    for c in out:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


def _file_to_module_id(path: Path, repo_root: Path):
    rel = path.relative_to(repo_root)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        return ".".join(parts[:-1]) or None
    parts[-1] = parts[-1][:-3]  # strip .py
    return ".".join(parts)


def build_mermaid(repo_root: Path, exclude_scripts_self=True):
    """Scan the repo and emit a Mermaid flowchart of module imports.

    Layout strategy:
      - Group `modules.*` into a subgraph `modules`.
      - Top-level app files (main, config, database) are separate nodes.
      - Edges go from caller -> callee.
      - We skip self-imports and imports of the script itself (when its
        mermaid output is read from the same module).
    """
    py_files = list((repo_root / "modules").glob("*.py"))
    py_files += [repo_root / "main.py", repo_root / "config.py", repo_root / "database.py"]
    scripts_dir = repo_root / "scripts"
    if scripts_dir.is_dir():
        py_files += list(scripts_dir.glob("*.py"))
    owner_module = next((p for p in (repo_root / "modules").glob("owner.py")), None)
    owner_id = _file_to_module_id(owner_module, repo_root) if owner_module else None

    edges = []
    nodes = set()
    for path in py_files:
        src_id = _file_to_module_id(path, repo_root)
        if not src_id or src_id not in _PROJECT_MODULES:
            continue
        nodes.add(src_id)
        for tgt in _scan_imports(path):
            if tgt == src_id:
                continue
            if exclude_scripts_self and src_id == "scripts.deps_mermaid" and tgt == "scripts.deps_mermaid":
                continue
            if tgt in _PROJECT_MODULES:
                nodes.add(tgt)
                edges.append((src_id, tgt))

    # Build mermaid.
    lines = ["flowchart LR"]
    # External libs (for context — also implicit, but visible).
    lines.append("    %% External libs grouped separately")
    lines.append("    subgraph ext[External]")
    lines.append("      fastapi:::ext")
    lines.append("      sqlite3:::ext")
    lines.append("      pdfplumber:::ext")
    lines.append("      requests:::ext")
    lines.append("      reportlab:::ext")
    lines.append("      resend_API:::ext")
    lines.append("    end")
    lines.append("    classDef ext fill:#eef0f4,stroke:#aab,color:#445,font-size:11px;")
    lines.append("")

    # Top-level app files.
    top = ["main", "config", "database"]
    lines.append("    subgraph app[App]")
    for n in top:
        if n in nodes:
            lines.append(f"      {_safe_id(n)}[\"{_PROJECT_MODULES[n]}\"]")
    lines.append("    end")
    lines.append("")

    # Modules subgraph.
    lines.append("    subgraph mods[modules/]")
    mod_nodes = sorted(n for n in nodes if n.startswith("modules."))
    for n in mod_nodes:
        lines.append(f"      {_safe_id(n)}[\"{_PROJECT_MODULES[n]}\"]")
    lines.append("    end")
    lines.append("")

    # Scripts.
    script_nodes = [n for n in nodes if n.startswith("scripts.")]
    if script_nodes:
        lines.append("    subgraph scripts[scripts/]")
        for n in script_nodes:
            lines.append(f"      {_safe_id(n)}[\"{_PROJECT_MODULES[n]}\"]")
        lines.append("    end")
        lines.append("")

    # Edges.
    lines.append("    %% Dependencies")
    for src, tgt in sorted(set(edges)):
        lines.append(f"    {_safe_id(src)} --> {_safe_id(tgt)}")
    lines.append("")

    lines.append("    linkStyle default stroke:#8a7a5a,stroke-width:1px;")
    lines.append("    classDef default fill:#fbf6e6,stroke:#9a5a1f,color:#1a1a1a,font-size:11px;")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    root = Path(__file__).resolve().parent.parent
    print(build_mermaid(root))
