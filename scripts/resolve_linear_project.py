"""One-shot helper: find the Linear "CIO ARs" project and print the correct
LINEAR_PROJECT_ID. Run after rotating LINEAR_API_KEY.

Usage:
  .venv/Scripts/python.exe scripts/resolve_linear_project.py
  .venv/Scripts/python.exe scripts/resolve_linear_project.py --name "CIO ARs"
  .venv/Scripts/python.exe scripts/resolve_linear_project.py --write   # patches .env in place
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from haven import config


async def search_projects(name_substr: str) -> list[dict]:
    headers = {"Authorization": config.LINEAR_API_KEY, "Content-Type": "application/json"}
    query = """
    query ($needle: String!) {
      projects(first: 50, filter: { name: { containsIgnoreCase: $needle } }) {
        nodes {
          id name url state
          teams { nodes { id key name } }
        }
      }
    }
    """
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "https://api.linear.app/graphql",
            headers=headers,
            json={"query": query, "variables": {"needle": name_substr}},
        )
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(f"Linear errors: {data['errors']}")
    hits: list[dict] = []
    for p in data["data"]["projects"]["nodes"]:
        teams = p.get("teams", {}).get("nodes") or []
        first_team = teams[0] if teams else {"key": "?", "name": "?", "id": "?"}
        hits.append({
            **p,
            "team_key": first_team.get("key"),
            "team_name": first_team.get("name"),
            "team_id": first_team.get("id"),
        })
    return hits


def patch_env(new_id: str) -> None:
    env_path = ROOT / ".env"
    text = env_path.read_text()
    if re.search(r"^LINEAR_PROJECT_ID=", text, flags=re.M):
        text = re.sub(r"^LINEAR_PROJECT_ID=.*$", f"LINEAR_PROJECT_ID={new_id}", text, flags=re.M)
    else:
        text = text.rstrip() + f"\nLINEAR_PROJECT_ID={new_id}\n"
    env_path.write_text(text)
    print(f"\n.env updated: LINEAR_PROJECT_ID={new_id}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="CIO ARs", help="Project name substring (default: 'CIO ARs')")
    ap.add_argument("--write", action="store_true", help="Patch .env with the resolved ID")
    args = ap.parse_args()

    if not config.LINEAR_API_KEY:
        print("LINEAR_API_KEY missing in .env"); sys.exit(2)

    print(f"Searching projects matching: {args.name!r}")
    hits = await search_projects(args.name)
    if not hits:
        print("No projects matched. The API key may not have access — re-issue it logged in as the user who can see CIO ARs.")
        sys.exit(1)

    print()
    for h in hits:
        print(f"  {h['id']}  team={h['team_key']:>6}  state={h.get('state','?'):>10}  {h['name']}")
        print(f"     url: {h['url']}")

    if len(hits) > 1:
        print("\nMultiple matches — narrow with --name")
        sys.exit(1)

    chosen = hits[0]
    print(f"\nResolved: {chosen['name']} -> {chosen['id']}")
    if args.write:
        patch_env(chosen["id"])


if __name__ == "__main__":
    asyncio.run(main())
