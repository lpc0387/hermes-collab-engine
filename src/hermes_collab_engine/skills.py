"""Skill registry and worker prompt selection.

Skills are short markdown instruction blocks the leader can attach to workers
based on WBS node capability and task wording.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class SkillEntry:
    name: str
    display_name: str
    category: str
    description: str
    content: str
    applicable_node_types: list[str]
    priority: int
    source: str
    required_tools: list[str] = field(default_factory=list)  # tool profiles needed by this skill (used by SkillDistributor)

    def to_dict(self) -> dict:
        return asdict(self)


class SkillRegistry:
    """In-memory registry for built-in and custom worker skills."""

    def __init__(self):
        self._skills: dict[str, SkillEntry] = {}
        self._load_builtin_skills()

    def refresh(self) -> None:
        """Reload builtin skills (no-op stub)."""
        self._load_builtin_skills()

    def register(self, skill: SkillEntry) -> None:
        if not skill.name:
            raise ValueError("skill name is required")
        self._skills[skill.name] = skill

    def get(self, name: str) -> SkillEntry | None:
        return self._skills.get(name)

    def list_all(self) -> list[SkillEntry]:
        return sorted(self._skills.values(), key=lambda s: (s.priority, s.name))

    def select_for_node(
        self,
        node_type: str,
        task_text: str = "",
        *,
        max_skills: int = 3,
    ) -> list[SkillEntry]:
        """Select relevant skills for a node capability and task text."""
        normalized_type = (node_type or "").strip().lower()
        text = (task_text or "").lower()
        candidates = [
            skill for skill in self._skills.values()
            if normalized_type in [item.lower() for item in skill.applicable_node_types]
            or "*" in skill.applicable_node_types
        ]
        scored = [(self._score(skill, normalized_type, text), skill) for skill in candidates]
        selected = [skill for score, skill in scored if score > 0]
        selected.sort(key=lambda s: (s.priority, s.name))
        return selected[:max(0, max_skills)]

    def render_for_prompt(self, skills: list[SkillEntry]) -> str:
        if not skills:
            return ""
        parts = ["Relevant skills injected by Hermes:"]
        for skill in skills:
            parts.append(f"\n### {skill.display_name} ({skill.name})\n{skill.content.strip()}")
        return "\n".join(parts) + "\n\n"

    def _score(self, skill: SkillEntry, node_type: str, text: str) -> int:
        score = 4 - max(1, min(3, skill.priority))
        haystack = f"{skill.name} {skill.display_name} {skill.category} {skill.description}".lower()
        for word in _TASK_KEYWORDS.get(skill.name, ()):  # skill-specific task hints
            if word in text:
                score += 2
        if skill.category in text or any(token in text for token in haystack.split() if len(token) > 5):
            score += 1
        if node_type in [item.lower() for item in skill.applicable_node_types]:
            score += 1
        return score

    def _load_builtin_skills(self) -> None:
        for skill in _BUILTIN_SKILLS:
            self.register(skill)


_TASK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "implementation-focus": ("implement", "modify", "write", "code", "working implementation"),
    "test-verify": ("test", "verify", "unittest", "pytest", "regression"),
    "search-verify": ("find", "search", "scope", "evidence", "read-only", "locate"),
    "debug-root-cause": ("bug", "debug", "failure", "traceback", "error", "fix"),
    "risk-checkpoint": ("risk", "checkpoint", "security", "permission", "destructive"),
}


_BUILTIN_SKILLS = [
    SkillEntry(
        name="ui-design-v2",
        display_name="UI Design v2 — shadcn/ui",
        category="design",
        description="Advanced UI design skill using shadcn/ui v4 components with Linear/Stripe/Vercel aesthetic.",
        content=(
            "# UI Design v2 — shadcn/ui\n\n"
            "Build beautiful, modern interfaces using shadcn/ui v4 components with a premium aesthetic "
            "(Linear / Stripe / Vercel / shadcn style).\n\n"
            "## Available MCP Tools\n"
            "The `shadcn-ui` MCP server exposes these tools:\n"
            "- `list_components` — browse all available shadcn/ui components\n"
            "- `get_component` — fetch source code, props, and usage for a specific component\n"
            "- `list_blocks` — explore full block implementations (dashboards, forms, pricing, etc.)\n"
            "- `get_block` — get a complete block with all its component code\n\n"
            "## Design Principles\n"
            "- **Minimal & refined**: generous whitespace, subtle borders, restrained use of color\n"
            "- **Functional hierarchy**: clear visual weight through typography size/weight, not decorations\n"
            "- **Subtle interactions**: hover states, focus rings, smooth transitions (`transition-all duration-200`)\n"
            "- **Monochromatic base**: use grays for structure, one accent color for interactive elements\n"
            "- **Consistent rhythm**: 4px spacing scale (Tailwind: `p-1` through `p-16`)\n\n"
            "## Component Usage Guide\n"
            "- Button variants: `default`, `secondary`, `destructive`, `outline`, `ghost`, `link`\n"
            "- Card: use `Card`, `CardHeader`, `CardTitle`, `CardDescription`, `CardContent`, `CardFooter`\n"
            "- Forms: combine `Form`, `Input`, `Label`, `Select`, `Checkbox`, `Textarea` with react-hook-form\n"
            "- Dialogs: `Dialog` + `DialogTrigger` + `DialogContent` + `DialogHeader` + `DialogTitle` + `DialogDescription`\n"
            "- Navigation: `Tabs`, `DropdownMenu`, `Command` (cmdk) for command palettes\n"
            "- Data display: `Table`, `DataTable` (TanStack Table), `Badge`, `Avatar`, `Separator`\n"
            "- Feedback: `Toast`, `Alert`, `Skeleton`, `Progress`, `Spinner` (via `lucide-react` `Loader2`)\n\n"
            "## Layout Patterns\n"
            "- Max-width containers: `max-w-7xl mx-auto px-4 sm:px-6 lg:px-8`\n"
            "- Responsive grid: `grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6`\n"
            "- Center sections: `flex items-center justify-center min-h-[calc(100vh-4rem)]`\n"
            "- Sidebar + content: `flex` with `w-64` sidebar and `flex-1` main area\n\n"
            "## Typography\n"
            "- Headings: `text-4xl font-bold tracking-tight`, `text-3xl font-semibold`, `text-2xl font-medium`\n"
            "- Body: `text-base leading-7 text-muted-foreground`\n"
            "- Small/caption: `text-sm text-muted-foreground`\n"
            "- Code/monospace: `font-mono text-sm`\n\n"
            "## Colors & Theming (shadcn/ui CSS variables)\n"
            "- `--background` / `--foreground` — page background and default text\n"
            "- `--card` / `--card-foreground` — card surfaces\n"
            "- `--primary` / `--primary-foreground` — main accent (usually indigo/blue)\n"
            "- `--secondary` / `--secondary-foreground` — secondary accent\n"
            "- `--muted` / `--muted-foreground` — subdued surfaces and text\n"
            "- `--accent` / `--accent-foreground` — hover/selection states\n"
            "- `--destructive` / `--destructive-foreground` — errors and deletions\n"
            "- `--border` / `--input` / `--ring` — borders and focus rings\n"
            "- Use Tailwind classes: `bg-background`, `text-foreground`, `text-muted-foreground`, `border-border`, etc.\n\n"
            "## Code Generation Pattern\n"
            "1. Use `list_components` or `search_components` to find the right component\n"
            "2. Use `get_component` to fetch exact source code and props\n"
            "3. Combine components into pages using the layout patterns above\n"
            "4. Always include: imports, TypeScript types, Tailwind classes, responsive variants\n"
            "5. For complete sections, use `list_blocks` / `get_block` to get full implementations\n\n"
            "## Accessibility\n"
            "- shadcn/ui uses Radix UI primitives — accessibility is built-in\n"
            "- Always add `aria-label` on icon-only buttons\n"
            "- Use semantic HTML: `<nav>`, `<main>`, `<section>`, `<footer>`\n"
            "- Maintain keyboard navigation for all interactive elements\n"
            "- Respect `prefers-reduced-motion` for animations"
        ),
        applicable_node_types=["implementation", "design", "design-v2", "frontend", "ui", "coding"],
        priority=1,
        source="hermes",
        required_tools=["file-edit", "mcp-readonly"],
    ),
    SkillEntry(
        name="implementation-focus",
        display_name="Focused Implementation",
        category="coding",
        description="Keep implementation shards concrete, minimal, and file-level.",
        content=(
            "- Make the smallest useful code change that satisfies this node.\n"
            "- Match surrounding naming, comments, and style.\n"
            "- Report exact files modified and avoid claiming unrun verification."
        ),
        applicable_node_types=["implementation", "coding", "docs", "general"],
        priority=1,
        source="hermes",
    ),
    SkillEntry(
        name="test-verify",
        display_name="Test & Verification",
        category="verification",
        description="Run targeted checks and report failures honestly.",
        content=(
            "- Prefer the narrowest regression test that proves this node.\n"
            "- If a command fails, include the failure reason in verification.\n"
            "- Do not mark partial work as complete when tests are failing."
        ),
        applicable_node_types=["implementation", "verification", "debugging"],
        priority=1,
        source="hermes",
    ),
    SkillEntry(
        name="search-verify",
        display_name="Multi-Source Search & Verification",
        category="research",
        description="Multi-source search verification — search, fact-check, and cross-validate across multiple engines and platforms.",
        content=(
            "# Multi-Source Search & Verification\n\n"
            "When searching for information and verifying results, use multiple sources in parallel:\n\n"
            "## Available MCP Search Tools\n"
            "- `ferris-search`: Multi-engine search (`web_search`) with fetch tools for CSDN, Zhihu, Juejin, Linux.do, GitHub\n"
            "- `baidu-search`: Baidu search (`baidu_search`)\n"
            "- `open-websearch`: General web search\n\n"
            "## Search Strategy\n"
            "1. Call MCP tools directly — do not pre-check availability, just call and handle errors\n"
            "2. Parallel multi-source searches on the same query\n"
            "3. Fetch full content from important links using platform-specific fetch tools\n"
            "4. Cross-validate: multiple sources agreeing = high confidence\n"
            "5. Fallback: curl Bing/Baidu if all MCP tools fail\n\n"
            "## Output Format\n"
            "Produce a verification report: query, sources used, core findings with confidence levels, detailed source list."
        ),
        applicable_node_types=["analysis", "research", "planning", "scope", "evidence"],
        priority=1,
        source="hermes",
    ),
    SkillEntry(
        name="debug-root-cause",
        display_name="Debug Root Cause",
        category="debugging",
        description="Trace failures to a concrete cause before fixing.",
        content=(
            "- Reproduce or inspect the failing path before changing code.\n"
            "- Fix the cause rather than adding broad fallback behavior.\n"
            "- Add or update a regression check when practical."
        ),
        applicable_node_types=["debugging", "implementation"],
        priority=2,
        source="hermes",
    ),
    SkillEntry(
        name="risk-checkpoint",
        display_name="Risk Checkpoint",
        category="planning",
        description="Call out high-risk or irreversible actions before proceeding.",
        content=(
            "- Avoid destructive, outward-facing, or credential-affecting actions unless explicitly authorized.\n"
            "- Surface blockers and risky assumptions in the result JSON notes.\n"
            "- Keep verification local unless the task asks for external effects."
        ),
        applicable_node_types=["implementation", "planning", "verification"],
        priority=3,
        source="hermes",
    ),
    SkillEntry(
        name="browser-automation",
        display_name="Browser Automation",
        category="automation",
        description="Control a headless Chrome browser via GuidedRunner.",
        content=(
            "You have a headless Chrome browser available. "
            "Use the GuidedRunner to execute browser automation tasks: "
            "navigate to pages, click elements, fill forms, take screenshots, "
            "and verify rendered content. "
            "Common actions: goto, click, fill, wait, expect_text, screenshot. "
            "For complex interactions (drag-and-drop, file uploads, iframes), "
            "use the custom 'code' field with raw JavaScript."
        ),
        applicable_node_types=["implementation", "verification", "debugging"],
        priority=1,
        source="hermes",
    ),
    SkillEntry(
        name="frontend-optimization",
        display_name="Frontend Optimization & UI Design",
        category="design",
        description="Build beautiful, accessible, performant frontends with Tailwind CSS, daisyUI, and modern UX patterns.",
        content=(
            "# Frontend Optimization & UI Design\n\n"
            "Build interfaces that are beautiful, responsive, accessible, and performant.\n\n"
            "## Tailwind CSS & daisyUI\n"
            "- Use daisyUI components when available: buttons, cards, modals, navbar, drawer, tabs, forms\n"
            "- daisyUI theme classes: `data-theme=\"light\"`, `data-theme=\"dark\"`, `data-theme=\"cupcake\"` etc.\n"
            "- Tailwind utility-first: prefer `flex`, `grid`, `gap-*`, `p-*`, `m-*` over custom CSS\n"
            "- Responsive breakpoints: `sm:`, `md:`, `lg:`, `xl:`, `2xl:` in that order\n"
            "- Dark mode: use `dark:` prefix or `class` strategy with daisyUI\n"
            "- Avoid hard-coded colors; use design tokens: `primary`, `secondary`, `accent`, `base-*`\n"
            "- Component variation: daisyUI uses `btn-primary`, `btn-outline`, `btn-ghost`, `btn-sm` modifiers\n\n"
            "## Responsive Design\n"
            "- Mobile-first: start with mobile layout, add `sm:`/`md:` breakpoints to enhance\n"
            "- Use `container` with `mx-auto` for centered layouts\n"
            "- Test all states: 320px, 768px, 1024px, 1440px\n"
            "- Touch targets: minimum 44x44px for interactive elements\n"
            "- Text: min 16px body font to prevent iOS zoom on input focus\n\n"
            "## Interaction & UX\n"
            "- Feedback states for every interactive element: hover, active, focus, disabled, loading\n"
            "- Use daisyUI `loading` class or animated spinners for loading states\n"
            "- Transitions: `transition-all duration-200` for smooth state changes\n"
            "- Empty states: show helpful placeholder when list/data is empty\n"
            "- Error states: show inline validation, not just toast alerts\n"
            "- Skeleton loading: use `skeleton` component for content placeholders\n"
            "- Micro-interactions: subtle hover scale, button press, card elevation change\n\n"
            "## Accessibility (A11Y)\n"
            "- Semantic HTML: `<nav>`, `<main>`, `<section>`, `<article>`, `<aside>`, `<footer>`\n"
            "- ARIA labels: `aria-label`, `aria-labelledby`, `aria-describedby` on all actionable elements\n"
            "- Keyboard navigation: all interactive elements focusable via Tab, activate with Enter/Space\n"
            "- Focus indicators: never remove `outline` without providing visible focus ring\n"
            "- Color contrast: text on background ≥ 4.5:1 ratio (WCAG AA), large text ≥ 3:1\n"
            "- Form labels: every `<input>` must have an associated `<label>` or `aria-label`\n"
            "- Screen readers: `sr-only` class for visually-hidden but accessible text\n"
            "- Reduced motion: respect `prefers-reduced-motion` for animations\n\n"
            "## Design System Consistency\n"
            "- Establish a spacing scale: 4/8/12/16/24/32/48/64px (Tailwind `p-1` to `p-16`)\n"
            "- Typography: max 2-3 font sizes per page, clear hierarchy (heading/subtitle/body/caption)\n"
            "- Color: use 1 primary + 1 secondary + semantic colors (success/warning/error/info)\n"
            "- Border radius: consistent `rounded-box`/`rounded-btn` from daisyUI\n"
            "- Shadows: daisyUI `shadow-sm`, `shadow-md`, `shadow-lg`, `shadow-xl`\n"
            "- Icons: use Lucide or Heroicons — consistent style and sizing throughout\n\n"
            "## Performance\n"
            "- Lazy load below-fold images and components\n"
            "- Code split at route level — each page only loads its own JS\n"
            "- Optimize images: WebP/AVIF format, responsive srcset\n"
            "- Minimize DOM size — virtualize long lists (>100 items)\n"
            "- CSS: use Tailwind JIT — only ship what you use\n"
            "- Monitor: Core Web Vitals (LCP < 2.5s, FID < 100ms, CLS < 0.1)"
        ),
        applicable_node_types=["implementation", "verification", "design", "frontend", "ui"],
        priority=2,
        source="hermes",
    ),
]


_DEFAULT_REGISTRY = SkillRegistry()


def get_default_registry() -> SkillRegistry:
    return _DEFAULT_REGISTRY
