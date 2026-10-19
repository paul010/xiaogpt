import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    "home_services", Path(__file__).parents[1] / "deployment/install_user_services.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class UserServiceTests(unittest.TestCase):
    def test_paths_are_quoted_and_user_specifiers_cannot_expand(self):
        unit = module.render_unit(
            Path("/tmp/a b%h"), Path("/tmp/env/bin/python"), Path("/tmp/cfg")
        )
        self.assertIn("WorkingDirectory=/tmp/a b%%h", unit)
        self.assertIn('"/tmp/cfg/%i.yaml"', unit)
        self.assertIn("EnvironmentFile=-/tmp/cfg/common.env", unit)
        self.assertIn("XIAOGPT_MI_TOKEN_PATH=/tmp/cfg/%i.mi.token", unit)
        self.assertNotIn("OPENAI_API_KEY=", unit)
        self.assertIn("Restart=on-failure", unit)

    def test_multiline_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            module.quote("/tmp/config\nExecStart=/bin/false")


if __name__ == "__main__":
    unittest.main()
