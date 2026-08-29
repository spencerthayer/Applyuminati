"""Text mining for job postings: HTML stripping, requirement splitting, skills.

Stdlib only — no bs4, no lxml. A job description is the single richest signal
source in the system, so a small, dependency-free, deterministic extractor
that handles the ~95% common case is worth more than a heavy one that needs
network installs to parse.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from unicodedata import normalize

__all__ = [
    "TECH_VOCABULARY",
    "extract_skills",
    "html_to_text",
    "split_requirements",
]

#: ~150 common technology tokens, lowercased. Deliberately curated rather than
#: scraped: precision matters more than coverage, because a false positive
#: skill shows up in the score explanation and erodes trust.
TECH_VOCABULARY: frozenset[str] = frozenset(
    {
        "python",
        "java",
        "javascript",
        "typescript",
        "go",
        "golang",
        "rust",
        "ruby",
        "php",
        "swift",
        "kotlin",
        "scala",
        "c",
        "c++",
        "c#",
        "objective-c",
        "elixir",
        "clojure",
        "haskell",
        "perl",
        "r",
        "julia",
        "dart",
        "lua",
        "shell",
        "bash",
        "sql",
        "postgresql",
        "mysql",
        "sqlite",
        "mongodb",
        "redis",
        "dynamodb",
        "cassandra",
        "elasticsearch",
        "snowflake",
        "bigquery",
        "redshift",
        "clickhouse",
        "kafka",
        "rabbitmq",
        "sqs",
        "kinesis",
        "nats",
        "react",
        "react native",
        "vue",
        "angular",
        "svelte",
        "next.js",
        "nuxt",
        "node.js",
        "express",
        "django",
        "flask",
        "fastapi",
        "spring",
        "spring boot",
        "rails",
        "laravel",
        "asp.net",
        ".net",
        "gin",
        "echo",
        "actix",
        "axum",
        "graphql",
        "grpc",
        "rest",
        "openapi",
        "protobuf",
        "aws",
        "gcp",
        "azure",
        "docker",
        "kubernetes",
        "k8s",
        "terraform",
        "ansible",
        "puppet",
        "chef",
        "helm",
        "pulumi",
        "cloudformation",
        "linux",
        "unix",
        "windows",
        "macos",
        "git",
        "github",
        "gitlab",
        "bitbucket",
        "jenkins",
        "circleci",
        "github actions",
        "pytest",
        "jest",
        "vitest",
        "cypress",
        "playwright",
        "selenium",
        "junit",
        "pandas",
        "numpy",
        "scipy",
        "scikit-learn",
        "tensorflow",
        "pytorch",
        "keras",
        "xgboost",
        "spark",
        "hadoop",
        "airflow",
        "dbt",
        "prefect",
        "dagster",
        "tableau",
        "looker",
        "power bi",
        "superset",
        "html",
        "css",
        "tailwind",
        "sass",
        "scss",
        "less",
        "webpack",
        "vite",
        "rollup",
        "esbuild",
        "babel",
        "memcached",
        "celery",
        "sidekiq",
        "resque",
        "oauth",
        "oidc",
        "saml",
        "jwt",
        "sso",
        "microservices",
        "event-driven",
        "ddd",
        "tdd",
        "agile",
        "scrum",
        "kanban",
        "gradle",
        "maven",
        "sbt",
        "nginx",
        "apache",
        "haproxy",
        "envoy",
        "traefik",
        "prometheus",
        "grafana",
        "datadog",
        "sentry",
        "opentelemetry",
        "fpga",
        "verilog",
        "vhdl",
        "embedded",
        "rtos",
        "arduino",
        "raspberry pi",
    }
)


class _TextExtractor(HTMLParser):
    """Collects text, turning block elements into newlines and dropping noise."""

    _BLOCK_TAGS = frozenset(
        {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
    )
    _SKIP_TAGS = frozenset({"script", "style", "noscript", "head", "svg", "meta", "link"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        if tag in self._BLOCK_TAGS and self._chunks and not self._chunks[-1].endswith("\n"):
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag in self._BLOCK_TAGS and self._chunks and not self._chunks[-1].endswith("\n"):
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse runs of whitespace within a line, keep line breaks.
        lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(html: str) -> str:
    """Convert HTML to plain text. Non-HTML input passes through untouched."""
    if "<" not in html:
        return html
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return normalize("NFKC", parser.text())


# -- requirement splitting ------------------------------------------------

_REQUIRED_HEADINGS = re.compile(
    r"""
    ^\s*(?:[-*]\s*)?           # optional bullet
    (?:requirements?|what\s+you(?:'ll|\\u2019ll)?\s+(?:need|do)|must[- ]have|
       minimum\s+(?:requirements?|qualifications?)|essential|
       about\s+you|you\s+have|qualifications?)\s*:?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_PREFERRED_HEADINGS = re.compile(
    r"""
    ^\s*(?:[-*]\s*)?
    (?:nice[- ]to[- ]have|preferred(?:\s+qualifications?)?|bonus(?:\s+points?)?|
       plus|optional|would[- ]be[- ]nice|extra\s+credit)\s*:?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_BULLET_RE = re.compile(r"^\s*(?:[-*\u2022\u2023\u25e6\u2043]|\d+[.)])\s+(.*)$")


def _is_heading(line: str) -> str | None:
    if _REQUIRED_HEADINGS.match(line):
        return "required"
    if _PREFERRED_HEADINGS.match(line):
        return "preferred"
    return None


def split_requirements(text: str) -> tuple[list[str], list[str]]:
    """Split a description into (required, preferred) bullet lines.

    Heuristic, deliberately conservative: when no section heading is found,
    everything is returned as "required" and "preferred" stays empty, because
    a missing preferred section is far more common than a missing required one
    and guessing the wrong bucket misleads the scorer.
    """
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    section: str | None = None
    required: list[str] = []
    preferred: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        target = preferred if section == "preferred" else required
        target.extend(buffer)
        buffer.clear()

    for line in lines:
        heading = _is_heading(line)
        if heading is not None:
            flush()
            section = heading
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            flush()
            section = section or "required"
            (preferred if section == "preferred" else required).append(bullet.group(1).strip())
        elif section is not None:
            buffer.append(line.strip())
        else:
            required.append(line.strip())
    flush()
    return required, preferred


# -- skill extraction -----------------------------------------------------

_WORD_BOUNDARY_CACHE: dict[str, re.Pattern[str]] = {}


def _skill_pattern(token: str) -> re.Pattern[str]:
    pattern = _WORD_BOUNDARY_CACHE.get(token)
    if pattern is None:
        # Allow dots and + inside the token (node.js, c++); require a
        # non-word boundary outside it. A trailing "." is treated as a
        # boundary (sentence punctuation) unless it continues into another
        # dotted segment (kafka.io), which would indicate a different token.
        escaped = re.escape(token)
        pattern = re.compile(
            rf"(?<![A-Za-z0-9+#]){escaped}(?![A-Za-z0-9+#])(?!\.[A-Za-z0-9])",
            re.IGNORECASE,
        )
        _WORD_BOUNDARY_CACHE[token] = pattern
    return pattern


def extract_skills(text: str, vocabulary: set[str] | None = None) -> list[str]:
    """Whole-token skill match against a vocabulary plus the built-in tech set.

    Returns sorted unique tokens in their original casing from the vocabulary
    (not the text), so the same skill always serialises the same way.
    """
    vocab = vocabulary | TECH_VOCABULARY if vocabulary else set(TECH_VOCABULARY)
    haystack = text.lower()
    found: set[str] = set()
    for token in vocab:
        if _skill_pattern(token).search(haystack):
            found.add(token)
    return sorted(found)
