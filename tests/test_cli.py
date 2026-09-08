from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from github_ai_bench.cli import (  # noqa: E402
    CANDIDATE_SCHEMA,
    DISCOVERY_SCHEMA,
    RUN_SCHEMA,
    artifact_ref,
    command_export_handoff,
    command_package_work,
    compare_snapshots,
    render_report,
    sha256_file,
    shortlist_snapshot,
    validate_editorial_review,
    validate_run_data,
    write_json,
)


def repo_row(name: str, *, stars: int, license_id: str = "MIT") -> dict:
    return {
        "id": stars,
        "full_name": name,
        "html_url": f"https://github.com/{name}",
        "clone_url": f"https://github.com/{name}.git",
        "description": "fixture",
        "topics": ["ai-agent"],
        "language": "Python",
        "stars": stars,
        "forks": 10,
        "open_issues": 2,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-09-06T00:00:00Z",
        "pushed_at": "2026-09-05T00:00:00Z",
        "archived": False,
        "disabled": False,
        "fork": False,
        "default_branch": "main",
        "license": {"key": license_id.lower(), "name": license_id, "spdx_id": license_id},
        "matched_query_ids": ["fixture"],
    }


class SnapshotTests(unittest.TestCase):
    def test_compare_uses_two_snapshots(self) -> None:
        previous = {
            "schema": DISCOVERY_SCHEMA,
            "observed_at": "2026-09-01T00:00:00+00:00",
            "repositories": [repo_row("demo/agent", stars=100)],
        }
        current = {
            "schema": DISCOVERY_SCHEMA,
            "observed_at": "2026-09-06T00:00:00+00:00",
            "repositories": [repo_row("demo/agent", stars=150)],
        }
        result = compare_snapshots(previous, current)
        self.assertEqual(result["repositories"][0]["star_delta"], 50)
        self.assertEqual(result["repositories"][0]["stars_per_day"], 10)

    def test_shortlist_keeps_license_as_hard_gate(self) -> None:
        licensed = repo_row("demo/licensed", stars=600)
        unknown = repo_row("demo/unknown", stars=9000, license_id="")
        snapshot = {
            "schema": DISCOVERY_SCHEMA,
            "observed_at": "2026-09-07T00:00:00+00:00",
            "repositories": [licensed, unknown],
        }
        config = {
            "selection_rules": {
                "min_stars": 300,
                "high_reach_stars": 5000,
                "max_days_since_push": 180,
                "min_star_velocity_per_day": 5,
            }
        }
        result = shortlist_snapshot(snapshot, config)
        by_name = {row["full_name"]: row for row in result["repositories"]}
        self.assertEqual(by_name["demo/licensed"]["decision"], "reproduction_candidate")
        self.assertEqual(by_name["demo/unknown"]["decision"], "market_watch")
        self.assertIn("license_unverified", by_name["demo/unknown"]["hard_holds"])


class EditorialReviewTests(unittest.TestCase):
    def review(self) -> dict:
        return {
            "schema": "GITHUB-AI-CANDIDATE-EDITORIAL-REVIEW-2.0",
            "repository": "demo/skill",
            "ordinary_user_task": "把一篇冻结原稿改得更自然",
            "desired_delivery": "改写稿、事实核对和人工判断",
            "skill_or_agent_contribution": "提供明确的风格检查规则",
            "content_mode": "AS-A_with_comparison",
            "novelty_check": {
                "topic_library_checked": True,
                "history_library_checked": True,
                "produced_media_checked": True,
                "closest_existing_work": "none",
                "material_difference": "same-task factual retention plus blind verdict",
            },
            "account_fit": {
                "route": "USE",
                "account_name": "老杨用AI",
                "audience": "经常写工作稿但讨厌模板腔的人",
                "tone_fit": "真实对照、直接判断、不神化工具",
                "other_account_failure_reason": "只有一次改写与判断，没有跨阶段交接",
                "public_signal": "同一原稿前后对照并给出能用/需改结论",
                "required_chain": {
                    "named_object": "demo/skill",
                    "bounded_task": "改写一篇冻结原稿",
                    "visible_result": "前后稿和事实差异",
                    "human_verdict": "能用/需改/别用",
                },
            },
            "viral_logic": {
                "pre_screen_state": "pass",
                "dominant_engine": "same-task reversal",
                "click_promise": "改自然但不改事实",
                "cover_payoff": "改前/改后",
                "first_five_seconds": "同屏出现最明显的一组句子变化",
                "visible_proof": "差异和事实核对同屏",
                "takeaway": "公开测试输入和核对表",
                "share_or_save_reason": "写作者可以复用核对方法",
                "truth_boundary": "只报告本次中文输入",
            },
            "feasibility": {
                "run_route": "controlled_minimal_run",
                "complexity": "low",
                "isolated_environment": True,
                "secrets_prohibited": True,
                "representative_input": "原创中文稿",
                "acceptance": "事实不变且盲读认为更自然",
                "stop_condition": "安装失败或事实改变立即停止",
                "cost_guard": "只跑一个输入",
                "max_runs": 1,
            },
            "formal_viral_review": {"status": "not_started", "artifact_path": "", "artifact_sha256": ""},
            "public_claim": "待实跑；不得公开称已安装、已跑通或已实测",
            "production_approved": False,
            "decision": "ready_for_isolated_run",
        }

    def test_candidate_screen_can_pass_without_faking_formal_review(self) -> None:
        receipt = validate_editorial_review(self.review())
        self.assertEqual(receipt["status"], "pass")
        self.assertEqual(receipt["formal_viral_review_status"], "not_started")

    def test_formal_pass_requires_hash_bound_artifact(self) -> None:
        review = self.review()
        review["formal_viral_review"]["status"] = "passed"
        receipt = validate_editorial_review(review)
        self.assertEqual(receipt["status"], "fail")
        failed = {item["id"] for item in receipt["checks"] if item["status"] == "fail"}
        self.assertIn("formal_review_evidence", failed)

    def test_ready_decision_rejects_conditional_viral_screen(self) -> None:
        review = self.review()
        review["viral_logic"]["pre_screen_state"] = "conditional"
        receipt = validate_editorial_review(review)
        self.assertEqual(receipt["status"], "fail")
        failed = {item["id"] for item in receipt["checks"] if item["status"] == "fail"}
        self.assertIn("decision_consistency", failed)


class RunValidationTests(unittest.TestCase):
    def build_run(self, run_dir: Path, claim_type: str = "actual_result") -> dict:
        case_path = run_dir / "case.json"
        environment = run_dir / "raw" / "environment.json"
        install_log = run_dir / "raw" / "install.log"
        raw_log = run_dir / "raw" / "run.log"
        result_path = run_dir / "results" / "result.json"
        result_review_path = run_dir / "results" / "result-review.json"
        environment.parent.mkdir(parents=True)
        result_path.parent.mkdir(parents=True)
        case = {
            "schema": "GITHUB-AI-BENCHMARK-CASE-1.0",
            "case_id": "FIXTURE-CASE-001",
            "task": "fixture task",
            "acceptance": [{"id": "delivery", "text": "one delivery exists"}],
        }
        write_json(case_path, case)
        write_json(environment, {"os": "fixture", "runtime": "fixture"})
        install_log.write_text("install ok\n", encoding="utf-8")
        raw_log.write_text("run ok\n", encoding="utf-8")
        write_json(result_path, {"delivery": "fixture result"})
        write_json(
            result_review_path,
            {
                "schema": "GITHUB-AI-RESULT-REVIEW-1.0",
                "case_id": "FIXTURE-CASE-001",
                "human_verdict": "trial",
                "checks": [
                    {"id": "delivery", "status": "pass", "evidence": "fixture result contains delivery"}
                ],
            },
        )
        candidate = {
            "schema": CANDIDATE_SCHEMA,
            "official_url": "https://github.com/demo/agent",
            "full_name": "demo/agent",
            "head_commit_sha": "a" * 40,
            "license": {"spdx_id": "MIT"},
            "readme": {"present": True},
            "repository_state": {"archived": False, "disabled": False, "fork": False},
            "heat_snapshot": {"stars": 1234, "forks": 40, "pushed_at": "2026-09-05T00:00:00Z"},
            "synthetic_fixture": True,
        }
        return {
            "schema": RUN_SCHEMA,
            "run_id": "FIXTURE-001",
            "candidate": candidate,
            "case": case,
            "case_sha256": sha256_file(case_path),
            "status": "succeeded",
            "reproduction": {
                "environment": artifact_ref(str(environment), run_dir),
                "install_log": artifact_ref(str(install_log), run_dir),
                "frozen_input": artifact_ref(str(case_path), run_dir),
                "raw_log": artifact_ref(str(raw_log), run_dir),
                "result": artifact_ref(str(result_path), run_dir),
                "result_review": artifact_ref(str(result_review_path), run_dir),
                "capture": {"path": "", "sha256": ""},
                "capture_qa": {"path": "", "sha256": ""},
                "human_verdict": "trial",
                "claim": {"type": claim_type, "text": "fixture scope only"},
            },
        }

    def test_complete_actual_result_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            run = self.build_run(run_dir)
            receipt = validate_run_data(run, run_dir)
            self.assertEqual(receipt["decision"], "evidence_complete_success")

    def test_native_run_requires_capture_and_qa(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            run = self.build_run(run_dir, claim_type="native_run")
            receipt = validate_run_data(run, run_dir)
            self.assertEqual(receipt["decision"], "hold")
            failed = {item["id"] for item in receipt["checks"] if item["status"] == "fail"}
            self.assertIn("native_run_capture", failed)

    def test_success_cannot_hide_failed_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            run = self.build_run(run_dir)
            review_path = run_dir / "results" / "result-review.json"
            write_json(
                review_path,
                {
                    "schema": "GITHUB-AI-RESULT-REVIEW-1.0",
                    "case_id": "FIXTURE-CASE-001",
                    "human_verdict": "trial",
                    "checks": [
                        {"id": "delivery", "status": "fail", "evidence": "delivery is unusable"}
                    ],
                },
            )
            run["reproduction"]["result_review"] = artifact_ref(str(review_path), run_dir)
            receipt = validate_run_data(run, run_dir)
            self.assertEqual(receipt["decision"], "hold")
            failed = {item["id"] for item in receipt["checks"] if item["status"] == "fail"}
            self.assertIn("result_review", failed)

    def test_report_preserves_narrow_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            run = self.build_run(run_dir)
            receipt = validate_run_data(run, run_dir)
            report = render_report(run, receipt)
            self.assertIn("不把 Star 写成好用", report)
            self.assertIn("fixture scope only", report)
            self.assertNotIn("一定爆款", report)

    def publication_manifest(self, root: Path) -> dict:
        source_dir = root / "publication-source"
        source_dir.mkdir()
        write_json(source_dir / "input.json", {"request": "sanitized fixture"})
        write_json(source_dir / "output.json", {"delivery": "sanitized fixture result"})
        (source_dir / "example.py").write_text("print('fixture')\n", encoding="utf-8")
        return {
            "schema": "GITHUB-AI-WORK-PUBLICATION-1.0",
            "work_id": "FIXTURE-001",
            "repo_name": "ai-skill-lab-fixture",
            "title": "Fixture public work",
            "summary": "A sanitized fixture bundle.",
            "author": "Fixture Author",
            "public_task": "Produce one sanitized fixture delivery.",
            "public_acceptance": [{"id": "delivery", "text": "delivery is inspectable"}],
            "source_boundary": "Fixture commit, environment, and input only.",
            "sanitized_input": "input.json",
            "sanitized_output": "output.json",
            "owned_code": [{"source": "example.py", "destination": "code/example.py"}],
            "verification_command": "python code/example.py",
            "rights": {
                "owned_original_code": True,
                "third_party_source_included": False,
                "third_party_assets_included": False,
                "installed_skill_source_included": False,
                "internal_workflow_material_included": False,
                "license_review": "verified",
                "secrets_removed": True,
                "personal_data_removed": True,
            },
            "destination": {"owner": "", "repository": "", "visibility": "public"},
            "authorization": {"upload_authorized": False, "message_ref": ""},
        }

    def test_export_handoff_is_read_only_and_preserves_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run = self.build_run(root)
            run_path = root / "EVALUATION-RUN.json"
            output = root / "FLOW-HANDOFF.json"
            write_json(run_path, run)
            before = sha256_file(run_path)
            result = command_export_handoff(SimpleNamespace(run_dir=str(root), output=str(output)))
            handoff = __import__("json").loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result, 0)
            self.assertEqual(before, sha256_file(run_path))
            self.assertEqual(handoff["existing_workflow"]["next_gate"], "complete_text_package")
            self.assertFalse(handoff["existing_workflow"]["writes_existing_tables"])
            self.assertFalse(handoff["existing_workflow"]["grants_production_authority"])

    def test_package_work_builds_one_sanitized_draft_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            run_dir.mkdir()
            run = self.build_run(run_dir)
            write_json(run_dir / "EVALUATION-RUN.json", run)
            source_dir = root / "publication-source"
            manifest = self.publication_manifest(root)
            manifest_path = source_dir / "PUBLICATION.json"
            write_json(manifest_path, manifest)
            output = root / "public-work"
            result = command_package_work(
                SimpleNamespace(run_dir=str(run_dir), manifest=str(manifest_path), output_dir=str(output))
            )
            public_manifest = __import__("json").loads(
                (output / "PUBLICATION-MANIFEST.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result, 0)
            self.assertEqual(public_manifest["publication_state"], "draft_ready")
            self.assertFalse(public_manifest["source"]["upstream_source_included"])
            self.assertTrue((output / "code" / "example.py").is_file())
            self.assertTrue((output / "examples" / "output" / "example.json").is_file())
            self.assertEqual(
                __import__("json").loads(
                    (output / "PUBLIC-BUNDLE-RECEIPT.json").read_text(encoding="utf-8")
                )["status"],
                "pass",
            )

    def test_package_work_rejects_third_party_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "run"
            run_dir.mkdir()
            run = self.build_run(run_dir)
            write_json(run_dir / "EVALUATION-RUN.json", run)
            source_dir = root / "publication-source"
            manifest = self.publication_manifest(root)
            manifest["rights"]["third_party_source_included"] = True
            manifest_path = source_dir / "PUBLICATION.json"
            write_json(manifest_path, manifest)
            with self.assertRaises(ValueError):
                command_package_work(
                    SimpleNamespace(
                        run_dir=str(run_dir),
                        manifest=str(manifest_path),
                        output_dir=str(root / "public-work"),
                    )
                )


if __name__ == "__main__":
    unittest.main()
