#!/usr/bin/env python3
"""
ORA Kernel Installer — Integrates the agentic orchestration kernel into a Claude Code project.

Usage:
    python3 install.py /path/to/target/project
    python3 install.py /path/to/target/project --dry-run
    python3 install.py /path/to/target/project --force

Phases:
    1. Pre-flight: scan target, detect conflicts
    2. Copy: non-conflicting files (kernel-owned, create-if-absent)
    3. Merge: CLAUDE.md (section markers), settings.json (JSON deep merge),
             agents.yaml (section markers), kernel-listen (rename if collision)
    4. Infrastructure: copy namespaced, optionally run postgres migrations
    5. Report: summary + next steps

Stdlib only — no external dependencies.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Markers for idempotent section merging
CLAUDE_MD_START = "<!-- ORA-KERNEL:START — Do not edit this block manually. Managed by ora-kernel installer. -->"
CLAUDE_MD_END = "<!-- ORA-KERNEL:END -->"
YAML_SECTION_RE = re.compile(
    r"# === ORA-KERNEL:START (\w+) ===\n(.*?)# === ORA-KERNEL:END \1 ===\n",
    re.DOTALL,
)

SCRIPT_DIR = Path(__file__).resolve().parent
KERNEL_FILES = SCRIPT_DIR / "kernel-files"
VERSION_FILE = SCRIPT_DIR / "VERSION"

if not KERNEL_FILES.exists():
    print(f"ERROR: kernel-files directory not found at {KERNEL_FILES}")
    print("Are you running install.py from the ora-kernel repository?")
    sys.exit(1)


def get_version() -> str:
    return VERSION_FILE.read_text().strip()


def log(msg: str, level: str = "INFO"):
    symbols = {"INFO": "  ", "OK": "  ✓", "SKIP": "  -", "WARN": "  !", "CONFLICT": "  ⚠", "COPY": "  →"}
    print(f"{symbols.get(level, '  ')} {msg}")


# ============================================================================
# Phase 1: Pre-flight
# ============================================================================

def preflight(target: Path) -> dict:
    """Scan target directory and classify conflicts."""
    report = {"conflicts": [], "creates": [], "overwrites": [], "merges": []}

    if not target.exists():
        print(f"ERROR: Target directory does not exist: {target}")
        sys.exit(1)

    # Check for existing files that need merge handling
    checks = {
        "CLAUDE.md": "section_merge",
        ".claude/settings.json": "json_deep_merge",
        ".claude/agents.yaml": "section_merge",
        ".claude/commands/kernel-listen.md": "rename_if_exists",
    }

    for rel_path, strategy in checks.items():
        target_file = target / rel_path
        if target_file.exists():
            report["conflicts"].append({
                "file": rel_path,
                "strategy": strategy,
                "target_size": target_file.stat().st_size,
            })
        else:
            report["creates"].append(rel_path)

    # Check kernel-owned dirs
    for d in [".claude/kernel", ".claude/hooks"]:
        target_dir = target / d
        if target_dir.exists():
            count = sum(1 for _ in target_dir.rglob("*") if _.is_file())
            report["overwrites"].append({"dir": d, "existing_files": count})

    return report


def print_report(report: dict):
    print("\n📋 Pre-flight Report")
    print("=" * 50)

    if report["conflicts"]:
        print("\nFiles requiring merge:")
        for c in report["conflicts"]:
            log(f"{c['file']} ({c['target_size']} bytes) — strategy: {c['strategy']}", "CONFLICT")

    if report["overwrites"]:
        print("\nKernel-owned directories (will be overwritten):")
        for o in report["overwrites"]:
            log(f"{o['dir']}/ ({o['existing_files']} existing files)", "WARN")

    if report["creates"]:
        print("\nNew files to create:")
        for c in report["creates"]:
            log(c, "COPY")

    print()


# ============================================================================
# Phase 2: Copy non-conflicting files
# ============================================================================

def copy_kernel_owned(target: Path, dry_run: bool):
    """Copy kernel-owned directories (always overwrite)."""
    print("\n📦 Phase 2: Copying kernel-owned files")

    # Kernel directory (schemas, nodes, references)
    kernel_src = KERNEL_FILES / ".claude" / "kernel"
    kernel_dst = target / ".claude" / "kernel"
    if not dry_run:
        if kernel_dst.exists():
            shutil.rmtree(kernel_dst)
        shutil.copytree(kernel_src, kernel_dst)
    log(f".claude/kernel/ ({sum(1 for _ in kernel_src.rglob('*') if _.is_file())} files)", "COPY")

    # Hooks
    hooks_src = KERNEL_FILES / ".claude" / "hooks"
    hooks_dst = target / ".claude" / "hooks"
    if not dry_run:
        hooks_dst.mkdir(parents=True, exist_ok=True)
        for f in hooks_src.iterdir():
            if f.is_file():
                shutil.copy2(f, hooks_dst / f.name)
    log(f".claude/hooks/ ({sum(1 for _ in hooks_src.iterdir() if _.is_file())} files)", "COPY")

    # Cron scripts
    cron_src = KERNEL_FILES / ".claude" / "cron"
    if cron_src.exists():
        cron_dst = target / ".claude" / "cron"
        if not dry_run:
            if cron_dst.exists():
                shutil.rmtree(cron_dst)
            shutil.copytree(cron_src, cron_dst)
        log(f".claude/cron/ ({sum(1 for _ in cron_src.rglob('*') if _.is_file())} files)", "COPY")

    # Commands (create_if_absent)
    for cmd in ["self-improve.md"]:
        cmd_dst = target / ".claude" / "commands" / cmd
        if not cmd_dst.exists():
            if not dry_run:
                cmd_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(KERNEL_FILES / ".claude" / "commands" / cmd, cmd_dst)
            log(f".claude/commands/{cmd}", "COPY")
        else:
            log(f".claude/commands/{cmd} (already exists)", "SKIP")

    # Event queue infrastructure (create_if_absent)
    events_dir = target / ".claude" / "events"
    if not dry_run:
        events_dir.mkdir(parents=True, exist_ok=True)
    for f in ["inbox.jsonl", "pending_briefing.md"]:
        fpath = events_dir / f
        if not fpath.exists():
            if not dry_run:
                fpath.touch()
            log(f".claude/events/{f}", "COPY")
        else:
            log(f".claude/events/{f} (already exists)", "SKIP")

    # PROJECT_DNA.md (skip_if_exists)
    dna_dst = target / "PROJECT_DNA.md"
    if not dna_dst.exists():
        if not dry_run:
            shutil.copy2(KERNEL_FILES / "PROJECT_DNA.md", dna_dst)
        log("PROJECT_DNA.md", "COPY")
    else:
        log("PROJECT_DNA.md (already exists — preserving your config)", "SKIP")


# ============================================================================
# Phase 3: Merge conflicting files
# ============================================================================

def merge_claude_md(target: Path, dry_run: bool):
    """Merge CLAUDE.md using section markers."""
    print("\n🔀 Phase 3a: Merging CLAUDE.md")

    kernel_md = (KERNEL_FILES / "CLAUDE.md").read_text()
    target_md_path = target / "CLAUDE.md"

    if not target_md_path.exists():
        # No existing CLAUDE.md — just copy
        if not dry_run:
            target_md_path.write_text(kernel_md)
        log("CLAUDE.md created (no existing file)", "OK")
        return

    existing = target_md_path.read_text()

    # Check if markers already exist (idempotent update)
    if CLAUDE_MD_START in existing:
        # Replace between markers
        pattern = re.escape(CLAUDE_MD_START) + r".*?" + re.escape(CLAUDE_MD_END)
        updated = re.sub(pattern, kernel_md.strip(), existing, flags=re.DOTALL)
        if not dry_run:
            target_md_path.write_text(updated)
        log("CLAUDE.md updated (replaced existing kernel block)", "OK")
    else:
        # Prepend kernel block above existing content
        merged = kernel_md.strip() + "\n\n" + existing
        if not dry_run:
            target_md_path.write_text(merged)
        log(f"CLAUDE.md merged (kernel block prepended above {len(existing)} bytes of project content)", "OK")


def merge_settings_json(target: Path, dry_run: bool):
    """Deep merge settings.json — combine hooks arrays, union permissions."""
    print("\n🔀 Phase 3b: Merging settings.json")

    kernel_settings = json.loads((KERNEL_FILES / ".claude" / "settings.json").read_text())
    target_settings_path = target / ".claude" / "settings.json"

    if not target_settings_path.exists():
        if not dry_run:
            target_settings_path.parent.mkdir(parents=True, exist_ok=True)
            target_settings_path.write_text(json.dumps(kernel_settings, indent=2) + "\n")
        log("settings.json created (no existing file)", "OK")
        return

    existing = json.loads(target_settings_path.read_text())

    # Merge hooks
    kernel_hooks = kernel_settings.get("hooks", {})
    existing_hooks = existing.get("hooks", {})

    for event_name, kernel_entries in kernel_hooks.items():
        if event_name not in existing_hooks:
            existing_hooks[event_name] = kernel_entries
            log(f"  hooks.{event_name}: added ({len(kernel_entries)} entries)", "OK")
        else:
            # Append kernel hook entries, deduplicate by command string
            existing_cmds = set()
            for entry in existing_hooks[event_name]:
                for hook in entry.get("hooks", []):
                    existing_cmds.add(hook.get("command", ""))

            added = 0
            for kernel_entry in kernel_entries:
                for hook in kernel_entry.get("hooks", []):
                    if hook.get("command", "") not in existing_cmds:
                        # Find matching matcher group or create new
                        matcher = kernel_entry.get("matcher")
                        matched = False
                        for existing_entry in existing_hooks[event_name]:
                            if existing_entry.get("matcher") == matcher:
                                existing_entry["hooks"].append(hook)
                                matched = True
                                break
                        if not matched:
                            existing_hooks[event_name].append(kernel_entry)
                        added += 1

            if added:
                log(f"  hooks.{event_name}: added {added} new hook(s)", "OK")
            else:
                log(f"  hooks.{event_name}: already up to date", "SKIP")

    existing["hooks"] = existing_hooks

    # Merge permissions
    kernel_perms = kernel_settings.get("permissions", {})
    existing_perms = existing.get("permissions", {})

    for key in ["allow", "deny"]:
        kernel_list = set(kernel_perms.get(key, []))
        existing_list = set(existing_perms.get(key, []))
        merged = sorted(existing_list | kernel_list)
        added_count = len(kernel_list - existing_list)
        existing_perms[key] = merged
        if added_count:
            log(f"  permissions.{key}: added {added_count} entries", "OK")

    existing["permissions"] = existing_perms

    if not dry_run:
        target_settings_path.write_text(json.dumps(existing, indent=2) + "\n")
    log("settings.json merged", "OK")


def merge_agents_yaml(target: Path, dry_run: bool):
    """Merge agents.yaml using section markers."""
    print("\n🔀 Phase 3c: Merging agents.yaml")

    kernel_yaml = (KERNEL_FILES / ".claude" / "agents.yaml").read_text()
    target_yaml_path = target / ".claude" / "agents.yaml"

    if not target_yaml_path.exists():
        if not dry_run:
            target_yaml_path.parent.mkdir(parents=True, exist_ok=True)
            target_yaml_path.write_text(kernel_yaml)
        log("agents.yaml created (no existing file)", "OK")
        return

    existing = target_yaml_path.read_text()

    # Extract kernel sections
    kernel_sections = YAML_SECTION_RE.findall(kernel_yaml)

    for section_name, section_content in kernel_sections:
        start_marker = f"# === ORA-KERNEL:START {section_name} ==="
        end_marker = f"# === ORA-KERNEL:END {section_name} ==="

        if start_marker in existing:
            # Replace existing section
            pattern = re.escape(start_marker) + r"\n.*?" + re.escape(end_marker) + r"\n"
            replacement = f"{start_marker}\n{section_content}{end_marker}\n"
            existing = re.sub(pattern, replacement, existing, flags=re.DOTALL)
            log(f"  agents.yaml section '{section_name}': updated", "OK")
        else:
            # Append new section
            existing = existing.rstrip() + "\n\n" + f"{start_marker}\n{section_content}{end_marker}\n"
            log(f"  agents.yaml section '{section_name}': added", "OK")

    if not dry_run:
        target_yaml_path.write_text(existing)
    log("agents.yaml merged", "OK")


def handle_kernel_listen(target: Path, dry_run: bool):
    """Handle kernel-listen.md collision — rename if exists."""
    print("\n🔀 Phase 3d: Handling kernel-listen command")

    target_cmd = target / ".claude" / "commands" / "kernel-listen.md"
    kernel_cmd = KERNEL_FILES / ".claude" / "commands" / "kernel-listen.md"

    if target_cmd.exists():
        # Check if it's already the kernel version (has "Kernel listener" in first lines)
        existing_content = target_cmd.read_text()
        if "Kernel listener" in existing_content or "ORA-KERNEL" in existing_content:
            # Already installed — overwrite
            if not dry_run:
                shutil.copy2(kernel_cmd, target_cmd)
            log("kernel-listen.md updated (kernel version already installed)", "OK")
        else:
            # Collision — install as alternate name
            alt_path = target / ".claude" / "commands" / "ora-kernel-listen.md"
            if not dry_run:
                shutil.copy2(kernel_cmd, alt_path)
            log(f"kernel-listen.md collision detected — installed as ora-kernel-listen.md", "WARN")
            log(f"  Existing kernel-listen.md preserved ({len(existing_content)} bytes)", "SKIP")
    else:
        if not dry_run:
            target_cmd.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(kernel_cmd, target_cmd)
        log("kernel-listen.md created", "OK")


# ============================================================================
# Phase 4: Infrastructure
# ============================================================================

def copy_infrastructure(target: Path, dry_run: bool):
    """Copy infrastructure files to namespaced directory."""
    print("\n🏗️  Phase 4: Infrastructure")

    infra_src = KERNEL_FILES / "infrastructure"
    infra_dst = target / "infrastructure" / "ora-kernel"

    if not dry_run:
        if infra_dst.exists():
            shutil.rmtree(infra_dst)
        shutil.copytree(infra_src, infra_dst)
    log(f"infrastructure/ora-kernel/ ({sum(1 for _ in infra_src.rglob('*') if _.is_file())} files)", "COPY")

    # Check if postgres is available
    try:
        result = subprocess.run(
            ["pg_isready", "-h", "localhost", "-p", "5432"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            log("PostgreSQL is running — migrations can be applied", "OK")
            log("  Run: psql -U <user> -d ora_kernel -f infrastructure/ora-kernel/db/001_core_schema.sql", "INFO")
        else:
            log("PostgreSQL not detected — start it and run migrations manually", "WARN")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        log("pg_isready not found — run migrations manually after starting postgres", "WARN")


# ============================================================================
# Phase 5: Report
# ============================================================================

def write_report(target: Path, report: dict, dry_run: bool):
    """Generate install report and version marker."""
    print("\n📝 Phase 5: Report")

    version = get_version()

    # Write version marker
    version_path = target / ".claude" / "kernel" / "INSTALLED_VERSION"
    if not dry_run:
        version_path.parent.mkdir(parents=True, exist_ok=True)
        version_path.write_text(version)
    log(f"ORA Kernel v{version} installed", "OK")

    # Print next steps
    print("\n" + "=" * 50)
    print("✅ Installation complete!")
    print("=" * 50)
    print("\nNext steps:")
    print("  1. Fill in PROJECT_DNA.md with your project's mission and constraints")
    print("  2. Restart Claude Code to load the new CLAUDE.md and hooks")
    print("  3. Create the ora_kernel database and run migrations:")
    print("       createdb ora_kernel")
    print("       for f in infrastructure/ora-kernel/db/*.sql; do psql -d ora_kernel -f $f; done")
    print("  4. Run /kernel-listen to start the Kernel event loop")
    print("  5. Push inotifywait to background when prompted, then use the TUI normally")
    print()

    if dry_run:
        print("  ⚠️  DRY RUN — no files were actually modified\n")


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Install ORA Kernel into a Claude Code project",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example: python3 install.py ~/projects/my-project",
    )
    parser.add_argument("target", type=Path, help="Target project directory")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without changing files")
    parser.add_argument("--force", action="store_true", help="Skip confirmation prompt")

    args = parser.parse_args()
    target = args.target.resolve()
    dry_run = args.dry_run

    print(f"ORA Kernel Installer v{get_version()}")
    print(f"Target: {target}")
    if dry_run:
        print("Mode: DRY RUN (no changes will be made)\n")
    print()

    # Phase 1
    report = preflight(target)
    print_report(report)

    if not args.force and not dry_run:
        response = input("Proceed with installation? [y/N] ")
        if response.lower() not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    # Phase 2
    copy_kernel_owned(target, dry_run)

    # Phase 3
    merge_claude_md(target, dry_run)
    merge_settings_json(target, dry_run)
    merge_agents_yaml(target, dry_run)
    handle_kernel_listen(target, dry_run)

    # Phase 4
    copy_infrastructure(target, dry_run)

    # Phase 5
    write_report(target, report, dry_run)


if __name__ == "__main__":
    main()
