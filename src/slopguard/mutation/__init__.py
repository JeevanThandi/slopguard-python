"""Mutation testing (``mutate``): change the source one operator at a time,
run the project's tests against each mutant, and report the mutants the tests
miss.

Coverage says a line ran in a test; a killed mutant says a test checks what the
line does. Mutants are written in place, one at a time, and the
:class:`WorkspaceGuard` restores every file — on normal completion, on errors,
and on SIGINT/SIGTERM/SIGHUP.

Library use::

    from slopguard.mutation import MutateOptions, run
    report = run(MutateOptions(source_path="./src", dry_run=True))
"""

from .guard import SourceChanged, WorkspaceGuard, guard_directory
from .models import (
    MUTATION_SCHEMA_VERSION,
    STATUS_COMPILE_ERROR,
    STATUS_IGNORED,
    STATUS_KILLED,
    STATUS_NO_COVERAGE,
    STATUS_PENDING,
    STATUS_SURVIVED,
    STATUS_TIMEOUT,
    MutantSite,
    PlannedMutant,
    mutant_id,
    mutation_score,
    summarize,
)
from .operators import OPERATOR_IDS, generate_mutants, parse_operators
from .pipeline import MutateOptions, MutationInterrupted, MutationPipeline, build_report, run
from .planner import enclosing_method, plan_file
from .source import SourceFile

__all__ = [
    "build_report",
    "enclosing_method",
    "generate_mutants",
    "guard_directory",
    "MUTATION_SCHEMA_VERSION",
    "MutantSite",
    "MutateOptions",
    "MutationInterrupted",
    "MutationPipeline",
    "mutant_id",
    "mutation_score",
    "OPERATOR_IDS",
    "parse_operators",
    "plan_file",
    "PlannedMutant",
    "run",
    "SourceChanged",
    "SourceFile",
    "STATUS_COMPILE_ERROR",
    "STATUS_IGNORED",
    "STATUS_KILLED",
    "STATUS_NO_COVERAGE",
    "STATUS_PENDING",
    "STATUS_SURVIVED",
    "STATUS_TIMEOUT",
    "summarize",
    "WorkspaceGuard",
]
