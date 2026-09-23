"""Refresh the generated parts of README.md and the two figures in it.

README.md is hand-written apart from the text between `<!-- name -->` and
`<!-- /name -->` markers, which this rewrites from live data:

    crates    downloads of my crates, from crates.io
    mods      downloads of my Hearts of Iron IV mods, from the Steam Workshop
    practice  problems I've solved on Project Euler and NeetCode, drawn in euler.svg
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
EULER_CHART = ROOT / "euler.svg"
COMMITS_CHART = ROOT / "commits.svg"

USER = "JonathanWoollett-Light"
USER_AGENT = f"{USER} profile README (https://github.com/{USER}/{USER})"

CRATES_USER_ID = 79784  # https://crates.io/users/JonathanWoollett-Light
# Crates from these organisations are team efforts I helped maintain, not mine alone.
TEAM_ORGS = ("rust-vmm",)

# Steam Workshop ids of my Hearts of Iron IV mods, and of the team one I help develop.
MY_MODS = {3342313594: "The Think Tank", 3798403425: "Rising Tide"}
TEAM_MODS = {2777392649: "Millennium Dawn"}

# My project_euler repository draws its own progress grid; the problems it
# marks solved are redrawn smaller here.
EULER_GRID = f"https://raw.githubusercontent.com/{USER}/project_euler/master/progress.svg"
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
    """The reply to a GET of `url`, or a POST of `data`, retrying server errors."""
    request = urllib.request.Request(url, data, {"User-Agent": USER_AGENT, **(headers or {})})
    for delay in (5, 15, 45, None):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as error:
            # Asking again won't fix a client error, like a bad token.
            if delay is None or isinstance(error, urllib.error.HTTPError) and error.code < 500:
                raise
            time.sleep(delay)


def fetch_json(url, data=None, headers=None):
    return json.loads(fetch(url, data, headers))


def github_headers():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN isn't set")
    return {"Authorization": f"bearer {token}"}


def graphql(query, **variables):
    """The data and errors from a GitHub GraphQL query, which can partly fail."""
    body = json.dumps({"query": query, "variables": variables}).encode()
    reply = fetch_json("https://api.github.com/graphql", body, github_headers())
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


def listing(items):
    """["a", "b", "c"] as "a, b and c"."""
    return " and ".join(filter(None, [", ".join(items[:-1]), items[-1]]))


# crates.io


def crates_section():
    crates, page = [], 1
    while True:
        query = urllib.parse.urlencode({"user_id": CRATES_USER_ID, "per_page": 100, "page": page})
        reply = fetch_json(f"https://crates.io/api/v1/crates?{query}")
        crates += reply["crates"]
        if not reply["crates"] or len(crates) >= reply["meta"]["total"]:
            break
        page += 1

    def is_team(crate):
        return any(f"github.com/{org}/" in (crate["repository"] or "") for org in TEAM_ORGS)

    team = [c["downloads"] for c in crates if is_team(c)]
    mine = [c["downloads"] for c in crates if not is_team(c)]
    orgs = listing([f"[{org}](https://github.com/{org})" for org in TEAM_ORGS])
    return (
        f"{compact(sum(team) + sum(mine))}, of which {compact(sum(mine))} are for "
        f"[{len(mine)} crates of my own](https://crates.io/users/{USER}) and {compact(sum(team))} "
        f"for {len(team)} {orgs} crates I co-maintained (team efforts)"
    )


# Steam Workshop


def mods_section():
    ids = [*MY_MODS, *TEAM_MODS]
    form = {"itemcount": len(ids)} | {f"publishedfileids[{i}]": id for i, id in enumerate(ids)}
    reply = fetch_json(
        "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/",
        urllib.parse.urlencode(form).encode(),
    )
    downloads = {}
    for mod in reply["response"]["publishedfiledetails"]:
        if mod["result"] != 1:
            raise RuntimeError(f"Steam Workshop item {mod['publishedfileid']} is unavailable")
        # Steam doesn't count downloads; its lifetime subscriptions are the nearest thing.
        downloads[int(mod["publishedfileid"])] = mod["lifetime_subscriptions"]

    def links(mods):
        return listing([
            f"[{name}](https://steamcommunity.com/sharedfiles/filedetails/?id={id}) {compact(downloads[id])}"
            for id, name in mods.items()
        ])

    return f"{links(MY_MODS)}, which are mine, and {links(TEAM_MODS)}, a team project I'm one of the developers of"


# Project Euler and NeetCode


def practice_section():
    grid = fetch(EULER_GRID).decode()
    total = int(re.search(r'data-problems="(\d+)"', grid)[1])
    solved = {int(n) for n in re.findall(r"Problem (\d+): solved", grid)}
    url = f"https://api.github.com/repos/{NEETCODE_REPO}/git/trees/HEAD?recursive=1"
    tree = fetch_json(url, headers=github_headers())["tree"]
    # Solutions are synced to "<topic>/<problem>/submission-<n>.<ext>".
    neetcode = {entry["path"].split("/")[1] for entry in tree if entry["path"].count("/") == 2}
    write_if_changed(EULER_CHART, euler_chart(solved, total))
    return (
        f"{len(solved)} [Project Euler](https://github.com/{USER}/project_euler) and "
        f"{len(neetcode)} [NeetCode](https://github.com/{NEETCODE_REPO}) problems solved"
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
    era = datetime(AI_ERA, 1, 1, tzinfo=timezone.utc)
    periods = [  # (heading, commits, years spanned)
        (f"Before AI ({first.year}–{AI_ERA - 1})", [c for c in commits if c.date < era], (era - first).days / 365.25),
        (f"After AI ({AI_ERA}–{now.year})", [c for c in commits if c.date >= era], (now - era).days / 365.25),
        ("All time", commits, (now - first).days / 365.25),
    ]

    def counted(group):
        return [c for c in group if c.lines and sum(c.lines) <= BULK_LINES]

    def lines(group):
        added, removed = (compact(sum(c.lines[i] for c in counted(group))) for i in (0, 1))
        return f"+{added} / −{removed}"

    def ai_share(group):
        ai = sum(1 for c in group if c.ai)
        return f"{ai:,} ({ai / len(group):.0%})" if ai else "0"

    rows = {
        "Commits": lambda group, years: f"{len(group):,}",
        "Commits a year": lambda group, years: f"{len(group) / years:,.0f}",
        "Lines added / removed": lambda group, years: lines(group),
        "Median lines changed per commit": lambda group, years: (
            f"{statistics.median(sum(c.lines) for c in group if c.lines):,.0f}"
        ),
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
    private = sum(repos.values())
    bulk = len(commits) - len(counted(commits))
    tools = {tool: sum(1 for c in commits if tool in c.ai) for tool in AI_TOOLS}
    tools = ", ".join(f"{tool} {n:,}" for tool, n in sorted(tools.items(), key=lambda kv: -kv[1]) if n)
    notes = (
        f"Non-merge commits on the default branches of {len(repos)} repositories"
        + (f" ({private} private)" if private else "")
        + f". Line totals skip {bulk} commits of over {BULK_LINES:,} lines, mostly data. AI co-authors are "
        f"`Co-authored-by` trailers naming an AI tool ({tools or 'none yet'}), so uncredited AI help isn't "
        f"counted. [Refreshed daily](https://github.com/{USER}/{USER}/blob/main/scripts/update_readme.py)."
    )

    by_year = {year: [c for c in commits if c.date.year == year] for year in range(first.year, now.year + 1)}
    totals = {year: (len(group), sum(1 for c in group if c.ai)) for year, group in by_year.items()}
    write_if_changed(COMMITS_CHART, commits_chart(totals))
    return "\n".join([*table, "", f"<sub>{notes}</sub>"])


# Figures

# The README can be viewed on a light or dark page, and GitHub can't be relied
# on to pick an image per theme, so these colours work on both. They match the
# Project Euler grid's.
BLUE = "#2a78d6"
ORANGE = "#d95926"
INK = "#7d7b76"
GREY = "#898781"  # drawn faint, for empty squares and rules
# The figures share a size, which fits two side by side in the README on a
# desktop screen and stacks them on narrower ones.
WIDTH, HEIGHT = 412, 200


def svg_start(title, height=HEIGHT):
    """The opening of a figure, with the styles both share."""
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img">',
        f"<title>{title}</title>",
        "<style>",
        f"text {{ font: 11px system-ui, -apple-system, 'Segoe UI', sans-serif; fill: {INK} }}",
        ".head { font-size: 12px; font-weight: 600 }",
        ".num { text-anchor: middle; font-variant-numeric: tabular-nums }",
        f"rect {{ fill: {GREY}; fill-opacity: 0.2 }}",
        f".blue {{ fill: {BLUE}; fill-opacity: 1 }}",
        f".orange {{ fill: {ORANGE}; fill-opacity: 1 }}",
        f"line {{ stroke: {GREY}; stroke-opacity: 0.5 }}",
        "</style>",
    ]


def key(x, colour, label):
    """A square of `colour` and its label, along a figure's top."""
    return [
        f'<rect class="{colour}" x="{x}" y="4" width="12" height="12" rx="2"/>',
        f'<text class="head" x="{x + 18}" y="15">{label}</text>',
    ]


EULER_COLUMNS = 50  # as on the Project Euler archive's pages, and in my full grid
CELL, CELL_GAP = 6, 2
GROUP_GAP = 3  # extra space after every 10 columns, to make counting easier
GRID_TOP = 26


def euler_chart(solved, total):
    """Every Project Euler problem as a square, filled in if I've solved it."""
    pitch = CELL + CELL_GAP
    rows = -(-total // EULER_COLUMNS)
    height = max(HEIGHT, GRID_TOP + rows * pitch - CELL_GAP)
    svg = svg_start(f"Project Euler: {len(solved)} of {total:,} problems solved", height)
    svg += key(0, "blue", f"{len(solved)} of {total:,} Project Euler problems solved")
    for n in range(1, total + 1):
        row, col = divmod(n - 1, EULER_COLUMNS)
        fill = ' class="blue"' if n in solved else ""
        svg.append(
            f'<rect{fill} x="{col * pitch + col // 10 * GROUP_GAP}" y="{GRID_TOP + row * pitch}" '
            f'width="{CELL}" height="{CELL}" rx="1"/>'
        )
    svg.append("</svg>")
    return "\n".join(svg) + "\n"


PLOT_TOP, PLOT_BOTTOM = 66, 178  # the tallest column reaches PLOT_TOP
BAR, BAR_GAP, RADIUS = 22, 2, 4


def column(x, bottom, height, rounded):
    """A column segment's outline: square at the bottom and, if `rounded`, rounded on top."""
    top = bottom - height
    r = min(RADIUS, height) if rounded else 0
    return (
        f"M{x},{bottom:.1f}V{top + r:.1f}Q{x},{top:.1f} {x + r:.1f},{top:.1f}"
        f"H{x + BAR - r:.1f}Q{x + BAR},{top:.1f} {x + BAR},{top + r:.1f}V{bottom:.1f}Z"
    )


def commits_chart(totals):
    """Commits per year as columns, each topped by the part with an AI co-author."""
    years = list(totals)
    slot = WIDTH / len(years)
    scale = (PLOT_BOTTOM - PLOT_TOP) / max(total for total, _ in totals.values())
    split = slot * sum(1 for year in years if year < AI_ERA)  # x of the before/after divide
    svg = svg_start("Commits per year, before and after AI")
    svg += key(0, "blue", "Commits") + key(84, "orange", "with an AI co-author")
    svg += [
        f'<text class="head" x="{split / 2:.1f}" y="40" text-anchor="middle">Before AI</text>',
        f'<text class="head" x="{(split + WIDTH) / 2:.1f}" y="40" text-anchor="middle">After AI</text>',
        f'<line x1="{split:.1f}" y1="28" x2="{split:.1f}" y2="{PLOT_BOTTOM}"/>',
        f'<line x1="0" y1="{PLOT_BOTTOM + 0.5}" x2="{WIDTH}" y2="{PLOT_BOTTOM + 0.5}"/>',
    ]
    for i, year in enumerate(years):
        total, ai = totals[year]
        x = round(slot * i + (slot - BAR) / 2)
        centre = x + BAR / 2
        # Keep the smallest non-zero segment visible.
        human, assisted = [max(n * scale, 2) if n else 0 for n in (total - ai, ai)]
        if human:
            svg.append(f'<path class="blue" d="{column(x, PLOT_BOTTOM, human, not assisted)}"/>')
        if assisted:
            bottom = PLOT_BOTTOM - human - (BAR_GAP if human else 0)
            svg.append(f'<path class="orange" d="{column(x, bottom, assisted, True)}"/>')
        top = PLOT_BOTTOM - human - assisted - (BAR_GAP if human and assisted else 0)
        svg.append(f'<text class="num" x="{centre:.1f}" y="{top - 5:.1f}">{total:,}</text>')
        svg.append(f'<text class="num" x="{centre:.1f}" y="{PLOT_BOTTOM + 16}">{year}</text>')
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
        marked = re.compile(rf"(<!-- {name} -->)(.*?)(<!-- /{name} -->)", re.DOTALL)
        if not marked.search(readme):
            sys.exit(f"update_readme: README.md has no {name} section")
        try:
            body = build()
        except Exception as error:  # one source being down shouldn't hold back the others
            print(f"update_readme: couldn't update {name}: {error}", file=sys.stderr)
            failed.append(name)
            continue
        # A section on lines of its own stays that way, and one within a line stays inline.
        readme = marked.sub(lambda m: m[1] + (f"\n{body}\n" if m[2].startswith("\n") else body) + m[3], readme)
        print(f"update_readme: updated {name}")
    write_if_changed(README, readme)
    if failed:
        sys.exit(f"update_readme: left {', '.join(failed)} as they were")


if __name__ == "__main__":
    main()
