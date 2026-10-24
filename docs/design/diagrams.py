"""Draws the design document's diagrams as standalone SVG files.

Stdlib only, so it runs anywhere `make design` does. Each diagram is one
function returning SVG markup; `main` writes them to docs/design/diagrams/,
where both design.html (the printable PDF) and README.md (GitHub renders SVG
inline) pick them up. The drawings restate facts that live in code and config
(routing.yaml, the policy files, the chart, the workflows): when one of those
changes, change the matching function here and run `make design`.

Numbered pins (`pin(n, ...)`) are the pointers the notes beside each diagram in
design.html refer to; keep their numbers in step with those notes.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parent / "diagrams"
FONT = "'Helvetica Neue', Helvetica, Arial, sans-serif"

# One colour per layer, used the same way on every diagram (the legend on the
# cover page explains them): fill, stroke.
KINDS: dict[str, tuple[str, str]] = {
    "consumer": ("#E8F1FD", "#2F6FDB"),
    "edge": ("#F1ECFD", "#6B4FD8"),
    "core": ("#E3F5F2", "#0F8C7E"),
    "local": ("#E8F5E3", "#3C8D2F"),
    "cloud": ("#FDEFE2", "#D9822B"),
    "state": ("#FCE8EC", "#C23B57"),
    "obs": ("#FFF6D9", "#A87F00"),
    "config": ("#F2F3F5", "#5F6673"),
    "infra": ("#EEF1FE", "#4F46E5"),
    "error": ("#FDECEC", "#C62828"),
    "plain": ("#FFFFFF", "#9AA1AC"),
}
INK = "#1F2430"
MUTED = "#5F6673"


def svg(width: int, height: int, body: list[str], title: str) -> str:
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" font-family="{FONT}" role="img">',
            f"<title>{escape(title)}</title>",
            "<defs>",
            *(
                f'<marker id="arrow-{name}" viewBox="0 0 10 10" refX="9" refY="5" '
                f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
                f'<path d="M0,0 L10,5 L0,10 z" fill="{colour}"/></marker>'
                for name, colour in (("ink", INK), ("muted", MUTED), ("error", "#C62828"))
            ),
            "</defs>",
            # An explicit background, so the drawing stays legible on GitHub's
            # dark theme as well as on paper.
            f'<rect width="{width}" height="{height}" fill="#FFFFFF"/>',
            *body,
            "</svg>",
        ]
    )


def text(
    x: float,
    y: float,
    s: str,
    *,
    size: float = 12,
    weight: str = "normal",
    colour: str = INK,
    anchor: str = "start",
    italic: bool = False,
) -> str:
    style = ' font-style="italic"' if italic else ""
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{colour}" '
        f'text-anchor="{anchor}"{style}>{escape(s)}</text>'
    )


def box(
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    lines: tuple[str, ...] = (),
    *,
    kind: str = "plain",
    size: float = 13,
    centre: bool = False,
    mono_title: bool = False,
) -> str:
    fill, stroke = KINDS[kind]
    parts = [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="7" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="1.4"/>'
    ]
    tx = x + w / 2 if centre else x + 10
    anchor = "middle" if centre else "start"
    block = size + 4 + len(lines) * (size - 1.5)
    ty = y + (h - block) / 2 + size if centre else y + size + 7
    family = ' font-family="Menlo, Consolas, monospace"' if mono_title else ""
    parts.append(
        f'<text x="{tx}" y="{ty}" font-size="{size}" font-weight="bold" fill="{INK}" '
        f'text-anchor="{anchor}"{family}>{escape(title)}</text>'
    )
    for i, line in enumerate(lines):
        parts.append(
            text(tx, ty + 4 + (i + 1) * (size - 1.5), line, size=size - 2.5, colour=MUTED, anchor=anchor)
        )
    return "\n".join(parts)


def group(x: float, y: float, w: float, h: float, label: str, *, kind: str = "plain") -> str:
    _, stroke = KINDS[kind]
    return "\n".join(
        [
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" fill="none" '
            f'stroke="{stroke}" stroke-width="1.3" stroke-dasharray="6 4"/>',
            text(x + 12, y + 18, label, size=12, weight="bold", colour=stroke),
        ]
    )


def arrow(
    points: list[tuple[float, float]],
    label: str = "",
    *,
    style: str = "ink",
    dashed: bool = False,
    both: bool = False,
    label_at: tuple[float, float] | None = None,
    label_anchor: str = "middle",
) -> str:
    colour = {"ink": INK, "muted": MUTED, "error": "#C62828"}[style]
    path = " ".join(f"{px},{py}" for px, py in points)
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    start = f' marker-start="url(#arrow-{style})"' if both else ""
    parts = [
        f'<polyline points="{path}" fill="none" stroke="{colour}" stroke-width="1.5"{dash}'
        f'{start} marker-end="url(#arrow-{style})"/>'
    ]
    if label:
        if label_at is None:
            (x1, y1), (x2, y2) = points[0], points[-1]
            label_at = ((x1 + x2) / 2, (y1 + y2) / 2 - 6)
        lx, ly = label_at
        parts.append(text(lx, ly, label, size=10.5, colour=colour, anchor=label_anchor))
    return "\n".join(parts)


def pin(n: int, x: float, y: float) -> str:
    return "\n".join(
        [
            f'<circle cx="{x}" cy="{y}" r="11" fill="{INK}"/>',
            text(x, y + 4.2, str(n), size=12, weight="bold", colour="#FFFFFF", anchor="middle"),
        ]
    )


def chip(x: float, y: float, label: str, kind: str, *, w: float | None = None) -> tuple[str, float]:
    """A rounded label for one rung of a chain; returns the markup and its width."""
    fill, stroke = KINDS[kind]
    width = w if w is not None else 16 + len(label) * 6.4
    return (
        "\n".join(
            [
                f'<rect x="{x}" y="{y}" width="{width}" height="24" rx="12" fill="{fill}" '
                f'stroke="{stroke}" stroke-width="1.2"/>',
                text(x + width / 2, y + 16, label, size=11, anchor="middle"),
            ]
        ),
        width,
    )


def chain(x: float, y: float, rungs: list[tuple[str, str]]) -> list[str]:
    """Rungs left to right, joined by arrows: the order the router tries them."""
    parts: list[str] = []
    cx = x
    for i, (label, kind) in enumerate(rungs):
        if i:
            parts.append(arrow([(cx - 18, y + 12), (cx - 3, y + 12)], style="muted"))
        markup, width = chip(cx, y, label, kind)
        parts.append(markup)
        cx += width + 22
    return parts


# ---- 1. System context ------------------------------------------------------


def context() -> str:
    b: list[str] = []
    b.append(group(20, 70, 265, 440, "Consumers · AIR services", kind="consumer"))
    consumers = [
        ("air-classifier-service", "generate local · auto · cloud, embed"),
        ("air-orchestrator-service", "generate cloud (streams), embed"),
        ("air-tools", "generate cloud (agent steps)"),
        ("air-rag", "embed local-768-v1"),
        ("air-web-console", "generate, pin, admin_read"),
    ]
    for i, (name, use) in enumerate(consumers):
        y = 100 + i * 80
        b.append(box(35, y, 235, 62, name, (use,), kind="consumer"))
        b.append(arrow([(270, y + 31), (380, 290)], style="muted"))
    b.append(text(325, 250, "HTTP + X-API-Key", size=11, colour=MUTED, anchor="middle"))

    b.append(group(385, 70, 400, 440, "air-model-gateway  (FastAPI · Python 3.12)", kind="core"))
    b.append(
        box(
            400,
            100,
            370,
            70,
            "HTTP API",
            ("POST /v1/generate · POST /v1/embed", "GET /v1/capabilities · /v1/health · /v1/ready · /metrics"),
            kind="edge",
        )
    )
    b.append(
        box(
            400,
            185,
            370,
            62,
            "Control plane",
            ("service tokens · policy grants · rate limits · daily budgets",),
            kind="edge",
        )
    )
    b.append(
        box(400, 262, 370, 62, "Model catalog", ("class or embedding name → ordered chain",), kind="core")
    )
    b.append(
        box(
            400,
            339,
            370,
            62,
            "Router",
            ("failover · retries · circuit breakers · provider quotas",),
            kind="core",
        )
    )
    b.append(
        box(
            400,
            416,
            370,
            70,
            "Bookkeeping",
            ("pricing · spend ledger · response cache", "metrics · model.call audit line"),
            kind="obs",
        )
    )

    b.append(group(880, 70, 260, 200, "Local model servers  (free, private)", kind="local"))
    b.append(box(895, 100, 230, 62, "Ollama", ("llama3.2:3b · ministral-3:3b · nomic-embed-text",), kind="local", size=13))
    b.append(box(895, 180, 230, 62, "Self-hosted slot  (llama.cpp)", ("any OpenAI-compatible server: vLLM, TGI",), kind="local"))
    b.append(group(880, 290, 260, 220, "Cloud APIs  (billed per token)", kind="cloud"))
    b.append(box(895, 320, 230, 52, "Gemini", ("gemini-2.5-flash · 3.1-pro-preview",), kind="cloud"))
    b.append(box(895, 382, 230, 52, "Anthropic", ("claude-haiku-4-5 · claude-sonnet-5",), kind="cloud"))
    b.append(box(895, 444, 230, 52, "OpenAI", ("gpt-4o-mini · text-embedding-3-small",), kind="cloud"))
    b.append(arrow([(770, 370), (880, 170)], style="muted"))
    b.append(arrow([(770, 370), (880, 400)], style="muted"))

    b.append(box(400, 545, 175, 62, "Redis  (optional)", ("shared cache, limits, spend",), kind="state"))
    b.append(box(595, 545, 175, 62, "Prometheus · logs", ("/metrics · JSON lines",), kind="obs"))
    b.append(arrow([(487, 510), (487, 545)], both=True, style="muted"))
    b.append(arrow([(682, 510), (682, 545)], style="muted"))
    b.append(
        box(
            300,
            8,
            560,
            46,
            "Owner-managed config: deploy/routing.yaml · pricing.yaml · policy/policy.<env>.yaml",
            (),
            kind="config",
            size=12,
            centre=True,
        )
    )
    b.append(arrow([(585, 54), (585, 70)], style="muted"))
    b.append(
        text(
            880,
            580,
            "No runtime dependency on any other AIR service.",
            size=12,
            weight="bold",
            colour=KINDS["core"][1],
        )
    )
    b.append(text(880, 597, "Consumers call it directly; it calls only", size=11, colour=MUTED))
    b.append(text(880, 612, "model servers, cloud APIs and its own Redis.", size=11, colour=MUTED))
    for n, (x, y) in enumerate(
        [(30, 70), (395, 100), (295, 30), (870, 80), (870, 300), (390, 545), (585, 545), (870, 575)], 1
    ):
        b.append(pin(n, x, y))
    return svg(1160, 630, b, "air-model-gateway: system context")


# ---- 2. Components ----------------------------------------------------------


def components() -> str:
    b: list[str] = []
    b.append(text(580, 22, "Request path (top to bottom)", size=12, weight="bold", colour=MUTED, anchor="middle"))
    path = [
        ("api/middleware.py", ("request id · metrics · last-resort 500",), "edge"),
        ("api/v1/generate.py · embed.py", ("thin routes; also system.py, admin.py",), "edge"),
        ("api/deps.py · require_principal", ("X-API-Key → calling service",), "edge"),
        ("model_calls.admit", ("policy grant · rate limit",), "edge"),
        ("model_calls.generate / embed", ("choose chain · cache · budget · bookkeeping",), "core"),
        ("gateway/router.py · ModelRouter", ("quota · breaker · deadline · failover · retry",), "core"),
        ("gateway/providers/*", ("ollama · llama.cpp · anthropic · openai · gemini",), "core"),
    ]
    for i, (title, lines, kind) in enumerate(path):
        y = 34 + i * 76
        b.append(box(425, y, 310, 56, title, lines, kind=kind, size=13))
        if i:
            b.append(arrow([(580, y - 20), (580, y)]))
    left = [
        (34, "main.py · create_app", ("composition root: builds everything once,", "hangs it on app.state"), "config"),
        (110, "config.py · Settings", ("env > SECRETS_DIR files > .env",), "config"),
        (186, "security/apikeys.py", ("ApiKeyVerifier: token → service",), "edge"),
        (262, "policy/engine.py · ratelimit.py", ("grants, fail-closed · token buckets",), "edge"),
        (338, "gateway/models.py · ModelCatalog", ("class · pin · embed name → chain",), "core"),
        (414, "gateway/resilience.py", ("CircuitBreaker · jittered backoff",), "core"),
    ]
    for y, title, lines, kind in left:
        b.append(box(40, y, 320, 56, title, lines, kind=kind, size=13))
    b.append(arrow([(360, 214), (425, 186 + 28 + 0)], style="muted"))
    b.append(arrow([(360, 290), (425, 262)], style="muted"))
    b.append(arrow([(360, 366), (425, 338)], style="muted"))
    b.append(arrow([(360, 442), (425, 414)], style="muted"))
    right = [
        (34, "observability/", ("metrics.py: Prometheus · logging.py: JSON",), "obs"),
        (186, "api/errors.py", ("RFC 9457 problem+json, request id",), "edge"),
        (262, "policy/budget.py · SpendLedger", ("USD per service per UTC day",), "state"),
        (338, "gateway/cache.py · ResponseCache", ("per service; keyed on chain + question",), "state"),
        (414, "gateway/cost.py · PriceBook", ("USD per million tokens (pricing.yaml)",), "obs"),
        (490, "schemas/", ("wire: generate.py, embed.py", "internal: models.py (adapters)"), "config"),
    ]
    for y, title, lines, kind in right:
        b.append(box(800, y, 320, 56 if len(lines) == 1 else 62, title, lines, kind=kind, size=13))
    b.append(arrow([(735, 62), (800, 62)], style="muted"))
    b.append(arrow([(735, 338), (800, 290)], style="muted"))
    b.append(arrow([(735, 352), (800, 366)], style="muted"))
    b.append(arrow([(735, 366), (800, 442)], style="muted"))
    b.append(
        box(
            40,
            580,
            1080,
            48,
            "redis_store.py · RedisStore  (optional)",
            ("owns timeout, breaker and error metric; limiter, cache and ledger fall back to memory on any Redis failure",),
            kind="state",
            size=13,
        )
    )
    b.append(arrow([(200, 318), (200, 580)], style="muted", dashed=True))
    b.append(arrow([(960, 394), (960, 470), (1140, 470), (1140, 604), (1120, 604)], style="muted", dashed=True))
    b.append(arrow([(960, 318), (960, 330)], style="muted", dashed=True))
    b.append(arrow([(735, 524), (780, 524), (780, 640)], style="muted"))
    b.append(text(788, 650, "→ model servers and cloud APIs", size=11, colour=MUTED))
    for n, (x, y) in enumerate([(415, 34), (30, 34), (30, 262), (415, 338), (415, 414), (790, 338), (30, 580)], 1):
        b.append(pin(n, x, y))
    return svg(1160, 660, b, "air-model-gateway: components")


# ---- 3. Request flow --------------------------------------------------------


def request_flow() -> str:
    b: list[str] = []
    row1 = [
        ("1  Request id", ("kept if valid, else new",), ""),
        ("2  Authenticate", ("X-API-Key → service",), "401 unauthorized"),
        ("3  Policy grant", ("inference : generate",), "403 policy_denied"),
        ("4  Rate limit", ("service bucket / min",), "429 rate_limited"),
        ("5  Model choice", ("grant + catalog → chain",), "403 · 422 invalid_model_choice"),
    ]
    row2 = [
        ("6  Cache", ("per service · hit = answer",), "200 cached: true"),
        ("7  Daily budget", ("paid calls only",), "429 budget_exceeded"),
        ("8  Router", ("walk chain, failover",), "502 · 429 if all throttled"),
        ("9  Bookkeeping", ("price · spend · cache · audit",), ""),
        ("200  Answer", ("JSON, or SSE stream",), ""),
    ]
    w, gap = 190, 32
    for r, (row, y) in enumerate(((row1, 40), (row2, 290))):
        for i, (title, lines, err) in enumerate(row):
            x = 25 + i * (w + gap)
            kind = "core" if title.startswith("200") else ("edge" if r == 0 else "core")
            b.append(box(x, y, w, 72, title, lines, kind=kind, size=14))
            if i:
                b.append(arrow([(x - gap, y + 36), (x, y + 36)]))
            if err:
                kind_err = "obs" if err.startswith("200") else "error"
                b.append(arrow([(x + w / 2, y + 72), (x + w / 2, y + 112)], style="error" if kind_err == "error" else "muted"))
                b.append(box(x + 8, y + 112, w - 16, 34, err, (), kind=kind_err, size=11, centre=True))
    last_x = 25 + 4 * (w + gap) + w / 2
    b.append(arrow([(last_x, 112), (last_x, 200), (25 + w / 2, 200), (25 + w / 2, 290)]))
    b.append(text(580, 193, "then", size=11, colour=MUTED, anchor="middle"))
    b.append(
        text(
            25,
            480,
            "POST /v1/embed takes the same path with action embed, an embedding name instead of a class, and no cache step.",
            size=12,
            colour=MUTED,
        )
    )
    b.append(
        text(
            25,
            500,
            "Every error is application/problem+json with a stable code and the request id. Retry only 429 rate_limited, 502, 503.",
            size=12,
            colour=MUTED,
        )
    )
    return svg(1160, 515, b, "air-model-gateway: request flow")


# ---- 4. Model choice --------------------------------------------------------


def model_choice() -> str:
    b: list[str] = []
    b.append(group(20, 20, 290, 300, "What a caller sends", kind="consumer"))
    b.append(box(35, 50, 260, 120, "POST /v1/generate", (
        "model: local | auto | cloud",
        "    (default auto)",
        "provider: optional preference",
        "pin: provider:model (granted only)",
    ), kind="consumer", size=13))
    b.append(box(35, 190, 260, 110, "POST /v1/embed", (
        "model: a published name, required",
        "<local|cloud>-<dims>-v<version>",
        "one model, never a chain",
    ), kind="consumer", size=13))
    b.append(box(345, 80, 200, 92, "Policy grant", ("model_classes · preferred_providers", "pin · embed_models", "outside it → 403"), kind="edge", size=13))
    b.append(box(345, 200, 200, 92, "ModelCatalog", ("deploy/routing.yaml", "classes · overrides · embed", "meaningless → 422"), kind="core", size=13))
    b.append(arrow([(295, 110), (345, 126)]))
    b.append(arrow([(295, 245), (345, 246)]))
    b.append(arrow([(445, 172), (445, 200)]))
    b.append(arrow([(545, 246), (590, 246)]))

    x0 = 600
    b.append(text(x0, 38, "Default chains (tried left to right)", size=13, weight="bold"))
    rows = [
        ("local", [("ollama llama3.2:3b", "local"), ("ollama ministral-3:3b", "local"), ("llama.cpp 3.2-3b-gguf", "local")]),
        ("auto", [("ollama llama3.2:3b", "local"), ("gemini-2.5-flash", "cloud"), ("claude-haiku-4-5", "cloud"), ("gpt-4o-mini", "cloud")]),
        ("cloud", [("gemini-2.5-flash", "cloud"), ("claude-haiku-4-5", "cloud"), ("gpt-4o-mini", "cloud")]),
    ]
    for i, (name, rungs) in enumerate(rows):
        y = 54 + i * 36
        b.append(text(x0, y + 16, name, size=13, weight="bold"))
        b.extend(chain(x0 + 48, y, rungs))
    b.append(text(x0, 186, "Per-service overrides (only the classes they name)", size=13, weight="bold"))
    overrides = [
        ("classifier local", [("ollama llama3.2:3b", "local")]),
        ("classifier auto", [("gemini-2.5-flash", "cloud"), ("claude-haiku-4-5", "cloud"), ("gpt-4o-mini", "cloud")]),
        ("classifier cloud", [("gemini-3.1-pro-preview", "cloud"), ("claude-sonnet-5", "cloud"), ("gpt-4o-mini", "cloud")]),
        ("orchestrator cloud", [("gemini-2.5-flash", "cloud"), ("claude-sonnet-5", "cloud"), ("gpt-4o-mini", "cloud")]),
    ]
    for i, (name, rungs) in enumerate(overrides):
        y = 200 + i * 34
        b.append(text(x0, y + 16, name, size=11.5, colour=MUTED))
        b.extend(chain(x0 + 120, y, rungs))
    b.append(text(x0, 362, "Embedding models (permanent names)", size=13, weight="bold"))
    b.append(text(x0, 390, "local-768-v1", size=12, weight="bold"))
    b.extend(chain(x0 + 110, 374, [("ollama nomic-embed-text · 768", "local")]))
    b.append(text(x0, 424, "cloud-1536-v1", size=12, weight="bold"))
    b.extend(chain(x0 + 110, 408, [("openai text-embedding-3-small · 1536", "cloud")]))

    b.append(group(20, 345, 525, 120, "Provider preference moves rungs, never removes them", kind="core"))
    b.append(text(35, 385, "cloud + provider: anthropic", size=12, weight="bold"))
    b.extend(chain(35, 398, [("claude-haiku-4-5", "cloud"), ("gemini-2.5-flash", "cloud"), ("gpt-4o-mini", "cloud")]))
    b.append(text(35, 450, "local + provider: anthropic → 422 (the class doesn't offer it)", size=11.5, colour=MUTED))
    b.append(text(20, 492, "Rules enforced when routing.yaml loads: a local chain holds only local providers, a cloud chain only cloud ones;", size=11.5, colour=MUTED))
    b.append(text(20, 508, "an embedding name states its location and dimensions, and the gateway checks every returned vector against them.", size=11.5, colour=MUTED))
    for n, (x, y) in enumerate([(30, 50), (340, 80), (340, 200), (590, 54), (590, 200), (590, 374), (30, 365)], 1):
        b.append(pin(n, x, y))
    return svg(1160, 520, b, "air-model-gateway: model choice")


# ---- 5. Resilience ----------------------------------------------------------


def resilience() -> str:
    b: list[str] = []
    b.append(text(20, 24, "Walking a chain (gateway/router.py)", size=14, weight="bold"))
    b.append(box(20, 40, 170, 50, "Next rung", ("(provider, model)",), kind="core", centre=True))
    b.append(box(230, 40, 170, 50, "Breaker open?", (), kind="plain", centre=True))
    b.append(box(440, 40, 170, 50, "Quota spent?", ("provider_quotas",), kind="plain", centre=True))
    b.append(box(650, 40, 200, 50, "Call with deadline", ("60 s per attempt",), kind="core", centre=True))
    b.append(box(890, 40, 150, 50, "Served", ("bookkeeping",), kind="local", centre=True))
    b.append(arrow([(190, 65), (230, 65)]))
    b.append(arrow([(400, 65), (440, 65)], "no", label_at=(420, 59)))
    b.append(arrow([(610, 65), (650, 65)], "no", label_at=(630, 59)))
    b.append(arrow([(850, 65), (890, 65)], "ok", label_at=(870, 59)))
    b.append(arrow([(315, 90), (315, 130), (105, 130), (105, 90)], "yes: skip (circuit_skips)", style="muted", label_at=(210, 146)))
    b.append(arrow([(525, 90), (525, 130), (315, 130)], "yes: skip", style="muted", label_at=(440, 146)))
    b.append(arrow([(750, 90), (750, 180), (105, 180), (105, 90)], "error or timeout: next rung", style="error", label_at=(430, 196)))
    b.append(box(20, 220, 330, 78, "Every rung failed", ("any failure retryable (timeout, 429, 5xx, dropped)?",), kind="error", size=13))
    b.append(arrow([(105, 180), (105, 220)], style="error"))
    b.append(box(400, 220, 300, 78, "Retry the whole chain", ("once (retry_attempts 1), after jittered", "backoff from 0.5 s"), kind="core", size=13))
    b.append(box(750, 220, 290, 78, "Answer the caller", ("429 if every rung was throttled", "otherwise 502 naming each rung"), kind="error", size=13))
    b.append(arrow([(350, 250), (400, 250)], "yes", label_at=(375, 244)))
    b.append(arrow([(350, 280), (370, 280), (370, 320), (895, 320), (895, 298)], "no, or retries used", style="error", label_at=(630, 336)))
    b.append(arrow([(700, 259), (720, 259), (720, 120), (20, 120), (20, 90)], style="muted", dashed=True))
    b.append(text(20, 380, "Circuit breaker per (provider, model)  (gateway/resilience.py)", size=14, weight="bold"))
    b.append(box(20, 400, 190, 66, "CLOSED", ("calls flow",), kind="local", centre=True, size=15))
    b.append(box(420, 400, 190, 66, "OPEN", ("rung skipped, no call",), kind="error", centre=True, size=15))
    b.append(box(820, 400, 220, 66, "HALF-OPEN", ("one trial call, others skip",), kind="obs", centre=True, size=15))
    b.append(arrow([(210, 420), (420, 420)], "5 transient failures in a row", label_at=(315, 412)))
    b.append(arrow([(610, 420), (820, 420)], "after 30 s", label_at=(715, 412)))
    b.append(arrow([(820, 450), (610, 450)], "trial fails", style="error", label_at=(715, 466)))
    b.append(arrow([(930, 466), (930, 510), (115, 510), (115, 466)], "trial succeeds", label_at=(520, 504)))
    b.append(text(20, 545, "A 429 means the provider is up: it fails the rung but never counts toward the breaker. Provider SDKs run with", size=11.5, colour=MUTED))
    b.append(text(20, 561, "max_retries=0, so the router is the only retry layer. Settings: gateway.request_timeout_seconds, retry_*, breaker_*.", size=11.5, colour=MUTED))
    for n, (x, y) in enumerate([(225, 40), (435, 40), (645, 40), (15, 220), (395, 220), (15, 400)], 1):
        b.append(pin(n, x, y))
    return svg(1060, 575, b, "air-model-gateway: resilience")


# ---- 6. Streaming -----------------------------------------------------------


def streaming() -> str:
    b: list[str] = []
    lanes = [("Consumer", 70), ("Gateway", 190), ("Provider", 310)]
    for name, y in lanes:
        b.append(text(20, y + 5, name, size=13, weight="bold"))
        b.append(f'<line x1="110" y1="{y}" x2="1140" y2="{y}" stroke="#D5D9E0" stroke-width="1.2"/>')
    b.append(
        f'<rect x="120" y="30" width="420" height="320" rx="8" fill="{KINDS["edge"][0]}" opacity="0.55"/>'
    )
    b.append(
        f'<rect x="560" y="30" width="580" height="320" rx="8" fill="{KINDS["core"][0]}" opacity="0.6"/>'
    )
    b.append(text(130, 48, "Before the first token: plain HTTP", size=12, weight="bold", colour=KINDS["edge"][1]))
    b.append(text(570, 48, "After it: Server-Sent Events, status already 200", size=12, weight="bold", colour=KINDS["core"][1]))
    b.append(arrow([(140, 70), (140, 190)], "POST stream: true", label_at=(150, 130), label_anchor="start"))
    b.append(arrow([(250, 190), (250, 310)], "rung 1", label_at=(258, 250), label_anchor="start"))
    b.append(arrow([(300, 310), (300, 190)], "fails", style="error", label_at=(308, 250), label_anchor="start"))
    b.append(arrow([(380, 190), (380, 310)], "rung 2", label_at=(388, 250), label_anchor="start"))
    b.append(arrow([(470, 310), (470, 190)], "first token", label_at=(478, 250), label_anchor="start"))
    b.append(text(130, 105, "errors here are ordinary 4xx/5xx;", size=11, colour=MUTED))
    b.append(text(130, 120, "the gateway may still fail over", size=11, colour=MUTED))
    events = [(600, "start"), (700, "delta"), (780, "delta"), (860, "delta"), (1000, "done")]
    for x, name in events:
        b.append(arrow([(x, 190), (x, 70)], name, label_at=(x + 6, 120), label_anchor="start"))
    for x in (700, 780, 860):
        b.append(arrow([(x - 30, 310), (x - 30, 190)], style="muted"))
    b.append(text(1008, 140, "usage, cost,", size=11, colour=MUTED))
    b.append(text(1008, 154, "finish_reason", size=11, colour=MUTED))
    b.append(box(570, 368, 270, 66, "or: error event", ("provider failed mid-stream, or", "no token for 30 s (idle timeout)"), kind="error", size=12))
    b.append(box(860, 368, 280, 66, "caller disconnects", ("provider stream closed; audited as", "client_disconnected"), kind="obs", size=12))
    b.append(box(120, 368, 420, 66, "bookkeeping runs before done", ("pricing, spend, cache store, metrics, audit: the same", "as a JSON call. No failover once a token has been sent."), kind="config", size=12))
    for n, (x, y) in enumerate([(125, 30), (565, 30), (995, 70), (565, 368), (855, 368)], 1):
        b.append(pin(n, x, y))
    return svg(1160, 450, b, "air-model-gateway: streaming")


# ---- 7. Governance ----------------------------------------------------------


def governance() -> str:
    b: list[str] = []
    b.append(group(20, 20, 380, 470, "deploy/policy/policy.<env>.yaml", kind="edge"))
    yaml = [
        "default_allow: false      # forced off outside local",
        "default_rate_per_minute: 60",
        "provider_quotas:          # per provider, all services",
        "  gemini: 600             # calls per minute",
        "services:",
        "  air-rag:",
        "    resources:",
        "      inference: [embed]",
        "    embed_models: [local-768-v1]",
        "    rate_per_minute: 600",
        "    daily_budget_usd: 25.0",
        "  air-web-console:",
        "    resources:",
        "      inference: [generate]",
        "      admin: [admin_read]",
        "    pin: true",
    ]
    for i, line in enumerate(yaml):
        b.append(
            f'<text x="36" y="{58 + i * 19}" font-size="12" font-family="Menlo, Consolas, monospace" '
            f'fill="{INK}">{escape(line)}</text>'
        )
    b.append(text(36, 380, "Unlisted fields mean \"all\": model_classes,", size=11.5, colour=MUTED))
    b.append(text(36, 396, "embed_models and preferred_providers.", size=11.5, colour=MUTED))
    b.append(text(36, 420, "Typed at load: an unknown action or class stops", size=11.5, colour=MUTED))
    b.append(text(36, 436, "startup. Quota and budget values are examples;", size=11.5, colour=MUTED))
    b.append(text(36, 452, "the shipped files set none yet.", size=11.5, colour=MUTED))

    cols = ["generate", "embed", "admin_read", "classes", "embed models", "pin", "rate / min"]
    widths = [80, 70, 90, 120, 115, 50, 80]
    x0, y0 = 430, 40
    names = ["air-classifier-service", "air-orchestrator-service", "air-tools", "air-rag", "air-web-console"]
    rows = [
        ["yes", "yes", "", "all", "all", "", "300"],
        ["yes", "yes", "", "all", "all", "", "600"],
        ["yes", "", "", "all", "", "", "300"],
        ["", "yes", "", "", "local-768-v1", "", "600"],
        ["yes", "", "yes", "all", "", "yes", "60"],
    ]
    b.append(text(x0, 28, "Production grants (policy.prod.yaml; staging is the same)", size=13, weight="bold"))
    header_x = x0 + 190
    b.append(f'<rect x="{x0}" y="{y0}" width="{190 + sum(widths)}" height="30" fill="{KINDS["edge"][0]}"/>')
    cx = header_x
    for col, w in zip(cols, widths, strict=True):
        b.append(text(cx + w / 2, y0 + 20, col, size=11.5, weight="bold", anchor="middle"))
        cx += w
    for r, (name, row) in enumerate(zip(names, rows, strict=True)):
        y = y0 + 30 + r * 40
        fill = "#FFFFFF" if r % 2 == 0 else "#F7F8FA"
        b.append(f'<rect x="{x0}" y="{y}" width="{190 + sum(widths)}" height="40" fill="{fill}" stroke="#E3E6EB"/>')
        b.append(text(x0 + 10, y + 25, name, size=12, weight="bold", colour=KINDS["consumer"][1]))
        cx = header_x
        for value, w in zip(row, widths, strict=True):
            mark = "✓" if value == "yes" else ("—" if value == "" else value)
            colour = KINDS["local"][1] if value == "yes" else (MUTED if value == "" else INK)
            b.append(text(cx + w / 2, y + 25, mark, size=12, colour=colour, anchor="middle"))
            cx += w
    y = y0 + 30 + 5 * 40 + 30
    notes = [
        ("Fails closed", "Outside local, a service not listed gets nothing (403)."),
        ("Rate limit", "Token bucket per service, shared across replicas through Redis."),
        ("Provider quota", "Shared by all services; a spent quota skips that rung."),
        ("Daily budget", "USD per UTC day; then 429 budget_exceeded + Retry-After."),
        ("Tokens", "X-API-Key per service, from Secret Manager; rotation keeps old + new."),
    ]
    for i, (k, v) in enumerate(notes):
        b.append(text(x0, y + i * 24, k, size=12, weight="bold"))
        b.append(text(x0 + 120, y + i * 24, v, size=12, colour=MUTED))
    for n, (x, y2) in enumerate([(30, 40), (30, 120), (425, 40), (425, 300)], 1):
        b.append(pin(n, x, y2))
    return svg(1160, 500, b, "air-model-gateway: governance")


# ---- 8. GKE deployment ------------------------------------------------------


def gke() -> str:
    b: list[str] = []
    b.append(group(15, 15, 1130, 600, "GCP project  (one per environment: staging, prod)", kind="infra"))
    b.append(group(35, 50, 800, 420, "GKE Autopilot cluster  (private nodes)", kind="infra"))
    b.append(group(55, 85, 230, 365, "Consumer namespaces", kind="consumer"))
    for i, ns in enumerate(["air-classifier-service", "air-orchestrator-service", "air-tools", "air-rag", "air-web-console"]):
        b.append(box(70, 115 + i * 64, 200, 48, f"ns {ns}", (), kind="consumer", size=11.5, centre=True))
    b.append(group(320, 85, 495, 260, "ns air-model-gateway  (Helm chart)", kind="core"))
    b.append(box(340, 115, 150, 54, "Service", ("ClusterIP :80, no Ingress",), kind="edge", size=13))
    b.append(box(340, 185, 150, 54, "NetworkPolicy", ("consumer ns only",), kind="edge", size=13))
    b.append(box(510, 115, 285, 124, "Deployment", (
        "prod 3–10 pods (HPA) · staging 2–4",
        "PDB · spread over zones and nodes",
        "non-root · read-only root FS",
        "drains in-flight calls on SIGTERM",
        "Workload Identity service account",
    ), kind="core", size=13))
    b.append(box(340, 255, 150, 74, "ConfigMap", ("routing · pricing", "policy.<env>.yaml"), kind="config", size=13))
    b.append(box(510, 255, 140, 74, "SecretProvider", ("Class (CSI) →", "SECRETS_DIR"), kind="state", size=13))
    b.append(box(665, 255, 130, 74, "PodMonitoring", ("scrapes", "/metrics"), kind="obs", size=13))
    b.append(arrow([(270, 240), (340, 142)]))
    b.append(text(300, 182, "X-API-Key", size=10.5, colour=MUTED, anchor="middle"))
    b.append(arrow([(490, 142), (510, 142)]))
    b.append(group(320, 365, 495, 90, "ns air-model-servers  (owned separately)", kind="local"))
    b.append(box(340, 393, 220, 50, "Ollama", ("chat + embeddings",), kind="local", size=13))
    b.append(box(575, 393, 220, 50, "llama.cpp / vLLM", ("OpenAI-compatible slot",), kind="local", size=13))
    b.append(arrow([(652, 239), (652, 255)], style="muted"))
    b.append(arrow([(760, 239), (760, 393)], style="muted"))
    b.append(text(770, 360, "URLs in values", size=10.5, colour=MUTED))
    b.append(box(870, 60, 255, 62, "Secret Manager", ("tokens · provider keys · Redis URL + CA",), kind="state", size=13))
    b.append(box(870, 140, 255, 62, "Memorystore Redis", ("AUTH + TLS, private IP",), kind="state", size=13))
    b.append(box(870, 220, 255, 62, "Cloud NAT", ("egress to cloud model APIs",), kind="infra", size=13))
    b.append(box(870, 300, 255, 62, "Cloud Logging", ("JSON lines with severity",), kind="obs", size=13))
    b.append(box(870, 380, 255, 62, "Managed Prometheus", ("alerts on breakers, Redis, spend",), kind="obs", size=13))
    b.append(box(870, 460, 255, 62, "Artifact Registry", ("one image digest, promoted",), kind="infra", size=13))
    b.append(arrow([(870, 91), (650, 291)], style="muted", dashed=True))
    b.append(arrow([(795, 175), (870, 171)], style="muted"))
    b.append(arrow([(795, 200), (870, 251)], style="muted"))
    b.append(box(35, 490, 255, 62, "Gemini · Anthropic · OpenAI", ("public APIs, via Cloud NAT",), kind="cloud", size=13))
    b.append(arrow([(1000, 282), (1000, 300)], style="muted", dashed=True))
    b.append(text(320, 500, "Terraform (deploy/terraform) creates: Memorystore, the secrets and their IAM, Workload Identity bindings;", size=11.5, colour=MUTED))
    b.append(text(320, 517, "optionally the VPC, Cloud NAT, Autopilot cluster and Artifact Registry (gke-platform module).", size=11.5, colour=MUTED))
    b.append(text(320, 534, "Helm (deploy/helm + deploy/gke/values-<env>.yaml) creates everything inside ns air-model-gateway.", size=11.5, colour=MUTED))
    b.append(text(320, 560, "Consumers call http://air-model-gateway.air-model-gateway.svc.cluster.local", size=12, weight="bold"))
    for n, (x, y) in enumerate([(50, 85), (335, 115), (505, 115), (335, 255), (505, 255), (315, 365), (865, 60), (865, 140)], 1):
        b.append(pin(n, x, y))
    return svg(1160, 630, b, "air-model-gateway: GKE deployment")


# ---- 9. Delivery ------------------------------------------------------------


def delivery() -> str:
    b: list[str] = []
    b.append(text(20, 24, "Every push and pull request: .github/workflows/ci.yml (= make verify)", size=14, weight="bold"))
    jobs = [
        ("check", ("make check: ruff, mypy strict, tests ≥ 90%", "lock-check · OpenAPI drift")),
        ("supply-chain", ("pip-audit · shellcheck · hadolint", "docker build")),
        ("deploy-config", ("helm lint · kubeconform", "terraform validate (make gke-validate)")),
    ]
    for i, (name, lines) in enumerate(jobs):
        b.append(box(20 + i * 370, 40, 345, 66, name, lines, kind="core", size=13))
    b.append(text(20, 150, "Tag v* (or manual): .github/workflows/deploy.yml", size=14, weight="bold"))
    steps = [
        ("Build once", ("push image to staging", "Artifact Registry"), "infra"),
        ("Staging", ("helm upgrade (gke-deploy)", "then gke-smoke"), "core"),
        ("Approval", ("prod environment needs", "a reviewer"), "obs"),
        ("Promote", ("copy the same digest", "into prod's registry"), "infra"),
        ("Prod", ("helm upgrade", "then gke-smoke"), "core"),
    ]
    for i, (name, lines, kind) in enumerate(steps):
        x = 20 + i * 222
        b.append(box(x, 166, 196, 66, name, lines, kind=kind, size=13))
        if i:
            b.append(arrow([(x - 26, 199), (x, 199)]))
    b.append(text(20, 260, "CI authenticates with Workload Identity Federation: no keys are stored in GitHub.", size=11.5, colour=MUTED))
    b.append(text(20, 300, "Once per environment (DevOps)", size=14, weight="bold"))
    once = [
        ("terraform apply", ("deploy/terraform/environments/<env>", "modules: air-model-gateway, gke-platform"), "infra"),
        ("bootstrap-secrets.sh", ("issue consumer tokens, load provider", "keys; --rotate / --retire-old"), "state"),
        ("values-<env>.yaml", ("fill placeholders: project, model-server", "URLs, consumer namespaces"), "config"),
        ("Each consumer", ("mounts its token, sets the gateway URL", "(03-deployment.md §4.4)"), "consumer"),
    ]
    for i, (name, lines, kind) in enumerate(once):
        x = 20 + i * 278
        b.append(box(x, 316, 256, 66, name, lines, kind=kind, size=13))
        if i:
            b.append(arrow([(x - 22, 349), (x, 349)]))
    b.append(text(20, 420, "Config change (routing, pricing, policy): edit the file, merge, deploy. The ConfigMap changes, the pods roll; no new image.", size=12, colour=INK))
    for n, (x, y) in enumerate([(15, 40), (15, 166), (459, 166), (15, 316), (293, 316)], 1):
        b.append(pin(n, x, y))
    return svg(1130, 440, b, "air-model-gateway: delivery")


# ---- 10. Environments and gates --------------------------------------------


def environments() -> str:
    b: list[str] = []
    cols = [
        ("Laptop dev", "make run · make up", "local", [
            "ENVIRONMENT=local",
            "policy.local.yaml (default allow)",
            "Redis: none, or REDIS=infra",
            "native Ollama (Metal GPU)",
            "port 8083",
        ], "make verify"),
        ("Integration stack", "make integration-up", "core", [
            "ENVIRONMENT=staging",
            "policy.staging.yaml, fails closed",
            "2 replicas behind nginx :9083",
            "shared Redis, generated tokens",
            "for consumers on the laptop",
        ], "make integration-test"),
        ("kind cluster", "make kind-up", "infra", [
            "the Helm chart, prod values",
            "policy.prod.yaml",
            "TLS + AUTH Redis",
            "NetworkPolicy enforced",
            "two workers in two zones",
        ], "make kind-test"),
        ("GKE staging · prod", "Deploy workflow", "cloud", [
            "ENVIRONMENT=staging | prod",
            "policy.<env>.yaml",
            "Memorystore, Secret Manager",
            "in-cluster only",
            "same image digest in both",
        ], "make gke-smoke"),
    ]
    for i, (name, start, kind, lines, gate) in enumerate(cols):
        x = 20 + i * 282
        fill, stroke = KINDS[kind]
        b.append(f'<rect x="{x}" y="20" width="262" height="300" rx="9" fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>')
        b.append(text(x + 14, 46, name, size=15, weight="bold"))
        b.append(
            f'<text x="{x + 14}" y="68" font-size="12" font-family="Menlo, Consolas, monospace" '
            f'fill="{stroke}">{escape(start)}</text>'
        )
        for j, line in enumerate(lines):
            b.append(text(x + 14, 100 + j * 24, "· " + line, size=12, colour=INK))
        b.append(f'<rect x="{x + 14}" y="250" width="234" height="50" rx="7" fill="#FFFFFF" stroke="{stroke}"/>')
        b.append(text(x + 24, 270, "Gate", size=10.5, colour=MUTED))
        b.append(
            f'<text x="{x + 24}" y="289" font-size="12.5" font-weight="bold" '
            f'font-family="Menlo, Consolas, monospace" fill="{INK}">{escape(gate)}</text>'
        )
        if i:
            b.append(arrow([(x - 18, 170), (x - 2, 170)], style="muted"))
    b.append(text(20, 350, "Definition of done: make verify (what CI runs). A change to runtime behaviour, deployment or a consumer-visible contract", size=12))
    b.append(text(20, 368, "also passes make kind-up kind-test and make integration-up integration-test.", size=12))
    return svg(1150, 380, b, "air-model-gateway: environments and gates")


DIAGRAMS = {
    "01-context": context,
    "02-components": components,
    "03-request-flow": request_flow,
    "04-model-choice": model_choice,
    "05-resilience": resilience,
    "06-streaming": streaming,
    "07-governance": governance,
    "08-gke": gke,
    "09-delivery": delivery,
    "10-environments": environments,
}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, draw in DIAGRAMS.items():
        (OUT / f"{name}.svg").write_text(draw() + "\n")


if __name__ == "__main__":
    main()
