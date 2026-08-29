from __future__ import annotations

import json
import re
import stat
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[1]
SMOKE_SCRIPT = PROJECT_ROOT / "scripts" / "smoke-release-asset.py"
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "release.yml"


def workflow_job(workflow: str, job_name: str) -> str:
    marker = f"  {job_name}:\n"
    start = workflow.index(marker)
    next_job = re.search(r"(?m)^  [a-zA-Z0-9_-]+:\n", workflow[start + len(marker) :])
    end = len(workflow) if next_job is None else start + len(marker) + next_job.start()
    return workflow[start:end]


def workflow_steps(job: str) -> list[str]:
    lines = job.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.startswith("      - ")]
    return [
        "".join(lines[start : starts[index + 1] if index + 1 < len(starts) else len(lines)])
        for index, start in enumerate(starts)
    ]


def named_step(steps: list[str], name: str) -> tuple[int, str]:
    marker = f"      - name: {name}\n"
    matches = [(index, step) for index, step in enumerate(steps) if step.startswith(marker)]
    assert len(matches) == 1, f"expected one workflow step named {name!r}"
    return matches[0]


def workflow_run_sources(workflow: str) -> str:
    run_steps = [
        step
        for job_name in ("test", "build-macos", "publish-release")
        for step in workflow_steps(workflow_job(workflow, job_name))
        if "        run:" in step
    ]
    return "\n".join(run_steps)


def release_workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def assert_release_workflow_contract(workflow: str) -> None:
    build_job = workflow_job(workflow, "build-macos")
    publish_job = workflow_job(workflow, "publish-release")
    build_steps = workflow_steps(build_job)
    publish_steps = workflow_steps(publish_job)

    validate_position, validate_step = named_step(build_steps, "Validate release tag")
    package_position, package_step = named_step(build_steps, "Package exact release archive")
    smoke_position, smoke_step = named_step(build_steps, "Smoke packaged release archive")
    result_position, result_step = named_step(build_steps, "Preserve packaged smoke result")
    checksum_position, checksum_step = named_step(build_steps, "Generate asset checksum")
    upload_position, upload_step = named_step(
        build_steps,
        "Preserve smoke-approved release asset",
    )

    run_sources = workflow_run_sources(workflow)
    assert "${{ github.ref_name }}" not in run_sources, "direct ref interpolation in run source"
    assert "$(" not in run_sources, "command substitution is forbidden in release shell"
    assert "`" not in run_sources, "backtick substitution is forbidden in release shell"

    assert validate_position < package_position < smoke_position, "release tag validation order"
    assert smoke_position + 1 == result_position, "failed smoke result upload must be immediate"
    assert result_position < checksum_position < upload_position, "release gate step order"
    assert "continue-on-error:" not in smoke_step, "smoke must not continue on error"
    assert "        if:" not in smoke_step, "smoke must not suppress failures with if"
    assert "scripts/smoke-release-asset.py" in smoke_step
    assert build_job.count("scripts/smoke-release-asset.py") == 1

    strict_tag_regex = r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    assert strict_tag_regex in validate_step, "strict release tag validation is required"
    assert 'scripts/package-release-asset.sh "$RELEASE_TAG" "$ASSET_SUFFIX"' in package_step
    assert '"$archive" "$RELEASE_TAG" --result "$result"' in smoke_step

    assert "        if: ${{ always() }}" in result_step, "failed smoke result needs always upload"
    assert "uses: actions/upload-artifact@v4" in result_step
    assert ".smoke.json" in result_step
    assert "if-no-files-found: warn" in result_step
    assert "RELEASE_TAG" not in result_step, "failed result path must not use an unvalidated tag"

    assert "        if: ${{ success() }}" in checksum_step
    assert "        if: ${{ success() }}" in upload_step
    assert "always()" not in checksum_step
    assert "always()" not in upload_step
    assert "*" not in checksum_step, "checksum must use one exact archive path"
    assert ".tar.gz.sha256" in upload_step
    assert ".smoke.json" not in upload_step
    assert "if-no-files-found: error" in upload_step

    assert "needs: build-macos" in publish_job
    _, publish_validate = named_step(publish_steps, "Validate release tag")
    _, assemble_step = named_step(publish_steps, "Assemble smoke-approved checksums")
    assert strict_tag_regex in publish_validate
    assert "*" not in assemble_step, "published checksums must use exact asset paths"
    assert "macos-arm64.tar.gz.sha256" in assemble_step
    assert "macos-x86_64.tar.gz.sha256" in assemble_step

    assert "macos-arm64" in build_job
    assert "macos-x86_64" in build_job
    assert "ASSET_SUFFIX: ${{ matrix.suffix }}" in build_job
    assert "RELEASE_TAG: ${{ github.ref_name }}" in build_job


def replace_workflow_step(workflow: str, original: str, replacement: str) -> str:
    assert workflow.count(original) == 1
    return workflow.replace(original, replacement, 1)


def fake_binary_source(version: str, mode: str) -> str:
    source = """
    #!/usr/bin/env python3
    import json
    import os
    import sqlite3
    import sys
    from pathlib import Path

    VERSION = "__VERSION__"
    MODE = "__MODE__"
    args = sys.argv[1:]

    if "PYTHONPATH" in os.environ or "VIRTUAL_ENV" in os.environ:
        print("project Python environment leaked into smoke", file=sys.stderr)
        raise SystemExit(70)
    if Path.cwd() == Path(__file__).resolve().parent:
        print("binary was run from its bundle directory", file=sys.stderr)
        raise SystemExit(71)

    if args == ["--version"]:
        print(f"meetily-memory {VERSION}")
        raise SystemExit(0)
    if args == ["--help"]:
        print("Usage: mm COMMAND\\nCommands: init s doctor")
        raise SystemExit(0)

    data_dir = Path(os.environ["MEETILY_MEMORY_DATA_DIR"])
    index_path = data_dir / "index.sqlite"
    state_path = data_dir / "state.sqlite"
    internal_dir = Path(__file__).resolve().parent / "_internal"

    if args and args[0] == "init":
        if not (internal_dir / "sqlite.available").is_file():
            print("packaged sqlite module is unavailable", file=sys.stderr)
            raise SystemExit(72)
        if MODE == "checkout_dependency" and not (Path.cwd() / "pyproject.toml").exists():
            print("checkout file is unavailable", file=sys.stderr)
            raise SystemExit(73)
        source_path = str(Path(args[args.index("--source") + 1]).resolve())
        data_dir.mkdir(parents=True, exist_ok=True)
        for database in (index_path, state_path):
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE smoke_rows (value TEXT NOT NULL)")
                connection.execute("INSERT INTO smoke_rows VALUES ('synthetic')")
                connection.commit()
        (data_dir / "settings.json").write_text(
            json.dumps({"source_uuid": "synthetic-source-uuid"}) + "\\n",
            encoding="utf-8",
        )
        print(json.dumps({
            "initialized": True,
            "index_path": str(index_path),
            "source_path": source_path,
            "autosync_enabled": False,
            "source_uuid": "synthetic-source-uuid",
            "meetings_seen": 1,
            "meetings_inserted": 1,
            "chunks_seen": 1,
        }))
        raise SystemExit(0)

    if args and args[0] == "s":
        print(json.dumps([{
            "meeting": {
                "ref": {
                    "source_uuid": "synthetic-source-uuid",
                    "external_id": "synthetic-release-smoke-meeting",
                },
            },
            "evidence": [{
                "excerpt": {
                    "chunk_external_id": "synthetic-release-smoke-transcript",
                    "text": "Synthetic packagedsmokemarker evidence.",
                },
            }],
        }]))
        raise SystemExit(0)

    if args and args[0] == "doctor":
        source_path = str(Path(args[args.index("--source") + 1]).resolve())
        if MODE == "doctor_mutates":
            with index_path.open("ab") as handle:
                handle.write(b"mutation")
        print(json.dumps({
            "source_path": source_path,
            "source_readable": True,
            "source_schema_valid": True,
            "fts5": (internal_dir / "fts5.available").is_file(),
            "meetings": 1,
            "chunks": 1,
            "index_database": {"status": "current"},
            "state_database": {"status": "current"},
            "last_completed_run": {"status": "completed"},
        }))
        raise SystemExit(0)

    print("unexpected command", file=sys.stderr)
    raise SystemExit(74)
    """
    return (
        textwrap.dedent(source).lstrip().replace("__VERSION__", version).replace("__MODE__", mode)
    )


def create_fake_archive(
    root: Path,
    *,
    version: str = "0.6.0",
    mode: str = "passing",
    executable: bool = True,
) -> Path:
    release_root = root / "release"
    bundle_name = "meetily-memory-v0.6.0-macos-arm64"
    bundle_root = root / "bundle" / bundle_name
    internal = bundle_root / "_internal"
    internal.mkdir(parents=True)
    (internal / "packaged-component.txt").write_text("synthetic\n", encoding="utf-8")
    if mode != "missing_sqlite":
        (internal / "sqlite.available").write_text("synthetic\n", encoding="utf-8")
    if mode != "missing_fts":
        (internal / "fts5.available").write_text("synthetic\n", encoding="utf-8")
    for document in ("README.md", "CHANGELOG.md", "LICENSE"):
        (bundle_root / document).write_text(f"synthetic {document}\n", encoding="utf-8")
    binary = bundle_root / "mm"
    binary.write_text(fake_binary_source(version, mode), encoding="utf-8")
    binary.chmod(0o755 if executable else 0o644)

    release_root.mkdir()
    archive = release_root / f"{bundle_name}.tar.gz"
    with tarfile.open(archive, "w:gz") as package:
        package.add(bundle_root, arcname=bundle_name)
    return archive


def invoke_smoke(
    tmp_path: Path,
    *,
    version: str = "0.6.0",
    mode: str = "passing",
    executable: bool = True,
    expected_tag: str = "v0.6.0",
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    archive = create_fake_archive(
        tmp_path,
        version=version,
        mode=mode,
        executable=executable,
    )
    tool_dir = tmp_path / "standalone-tool"
    tool_dir.mkdir()
    standalone_script = tool_dir / SMOKE_SCRIPT.name
    standalone_script.write_bytes(SMOKE_SCRIPT.read_bytes())
    standalone_script.chmod(standalone_script.stat().st_mode | stat.S_IXUSR)
    result_path = tmp_path / "result" / "smoke.json"
    clean_cwd = tmp_path / "invocation-cwd"
    clean_cwd.mkdir()

    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(standalone_script),
            str(archive),
            expected_tag,
            "--result",
            str(result_path),
        ],
        cwd=clean_cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed, json.loads(result_path.read_text(encoding="utf-8"))


def test_smoke_passes_for_self_contained_archive_outside_checkout(tmp_path: Path) -> None:
    completed, result = invoke_smoke(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert result["status"] == "passed"
    assert result["expected_tag"] == "v0.6.0"
    assert result["expected_version"] == "0.6.0"
    assert result["checks"] == {
        "doctor": True,
        "doctor_read_only": True,
        "executable": True,
        "fixture": True,
        "fts_search": True,
        "help": True,
        "init": True,
        "layout": True,
        "version": True,
    }
    assert result["isolation"] == {
        "cwd_outside_checkout": True,
        "data_dir_isolated": True,
        "project_python_environment_removed": True,
    }


@pytest.mark.parametrize(
    ("version", "mode", "executable", "expected_error"),
    [
        ("0.6.0", "passing", False, "executable bit"),
        ("0.6.1", "passing", True, "version does not match tag"),
        ("0.6.0", "missing_sqlite", True, "sqlite module is unavailable"),
        ("0.6.0", "missing_fts", True, "does not provide FTS5"),
        ("0.6.0", "checkout_dependency", True, "checkout file is unavailable"),
        ("0.6.0", "doctor_mutates", True, "doctor modified files or SQLite rows"),
    ],
)
def test_smoke_rejects_broken_release_archives(
    tmp_path: Path,
    version: str,
    mode: str,
    executable: object,
    expected_error: str,
) -> None:
    assert isinstance(executable, bool)
    completed, result = invoke_smoke(
        tmp_path,
        version=version,
        mode=mode,
        executable=executable,
    )

    assert completed.returncode == 1
    assert result["status"] == "failed"
    assert expected_error.casefold() in str(result["error"]).casefold()


def test_release_workflow_preserves_failed_results_and_gates_assets() -> None:
    assert_release_workflow_contract(release_workflow())


def test_release_workflow_rejects_smoke_continue_on_error_mutation() -> None:
    workflow = release_workflow()
    steps = workflow_steps(workflow_job(workflow, "build-macos"))
    _, smoke_step = named_step(steps, "Smoke packaged release archive")
    mutated_step = smoke_step.replace(
        "        run:",
        "        continue-on-error: true\n        run:",
        1,
    )

    with pytest.raises(AssertionError, match="continue on error"):
        assert_release_workflow_contract(replace_workflow_step(workflow, smoke_step, mutated_step))


def test_release_workflow_rejects_direct_ref_interpolation_mutation() -> None:
    workflow = release_workflow()
    steps = workflow_steps(workflow_job(workflow, "build-macos"))
    _, smoke_step = named_step(steps, "Smoke packaged release archive")
    mutated_step = smoke_step.replace(
        '"$archive" "$RELEASE_TAG" --result',
        '"$archive" "${{ github.ref_name }}" --result',
        1,
    )

    with pytest.raises(AssertionError, match="direct ref interpolation"):
        assert_release_workflow_contract(replace_workflow_step(workflow, smoke_step, mutated_step))


def test_release_workflow_rejects_missing_always_result_upload_mutation() -> None:
    workflow = release_workflow()
    steps = workflow_steps(workflow_job(workflow, "build-macos"))
    _, result_step = named_step(steps, "Preserve packaged smoke result")
    mutated_step = result_step.replace("        if: ${{ always() }}\n", "", 1)

    with pytest.raises(AssertionError, match="always upload"):
        assert_release_workflow_contract(replace_workflow_step(workflow, result_step, mutated_step))


def test_release_workflow_rejects_checksum_before_smoke_mutation() -> None:
    workflow = release_workflow()
    build_job = workflow_job(workflow, "build-macos")
    steps = workflow_steps(build_job)
    _, smoke_step = named_step(steps, "Smoke packaged release archive")
    _, result_step = named_step(steps, "Preserve packaged smoke result")
    _, checksum_step = named_step(steps, "Generate asset checksum")
    original_sequence = smoke_step + result_step + checksum_step
    mutated_sequence = checksum_step + smoke_step + result_step
    mutated_job = replace_workflow_step(build_job, original_sequence, mutated_sequence)

    with pytest.raises(AssertionError, match="order"):
        assert_release_workflow_contract(replace_workflow_step(workflow, build_job, mutated_job))


def test_build_and_package_scripts_preserve_release_gate_boundaries() -> None:
    build_script = (PROJECT_ROOT / "scripts" / "build-binary.sh").read_text(encoding="utf-8")
    package_script = (PROJECT_ROOT / "scripts" / "package-release-asset.sh").read_text(
        encoding="utf-8"
    )

    assert "--copy-metadata meetily-memory" in build_script
    assert "test -x dist/mm/mm" in package_script
    assert "test -d dist/mm/_internal" in package_script
    assert "COPYFILE_DISABLE=1 tar" in package_script
    assert "shasum" not in package_script
    assert "sha256sum" not in package_script
