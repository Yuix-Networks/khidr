#!/usr/bin/env python3
"""
Memory Analyzer for Git Changes
Analyzes git changes and saves important ones to ProjectMemory model
"""

import os
import sys

_proj_root = os.environ.get("KHIDR_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _proj_root)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "khidr.settings")
os.environ.setdefault("PROJECT_ROOT", _proj_root)

import django
django.setup()

import json
import subprocess
import requests
from datetime import datetime
from typing import List, Dict, Any

from core.models import Project, ProjectMemory

MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL = "https://api.minimax.io/v1"
PROJECT_NAME = os.environ.get("PROJECT_NAME", "khidr")

IMPORTANCE_KEYWORDS = [
    "important", "critical", "bug fix", "security", "breaking",
    "api change", "database migration", "config change",
    "new feature", "enhancement", "performance", "refactor",
    "architecture", "deployment", "infrastructure", "security fix",
    "memory leak", "crash", "hang", "deadlock"
]

def summarize_with_minimax(diff_text: str, commit_message: str) -> str:
    """Summarize git changes using MiniMax M2.5"""
    prompt = f"""Analyze this git commit and provide a concise summary for project memory:

Commit Message: {commit_message}

Changes:
{diff_text[:3000]}

Provide a 2-3 sentence summary explaining what was changed and why it matters for future development."""

    try:
        response = requests.post(
            f"{MINIMAX_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {MINIMAX_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "MiniMax-M2.5",
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 500
            },
            timeout=30
        )
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]
        else:
            print(f"MiniMax API error: {response.status_code}", file=sys.stderr)
            return f"{commit_message} - {diff_text[:500]}"
    except Exception as e:
        print(f"Error calling MiniMax: {e}", file=sys.stderr)
        return f"{commit_message} - {diff_text[:500]}"

def calculate_importance_score(diff_text: str, commit_message: str) -> float:
    """Calculate importance score based on keywords and changes"""
    score = 0.3

    combined_text = (commit_message + " " + diff_text).lower()

    for keyword in IMPORTANCE_KEYWORDS:
        if keyword in combined_text:
            score += 0.15

    if len(diff_text) > 2000:
        score += 0.2

    if any(x in combined_text for x in ["feat:", "fix:", "hotfix:", "security:"]):
        score += 0.3

    if "refactor" in combined_text or "chore" in combined_text:
        score -= 0.1

    return min(max(score, 0.0), 1.0)

def get_git_changes() -> List[Dict[str, Any]]:
    """Get recent git changes"""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-10"],
            capture_output=True,
            text=True,
            cwd=os.environ.get("PROJECT_ROOT", ".")
        )
        commits = result.stdout.strip().split("\n")

        changes = []
        for commit in commits:
            if not commit:
                continue
            commit_hash = commit.split()[0]

            diff_result = subprocess.run(
                ["git", "diff", f"{commit_hash}^..{commit_hash}", "--stat"],
                capture_output=True,
                text=True
            )

            msg_result = subprocess.run(
                ["git", "log", "-1", "--format=%B", commit_hash],
                capture_output=True,
                text=True
            )

            changes.append({
                "hash": commit_hash,
                "message": msg_result.stdout.strip(),
                "stats": diff_result.stdout.strip()
            })

        return changes
    except Exception as e:
        print(f"Error getting git changes: {e}", file=sys.stderr)
        return []

def save_to_project_memory(project: Project, commit_hash: str, message: str, summary: str, stats: str, score: float) -> bool:
    """Save important memory to ProjectMemory model"""
    try:
        importance = "critical" if score >= 0.9 else "high" if score >= 0.8 else "medium"

        ProjectMemory.objects.create(
            project=project,
            content=f"**Commit:** {commit_hash}\n**Message:** {message}\n\n**Summary:**\n{summary}\n\n**Changes:**\n{stats}",
            importance=importance,
        )
        print(f"Saved to ProjectMemory: {commit_hash}")
        return True
    except Exception as e:
        print(f"Error saving to ProjectMemory: {e}", file=sys.stderr)
        return False

def analyze_and_save():
    """Main function to analyze changes and save important ones"""
    try:
        project = Project.objects.filter(slug=PROJECT_NAME).first()
        if not project:
            print(f"Project not found: {PROJECT_NAME}")
            return
    except Exception as e:
        print(f"Error finding project: {e}", file=sys.stderr)
        return

    changes = get_git_changes()

    for change in changes:
        score = calculate_importance_score(change["stats"], change["message"])

        if score >= 0.7:
            summary = summarize_with_minimax(change["stats"], change["message"])
            save_to_project_memory(
                project=project,
                commit_hash=change["hash"],
                message=change["message"],
                summary=summary,
                stats=change["stats"],
                score=score
            )
            print(f"Saved important change: {change['hash']} - {change['message'][:50]}")

if __name__ == "__main__":
    analyze_and_save()