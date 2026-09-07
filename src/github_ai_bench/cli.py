from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DISCOVERY_SCHEMA = "GITHUB-AI-DISCOVERY-1.0"
MOMENTUM_SCHEMA = "GITHUB-AI-MOMENTUM-1.0"
SHORTLIST_SCHEMA = "GITHUB-AI-SHORTLIST-1.0"
CANDIDATE_SCHEMA = "GITHUB-AI-CANDIDATE-1.0"
RUN_SCHEMA = "GITHUB-AI-REPRODUCTION-RUN-1.0"
RECEIPT_SCHEMA = "GITHUB-AI-REPRODUCTION-RECEIPT-1.0"
API_VERSION_DEFAULT = "2026-03-10"
RUN_STATUSES = {"succeeded", "failed", "blocked"}
HUMAN_VERDICTS = {"adopt", "trial", "reject", "unknown"}
WORKFLOW_HANDOFF_SCHEMA = "GITHUB-AI-EVALUATION-HANDOFF-1.0"
PUBLICATION_SCHEMA = "GITHUB-AI-WORK-PUBLICATION-1.0"
PUBLIC_BUNDLE_SCHEMA = "GITHUB-AI-PUBLIC-BUNDLE-1.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_fields(obj: dict[str, Any], fields: list[str]) -> list[str]:
    return [field for field in fields if obj.get(field) in (None, "", [])]


def normalized_license(repo: dict[str, Any]) -> dict[str, str]:
    license_obj = repo.get("license") if isinstance(repo.get("license"), dict) else {}
    return {
        "key": str(license_obj.get("key") or ""),
        "name": str(license_obj.get("name") or ""),
        "spdx_id": str(license_obj.get("spdx_id") or ""),
    }


class GitHubClient:
    def __init__(self, token: str, api_version: str) -> None:
        self.token = token
        self.api_version = api_version
        self.rate_observations: list[dict[str, str]] = []

    def get(self, path: str, allow_404: bool = False) -> Any:
        url = path if path.startswith("https://") else f"https://api.github.com{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "github-ai-skill-agent-bench/0.1",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                self.rate_observations.append(
                    {
                        "resource": response.headers.get("x-ratelimit-resource", ""),
                        "limit": response.headers.get("x-ratelimit-limit", ""),
                        "remaining": response.headers.get("x-ratelimit-remaining", ""),
                        "reset": response.headers.get("x-ratelimit-reset", ""),
                    }
                )
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            retry_after = exc.headers.get("retry-after", "") if exc.headers else ""
            remaining = exc.headers.get("x-ratelimit-remaining", "") if exc.headers else ""
            reset = exc.headers.get("x-ratelimit-reset", "") if exc.headers else ""
            raise ValueError(
                f"GitHub API {exc.code} for {url}; retry_after={retry_after}; "
                f"remaining={remaining}; reset={reset}"
            ) from exc


def normalize_repo(repo: dict[str, Any], query_id: str) -> dict[str, Any]:
    return {
        "id": repo.get("id"),
        "full_name": repo.get("full_name", ""),
        "html_url": repo.get("html_url", ""),
        "clone_url": repo.get("clone_url", ""),
        "description": repo.get("description") or "",
        "topics": repo.get("topics") or [],
        "language": repo.get("language") or "",
        "stars": int(repo.get("stargazers_count") or 0),
        "forks": int(repo.get("forks_count") or 0),
        "open_issues": int(repo.get("open_issues_count") or 0),
        "created_at": repo.get("created_at", ""),
        "updated_at": repo.get("updated_at", ""),
        "pushed_at": repo.get("pushed_at", ""),
        "archived": bool(repo.get("archived")),
        "disabled": bool(repo.get("disabled")),
        "fork": bool(repo.get("fork")),
        "default_branch": repo.get("default_branch", ""),
        "license": normalized_license(repo),
        "matched_query_ids": [query_id],
    }


def repo_index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = snapshot.get("repositories") if isinstance(snapshot.get("repositories"), list) else []
    return {
        str(row.get("full_name", "")).lower(): row
        for row in rows
        if isinstance(row, dict) and row.get("full_name")
    }


def command_discover(args: argparse.Namespace) -> int:
    config = load_json(Path(args.config))
    queries = config.get("queries") if isinstance(config.get("queries"), list) else []
    if not queries:
        raise ValueError("discovery config requires at least one query")
    token = os.environ.get(args.token_env, "") if args.token_env else ""
    api_version = config.get("github_api_version") or API_VERSION_DEFAULT
    client = GitHubClient(token, api_version)
    repositories: dict[str, dict[str, Any]] = {}
    query_receipts: list[dict[str, Any]] = []

    for query in queries:
        query_id = str(query.get("id") or "")
        query_text = str(query.get("q") or "")
        if not query_id or not query_text:
            raise ValueError("every discovery query requires id and q")
        maximum = min(int(query.get("max_results") or 30), 1000)
        per_page = min(100, maximum)
        fetched = 0
        page = 1
        while fetched < maximum:
            params = urllib.parse.urlencode(
                {
                    "q": query_text,
                    "sort": query.get("sort") or "stars",
                    "order": query.get("order") or "desc",
                    "per_page": min(per_page, maximum - fetched),
                    "page": page,
                }
            )
            payload = client.get(f"/search/repositories?{params}")
            items = payload.get("items") if isinstance(payload, dict) else []
            if not isinstance(items, list) or not items:
                break
            for raw in items:
                normalized = normalize_repo(raw, query_id)
                key = normalized["full_name"].lower()
                if key in repositories:
                    matched = repositories[key]["matched_query_ids"]
                    if query_id not in matched:
                        matched.append(query_id)
                else:
                    repositories[key] = normalized
            fetched += len(items)
            if len(items) < per_page:
                break
            page += 1
        query_receipts.append({"id": query_id, "q": query_text, "fetched": fetched})

    result = {
        "schema": DISCOVERY_SCHEMA,
        "observed_at": utc_now(),
        "github_api_version": api_version,
        "authentication": "token" if token else "anonymous",
        "queries": query_receipts,
        "repositories": sorted(repositories.values(), key=lambda row: (-row["stars"], row["full_name"])),
        "rate_limit_observations": client.rate_observations,
        "claim_boundary": "当前快照只证明检索时点的仓库公开指标，不证明增长因果、可安装或实际好用。",
    }
    write_json(Path(args.output), result)
    return 0


def compare_snapshots(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    if previous.get("schema") != DISCOVERY_SCHEMA or current.get("schema") != DISCOVERY_SCHEMA:
        raise ValueError("both inputs must be GITHUB-AI-DISCOVERY-1.0 snapshots")
    previous_time = parse_time(str(previous["observed_at"]))
    current_time = parse_time(str(current["observed_at"]))
    elapsed_days = (current_time - previous_time).total_seconds() / 86400
    if elapsed_days <= 0:
        raise ValueError("current snapshot must be later than previous snapshot")
    before = repo_index(previous)
    after = repo_index(current)
    rows = []
    for key in sorted(set(before) & set(after)):
        old = before[key]
        new = after[key]
        star_delta = int(new.get("stars", 0)) - int(old.get("stars", 0))
        fork_delta = int(new.get("forks", 0)) - int(old.get("forks", 0))
        rows.append(
            {
                "full_name": new["full_name"],
                "elapsed_days": round(elapsed_days, 6),
                "stars_before": int(old.get("stars", 0)),
                "stars_after": int(new.get("stars", 0)),
                "star_delta": star_delta,
                "stars_per_day": round(star_delta / elapsed_days, 6),
                "fork_delta": fork_delta,
                "trend_observed": True,
            }
        )
    return {
        "schema": MOMENTUM_SCHEMA,
        "previous_observed_at": previous["observed_at"],
        "current_observed_at": current["observed_at"],
        "repositories": sorted(rows, key=lambda row: (-row["stars_per_day"], row["full_name"])),
        "claim_boundary": "增长量只来自两个可比快照；未覆盖的时间段和外部传播原因保持未知。",
    }


def command_compare(args: argparse.Namespace) -> int:
    result = compare_snapshots(load_json(Path(args.previous)), load_json(Path(args.current)))
    write_json(Path(args.output), result)
    return 0


def shortlist_snapshot(
    snapshot: dict[str, Any], config: dict[str, Any], momentum: dict[str, Any] | None = None
) -> dict[str, Any]:
    if snapshot.get("schema") != DISCOVERY_SCHEMA:
        raise ValueError("snapshot must use GITHUB-AI-DISCOVERY-1.0")
    rules = config.get("selection_rules") if isinstance(config.get("selection_rules"), dict) else {}
    min_stars = int(rules.get("min_stars") or 300)
    high_reach_stars = int(rules.get("high_reach_stars") or 5000)
    max_days_since_push = int(rules.get("max_days_since_push") or 180)
    min_velocity = float(rules.get("min_star_velocity_per_day") or 5)
    observed_at = parse_time(str(snapshot["observed_at"]))
    momentum_map: dict[str, dict[str, Any]] = {}
    if momentum:
        if momentum.get("schema") != MOMENTUM_SCHEMA:
            raise ValueError("momentum must use GITHUB-AI-MOMENTUM-1.0")
        momentum_map = {
            row["full_name"].lower(): row
            for row in momentum.get("repositories", [])
            if isinstance(row, dict) and row.get("full_name")
        }

    rows: list[dict[str, Any]] = []
    for repo in snapshot.get("repositories", []):
        if not isinstance(repo, dict):
            continue
        pushed_at = str(repo.get("pushed_at") or "")
        days_since_push = None
        if pushed_at:
            days_since_push = max(0, int((observed_at - parse_time(pushed_at)).total_seconds() // 86400))
        trend = momentum_map.get(str(repo.get("full_name", "")).lower())
        velocity = trend.get("stars_per_day") if trend else None
        reach_signal = "high" if int(repo.get("stars", 0)) >= high_reach_stars else (
            "present" if int(repo.get("stars", 0)) >= min_stars else "below_floor"
        )
        momentum_signal = (
            "observed_fast" if velocity is not None and velocity >= min_velocity else
            "observed_slow" if velocity is not None else "not_observed"
        )
        maintenance_signal = (
            "recent" if days_since_push is not None and days_since_push <= max_days_since_push else "stale_or_unknown"
        )
        license_info = repo.get("license") if isinstance(repo.get("license"), dict) else {}
        spdx = str(license_info.get("spdx_id") or "")
        hard_holds = []
        if repo.get("archived"):
            hard_holds.append("archived")
        if repo.get("disabled"):
            hard_holds.append("disabled")
        if repo.get("fork"):
            hard_holds.append("fork_not_independent_work")
        if not spdx or spdx == "NOASSERTION":
            hard_holds.append("license_unverified")
        market_signal = reach_signal != "below_floor" or momentum_signal == "observed_fast"
        if not hard_holds and market_signal and maintenance_signal == "recent":
            decision = "reproduction_candidate"
        elif market_signal:
            decision = "market_watch"
        else:
            decision = "background"
        rows.append(
            {
                **repo,
                "signals": {
                    "reach": reach_signal,
                    "momentum": momentum_signal,
                    "maintenance": maintenance_signal,
                    "days_since_push": days_since_push,
                    "stars_per_day": velocity,
                },
                "hard_holds": hard_holds,
                "decision": decision,
            }
        )
    order = {"reproduction_candidate": 0, "market_watch": 1, "background": 2}
    rows.sort(key=lambda row: (order[row["decision"]], -int(row.get("stars", 0)), row["full_name"]))
    return {
        "schema": SHORTLIST_SCHEMA,
        "observed_at": snapshot["observed_at"],
        "selection_rules": {
            "min_stars": min_stars,
            "high_reach_stars": high_reach_stars,
            "max_days_since_push": max_days_since_push,
            "min_star_velocity_per_day": min_velocity,
        },
        "repositories": rows,
        "decision_model": "硬门槛加独立信号，不计算可抵消硬伤的总分。",
        "claim_boundary": "入围只表示值得复现；不等于爆款、质量优秀、可安全安装或适合真实业务。",
    }


def command_shortlist(args: argparse.Namespace) -> int:
    momentum = load_json(Path(args.momentum)) if args.momentum else None
    result = shortlist_snapshot(load_json(Path(args.snapshot)), load_json(Path(args.config)), momentum)
    write_json(Path(args.output), result)
    return 0


def validate_repo_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("repo must be OWNER/REPO")
    return value


def command_freeze_candidate(args: argparse.Namespace) -> int:
    full_name = validate_repo_name(args.repo)
    token = os.environ.get(args.token_env, "") if args.token_env else ""
    api_version = args.api_version or API_VERSION_DEFAULT
    client = GitHubClient(token, api_version)
    repo = client.get(f"/repos/{full_name}")
    default_branch = repo.get("default_branch")
    commit = client.get(f"/repos/{full_name}/commits/{urllib.parse.quote(str(default_branch), safe='')}")
    readme = client.get(f"/repos/{full_name}/readme", allow_404=True)
    release = client.get(f"/repos/{full_name}/releases/latest", allow_404=True)
    license_info = normalized_license(repo)
    spdx = license_info.get("spdx_id", "")
    state = "reproduction_candidate" if readme and spdx and spdx != "NOASSERTION" and not repo.get("archived") else "hold"
    result = {
        "schema": CANDIDATE_SCHEMA,
        "observed_at": utc_now(),
        "github_api_version": api_version,
        "full_name": repo.get("full_name"),
        "official_url": repo.get("html_url"),
        "clone_url": repo.get("clone_url"),
        "default_branch": default_branch,
        "head_commit_sha": commit.get("sha") if isinstance(commit, dict) else "",
        "license": license_info,
        "license_boundary": "仓库许可证识别不覆盖依赖项许可证；衍生发布前必须另做依赖与素材权利检查。",
        "readme": {
            "present": bool(readme),
            "path": readme.get("path", "") if isinstance(readme, dict) else "",
            "sha": readme.get("sha", "") if isinstance(readme, dict) else "",
        },
        "latest_release": {
            "present": bool(release),
            "tag_name": release.get("tag_name", "") if isinstance(release, dict) else "",
            "published_at": release.get("published_at", "") if isinstance(release, dict) else "",
        },
        "repository_state": {
            "archived": bool(repo.get("archived")),
            "disabled": bool(repo.get("disabled")),
            "fork": bool(repo.get("fork")),
        },
        "heat_snapshot": {
            "stars": int(repo.get("stargazers_count") or 0),
            "forks": int(repo.get("forks_count") or 0),
            "open_issues": int(repo.get("open_issues_count") or 0),
            "created_at": repo.get("created_at", ""),
            "pushed_at": repo.get("pushed_at", ""),
        },
        "evaluation_state": state,
        "claim_boundary": "冻结候选只证明仓库身份、当前提交和公开元数据；尚未证明安装或任务结果。",
        "synthetic_fixture": False,
        "rate_limit_observations": client.rate_observations,
    }
    write_json(Path(args.output), result)
    return 0 if state == "reproduction_candidate" else 2


def stored_path(path: Path, run_dir: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(run_dir.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def artifact_ref(path_value: str | None, run_dir: Path) -> dict[str, str]:
    if not path_value:
        return {"path": "", "sha256": ""}
    path = Path(path_value)
    if not path.is_file():
        raise ValueError(f"evidence file does not exist: {path}")
    return {"path": stored_path(path, run_dir), "sha256": sha256_file(path)}


def resolve_artifact(ref: dict[str, Any], run_dir: Path) -> Path:
    path = Path(str(ref.get("path") or ""))
    return path if path.is_absolute() else run_dir / path


def artifact_is_valid(ref: Any, run_dir: Path) -> bool:
    if not isinstance(ref, dict) or not ref.get("path") or not ref.get("sha256"):
        return False
    path = resolve_artifact(ref, run_dir)
    return path.is_file() and sha256_file(path) == ref.get("sha256")


def acceptance_review_is_valid(run: dict[str, Any], reproduction: dict[str, Any], run_dir: Path) -> tuple[bool, str]:
    ref = reproduction.get("result_review")
    if not artifact_is_valid(ref, run_dir):
        return False, "result review file is missing or its hash changed"
    review = load_json(resolve_artifact(ref, run_dir))
    if review.get("schema") != "GITHUB-AI-RESULT-REVIEW-1.0":
        return False, "result review schema must be GITHUB-AI-RESULT-REVIEW-1.0"
    case = run.get("case") if isinstance(run.get("case"), dict) else {}
    if review.get("case_id") != case.get("case_id"):
        return False, "result review case_id must match the frozen case"
    expected = {
        item.get("id")
        for item in case.get("acceptance", [])
        if isinstance(item, dict) and item.get("id")
    }
    checks = review.get("checks") if isinstance(review.get("checks"), list) else []
    actual: dict[str, str] = {}
    evidence_ok = True
    for item in checks:
        if not isinstance(item, dict) or not item.get("id"):
            evidence_ok = False
            continue
        actual[str(item["id"])] = str(item.get("status") or "")
        evidence_ok = evidence_ok and bool(item.get("evidence"))
    if set(actual) != expected:
        return False, "result review must cover every frozen acceptance item exactly once"
    if not evidence_ok or any(status not in {"pass", "fail", "not_run"} for status in actual.values()):
        return False, "every acceptance item needs pass/fail/not_run and concrete evidence"
    if review.get("human_verdict") != reproduction.get("human_verdict"):
        return False, "result review and run manifest human verdict must match"
    status = run.get("status")
    if status == "succeeded" and any(value != "pass" for value in actual.values()):
        return False, "a succeeded run requires every acceptance item to pass"
    if status in {"failed", "blocked"} and all(value == "pass" for value in actual.values()):
        return False, "failed or blocked runs must identify at least one failed or unrun acceptance item"
    return True, "result review covers the frozen acceptance contract"


def command_init_run(args: argparse.Namespace) -> int:
    candidate_path = Path(args.candidate)
    case_path = Path(args.case)
    candidate = load_json(candidate_path)
    case = load_json(case_path)
    if candidate.get("schema") != CANDIDATE_SCHEMA:
        raise ValueError("candidate must use GITHUB-AI-CANDIDATE-1.0")
    if candidate.get("evaluation_state") != "reproduction_candidate":
        raise ValueError("candidate is not cleared for reproduction planning")
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    for name in ("raw", "results", "capture"):
        (run_dir / name).mkdir(exist_ok=True)
    run = {
        "schema": RUN_SCHEMA,
        "run_id": args.run_id,
        "created_at": utc_now(),
        "candidate": candidate,
        "candidate_sha256": sha256_file(candidate_path),
        "case": case,
        "case_sha256": sha256_file(case_path),
        "status": "planned",
        "sandbox_policy": {
            "host_install_forbidden": True,
            "preferred": "disposable_vm_or_container",
            "runtime_network_default": "off",
            "secrets_default": "none",
            "source_commit_pinned": candidate.get("head_commit_sha", ""),
        },
        "reproduction": {},
        "public_boundary": "计划已建立，尚未安装或实跑。",
    }
    write_json(run_dir / "EVALUATION-RUN.json", run)
    runbook = f"""# 本次复现运行卡

- 运行编号：`{args.run_id}`
- 仓库：{candidate.get('official_url', '')}
- 固定提交：`{candidate.get('head_commit_sha', '')}`
- 基准任务：{case.get('task', '')}

## 顺序

1. 在无密钥、无个人数据的临时容器或虚拟机中检出固定提交，不在日常工作主机直接安装。
2. 按该提交的官方文档安装，完整保存标准输出和错误输出；不自行“修到能跑”后隐藏偏差。
3. 输入本运行卡绑定的同一份任务材料，保存原始运行日志与实际结果文件。
4. 人工只在结果产生后判断 `adopt / trial / reject`；登录、验证码、付款、授权、外发与发布停止。
5. 用 `record-run` 绑定环境、输入、日志、逐项结果审阅、结果和可选可视证据，再用 `validate-run` 生成结论。

本文件不授权运行不受信任代码；执行前仍需核对容器/虚拟机隔离和项目安装说明。
"""
    write_text(run_dir / "RUNBOOK.md", runbook)
    return 0


def validate_run_data(run: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    checks: list[dict[str, str]] = []

    def add(check_id: str, passed: bool, detail: str) -> None:
        checks.append({"id": check_id, "status": "pass" if passed else "fail", "detail": detail})

    candidate = run.get("candidate") if isinstance(run.get("candidate"), dict) else {}
    reproduction = run.get("reproduction") if isinstance(run.get("reproduction"), dict) else {}
    license_info = candidate.get("license") if isinstance(candidate.get("license"), dict) else {}
    commit = str(candidate.get("head_commit_sha") or "")
    state = candidate.get("repository_state") if isinstance(candidate.get("repository_state"), dict) else {}
    add("run_schema", run.get("schema") == RUN_SCHEMA, "run schema must match")
    add("official_repository", bool(re.fullmatch(r"https://github\.com/[^/]+/[^/]+", str(candidate.get("official_url") or ""))), "official GitHub repository URL is required")
    add("immutable_commit", bool(re.fullmatch(r"[0-9a-fA-F]{40}", commit)), "one 40-character commit SHA is required")
    add("license", bool(license_info.get("spdx_id") and license_info.get("spdx_id") != "NOASSERTION"), "repository license must be identified; dependency licenses remain separate")
    add("repository_state", not any(state.get(key) for key in ("archived", "disabled", "fork")), "archived, disabled, and fork-only candidates remain on hold")
    add("readme", bool(candidate.get("readme", {}).get("present")) if isinstance(candidate.get("readme"), dict) else False, "official setup documentation must be present")
    add("case_lock", bool(run.get("case_sha256") and run.get("case")), "frozen benchmark case and hash are required")
    status = str(run.get("status") or "")
    add("execution_status", status in RUN_STATUSES, "status must be succeeded, failed, or blocked")
    add("environment", artifact_is_valid(reproduction.get("environment"), run_dir), "environment evidence must exist and match its hash")
    add("install_log", artifact_is_valid(reproduction.get("install_log"), run_dir), "unedited install log must exist and match its hash")
    input_ok = artifact_is_valid(reproduction.get("frozen_input"), run_dir)
    if input_ok:
        input_ok = reproduction["frozen_input"].get("sha256") == run.get("case_sha256")
    add("frozen_input", input_ok, "executed input must be the exact frozen benchmark case")
    add("raw_log", artifact_is_valid(reproduction.get("raw_log"), run_dir), "unedited run log must exist and match its hash")
    result_ok = artifact_is_valid(reproduction.get("result"), run_dir)
    add("result", result_ok if status == "succeeded" else True, "a successful run requires one actual result artifact")
    verdict = reproduction.get("human_verdict")
    add("human_verdict", verdict in HUMAN_VERDICTS - {"unknown"}, "a completed evidence chain needs adopt, trial, or reject")
    review_ok, review_detail = acceptance_review_is_valid(run, reproduction, run_dir)
    add("result_review", review_ok, review_detail)
    claim = reproduction.get("claim") if isinstance(reproduction.get("claim"), dict) else {}
    allowed_claims = {
        "succeeded": {"actual_result", "native_run", "applicability_boundary", "observed_limitation"},
        "failed": {"reproduction_record", "applicability_boundary"},
        "blocked": {"reproduction_record", "applicability_boundary"},
    }
    claim_type = claim.get("type")
    add("claim_type", claim_type in allowed_claims.get(status, set()), "claim type must match the observed reproduction state")
    add("claim_text", bool(claim.get("text")), "narrow observed claim text is required")
    capture_valid = artifact_is_valid(reproduction.get("capture"), run_dir)
    capture_qa_valid = artifact_is_valid(reproduction.get("capture_qa"), run_dir)
    if capture_qa_valid:
        qa = load_json(resolve_artifact(reproduction["capture_qa"], run_dir))
        capture_qa_valid = qa.get("status") == "pass"
    add("native_run_capture", claim_type != "native_run" or (capture_valid and capture_qa_valid), "native_run requires a visible capture and passing capture QA")

    passed = all(item["status"] == "pass" for item in checks)
    if not passed:
        decision = "hold"
    elif status == "succeeded":
        decision = "evidence_complete_success"
    elif status == "failed":
        decision = "evidence_complete_failed"
    else:
        decision = "evidence_complete_blocked"
    return {
        "schema": RECEIPT_SCHEMA,
        "checked_at": utc_now(),
        "run_id": run.get("run_id", ""),
        "decision": decision,
        "checks": checks,
        "allowed_public_claim": claim if passed else {"type": "none", "text": "证据链不完整，暂不形成公开实跑结论。"},
    }


def command_record_run(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    run_path = run_dir / "EVALUATION-RUN.json"
    run = load_json(run_path)
    if args.status not in RUN_STATUSES:
        raise ValueError(f"unsupported status: {args.status}")
    if args.human_verdict not in HUMAN_VERDICTS:
        raise ValueError(f"unsupported human verdict: {args.human_verdict}")
    run["status"] = args.status
    run["updated_at"] = utc_now()
    run["reproduction"] = {
        "environment": artifact_ref(args.environment, run_dir),
        "install_log": artifact_ref(args.install_log, run_dir),
        "frozen_input": artifact_ref(args.input, run_dir),
        "raw_log": artifact_ref(args.raw_log, run_dir),
        "result": artifact_ref(args.result, run_dir),
        "result_review": artifact_ref(args.result_review, run_dir),
        "capture": artifact_ref(args.capture, run_dir),
        "capture_qa": artifact_ref(args.capture_qa, run_dir),
        "human_verdict": args.human_verdict,
        "claim": {"type": args.claim_type, "text": args.claim_text},
        "notes": args.notes,
    }
    run["public_boundary"] = "结论仅适用于本仓库固定提交、当前环境和冻结输入。"
    write_json(run_path, run)
    receipt = validate_run_data(run, run_dir)
    write_json(run_dir / "EVALUATION-RECEIPT.json", receipt)
    return 0 if receipt["decision"] != "hold" else 2


def command_validate_run(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    run = load_json(run_dir / "EVALUATION-RUN.json")
    receipt = validate_run_data(run, run_dir)
    if args.output:
        write_json(Path(args.output), receipt)
    else:
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt["decision"] != "hold" else 2


def recommendation(run: dict[str, Any], receipt: dict[str, Any]) -> str:
    if receipt.get("decision") == "hold":
        return "HOLD：先补齐同一提交、环境、输入、日志、结果与人工判断，不能写成实测结论。"
    status = run.get("status")
    verdict = run.get("reproduction", {}).get("human_verdict")
    if status == "failed":
        return "本次复现失败：只报告失败位置，不外推为项目整体缺陷。"
    if status == "blocked":
        return "当前环境受阻：保留适用边界，换环境前不做采用结论。"
    return {
        "adopt": "建议采用：当前任务结果可用；扩大使用前只在公开承诺需要时再做变更输入复跑。",
        "trial": "建议限定试用：当前任务可用，但成本、依赖或人工接管仍需观察。",
        "reject": "不建议用于本题：这是任务适配结论，不是对仓库整体质量的否定。",
    }.get(str(verdict), "HOLD：缺少人工采用判断。")


def render_report(run: dict[str, Any], receipt: dict[str, Any]) -> str:
    candidate = run.get("candidate", {})
    reproduction = run.get("reproduction", {})
    heat = candidate.get("heat_snapshot", {})
    claim = receipt.get("allowed_public_claim", {})
    result_path = reproduction.get("result", {}).get("path", "") if isinstance(reproduction.get("result"), dict) else ""
    capture_path = reproduction.get("capture", {}).get("path", "") if isinstance(reproduction.get("capture"), dict) else ""
    return f"""# GitHub AI Skill / Agent 证据化评测报告

## 一眼结论

- 建议：{recommendation(run, receipt)}
- 证据状态：`{receipt.get('decision', 'hold')}`
- 允许公开的窄结论：`{claim.get('type', 'none')}`｜{claim.get('text', '')}
- 禁止外推：不把一次任务结果写成全项目质量，不把 Star 写成好用，不把 README 演示写成我方实跑。

## 项目身份与热度

- 官方仓库：{candidate.get('official_url', '')}
- 固定提交：`{candidate.get('head_commit_sha', '')}`
- 许可证：`{candidate.get('license', {}).get('spdx_id', '')}`（不自动覆盖依赖许可证）
- 当前快照：Star {heat.get('stars', '未知')}｜Fork {heat.get('forks', '未知')}｜最近推送 {heat.get('pushed_at', '未知')}
- 热度边界：当前数字是时点快照；只有两个同口径快照才能写增长速度。

## 本地复现

- 运行状态：`{run.get('status', 'planned')}`
- 冻结任务：{run.get('case', {}).get('task', '')}
- 人工判断：`{reproduction.get('human_verdict', 'unknown')}`
- 实际结果：{result_path or '未形成'}
- 可视过程：{capture_path or '未记录；因此不得宣称 native_run'}
- 环境与日志：均由 `EVALUATION-RUN.json` 的 SHA256 绑定，明细见 `EVALUATION-RECEIPT.json`。

## 结论与建议

1. 先按“是否解决这一个真实任务”做采用判断，再讨论传播包装；两条证据链互不代替。
2. 一旦标题使用“已安装、已实测、跑通、失败、修复”，必须展示同一提交的输入、操作/日志、结果和人工判断。
3. 只有公开承诺“换一份材料也能跑”时才增加第二输入；不要为了得到漂亮数字无限复跑。
4. 许可证未知、仓库归档、安装要主机管理员权限、需要真实密钥或会外发数据时先 HOLD。
5. 内容可把比较作为 AS-C 评测主体，也可作为 AS-A/AS-B 的证明模块；无发布数据时只称高潜候选，不称爆款。
"""


def command_report(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    run = load_json(run_dir / "EVALUATION-RUN.json")
    receipt = validate_run_data(run, run_dir)
    write_text(Path(args.output), render_report(run, receipt))
    return 0 if receipt["decision"] != "hold" else 2


def command_content_pack(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    run = load_json(run_dir / "EVALUATION-RUN.json")
    receipt = validate_run_data(run, run_dir)
    candidate = run.get("candidate", {})
    heat = candidate.get("heat_snapshot", {})
    name = candidate.get("full_name", "这个项目")
    claim = receipt.get("allowed_public_claim", {})
    content = f"""# 内容候选包（AS-C 评测候选，不承诺流量）

## 点击面

- 标题：GitHub 上 {heat.get('stars', '高热度')} Star 的 {name}，装到本机后值不值得用？
- 封面：高 Star ≠ 好用｜同一任务看真实结果
- 第一画面：左边是仓库热度快照，右边是本次冻结输入和实际结果；没有结果就显示“未完成复现”。

## 0—8 秒证明推进

1. 0—2 秒：给出普通人的具体任务与想得到的交付，不先念功能表。
2. 2—5 秒：显示仓库、许可证、固定提交和隔离安装状态。
3. 5—8 秒：只展示真实产生的结果或明确失败位置，再给人工采用判断。

## 本次可说

- 证据状态：`{receipt.get('decision')}`
- 窄结论：{claim.get('text', '证据不足，暂不形成实跑结论。')}
- 建议：{recommendation(run, receipt)}

## 本次不能说

- Star 多所以质量一定好、一定爆；
- README 或作者演示等于我方实测；
- 一次成功等于所有任务稳定；
- 一次失败等于项目有普遍缺陷；
- 没有可视过程却声称“原生操作全程跑通”。

## 观众可带走

- 同一份冻结测试题；
- 候选发现与趋势快照方法；
- 隔离安装运行卡；
- 哈希绑定的结果报告和采用建议；
- 许可证、安全、密钥、外发与人工接管清单。
"""
    write_text(Path(args.output), content)
    return 0 if receipt["decision"] != "hold" else 2


def command_export_handoff(args: argparse.Namespace) -> int:
    """Export a read-only bridge artifact; never mutate the existing Yang workflow."""
    run_dir = Path(args.run_dir)
    run_path = run_dir / "EVALUATION-RUN.json"
    run = load_json(run_path)
    receipt = validate_run_data(run, run_dir)
    candidate = run.get("candidate") if isinstance(run.get("candidate"), dict) else {}
    reproduction = run.get("reproduction") if isinstance(run.get("reproduction"), dict) else {}
    handoff = {
        "schema": WORKFLOW_HANDOFF_SCHEMA,
        "created_at": utc_now(),
        "run_id": run.get("run_id", ""),
        "evaluation_run_sha256": sha256_file(run_path),
        "evaluation_decision": receipt.get("decision", "hold"),
        "source_identity": {
            "official_repository": candidate.get("official_url", ""),
            "commit": candidate.get("head_commit_sha", ""),
            "license": candidate.get("license", {}).get("spdx_id", "")
            if isinstance(candidate.get("license"), dict)
            else "",
        },
        "public_claim": receipt.get("allowed_public_claim", {}),
        "flow_delivery": {
            "task_delivery": "本次固定任务的评测报告、脱敏示例与人工采用判断",
            "reusable_asset": "可复用的发现、隔离复现、结果审阅和公版打包工作流",
            "handoff": "评测回执 → 现有 FLOW 完整文本 → 现有文本审核与用户批准",
            "human_stop": "公开主张、题库准入、录制、发布仍由现有门禁决定",
        },
        "evidence": {
            "human_verdict": reproduction.get("human_verdict", "unknown"),
            "result_bound": artifact_is_valid(reproduction.get("result"), run_dir),
            "native_capture_bound": artifact_is_valid(reproduction.get("capture"), run_dir),
        },
        "existing_workflow": {
            "requested_account": "FLOW",
            "next_gate": "complete_text_package" if receipt.get("decision") != "hold" else "hold",
            "writes_existing_tables": False,
            "changes_existing_skills": False,
            "creates_approval": False,
            "grants_production_authority": False,
            "grants_publication_authority": False,
        },
        "github_publication": {
            "model": "one_evaluated_work_one_repository",
            "state": "not_packaged",
            "requires": [
                "owned original code",
                "sanitized input and output examples",
                "verified publication rights",
                "exact GitHub owner and repository",
                "current upload authorization",
            ],
        },
    }
    write_json(Path(args.output), handoff)
    return 0 if receipt["decision"] != "hold" else 2


def _manifest_source(value: str, manifest_path: Path) -> Path:
    source = Path(value)
    return source if source.is_absolute() else manifest_path.parent / source


def _safe_bundle_destination(root: Path, relative: str, prefix: str) -> Path:
    normalized = Path(relative.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(f"unsafe public bundle destination: {relative}")
    if not normalized.parts or normalized.parts[0] != prefix:
        raise ValueError(f"public bundle destination must be inside {prefix}/: {relative}")
    destination = (root / normalized).resolve()
    if not destination.is_relative_to(root.resolve()):
        raise ValueError(f"public bundle destination escapes bundle root: {relative}")
    return destination


def _public_scan(root: Path, *, excluded: Path | None = None) -> tuple[list[str], dict[str, str]]:
    patterns = [
        re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
        re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
        re.compile(r"sk-[A-Za-z0-9]{20,}"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(r"[A-Za-z]:\\Users\\", re.IGNORECASE),
        re.compile(r"D:\\kimiwork", re.IGNORECASE),
        re.compile(r"/(?:Users|home)/[^/\s]+/"),
    ]
    hits: list[str] = []
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in {".git", "__pycache__"} for part in path.parts):
            continue
        if excluded and path.resolve() == excluded.resolve():
            continue
        rel = path.relative_to(root).as_posix()
        hashes[rel] = sha256_file(path)
        try:
            value = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in patterns:
            if pattern.search(value):
                hits.append(f"{rel}: {pattern.pattern}")
    return hits, hashes


def _validate_publication_manifest(manifest: dict[str, Any], run: dict[str, Any]) -> None:
    if manifest.get("schema") != PUBLICATION_SCHEMA:
        raise ValueError(f"publication manifest must use {PUBLICATION_SCHEMA}")
    missing = require_fields(
        manifest,
        ["work_id", "repo_name", "title", "summary", "author", "public_task", "source_boundary"],
    )
    if missing:
        raise ValueError(f"publication manifest missing fields: {', '.join(missing)}")
    if manifest.get("work_id") != run.get("run_id"):
        raise ValueError("publication work_id must match the evaluated run_id")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(manifest.get("repo_name") or "")):
        raise ValueError("repo_name may contain only letters, numbers, dot, underscore, and hyphen")
    rights = manifest.get("rights") if isinstance(manifest.get("rights"), dict) else {}
    required_true = [
        "owned_original_code",
        "secrets_removed",
        "personal_data_removed",
    ]
    if any(rights.get(key) is not True for key in required_true):
        raise ValueError("owned code, secret removal, and personal-data removal must be explicitly true")
    forbidden_true = [
        "third_party_source_included",
        "third_party_assets_included",
        "installed_skill_source_included",
        "internal_workflow_material_included",
    ]
    if any(rights.get(key) is not False for key in forbidden_true):
        raise ValueError("third-party source/assets, installed skills, and internal workflow material must be excluded")
    if rights.get("license_review") != "verified":
        raise ValueError("publication license review must be verified")
    if not isinstance(manifest.get("owned_code"), list) or not manifest["owned_code"]:
        raise ValueError("each public work requires at least one owned original code file")
    if not isinstance(manifest.get("public_acceptance"), list) or not manifest["public_acceptance"]:
        raise ValueError("public_acceptance must contain at least one sanitized acceptance item")
    missing_examples = require_fields(manifest, ["sanitized_input", "sanitized_output"])
    if missing_examples:
        raise ValueError(f"publication manifest missing example files: {', '.join(missing_examples)}")


def command_package_work(args: argparse.Namespace) -> int:
    """Build one GitHub-ready public repository without copying upstream source."""
    run_dir = Path(args.run_dir)
    run = load_json(run_dir / "EVALUATION-RUN.json")
    receipt = validate_run_data(run, run_dir)
    if receipt.get("decision") == "hold":
        raise ValueError("evaluation evidence is on HOLD; public repository package is blocked")
    manifest_path = Path(args.manifest)
    manifest = load_json(manifest_path)
    _validate_publication_manifest(manifest, run)

    output = Path(args.output_dir).resolve()
    if output.exists():
        raise ValueError("output directory already exists; choose a new directory to avoid overwriting")
    output.parent.mkdir(parents=True, exist_ok=True)

    sanitized_input = _manifest_source(str(manifest["sanitized_input"]), manifest_path)
    sanitized_output = _manifest_source(str(manifest["sanitized_output"]), manifest_path)
    for label, source in (("sanitized_input", sanitized_input), ("sanitized_output", sanitized_output)):
        if not source.is_file():
            raise ValueError(f"{label} file does not exist: {source}")

    owned_code: list[tuple[Path, str]] = []
    seen_destinations: set[str] = set()
    for item in manifest["owned_code"]:
        if not isinstance(item, dict) or not item.get("source") or not item.get("destination"):
            raise ValueError("each owned_code item requires source and destination")
        source = _manifest_source(str(item["source"]), manifest_path)
        destination = str(item["destination"]).replace("\\", "/")
        if not source.is_file():
            raise ValueError(f"owned code file does not exist: {source}")
        if destination in seen_destinations:
            raise ValueError(f"duplicate public code destination: {destination}")
        seen_destinations.add(destination)
        owned_code.append((source, destination))

    destination = manifest.get("destination") if isinstance(manifest.get("destination"), dict) else {}
    authorization = manifest.get("authorization") if isinstance(manifest.get("authorization"), dict) else {}
    exact_destination = bool(
        destination.get("owner")
        and destination.get("repository") == manifest.get("repo_name")
        and destination.get("visibility") == "public"
    )
    current_authorization = bool(
        authorization.get("upload_authorized") is True and authorization.get("message_ref")
    )
    publication_state = "ready_for_publish" if exact_destination and current_authorization else "draft_ready"

    candidate = run.get("candidate") if isinstance(run.get("candidate"), dict) else {}
    reproduction = run.get("reproduction") if isinstance(run.get("reproduction"), dict) else {}
    claim = receipt.get("allowed_public_claim") if isinstance(receipt.get("allowed_public_claim"), dict) else {}
    stage = Path(tempfile.mkdtemp(prefix="github-ai-public-", dir=str(output.parent)))
    try:
        input_suffix = sanitized_input.suffix or ".txt"
        output_suffix = sanitized_output.suffix or ".txt"
        input_rel = f"examples/input/example{input_suffix}"
        output_rel = f"examples/output/example{output_suffix}"
        input_target = _safe_bundle_destination(stage, input_rel, "examples")
        output_target = _safe_bundle_destination(stage, output_rel, "examples")
        input_target.parent.mkdir(parents=True, exist_ok=True)
        output_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sanitized_input, input_target)
        shutil.copy2(sanitized_output, output_target)

        public_code: list[dict[str, str]] = []
        for source, relative in owned_code:
            target = _safe_bundle_destination(stage, relative, "code")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            public_code.append({"path": relative.replace("\\", "/"), "sha256": sha256_file(target)})

        public_manifest = {
            "schema": PUBLIC_BUNDLE_SCHEMA,
            "work_id": manifest["work_id"],
            "repo_name": manifest["repo_name"],
            "title": manifest["title"],
            "summary": manifest["summary"],
            "publication_state": publication_state,
            "destination": {
                "owner": destination.get("owner", ""),
                "repository": destination.get("repository", ""),
                "visibility": destination.get("visibility", "public"),
            },
            "source": {
                "official_repository": candidate.get("official_url", ""),
                "commit": candidate.get("head_commit_sha", ""),
                "upstream_license": candidate.get("license", {}).get("spdx_id", "")
                if isinstance(candidate.get("license"), dict)
                else "",
                "upstream_source_included": False,
            },
            "evaluation": {
                "decision": receipt.get("decision", "hold"),
                "human_verdict": reproduction.get("human_verdict", "unknown"),
                "allowed_public_claim": claim,
                "boundary": manifest["source_boundary"],
            },
            "examples": {"input": input_rel, "output": output_rel},
            "owned_code": public_code,
            "rights": manifest["rights"],
        }
        write_json(stage / "PUBLICATION-MANIFEST.json", public_manifest)
        write_json(
            stage / "workflow_asset" / "benchmark-case.json",
            {
                "schema": "GITHUB-AI-PUBLIC-BENCHMARK-CASE-1.0",
                "work_id": manifest["work_id"],
                "task": manifest["public_task"],
                "acceptance": manifest["public_acceptance"],
                "sanitized_input": input_rel,
            },
        )
        write_json(stage / "evaluation" / "EVALUATION-RECEIPT.json", receipt)
        write_json(
            stage / "task_delivery" / "claim-boundary.json",
            {
                "schema": "GITHUB-AI-PUBLIC-CLAIM-BOUNDARY-1.0",
                "allowed_public_claim": claim,
                "human_verdict": reproduction.get("human_verdict", "unknown"),
                "source_boundary": manifest["source_boundary"],
                "not_claimed": [
                    "Star proves quality or future traffic",
                    "one run proves universal stability",
                    "upstream source is authored by this repository",
                    "README or upstream demo is our own test",
                ],
            },
        )
        write_text(
            stage / "evaluation" / "report.md",
            f"""# 证据化评测报告

## 一眼结论

- 被评项目：{candidate.get('official_url', '')}
- 固定提交：`{candidate.get('head_commit_sha', '')}`
- 任务：{manifest['public_task']}
- 证据状态：`{receipt.get('decision', 'hold')}`
- 人工判断：`{reproduction.get('human_verdict', 'unknown')}`
- 窄结论：{claim.get('text', '')}

## 可复核材料

- 脱敏输入：`{input_rel}`
- 脱敏实际输出：`{output_rel}`
- 逐项验收：`EVALUATION-RECEIPT.json`
- 原创复用代码：`code/`

## 结论边界

{manifest['source_boundary']}

不把 Star 当质量，不把 README 或作者演示当我方实测，不把一次运行外推为所有任务稳定，也不把一次失败外推为项目整体缺陷。
""",
        )
        write_text(
            stage / "README_zh.md",
            f"""# {manifest['title']}

{manifest['summary']}

## 本仓库包含什么

- 我们原创、可公开复用的最小代码；
- 脱敏后的同任务输入与实际输出示例；
- 固定提交、评测边界、验收项、人工判断与窄结论；
- 不包含被评项目源码、安装到本机的 Skill 源文件、内部工作流门禁或密钥。

## 被评项目与固定版本

- 官方仓库：{candidate.get('official_url', '')}
- 固定提交：`{candidate.get('head_commit_sha', '')}`
- 上游许可证：`{candidate.get('license', {}).get('spdx_id', '') if isinstance(candidate.get('license'), dict) else ''}`
- 上游源码是否打包：否

## 本次评测

- 任务：{manifest['public_task']}
- 证据状态：`{receipt.get('decision', 'hold')}`
- 人工判断：`{reproduction.get('human_verdict', 'unknown')}`
- 允许公开的结论：{claim.get('text', '')}
- 适用边界：{manifest['source_boundary']}

输入见 [`{input_rel}`]({input_rel})，输出见 [`{output_rel}`]({output_rel})，完整结论见 [`evaluation/report.md`](evaluation/report.md)。

## 运行原创示例

```text
{manifest.get('verification_command', '请按 code/ 内说明运行。')}
```

本仓库与被评项目官方无隶属或背书关系。一次固定任务结果不代表所有环境与任务。
""",
        )
        write_text(
            stage / "PROVENANCE.md",
            f"""# 来源与权利边界

- 被评项目：{candidate.get('official_url', '')}
- 固定提交：`{candidate.get('head_commit_sha', '')}`
- 本仓库只发布作者 `{manifest['author']}` 拥有或已获再发布权的原创代码与脱敏示例。
- 被评项目源码、依赖、模型权重、商标、官方截图和已安装 Skill 文件均未复制到本仓库。
- 评测结论只适用于声明的任务、提交、环境和输入。
""",
        )
        write_text(
            stage / "SECURITY.md",
            """# 安全说明

本仓库不包含被评项目源码，也不把第三方安装脚本当作安全代码。若你自行复现上游项目，请使用一次性容器或虚拟机，默认不提供密钥和个人数据，并单独检查依赖、网络外发和许可证。

公开示例已按清单执行密钥、本机绝对路径和个人数据检查；发现问题请停止运行并提交安全报告。
""",
        )
        write_text(
            stage / "LICENSE",
            f"""MIT License

Copyright (c) {datetime.now().year} {manifest['author']}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
""",
        )
        write_text(stage / ".gitignore", "__pycache__/\n*.py[cod]\n.env\n.venv/\n")

        hits, hashes = _public_scan(stage)
        if hits:
            raise ValueError(f"public bundle secret/path scan failed: {'; '.join(hits)}")
        bundle_receipt_path = stage / "PUBLIC-BUNDLE-RECEIPT.json"
        write_json(
            bundle_receipt_path,
            {
                "schema": "GITHUB-AI-PUBLIC-BUNDLE-RECEIPT-1.0",
                "created_at": utc_now(),
                "status": "pass",
                "publication_state": publication_state,
                "secret_and_path_scan": "pass",
                "upstream_source_included": False,
                "existing_workflow_mutated": False,
                "file_hashes_excluding_this_receipt": hashes,
            },
        )
        stage.replace(output)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return 0


def command_verify_bundle(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    output_path = Path(args.output).resolve() if args.output else None
    required = [
        "README_zh.md",
        "LICENSE",
        "SECURITY.md",
        "PROVENANCE.md",
        "workflow_asset/workflow-map.md",
        "workflow_asset/discovery-config.json",
        "workflow_asset/benchmark-case.json",
        "workflow_asset/quality-gate.json",
        "workflow_asset/existing-workflow-bridge.json",
        "workflow_asset/publication-manifest-template.json",
        "docs/existing-workflow-integration.md",
        "task_delivery/engine-design.md",
        "src/github_ai_bench/cli.py",
        "tests/test_cli.py",
    ]
    missing = [item for item in required if not (root / item).is_file()]
    hits, hashes = _public_scan(root, excluded=output_path)
    passed = not missing and not hits
    receipt = {
        "schema": "GITHUB-AI-BUNDLE-VERIFICATION-1.0",
        "checked_at": utc_now(),
        "status": "pass" if passed else "fail",
        "missing_files": missing,
        "secret_scan": {"status": "pass" if not hits else "fail", "hits": hits},
        "file_hashes": hashes,
    }
    if args.output:
        write_json(Path(args.output), receipt)
    else:
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if passed else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="github-ai-bench")
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="collect a timestamped GitHub repository snapshot")
    discover.add_argument("--config", required=True)
    discover.add_argument("--output", required=True)
    discover.add_argument("--token-env", default="GITHUB_TOKEN")
    discover.set_defaults(func=command_discover)

    compare = sub.add_parser("compare", help="compare two snapshots without inventing trend data")
    compare.add_argument("--previous", required=True)
    compare.add_argument("--current", required=True)
    compare.add_argument("--output", required=True)
    compare.set_defaults(func=command_compare)

    shortlist = sub.add_parser("shortlist", help="apply hard gates and independent heat signals")
    shortlist.add_argument("--snapshot", required=True)
    shortlist.add_argument("--config", required=True)
    shortlist.add_argument("--momentum")
    shortlist.add_argument("--output", required=True)
    shortlist.set_defaults(func=command_shortlist)

    freeze = sub.add_parser("freeze-candidate", help="freeze repository identity, license and commit")
    freeze.add_argument("--repo", required=True)
    freeze.add_argument("--output", required=True)
    freeze.add_argument("--token-env", default="GITHUB_TOKEN")
    freeze.add_argument("--api-version", default=API_VERSION_DEFAULT)
    freeze.set_defaults(func=command_freeze_candidate)

    init = sub.add_parser("init-run", help="create an isolated reproduction run package")
    init.add_argument("--candidate", required=True)
    init.add_argument("--case", required=True)
    init.add_argument("--run-dir", required=True)
    init.add_argument("--run-id", required=True)
    init.set_defaults(func=command_init_run)

    record = sub.add_parser("record-run", help="hash-bind actual install and run evidence")
    record.add_argument("--run-dir", required=True)
    record.add_argument("--status", required=True, choices=sorted(RUN_STATUSES))
    record.add_argument("--environment", required=True)
    record.add_argument("--install-log", required=True)
    record.add_argument("--input", required=True)
    record.add_argument("--raw-log", required=True)
    record.add_argument("--result")
    record.add_argument("--result-review", required=True)
    record.add_argument("--capture")
    record.add_argument("--capture-qa")
    record.add_argument("--human-verdict", required=True, choices=sorted(HUMAN_VERDICTS))
    record.add_argument("--claim-type", required=True)
    record.add_argument("--claim-text", required=True)
    record.add_argument("--notes", default="")
    record.set_defaults(func=command_record_run)

    validate = sub.add_parser("validate-run", help="validate one same-run evidence chain")
    validate.add_argument("--run-dir", required=True)
    validate.add_argument("--output")
    validate.set_defaults(func=command_validate_run)

    report = sub.add_parser("report", help="render a conclusion and recommendation report")
    report.add_argument("--run-dir", required=True)
    report.add_argument("--output", required=True)
    report.set_defaults(func=command_report)

    content = sub.add_parser("content-pack", help="render a truthful AS-C content candidate")
    content.add_argument("--run-dir", required=True)
    content.add_argument("--output", required=True)
    content.set_defaults(func=command_content_pack)

    handoff = sub.add_parser("export-handoff", help="export a read-only bridge to the existing FLOW workflow")
    handoff.add_argument("--run-dir", required=True)
    handoff.add_argument("--output", required=True)
    handoff.set_defaults(func=command_export_handoff)

    package = sub.add_parser("package-work", help="build one sanitized GitHub-ready repository per evaluated work")
    package.add_argument("--run-dir", required=True)
    package.add_argument("--manifest", required=True)
    package.add_argument("--output-dir", required=True)
    package.set_defaults(func=command_package_work)

    verify = sub.add_parser("verify-bundle", help="verify required files and scan for secrets")
    verify.add_argument("--root", required=True)
    verify.add_argument("--output")
    verify.set_defaults(func=command_verify_bundle)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
