#!/usr/bin/env python3
import csv
import fnmatch
import pathlib
import shutil
import subprocess
import sys

WORKDIR = pathlib.Path("github-commit-audit")
REPORT_FILE = pathlib.Path("github_commit_audit_report.csv")


def run(cmd, cwd=None, check=True):
    result = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if check and result.returncode != 0:
        print(f"\nFehler bei: {' '.join(cmd)}")
        print(result.stderr.strip())
        sys.exit(1)

    return result.stdout.strip()


def require_tool(name):
    if shutil.which(name) is None:
        print(f"Fehlt: {name}")
        sys.exit(1)


def require_git_filter_repo_module():
    try:
        import git_filter_repo  # noqa
    except ImportError:
        print("Fehlt: git-filter-repo")
        print("Installieren mit:")
        print("python -m pip install git-filter-repo")
        sys.exit(1)


def repo_dir_for(repo):
    return WORKDIR / f"{repo.replace('/', '__')}.git"


def get_repos(username):
    output = run([
        "gh", "repo", "list", username,
        "--limit", "1000",
        "--json", "nameWithOwner",
        "-q", ".[].nameWithOwner"
    ])

    return [line.strip() for line in output.splitlines() if line.strip()]


def clone_or_update(repo):
    repo_dir = repo_dir_for(repo)

    if repo_dir.exists():
        print(f"Update: {repo}")
        run(["git", "remote", "update", "--prune"], cwd=repo_dir)
    else:
        print(f"Clone: {repo}")
        run([
            "git", "clone", "--mirror",
            f"https://github.com/{repo}.git",
            str(repo_dir)
        ])

    return repo_dir


def scan_repo(repo, repo_dir):
    output = run([
        "git", "log", "--all",
        "--format=%H%x09%an%x09%ae%x09%cn%x09%ce"
    ], cwd=repo_dir, check=False)

    entries = set()

    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            continue

        commit_hash, author_name, author_email, committer_name, committer_email = parts

        if author_email:
            entries.add((repo, author_name, author_email))

        if committer_email:
            entries.add((repo, committer_name, committer_email))

    return entries


def parse_filters(raw_filter):
    include_filters = []
    exclude_filters = []

    for item in raw_filter.split(","):
        item = item.strip().lower()
        if not item:
            continue

        if item.startswith("-") or item.startswith("!"):
            pattern = item[1:].strip()
            if pattern:
                exclude_filters.append(pattern)
        else:
            include_filters.append(item)

    return include_filters, exclude_filters


def email_matches_filter(email, include_filters, exclude_filters):
    email = email.lower()

    if any(fnmatch.fnmatch(email, pattern) for pattern in exclude_filters):
        return False

    if not include_filters:
        return True

    return any(fnmatch.fnmatch(email, pattern) for pattern in include_filters)


def scan():
    require_tool("gh")
    require_tool("git")

    username = input("GitHub Username: ").strip()

    if not username:
        print("Kein Username angegeben.")
        return

    raw_filter = input(
        "Welche Emails sollen in die CSV? Komma-getrennt, * erlaubt; - oder ! für Ausschluss, z.B. *@gmail.com, -noreply@*: "
    ).strip()

    include_filters, exclude_filters = parse_filters(raw_filter)

    WORKDIR.mkdir(exist_ok=True)

    print("\nHole Repository-Liste...")
    repos = get_repos(username)

    print(f"Gefundene Repos: {len(repos)}\n")

    all_entries = set()

    for repo in repos:
        repo_dir = clone_or_update(repo)
        entries = scan_repo(repo, repo_dir)

        for repo_name, name, email in entries:
            if email_matches_filter(email, include_filters, exclude_filters):
                all_entries.add((repo_name, name, email))

    rows = [
        {
            "repo": repo,
            "name": name,
            "email": email,
        }
        for repo, name, email in sorted(all_entries)
    ]

    with REPORT_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "repo",
            "name",
            "email",
        ])
        writer.writeheader()
        writer.writerows(rows)

    print("\nScan fertig.")
    print(f"Report: {REPORT_FILE}")
    print(f"Eindeutige Name/Mail-Einträge: {len(rows)}")

    if include_filters or exclude_filters:
        active_parts = []
        if include_filters:
            active_parts.append("inklusive: " + ", ".join(include_filters))
        if exclude_filters:
            active_parts.append("exklusive: " + ", ".join(exclude_filters))
        print(f"Filter aktiv: {', '.join(active_parts)}")

    print("\nGefundene Einträge:")
    for row in rows:
        print(f"  {row['repo']} | {row['name']} <{row['email']}>")

    print("\nLösche aus der CSV alle Zeilen, die NICHT geändert werden sollen.")
    print("Danach Option 2 ausführen.")


def load_report_rows():
    if not REPORT_FILE.exists():
        print(f"Report nicht gefunden: {REPORT_FILE}")
        sys.exit(1)

    with REPORT_FILE.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def collect_rewrites(rows, new_email):
    rewrites = {}

    for row in rows:
        repo = row["repo"].strip()
        old_email = row["email"].strip()

        if not repo or not old_email:
            continue

        if old_email == new_email:
            continue

        rewrites.setdefault(repo, set())
        rewrites[repo].add(old_email)

    return rewrites


def ensure_remote(repo, repo_dir):
    remotes = run(["git", "remote"], cwd=repo_dir, check=False).splitlines()

    if "origin" not in remotes:
        url = f"https://github.com/{repo}.git"
        print(f"Setze origin -> {url}")
        run(["git", "remote", "add", "origin", url], cwd=repo_dir)


def rewrite_repo(repo, old_emails, new_name, new_email, push):
    repo_dir = repo_dir_for(repo)

    # FIX: Repo automatisch klonen, falls nicht vorhanden
    if not repo_dir.exists():
        print(f"\nRepo fehlt lokal, klone: {repo}")
        clone_or_update(repo)

    print(f"\nBearbeite: {repo}")

    for old_email in sorted(old_emails):
        print(f"  {old_email} -> {new_email}")

    callback_lines = ["email_map = {"]

    for old_email in sorted(old_emails):
        callback_lines.append(
            f"    {old_email.encode()!r}: {new_email.encode()!r},"
        )

    callback_lines += [
        "}",
        "",
        "if commit.author_email in email_map:",
        f"    commit.author_name = {new_name.encode()!r}",
        "    commit.author_email = email_map[commit.author_email]",
        "",
        "if commit.committer_email in email_map:",
        f"    commit.committer_name = {new_name.encode()!r}",
        "    commit.committer_email = email_map[commit.committer_email]",
    ]

    callback = "\n".join(callback_lines)

    run([
        sys.executable,
        "-m",
        "git_filter_repo",
        "--force",
        "--commit-callback",
        callback
    ], cwd=repo_dir)

    if push:
        ensure_remote(repo, repo_dir)

        print("Push rewritten history...")
        run(["git", "push", "origin", "--force", "--all"], cwd=repo_dir)
        run(["git", "push", "origin", "--force", "--tags"], cwd=repo_dir)
    else:
        print("Rewrite lokal fertig. Push wurde nicht ausgeführt.")


def replace():
    require_tool("git")
    require_git_filter_repo_module()

    new_name = input("Name, der gesetzt werden soll: ").strip()
    new_email = input("Mail, die überall gesetzt werden soll: ").strip()

    if not new_name:
        print("Kein Name angegeben.")
        return

    if not new_email:
        print("Keine Mail angegeben.")
        return

    rows = load_report_rows()

    if not rows:
        print("CSV ist leer.")
        return

    rewrites = collect_rewrites(rows, new_email)

    if not rewrites:
        print("Keine Emails zum Ersetzen gefunden.")
        return

    print(f"\nRepos mit Änderungen: {len(rewrites)}")
    print("Diese Emails werden ersetzt:")

    for repo, emails in rewrites.items():
        print(f"\n{repo}")
        for email in sorted(emails):
            print(f"  {email} -> {new_email}")

    confirm = input("\nFortfahren? Tippe YES: ").strip()

    if confirm != "YES":
        print("Abgebrochen.")
        return

    push_confirm = input("Direkt force-pushen? Tippe PUSH oder Enter für nein: ").strip()
    push = push_confirm == "PUSH"

    for repo, old_emails in rewrites.items():
        rewrite_repo(repo, old_emails, new_name, new_email, push)

    print("\nReplace fertig.")

    if not push:
        print("\nZum manuellen Pushen pro Repo:")
        print("cd github-commit-audit/OWNER__REPO.git")
        print("git push origin --force --all")
        print("git push origin --force --tags")


def main():
    print("GitHub Commit Mail Audit/Rewriter")
    print()
    print("1: Scan")
    print("2: Replace")
    print()

    choice = input("Auswahl: ").strip()

    if choice == "1":
        scan()
    elif choice == "2":
        replace()
    else:
        print("Ungültige Auswahl.")


if __name__ == "__main__":
    main()