import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "uninstall.sh"

# A stateful Docker double keeps destructive commands away from the host daemon.
FAKE_DOCKER = r'''
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
path = Path(os.environ["DOCKER_TEST_STATE"])
state = json.loads(path.read_text())
with Path(os.environ["DOCKER_TEST_LOG"]).open("a") as log:
    log.write(json.dumps(args) + "\n")

def finish(code=0, message=""):
    if message:
        print(message, file=sys.stderr if code else sys.stdout)
    path.write_text(json.dumps(state))
    sys.exit(code)

for prefix in state.get("fail", []):
    if args[:len(prefix)] == prefix:
        finish(1, "simulated Docker failure")

def selected(resources):
    query = args[args.index("--filter") + 1] if "--filter" in args else ""
    for name, resource in resources.items():
        if query.startswith("label=com.docker.compose.project="):
            if resource.get("project") != query.split("=", 2)[2]:
                continue
        elif query.startswith("name=") and query[5:] not in name:
            continue
        elif query.startswith("ancestor=") and resource["image"] != query[9:]:
            continue
        yield name, resource

if args == ["info"]:
    finish(0 if state.get("running", True) else 1)
elif args[0] == "ps":
    finish(message="\n".join(name for name, _ in selected(state["containers"])))
elif args[0] == "inspect":
    container = state["containers"][args[-1]]
    finish(message=container["ref"] + " " + container["image"])
elif args[0] == "rm":
    del state["containers"][args[-1]]
    finish()
elif args[0] in ("network", "volume"):
    key = args[0] + "s"
    if args[1] == "ls":
        finish(message="\n".join(name for name, _ in selected(state[key])))
    if args[1] == "inspect":
        finish(message=state[key][args[-1]].get("project", ""))
    if args[1] == "rm":
        name = args[-1]
        if any(name in container.get(key, []) for container in state["containers"].values()):
            finish(1, "resource is in use")
        del state[key][name]
        finish()
elif args[:2] == ["image", "ls"]:
    lines = []
    for image_id, image in selected(state["images"]):
        if "-q" in args:
            lines.append(image_id)
        else:
            lines.extend(image_id + " " + tag for tag in image["tags"] or ["<none>:<none>"])
    finish(message="\n".join(lines))
elif args[:2] == ["image", "rm"]:
    target = args[-1]
    for image_id, image in list(state["images"].items()):
        if target != image_id and target not in image["tags"]:
            continue
        if any(container["image"] == image_id for container in state["containers"].values()):
            finish(1, "image is in use")
        if target == image_id:
            if len(image["tags"]) > 1:
                finish(1, "image has multiple repository tags")
            del state["images"][image_id]
        else:
            image["tags"].remove(target)
            if not image["tags"]:
                del state["images"][image_id]
        finish()
    finish(1, "image not found")
finish(1, "unexpected Docker command: " + repr(args))
'''


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        docker = bin_dir / "docker"
        docker.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER)
        docker.chmod(0o755)
        self.state_path = self.root / "state.json"
        self.log_path = self.root / "docker.log"
        self.env = {
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
            "DOCKER_TEST_STATE": str(self.state_path),
            "DOCKER_TEST_LOG": str(self.log_path),
        }
        self.state = {
            "containers": {
                "inbox": {"project": "email-agent", "image": "sha256:app", "ref": "email-agent-inbox:local"},
                "postgres": {"project": "email-agent", "image": "sha256:db", "ref": "postgres:16-alpine",
                             "volumes": ["email-agent_postgres_data"], "networks": ["email-agent_default"]},
                "foreign": {"project": "other", "image": "sha256:other", "ref": "other:latest"},
            },
            "networks": {"email-agent_default": {"project": "email-agent"}, "other_default": {"project": "other"}},
            "volumes": {
                "email-agent_postgres_data": {"project": "email-agent"},
                "email-agent_cache": {"project": "email-agent"},
                "other_data": {"project": "other"},
                "email-agent_postgres_data_backup": {},
            },
            "images": {
                "sha256:app": {"project": "email-agent", "tags": ["email-agent-inbox:local"]},
                "sha256:db": {"tags": ["postgres:16-alpine"]},
                "sha256:old": {"project": "email-agent", "tags": []},
                "sha256:other": {"tags": ["other:latest"]},
            },
        }

    def run_uninstall(self, *args):
        self.state_path.write_text(json.dumps(self.state))
        result = subprocess.run(
            ["sh", "-s", "--", *args], input=SCRIPT.read_text(),
            text=True, capture_output=True, env=self.env, cwd=self.root, timeout=30,
        )
        self.state = json.loads(self.state_path.read_text())
        return result

    def calls(self):
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def make_install_dir(self):
        folder = self.root / "email agent"
        (folder / "scripts").mkdir(parents=True)
        for name in ("docker-compose.yml", "deploy.sh", "scripts/main.py", ".docker.env"):
            (folder / name).write_text("test fixture")
        return folder

    def test_removes_project_resources_and_leaves_other_projects(self):
        result = self.run_uninstall("--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(self.state["containers"]), {"foreign"})
        self.assertEqual(set(self.state["networks"]), {"other_default"})
        self.assertEqual(
            set(self.state["volumes"]),
            {
                "email-agent_postgres_data",
                "email-agent_cache",
                "other_data",
                "email-agent_postgres_data_backup",
            },
        )
        self.assertEqual(set(self.state["images"]), {"sha256:other"})
        self.assertIn("保留数据卷: email-agent_postgres_data", result.stdout)
        self.assertIn("卸载完成", result.stdout)

    def test_repeat_uninstall_succeeds_when_resources_are_absent(self):
        self.assertEqual(self.run_uninstall("--yes").returncode, 0)
        result = self.run_uninstall("--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(self.state["images"]), {"sha256:other"})

    def test_removes_images_after_compose_stop(self):
        self.state["containers"] = {"foreign": self.state["containers"]["foreign"]}
        self.state["networks"].pop("email-agent_default")
        result = self.run_uninstall("--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(self.state["images"]), {"sha256:other"})

    def test_keeps_images_used_by_other_containers_including_stopped_ones(self):
        initial_state = copy.deepcopy(self.state)
        for image_id in ("sha256:db", "sha256:app", "sha256:old"):
            with self.subTest(image=image_id):
                self.state = copy.deepcopy(initial_state)
                self.state["containers"]["foreign"]["image"] = image_id
                result = self.run_uninstall("--yes")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(image_id, self.state["images"])
                self.assertIn("保留镜像", result.stdout)

    def test_removes_old_container_images_after_tag_changes(self):
        self.state["containers"]["inbox"]["image"] = "sha256:previous"
        self.state["containers"]["inbox"]["ref"] = "email-agent-inbox:previous"
        self.state["images"]["sha256:previous"] = {"tags": ["email-agent-inbox:previous"]}
        result = self.run_uninstall("--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("sha256:previous", self.state["images"])

    def test_keeps_unlabelled_legacy_volume(self):
        self.state["volumes"]["email-agent_postgres_data"] = {}
        result = self.run_uninstall("--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("email-agent_postgres_data", self.state["volumes"])
        self.assertIn("保留数据卷: email-agent_postgres_data", result.stdout)

    def test_keeps_legacy_named_volume_owned_by_another_project(self):
        self.state["volumes"]["email-agent_postgres_data"] = {"project": "other"}
        result = self.run_uninstall("--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("email-agent_postgres_data", self.state["volumes"])
        self.assertIn("保留数据卷", result.stdout)

    def test_custom_project_does_not_delete_default_project_resources(self):
        self.state["containers"]["custom"] = {"project": "custom", "image": "sha256:custom", "ref": "custom:latest"}
        self.state["images"]["sha256:custom"] = {"tags": ["custom:latest"]}
        self.state["volumes"]["custom_postgres_data"] = {"project": "custom"}
        self.state["networks"]["custom_default"] = {"project": "custom"}
        result = self.run_uninstall("--yes", "--project", "custom")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("custom", self.state["containers"])
        self.assertIn("custom_postgres_data", self.state["volumes"])
        self.assertNotIn("custom_default", self.state["networks"])
        self.assertNotIn("sha256:custom", self.state["images"])
        self.assertIn("inbox", self.state["containers"])
        self.assertIn("email-agent_postgres_data", self.state["volumes"])
        self.assertIn("sha256:app", self.state["images"])

    def test_install_directory_is_retained_by_default(self):
        folder = self.make_install_dir()
        result = self.run_uninstall("--yes", "--dir", str(folder))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((folder / ".docker.env").exists())

    def test_remove_directory_accepts_spaces_and_repeat_runs(self):
        folder = self.make_install_dir()
        for _ in range(2):
            result = self.run_uninstall("--yes", "--remove-dir", "--dir", str(folder))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(folder.exists())

    def test_refuses_invalid_or_system_directory_before_docker_calls(self):
        for folder in ("", "/", "/tmp/..", str(self.root)):
            with self.subTest(folder=folder):
                result = self.run_uninstall("--yes", "--remove-dir", "--dir", folder)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])
        self.assertTrue(self.root.exists())

    def test_refuses_symbolic_link_even_with_trailing_slash(self):
        folder = self.make_install_dir()
        link = self.root / "link"
        link.symlink_to(folder, target_is_directory=True)
        result = self.run_uninstall("--yes", "--remove-dir", "--dir", str(link) + "/")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertTrue(folder.exists())

    def test_refuses_home_directory_even_with_project_files(self):
        folder = self.make_install_dir()
        self.env["HOME"] = str(folder)
        result = self.run_uninstall("--yes", "--remove-dir", "--dir", str(folder))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertTrue(folder.exists())

    def test_delete_failures_return_error_and_preserve_install_directory(self):
        folder = self.make_install_dir()
        initial_state = copy.deepcopy(self.state)
        for failure in (["rm"], ["network", "rm"], ["image", "rm"]):
            with self.subTest(failure=failure):
                self.state = copy.deepcopy(initial_state)
                self.state["fail"] = [failure]
                result = self.run_uninstall("--yes", "--remove-dir", "--dir", str(folder))
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("卸载完成", result.stdout)
                self.assertIn("删除失败", result.stderr)
                self.assertTrue(folder.exists())

    def test_remove_volumes_deletes_only_when_requested(self):
        result = self.run_uninstall("--yes", "--remove-volumes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(self.state["volumes"]), {"other_data", "email-agent_postgres_data_backup"})
        self.assertIn("已删除数据卷: email-agent_postgres_data", result.stdout)

    def test_volume_delete_failure_preserves_install_directory(self):
        folder = self.make_install_dir()
        self.state["fail"] = [["volume", "rm"]]
        result = self.run_uninstall("--yes", "--remove-volumes", "--remove-dir", "--dir", str(folder))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("卸载完成", result.stdout)
        self.assertIn("删除失败", result.stderr)
        self.assertTrue(folder.exists())
        self.assertIn("email-agent_postgres_data", self.state["volumes"])

    def test_query_failure_prevents_resource_deletion(self):
        self.state["fail"] = [["volume", "ls"]]
        result = self.run_uninstall("--yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(set(self.state["containers"]), {"inbox", "postgres", "foreign"})
        self.assertFalse(any("rm" in call for call in self.calls()))

    def test_docker_unavailable_does_not_remove_install_directory(self):
        folder = self.make_install_dir()
        self.state["running"] = False
        result = self.run_uninstall("--yes", "--remove-dir", "--dir", str(folder))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(folder.exists())
        self.assertEqual(self.calls(), [["info"]])

    def test_confirmation_and_argument_validation_happen_before_docker_calls(self):
        for args in ((), ("--yes", "--unknown"), ("--yes", "--project"), ("--yes", "--dir"),
                     ("--yes", "--project", "../other"), ("--yes", "--project", "*")):
            with self.subTest(args=args):
                result = self.run_uninstall(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])

    def test_help_works_without_docker_and_preserves_line_continuation(self):
        result = self.run_uninstall("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" | \\\n", result.stdout)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
