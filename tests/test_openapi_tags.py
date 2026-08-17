"""OpenAPI tag coverage: every endpoint belongs to a named tag group.

Guards against new endpoints silently landing in Swagger UI's ``default``
bucket — every path operation in the generated schema must carry at least
one tag.
"""

from __future__ import annotations

import unittest

from covalent.api.app import create_app


class OpenApiTagTests(unittest.TestCase):
    def test_every_path_operation_is_tagged(self):
        schema = create_app().openapi()
        untagged = [
            f"{method.upper()} {path}"
            for path, ops in schema["paths"].items()
            for method, spec in ops.items()
            if method in {"get", "post", "put", "patch", "delete"} and not spec.get("tags")
        ]
        self.assertNotEqual({}, schema["paths"])
        self.assertEqual([], untagged)


if __name__ == "__main__":
    unittest.main()
