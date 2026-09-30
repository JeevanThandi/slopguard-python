"""slopguard-python: a CRAP (Change Risk Anti-Patterns) guardrail for Python.

Measures complex, undertested code by combining cyclomatic and cognitive
complexity (parsed via the standard-library ``ast``) with line coverage gathered
from the project's own test suite. The Python sibling of slopguard-go,
slopguard-kotlin, slopguard-swift and slopguard-typescript — same wCRAP formula,
same schema-2 JSON, same CLI UX.

Library use::

    from slopguard import analyze_source                  # complexity only, no I/O
    report = analyze_source(open("foo.py").read(), "foo.py")

    from slopguard.coverage import run, CoverageSource    # full pipeline
    report = run("./src", CoverageSource(), threshold=30.0,
                 options=default_analysis_options())

    from slopguard.mutation import MutateOptions, run     # mutation testing
    report = run(MutateOptions(source_path="./src", dry_run=True))
"""

from .aggregator import aggregate
from .cli import run as cli_run
from .complexity import analyze_source
from .crap import DEFAULT_CRAP_THRESHOLD, crap_score, weighted_complexity
from .diranalyzer import AnalysisOptions, analyze_tree, default_analysis_options
from .errors import SlopguardError
from .fileanalyzer import analyze_file
from .formatting import json_report, mutation_json_report, mutation_pretty_report, pretty_report
from .mutation import OPERATOR_IDS, MutateOptions, generate_mutants
from .version import SCHEMA_VERSION, TOOL_NAME, VERSION

__all__ = [
    "aggregate",
    "analyze_file",
    "analyze_source",
    "analyze_tree",
    "AnalysisOptions",
    "cli_run",
    "crap_score",
    "default_analysis_options",
    "DEFAULT_CRAP_THRESHOLD",
    "generate_mutants",
    "json_report",
    "MutateOptions",
    "mutation_json_report",
    "mutation_pretty_report",
    "OPERATOR_IDS",
    "pretty_report",
    "SlopguardError",
    "SCHEMA_VERSION",
    "TOOL_NAME",
    "VERSION",
    "weighted_complexity",
]

__version__ = VERSION
