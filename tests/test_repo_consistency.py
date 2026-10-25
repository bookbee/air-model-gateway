"""The docs, examples and scripts agree with the code.

Knowledge in this repo lives in prose as much as in code; these checks keep the
two from drifting apart silently:

- every AIR_MODEL_GATEWAY_* variable named anywhere is a real setting, and
  .env.example documents every setting;
- every `make <target>` the docs mention exists;
- every metric the code exports is documented, and every documented metric
  exists;
- every relative link in the Markdown docs resolves;
- `make ollama-pull` pulls every Ollama model deploy/routing.yaml names;
- the committed design diagrams are what docs/design/diagrams.py draws.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from air_model_gateway.config import Settings
from air_model_gateway.constants import Provider
from air_model_gateway.gateway.models import ModelCatalog
from air_model_gateway.observability import metrics

ROOT = Path(__file__).resolve().parents[1]
_SKIP = {
    ".venv",
    ".git",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".terraform",
    "node_modules",
}
# Variables that aren't settings: the secrets-dir switch, compose interpolation
# vars the Makefile exports, and the reference client's own.
_NOT_SETTINGS = {
    "AIR_MODEL_GATEWAY_SECRETS_DIR",
    "AIR_MODEL_GATEWAY_OLLAMA_URL",
    "AIR_MODEL_GATEWAY_REDIS_URL",
    "AIR_MODEL_GATEWAY_URL",
    "AIR_MODEL_GATEWAY_TOKEN",
}


def _settings_names() -> set[str]:
    names: set[str] = set()

    def walk(model: type[BaseModel], prefix: str) -> None:
        for field, info in model.model_fields.items():
            kind = info.annotation
            if isinstance(kind, type) and issubclass(kind, BaseModel):
                walk(kind, f"{prefix}{field.upper()}__")
            else:
                names.add(f"{prefix}{field.upper()}")

    walk(Settings, "AIR_MODEL_GATEWAY_")
    return names


def _files(*suffixes: str) -> list[Path]:
    return [
        p
        for p in ROOT.rglob("*")
        if p.is_file()
        and not _SKIP.intersection(p.relative_to(ROOT).parts)
        and (p.suffix in suffixes or p.name in suffixes)
    ]


def _docs() -> list[Path]:
    return [p for p in _files(".md") if "tests" not in p.parts]


def test_every_setting_is_documented_in_env_example() -> None:
    example = (ROOT / ".env.example").read_text()
    assert sorted(n for n in _settings_names() if n not in example) == []


def test_every_variable_named_anywhere_is_a_setting() -> None:
    names = _settings_names()
    unknown: dict[str, str] = {}
    for path in _files(".md", ".yaml", ".yml", ".sh", ".example", "Makefile", "Dockerfile"):
        for var in set(re.findall(r"AIR_MODEL_GATEWAY_[A-Z_]*[A-Z]", path.read_text())):
            known = var in names or var in _NOT_SETTINGS
            is_prefix = any(n.startswith(var + "_") for n in names)
            if not (known or is_prefix):
                unknown[var] = str(path.relative_to(ROOT))
    assert unknown == {}


def test_every_make_target_the_docs_mention_exists() -> None:
    targets = set(re.findall(r"^([a-zA-Z0-9_-]+):", (ROOT / "Makefile").read_text(), re.M))
    missing = {
        (t, str(p.relative_to(ROOT)))
        for p in _docs()
        for t in re.findall(r"`make ([a-z][a-z0-9-]*[a-z0-9])(?=[` ])", p.read_text())
        if t not in targets
    }
    assert missing == set()


def test_every_metric_is_documented_and_every_documented_metric_exists() -> None:
    exported = {
        c._name.removeprefix("air_model_gateway_").removesuffix("_total")  # type: ignore[attr-defined]
        for c in vars(metrics).values()
        if hasattr(c, "_name") and str(c._name).startswith("air_model_gateway_")
    }
    lld = (ROOT / "docs" / "02-lld.md").read_text()
    section = lld[lld.index("- **Metrics**") : lld.index("## 12.")]
    table = set(re.findall(r"^\| `([a-z_]+?)(?:_total)?` ", section, re.M))
    assert exported - table == set(), "export without docs/02-lld.md §11 row"
    assert table - exported == set(), "docs/02-lld.md §11 row for a metric that doesn't exist"


def test_every_relative_link_in_the_docs_resolves() -> None:
    broken = {
        (str(p.relative_to(ROOT)), link)
        for p in _docs()
        for link in re.findall(r"\]\(([^)#:\s]+)", p.read_text())
        if not (p.parent / link).exists()
    }
    assert broken == set()


def test_the_image_build_context_is_an_allowlist() -> None:
    """Generated secrets live under deploy/ (staging secrets, kind state). The
    build context must exclude everything by default, and the Dockerfile must
    never copy deploy/ wholesale, or a local build bakes provider keys in."""
    ignore = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert ignore[0] == "*"
    copies = re.findall(r"^COPY (?!--from)(.+)$", (ROOT / "Dockerfile").read_text(), re.M)
    sources = {src for line in copies for src in line.split()[:-1]}
    assert "deploy" not in sources and "." not in sources
    assert not any(s.startswith(("deploy/local", "deploy/gke")) for s in sources)


def test_every_source_file_is_in_the_overview_tree() -> None:
    """docs/overview.md §4 explains every file; a new module needs its line."""
    overview = (ROOT / "docs" / "overview.md").read_text()
    missing = {
        str(p.relative_to(ROOT))
        for p in (ROOT / "src").rglob("*.py")
        if p.name != "__init__.py" and "__pycache__" not in p.parts and p.name not in overview
    }
    assert missing == set()


def test_ollama_pull_covers_every_ollama_model_the_routing_uses() -> None:
    doc = ModelCatalog.load(ROOT / "deploy" / "routing.yaml").doc
    chains = [*doc.generate.values()]
    chains += [c for o in doc.overrides.values() for c in o.generate.values()]
    used = {t.model for chain in chains for t in chain if t.provider is Provider.OLLAMA}
    used |= {e.model for e in doc.embed.values() if e.provider is Provider.OLLAMA}
    line = re.search(r"^OLLAMA_MODELS \?= (.+)$", (ROOT / "Makefile").read_text(), re.M)
    assert line is not None
    assert used <= set(line.group(1).split())


def test_the_committed_design_diagrams_are_current() -> None:
    """Like the OpenAPI drift check: edit diagrams.py, then `make design`."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("diagrams", ROOT / "docs/design/diagrams.py")
    assert spec is not None and spec.loader is not None
    diagrams = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diagrams)
    for name, draw in diagrams.DIAGRAMS.items():
        committed = (ROOT / "docs/design/diagrams" / f"{name}.svg").read_text()
        assert committed == draw() + "\n", f"{name}.svg is stale: run make design"
