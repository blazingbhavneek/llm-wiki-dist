import unittest

import httpx

from growi_client import GrowiAPIError, GrowiSearchClient

ID = "507f1f77bcf86cd799439011"
OTHER = "507f1f77bcf86cd799439012"


def client_for(handler, token="tok", root="/Moove"):
    return GrowiSearchClient("http://growi.test", token, root_path=root, transport=httpx.MockTransport(handler))


def page_doc(pid=ID, path="/Moove/Manual", **extra):
    return {"_id": pid, "path": path, "revision": "rev1", "updatedAt": "2024-01-01", **extra}


def wrapped_search():
    return {
        "meta": {"total": 1, "took": 3, "hitsCount": 1},
        "data": [
            {
                "data": page_doc(),
                "meta": {"elasticSearchResult": {"snippet": "<em class=\"highlighted-keyword\">系統制御</em>ミドルウェア", "highlightedPath": "/Moove/Manual"}},
            }
        ],
    }


class Auth(unittest.TestCase):
    def test_bearer_header(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"ok": True})

        client_for(handler, token="sekret").health()
        self.assertEqual(seen["auth"], "Bearer sekret")

    def test_api_token_prefix_stripped(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"ok": True})

        client_for(handler, token="API Token: abc123").health()
        self.assertEqual(seen["auth"], "Bearer abc123")


class Search(unittest.TestCase):
    def test_params_encoded(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["q"] = request.url.params["q"]
            return httpx.Response(200, json=wrapped_search())

        client_for(handler).search_pages("系統 制御", path="/Moove", limit=5)
        self.assertIn("/_api/search?", seen["url"])
        self.assertEqual(seen["q"], "系統 制御")  # decoded back identically
        self.assertIn("limit=5", seen["url"])

    def test_wrapped_and_flat_normalize_identically(self):
        wrapped = client_for(lambda r: httpx.Response(200, json=wrapped_search()))
        flat = client_for(lambda r: httpx.Response(200, json=[page_doc()]))
        hits_w = wrapped.search_pages("q", path="/Moove", limit=5)
        hits_f = flat.search_pages("q", path="/Moove", limit=5)
        self.assertEqual(hits_w[0].page.path, hits_f[0].page.path)
        self.assertEqual(hits_w[0].page.id, ID)
        self.assertEqual(hits_f[0].snippet, "")

    def test_highlight_stripped_japanese_survives(self):
        client = client_for(lambda r: httpx.Response(200, json=wrapped_search()))
        hits = client.search_pages("q", path="/Moove", limit=5)
        self.assertEqual(hits[0].snippet, "系統制御ミドルウェア")

    def test_root_filter_rejects_out_of_scope(self):
        payload = {"data": [{"data": page_doc(pid=OTHER, path="/user/nope")}]}
        client = client_for(lambda r: httpx.Response(200, json=payload))
        self.assertEqual(client.search_pages("q", path="/", limit=5), [])

    def test_deleted_hits_skipped(self):
        payload = {"data": [{"data": page_doc(status="deleted")}]}
        client = client_for(lambda r: httpx.Response(200, json=payload))
        self.assertEqual(client.search_pages("q", path="/Moove", limit=5), [])


class Page(unittest.TestCase):
    def test_body_from_revision(self):
        payload = {"page": {**page_doc(), "revision": {"_id": "rev9", "body": "本文です"}}}
        client = client_for(lambda r: httpx.Response(200, json=payload))
        page = client.get_page(page_id=ID)
        self.assertEqual(page.body, "本文です")
        self.assertEqual(page.revision_id, "rev9")

    def test_exactly_one_selector(self):
        client = client_for(lambda r: httpx.Response(200, json={}))
        with self.assertRaises(ValueError):
            client.get_page()
        with self.assertRaises(ValueError):
            client.get_page(page_id=ID, path="/Moove/Manual")

    def test_404_returns_none(self):
        client = client_for(lambda r: httpx.Response(404, json={"errors": [{"code": "page-not-found"}]}))
        self.assertIsNone(client.get_page(page_id=ID))

    def test_out_of_scope_returns_none(self):
        payload = {"page": {**page_doc(path="/user/x"), "revision": {"_id": "r", "body": ""}}}
        client = client_for(lambda r: httpx.Response(200, json=payload))
        self.assertIsNone(client.get_page(page_id=ID))


class Errors(unittest.TestCase):
    def test_auth_failures_typed(self):
        for status in (401, 403):
            client = client_for(lambda r, s=status: httpx.Response(s, json={}))
            with self.assertRaises(GrowiAPIError) as ctx:
                client.get_page(page_id=ID)
            self.assertEqual(ctx.exception.status_code, status)

    def test_timeout_status_zero(self):
        def boom(request):
            raise httpx.ConnectTimeout("boom")

        with self.assertRaises(GrowiAPIError) as ctx:
            client_for(boom).get_page(page_id=ID)
        self.assertEqual(ctx.exception.status_code, 0)

    def test_malformed_json_typed(self):
        client = client_for(lambda r: httpx.Response(200, text="<html>SPA</html>"))
        with self.assertRaises(GrowiAPIError) as ctx:
            client.get_page(page_id=ID)
        self.assertIn("not JSON", str(ctx.exception))


class Children(unittest.TestCase):
    def test_one_level_only(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"children": [{"_id": OTHER, "path": "/Moove/Manual/Child", "parent": ID, "descendantCount": 0, "isEmpty": False}]})

        client = client_for(handler)
        kids = client.list_children(path="/Moove/Manual")
        self.assertEqual(len(kids), 1)
        self.assertEqual(kids[0].id, OTHER)
        self.assertEqual(len(calls), 1)  # no grandchild requests
        self.assertIn("/_api/v3/page-listing/children", calls[0])


if __name__ == "__main__":
    unittest.main()
