"""
dbt transform step — bronze into silver and silver_enhanced.

Invoked in-process via dbt's programmatic runner rather than a subprocess, so
dbt's own failures surface as Python exceptions with the parsed results
attached, and Prefect gets a real traceback instead of an exit code.
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Container layout puts the dbt project beside the scraper's modules; a checkout
# has it one level up. Both are checked so the same code runs either way.
DBT_PROJECT_CANDIDATES = (
    Path(__file__).parent / "data__bargin_sort",
    Path(__file__).parent.parent / "data__bargin_sort",
)


def find_dbt_project(explicit: str = "") -> Path:
    """Locate the dbt project directory, preferring an explicit setting."""
    if explicit:
        path = Path(explicit)
        if not (path / "dbt_project.yml").is_file():
            raise FileNotFoundError(f"No dbt_project.yml under {path}")
        return path

    for candidate in DBT_PROJECT_CANDIDATES:
        if (candidate / "dbt_project.yml").is_file():
            return candidate

    raise FileNotFoundError(
        "Could not find data__bargin_sort; set DBT_PROJECT_DIR to its path"
    )


def run_dbt(
    project_dir: Path,
    command: str = "run",
    target_run: Optional[str] = None,
    full_refresh: bool = False,
    select: Optional[str] = None,
) -> dict:
    """
    Invoke dbt and return a summary of what it built.

    `target_run` scopes the incremental models to a single scrape run, which is
    what keeps a post-scrape transform proportional to the new data rather than
    to everything bronze has ever held.
    """
    # Imported lazily so that scraping still works in an environment without
    # dbt installed — only the transform step actually needs it.
    from dbt.cli.main import dbtRunner

    args = [
        command,
        "--project-dir", str(project_dir),
        "--profiles-dir", str(project_dir),
    ]
    if full_refresh:
        args.append("--full-refresh")
    if select:
        args += ["--select", select]
    if target_run:
        args += ["--vars", f"{{target_run: {target_run}}}"]

    logger.info(f"Running dbt: {' '.join(args)}")
    result = dbtRunner().invoke(args)

    if not result.success:
        # result.exception is only set for whole-invocation failures. A model
        # that errors leaves success False with the reason on the node, so
        # without this the flow reports a useless "see logs".
        failures = []
        for node in getattr(result.result, "results", []) or []:
            if str(node.status) in ("error", "fail", "NodeStatus.Error", "TestStatus.Fail"):
                failures.append(f"{node.node.name}: {node.message}")

        detail = "; ".join(failures) or str(result.exception) or "see dbt logs"
        raise RuntimeError(f"dbt {command} failed — {detail}")

    models = []
    # A `run` returns RunExecutionResult; `test`/`seed` do too. Anything else
    # (parse, deps) has no per-node results worth summarising.
    for node in getattr(result.result, "results", []) or []:
        models.append(
            {
                "node": node.node.name,
                "status": str(node.status),
                "rows_affected": node.adapter_response.get("rows_affected"),
            }
        )

    summary = {
        "command": command,
        "target_run": target_run,
        "full_refresh": full_refresh,
        "models": models,
    }
    logger.info(f"dbt {command} completed: {len(models)} nodes")
    return summary
