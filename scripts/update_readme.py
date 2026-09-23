"""Refresh the generated parts of README.md and its commits-by-year chart.

README.md is hand-written apart from the sections between `<!-- name -->` and
`<!-- /name -->`, which this rewrites from live data:

    crates    downloads of my crates, from crates.io
    mods      subscriptions to my Hearts of Iron IV mods, from the Steam Workshop
    practice  NeetCode problems solved, from my submissions repository
    git       my commits before and after AI, from GitHub, charted in commits.svg

A section whose source can't be reached is left as it was, and the script exits
non-zero once the rest are written.

The GitHub sections need a token in GITHUB_TOKEN, and it decides what's counted:
the workflow's own token only sees public repositories, while a personal token
that can read my private ones counts those too (without naming them).

    GITHUB_TOKEN=... python scripts/update_readme.py

Runs daily from .github/workflows/update-readme.yml.
"""

import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CHART = ROOT / "commits.svg"

USER = "JonathanWoollett-Light"
USER_AGENT = f"{USER} profile README (https://github.com/{USER}/{USER})"
RAW = f"https://raw.githubusercontent.com/{USER}/{USER}/main"

CRATES_USER_ID = 79784  # https://crates.io/users/JonathanWoollett-Light
# Crates from these organisations are team efforts I helped maintain, not mine alone.
TEAM_ORGS = ("rust-vmm",)
# Crates that exist to serve a main crate (its proc macros, its internals) and
# share its downloads, so they'd only pad a list of highlights.
HELPER_CRATE = re.compile(r"-(macros?|attributes|core|consts)$")

# Steam Workshop ids of my Hearts of Iron IV mods, with my part in each.
MODS = {
    3342313594: "Author",  # OWB - The Think Tank
    3798403425: "Author",  # OWB - Rising Tide
    2777392649: "One of the developers",  # Millennium Dawn, a team project
}

NEETCODE_REPO = f"{USER}/neetcode-submissions"

# Addresses I've committed with that aren't linked to my GitHub account any more
# (an old work address and a typo), so GitHub's author filter misses them.
OTHER_EMAILS = ["jcawl@amazon.co.uk", "jonthanwoollettlight@gmail.com"]
AI_ERA = 2024  # commits authored from this year on count as "after AI"
# Commits changing more lines than this are nearly always datasets, experiment
# output or vendored code, so they're left out of the line totals.
BULK_LINES = 10_000
# Co-authored-by trailers that credit an AI tool, matched against "Name <email>".
AI_TOOLS = {
    "Claude": r"@anthropic\.com",
    "Copilot": r"copilot",
    "Cursor": r"@cursor\.com",
    "Codex": r"@openai\.com|\bcodex\b",
    "Gemini": r"\bgemini\b|google-labs-jules",
    "Aider": r"@aider\.chat",
    "Devin": r"devin-ai",
}
CO_AUTHOR = re.compile(r"^co-authored-by:(.*)$", re.IGNORECASE | re.MULTILINE)


def fetch(url, data=None, headers=None):
    """Parse the JSON reply to a GET of `url`, or a POST of `data`, retrying server errors."""
    request = urllib.request.Request(url, data, {"User-Agent": USER_AGENT, **(headers or {})})
    for delay in (5, 15, 45, None):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError) as error:
            # Asking again won't fix a client error, like a bad token.
            if delay is None or isinstance(error, urllib.error.HTTPError) and error.code < 500:
                raise
            time.sleep(delay)


def github_headers():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN isn't set")
    return {"Authorization": f"bearer {token}"}


def graphql(query, **variables):
    """The data and errors from a GitHub GraphQL query, which can partly fail."""
    body = json.dumps({"query": query, "variables": variables}).encode()
    reply = fetch("https://api.github.com/graphql", body, github_headers())
    if reply.get("data") is None:
        raise RuntimeError(f"GitHub GraphQL: {reply.get('errors')}")
    return reply["data"], reply.get("errors", [])


def compact(n):
    """1,234,567 as 1.2M, 23,456 as 23k and 4,567 as 4.6k."""
    if n >= 999_500:
        return f"{n / 1e6:.1f}M"
    if n >= 9_950:
        return f"{n / 1e3:.0f}k"
    if n >= 1_000:
        return f"{n / 1e3:.1f}k"
    return str(n)


# crates.io


def crates_section():
    crates, page = [], 1
    while True:
        query = urllib.parse.urlencode({"user_id": CRATES_USER_ID, "per_page": 100, "page": page})
        reply = fetch(f"https://crates.io/api/v1/crates?{query}")
        crates += reply["crates"]
        if not reply["crates"] or len(crates) >= reply["meta"]["total"]:
            break
        page += 1

    def is_team(crate):
        return any(f"github.com/{org}/" in (crate["repository"] or "") for org in TEAM_ORGS)

    def highlights(group):
        top = sorted((c for c in group if not HELPER_CRATE.search(c["name"])), key=lambda c: -c["downloads"])
        return " · ".join(
            f"[{c['name']}](https://crates.io/crates/{c['name']}) {compact(c['downloads'])}" for c in top[:5]
        )

    team = [c for c in crates if is_team(c)]
    mine = [c for c in crates if not is_team(c)]
    orgs = " and ".join(f"[{org}](https://github.com/{org})" for org in TEAM_ORGS)
    total = sum(c["downloads"] for c in crates)
    return "\n".join([
        f"**{compact(total)} downloads** of the {len(crates)} crates I own on "
        f"[crates.io](https://crates.io/users/{USER}):",
        "",
        f"- **{compact(sum(c['downloads'] for c in team))}** for {len(team)} {orgs} crates I helped maintain "
        f"at AWS. They're team efforts, so the credit is shared: {highlights(team)}",
        f"- **{compact(sum(c['downloads'] for c in mine))}** for {len(mine)} of my own: {highlights(mine)}",
    ])


# Steam Workshop


def mods_section():
    form = {"itemcount": len(MODS)} | {f"publishedfileids[{i}]": item for i, item in enumerate(MODS)}
    reply = fetch(
        "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
        urllib.parse.urlencode(form).encode(),
    )
    lines = [
        "| Mod | My part | Subscribers | Downloads |",
        "| :-- | :-- | --: | --: |",
    ]
    for mod in reply["response"]["publishedfiledetails"]:
        if mod["result"] != 1:
            raise RuntimeError(f"Steam Workshop item {mod['publishedfileid']} is unavailable")
        link = f"https://steamcommunity.com/sharedfiles/filedetails/?id={mod['publishedfileid']}"
        title = mod["title"].replace("|", "\\|")  # a bare | would end the table cell
        lines.append(
            f"| [{title}]({link}) | {MODS[int(mod['publishedfileid'])]} "
            f"| {mod['subscriptions']:,} | {mod['lifetime_subscriptions']:,} |"
        )
    # Steam doesn't publish downloads, so this is the closest it has.
    lines += ["", "<sub>Downloads are the Steam Workshop's lifetime subscriptions.</sub>"]
    return "\n".join(lines)


# NeetCode


def practice_section():
    url = f"https://api.github.com/repos/{NEETCODE_REPO}/git/trees/HEAD?recursive=1"
    tree = fetch(url, headers=github_headers())["tree"]
    # Solutions are synced to "<topic>/<problem>/submission-<n>.<ext>".
    problems = {entry["path"].split("/")[1] for entry in tree if entry["path"].count("/") == 2}
    return (
        f"I'm working through [Project Euler](https://projecteuler.net) "
        f"([solutions](https://github.com/{USER}/project_euler)) and [NeetCode](https://neetcode.io), "
        f"where I've solved {len(problems)} problems ([solutions](https://github.com/{NEETCODE_REPO}))."
    )


# Git


@dataclass
class Commit:
    repo: str
    private: bool
    date: datetime  # when it was authored
    lines: tuple[int, int] | None  # added and removed, None if GitHub couldn't count them
    ai: list[str]  # AI tools credited as co-authors


CONTRIBUTIONS = """
query($login: String!, $since: DateTime!, $until: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $since, to: $until) {
      commitContributionsByRepository(maxRepositories: 100) {
        repository { nameWithOwner }
      }
    }
  }
}
"""

HISTORY = """
query($owner: String!, $name: String!, $author: CommitAuthor!, $after: String, $lines: Boolean!) {
  repository(owner: $owner, name: $name) {
    isPrivate
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 50, after: $after, author: $author) {
            pageInfo { hasNextPage endCursor }
            nodes {
              oid
              authoredDate
              message
              parents { totalCount }
              additions @include(if: $lines)
              deletions @include(if: $lines)
            }
          }
        }
      }
    }
  }
}
"""


def contributed_repositories():
    """My GitHub node id and every repository GitHub credits me with commits to."""
    data, _ = graphql("query($login: String!) { user(login: $login) { id createdAt } }", login=USER)
    user, repos = data["user"], set()
    for year in range(int(user["createdAt"][:4]), datetime.now(timezone.utc).year + 1):
        # A contributions collection can span a year at most.
        data, errors = graphql(
            CONTRIBUTIONS, login=USER, since=f"{year}-01-01T00:00:00Z", until=f"{year}-12-31T23:59:59Z"
        )
        if errors:
            raise RuntimeError(f"GitHub GraphQL: {errors}")
        repos.update(
            entry["repository"]["nameWithOwner"]
            for entry in data["user"]["contributionsCollection"]["commitContributionsByRepository"]
        )
    return user["id"], sorted(repos)


def history_page(owner, name, author, after):
    """One page of my commits to a default branch, see HISTORY, or None if the repository's gone."""
    data, errors = graphql(HISTORY, owner=owner, name=name, author=author, after=after, lines=True)
    if data["repository"] is None and all(error.get("type") == "NOT_FOUND" for error in errors):
        return None
    if errors and all((error.get("path") or [""])[-1] in ("additions", "deletions") for error in errors):
        # GitHub gives up counting the lines of some huge commits, which blanks
        # them out, so fetch the page again without line counts to fill them in.
        retry, errors = graphql(HISTORY, owner=owner, name=name, author=author, after=after, lines=False)
        nodes = data["repository"]["defaultBranchRef"]["target"]["history"]["nodes"]
        history = retry["repository"]["defaultBranchRef"]["target"]["history"]
        history["nodes"] = [node or other for node, other in zip(nodes, history["nodes"])]
        data = retry
    if errors:
        raise RuntimeError(f"GitHub GraphQL, {owner}/{name}: {errors}")
    return data["repository"]


def repository_commits(repo, author):
    """My commits on the default branch of `repo` matching `author`, a CommitAuthor filter."""
    owner, name = repo.split("/")
    commits, after = {}, None
    while True:
        repository = history_page(owner, name, author, after)
        if not repository or not repository["defaultBranchRef"]:  # deleted or empty
            return commits
        history = repository["defaultBranchRef"]["target"]["history"]
        for node in history["nodes"]:
            if node["parents"]["totalCount"] > 1:  # a merge repeats its branch's changes
                continue
            trailers = " ".join(CO_AUTHOR.findall(node["message"]))
            commits[node["oid"]] = Commit(
                repo=repo,
                private=repository["isPrivate"],
                date=datetime.fromisoformat(node["authoredDate"].replace("Z", "+00:00")),
                lines=(node["additions"], node["deletions"]) if "additions" in node else None,
                ai=[tool for tool, pattern in AI_TOOLS.items() if re.search(pattern, trailers, re.IGNORECASE)],
            )
        if not history["pageInfo"]["hasNextPage"]:
            return commits
        after = history["pageInfo"]["endCursor"]


def my_commits():
    """Every non-merge commit I've authored on a default branch GitHub can show me."""
    user_id, repos = contributed_repositories()
    authors = [{"id": user_id}] + ([{"emails": OTHER_EMAILS}] if OTHER_EMAILS else [])
    with ThreadPoolExecutor(8) as pool:
        found = pool.map(lambda job: repository_commits(*job), [(r, a) for r in repos for a in authors])
        # The same commit can be on several repositories' branches, so key them by hash.
        return list({oid: commit for batch in found for oid, commit in batch.items()}.values())


def git_section():
    commits = my_commits()
    now = datetime.now(timezone.utc)
    first = min(c.date for c in commits)
    by_year = {year: [c for c in commits if c.date.year == year] for year in range(first.year, now.year + 1)}
    era = datetime(AI_ERA, 1, 1, tzinfo=timezone.utc)
    periods = [  # (heading, commits, years spanned)
        (f"Before AI<br><sub>{first.year}–{AI_ERA - 1}</sub>", [c for c in commits if c.date < era],
         (era - first).days / 365.25),
        (f"After AI<br><sub>{AI_ERA}–{now.year}</sub>", [c for c in commits if c.date >= era],
         (now - era).days / 365.25),
        ("All time", commits, (now - first).days / 365.25),
    ]

    def counted(group):
        return [c for c in group if c.lines and sum(c.lines) <= BULK_LINES]

    def ai_share(group):
        ai = sum(1 for c in group if c.ai)
        return f"{ai:,} ({ai / len(group):.0%})" if ai else "0"

    rows = {
        "Commits": lambda group, years: f"{len(group):,}",
        "Commits a year": lambda group, years: f"{len(group) / years:,.0f}",
        "Lines added": lambda group, years: f"{sum(c.lines[0] for c in counted(group)):,}",
        "Lines removed": lambda group, years: f"{sum(c.lines[1] for c in counted(group)):,}",
        "Lines changed per commit (median)": lambda group, years: (
            f"{statistics.median(sum(c.lines) for c in group if c.lines):,.0f}"
        ),
        "Repositories": lambda group, years: f"{len({c.repo for c in group}):,}",
        "With an AI co-author": lambda group, years: ai_share(group),
    }
    table = [
        "| | " + " | ".join(heading for heading, _, _ in periods) + " |",
        "| :-- |" + " --: |" * len(periods),
        *(
            f"| {label} | " + " | ".join(row(group, years) for _, group, years in periods) + " |"
            for label, row in rows.items()
        ),
    ]

    repos = {c.repo: c.private for c in commits}
    bulk = len(commits) - len(counted(commits))
    tools = {tool: sum(1 for c in commits if tool in c.ai) for tool in AI_TOOLS}
    tools = ", ".join(f"{tool} {n:,}" for tool, n in sorted(tools.items(), key=lambda kv: -kv[1]) if n)
    notes = (
        f"Commits I authored on the default branches of {len(repos)} repositories "
        f"({sum(repos.values())} of them private), not counting merges. Line totals leave out "
        f"{bulk} commits that change over {BULK_LINES:,} lines each, which are nearly all datasets, "
        f"experiment output or vendored code. The AI row counts commits with a `Co-authored-by` trailer "
        f"naming an AI tool ({tools or 'none yet'}); AI help without one doesn't show up."
    )

    per_year = [
        "| Year | Commits | With an AI co-author | Lines added | Lines removed |",
        "| --: | --: | --: | --: | --: |",
        *(
            f"| {year} | {len(group):,} | {sum(1 for c in group if c.ai):,} "
            f"| {sum(c.lines[0] for c in counted(group)):,} | {sum(c.lines[1] for c in counted(group)):,} |"
            for year, group in by_year.items()
        ),
    ]

    totals = {year: (len(group), sum(1 for c in group if c.ai)) for year, group in by_year.items()}
    write_if_changed(CHART, chart(totals))
    return "\n".join([
        f"![Column chart of my commits per year, split into before and after AI, with the commits that have "
        f"an AI co-author stacked on top; the table below has the numbers]({RAW}/{CHART.name})",
        "",
        *table,
        "",
        f"<sub>{notes}</sub>",
        "",
        "<details><summary>By year</summary>",
        "",
        *per_year,
        "",
        "</details>",
    ])


# Chart

# The README can be viewed on a light or dark page, and GitHub can't be relied
# on to pick an image per theme, so these colours work on both. They match the
# Project Euler grid's.
COMMITS = "#2a78d6"
AI = "#d95926"
INK = "#7d7b76"
RULE = "#898781"  # drawn at 50% opacity
WIDTH, HEIGHT = 746, 220
LEFT = 32  # lines up with the Project Euler grid, which keeps this for its row labels
PLOT_TOP, PLOT_BOTTOM = 72, 196  # the tallest column reaches PLOT_TOP
BAR, GAP, RADIUS = 24, 2, 4


def column(x, bottom, height, rounded):
    """A column segment's outline: square at the bottom and, if `rounded`, rounded on top."""
    top = bottom - height
    r = min(RADIUS, height) if rounded else 0
    return (
        f"M{x},{bottom:.1f}V{top + r:.1f}Q{x},{top:.1f} {x + r:.1f},{top:.1f}"
        f"H{x + BAR - r:.1f}Q{x + BAR},{top:.1f} {x + BAR},{top + r:.1f}V{bottom:.1f}Z"
    )


def chart(totals):
    """Commits per year as columns, each topped by the part with an AI co-author."""
    years = list(totals)
    slot = (WIDTH - LEFT) / len(years)
    scale = (PLOT_BOTTOM - PLOT_TOP) / max(total for total, _ in totals.values())
    split = LEFT + slot * sum(1 for year in years if year < AI_ERA)  # x of the before/after divide

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img">',
        "<title>Commits per year, before and after AI</title>",
        "<style>",
        f"text {{ font: 11px system-ui, -apple-system, 'Segoe UI', sans-serif; fill: {INK} }}",
        ".head { font-size: 13px; font-weight: 600 }",
        ".num { text-anchor: middle; font-variant-numeric: tabular-nums }",
        f".commits {{ fill: {COMMITS} }}",
        f".ai {{ fill: {AI} }}",
        f"line {{ stroke: {RULE}; stroke-opacity: 0.5 }}",
        "</style>",
        # The legend doubles as the chart's heading.
        f'<rect class="commits" x="{LEFT}" y="5" width="12" height="12" rx="2"/>',
        f'<text class="head" x="{LEFT + 18}" y="16">Commits</text>',
        f'<rect class="ai" x="{LEFT + 92}" y="5" width="12" height="12" rx="2"/>',
        f'<text class="head" x="{LEFT + 110}" y="16">with an AI co-author</text>',
        f'<text class="head" x="{(LEFT + split) / 2:.1f}" y="42" text-anchor="middle">Before AI</text>',
        f'<text class="head" x="{(split + WIDTH) / 2:.1f}" y="42" text-anchor="middle">After AI</text>',
        f'<line x1="{split:.1f}" y1="28" x2="{split:.1f}" y2="{PLOT_BOTTOM}"/>',
        f'<line x1="{LEFT}" y1="{PLOT_BOTTOM + 0.5}" x2="{WIDTH}" y2="{PLOT_BOTTOM + 0.5}"/>',
    ]
    for i, year in enumerate(years):
        total, ai = totals[year]
        x = round(LEFT + slot * i + (slot - BAR) / 2)
        centre = x + BAR / 2
        # Keep the smallest non-zero segment visible.
        human, assisted = [max(n * scale, 2) if n else 0 for n in (total - ai, ai)]
        if human:
            svg.append(f'<path class="commits" d="{column(x, PLOT_BOTTOM, human, not assisted)}"/>')
        if assisted:
            bottom = PLOT_BOTTOM - human - (GAP if human else 0)
            svg.append(f'<path class="ai" d="{column(x, bottom, assisted, True)}"/>')
        top = PLOT_BOTTOM - human - assisted - (GAP if human and assisted else 0)
        svg.append(f'<text class="num" x="{centre:.1f}" y="{top - 6:.1f}">{total:,}</text>')
        svg.append(f'<text class="num" x="{centre:.1f}" y="{PLOT_BOTTOM + 17}">{year}</text>')
    svg.append("</svg>")
    return "\n".join(svg) + "\n"


def write_if_changed(path, text):
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8", newline="\n")


def main():
    readme = README.read_text(encoding="utf-8")
    sections = {"crates": crates_section, "mods": mods_section, "practice": practice_section, "git": git_section}
    failed = []
    for name, build in sections.items():
        marked = re.compile(rf"(<!-- {name} -->\n).*?(<!-- /{name} -->)", re.DOTALL)
        if not marked.search(readme):
            sys.exit(f"update_readme: README.md has no {name} section")
        try:
            body = build()
        except Exception as error:  # one source being down shouldn't hold back the others
            print(f"update_readme: couldn't update {name}: {error}", file=sys.stderr)
            failed.append(name)
            continue
        readme = marked.sub(lambda match: match[1] + body + "\n" + match[2], readme)
        print(f"update_readme: updated {name}")
    write_if_changed(README, readme)
    if failed:
        sys.exit(f"update_readme: left {', '.join(failed)} as they were")


if __name__ == "__main__":
    main()
