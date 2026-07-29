import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "scorebench"
SKILL = SKILL_DIR / "SKILL.md"
README = ROOT / "README.md"


class SkillContractTests(unittest.TestCase):
    def test_core_skill_stays_concise_and_has_valid_frontmatter(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertLessEqual(len(text.splitlines()), 500)
        self.assertTrue(text.startswith("---\nname: scorebench\n"))
        self.assertIn("description:", text.split("---\n", 2)[1])

    def test_codex_interface_metadata_exists(self):
        text = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('display_name: "Scorebench"', text)
        self.assertIn("$scorebench", text)

    def test_referenced_markdown_files_exist(self):
        text = SKILL.read_text(encoding="utf-8")
        references = set(re.findall(r"`(references/[^`]+\.md)`", text))
        self.assertGreaterEqual(len(references), 6)
        for relative in references:
            with self.subTest(reference=relative):
                self.assertTrue((SKILL_DIR / relative).is_file())

    def test_worker_hard_gates_remain_in_core_skill(self):
        text = SKILL.read_text(encoding="utf-8")
        required = (
            "Required Installation Gate",
            "scorebench run ping --event start",
            "Every submission requires an exact, run-relative token total",
            "--prompt-file",
            "Do not use an external venue CLI, API, cookie, or credential",
            "scorebench invalidate",
            "scorebench reinstate",
            "scorebench run usage",
        )
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_docs_do_not_recommend_fabricated_usage_or_stale_commands(self):
        markdown = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [README, *sorted((SKILL_DIR / "references").glob("*.md"))]
        )
        self.assertNotIn("--tokens-total-source agent_claim", markdown)
        self.assertNotIn("`harness solutions`", markdown)
        self.assertNotIn("public report's embedded `report-data`", markdown)

    def test_readme_makes_skill_installation_separate_and_mandatory(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("Install the skill (required)", text)
        self.assertIn("Installing the `scorebench` CLI does **not** install the skill", text)
        self.assertIn("Use exact run-relative tokens", text)

    def test_bootstrap_is_deployment_first_with_explicit_refresh(self):
        text = (SKILL_DIR / "scripts" / "install_scorebench_cli.sh").read_text(
            encoding="utf-8"
        )
        self.assertLess(text.index('INSTALL_URL="${'), text.index('local_repo="${'))
        self.assertIn("SCOREBENCH_CLI_FORCE", text)
        self.assertIn("SCOREBENCH_CLI_CHECKOUT", text)

    def test_clean_room_installs_current_cli_and_skill(self):
        text = (SKILL_DIR / "references" / "clean-room-docker.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("https://scorebench.dev/install.sh", text)
        self.assertIn("COPY skills/scorebench", text)
        self.assertIn(".codex/skills/scorebench", text)
        self.assertIn(".claude/skills/scorebench", text)

    def test_tmux_launchers_require_installed_skills_and_current_env_names(self):
        text = (SKILL_DIR / "references" / "tmux-goal-sessions.md").read_text(
            encoding="utf-8"
        )
        self.assertIn('test -f "$HOME/.claude/skills/scorebench/SKILL.md"', text)
        self.assertIn(
            'test -f "${CODEX_HOME:-$HOME/.codex}/skills/scorebench/SKILL.md"',
            text,
        )
        self.assertIn("SCOREBENCH_RUN_TOKEN", text)
        self.assertEqual(text.count("token=\"$(field '.token.token')\""), 2)
        self.assertNotIn("--add-dir '$SKILL_DIR'", text)


if __name__ == "__main__":
    unittest.main()
