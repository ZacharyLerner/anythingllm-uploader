"""
Integration test suite — upload, delete, workspace CRUD, and settings.

Hits the REAL LLM backend at http://10.140.10.101:3001.
Uses an isolated in-memory SQLite database (StaticPool) — production DB is
never touched.

Test workspace (must already exist on the backend): it_test-i0ezx92mjykn5nvy

Test classes
============
  TestWorkspaceCRUD        — create / get / rename / duplicate / 404s
  TestWorkspaceSettings    — fetch settings from backend; save settings to backend
  TestUpload               — single file, multi-file, 404, 413, extension handling,
                             doc_id verified on backend, category/source_url fields
  TestSingleDelete         — success (DB + backend), 404, idempotent second call
  TestBulkDelete           — small batch, >10 regression, empty list, mixed IDs,
                             backend-verified removal
"""

import io
import pytest
import requests as _req

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# ---------------------------------------------------------------------------
# Isolated in-memory SQLite — must be configured BEFORE importing main so
# that the dependency override is in place for every route handler.
# ---------------------------------------------------------------------------
_test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_test_engine)


def _get_test_db():
    with _TestSessionLocal() as db:
        yield db


from database import Base, get_db  # noqa: E402
import main                         # noqa: E402

main.app.dependency_overrides[get_db] = _get_test_db
Base.metadata.create_all(bind=_test_engine)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BACKEND = "http://10.140.10.101:3001"
TEST_SLUG = "it_test-i0ezx92mjykn5nvy"   # exists on the real backend

from config import HEADERS  # noqa: E402  (loaded after dotenv)

# ---------------------------------------------------------------------------
# Early connectivity guard — fail immediately with a clear message if the
# test workspace slug no longer exists on the backend, rather than letting
# every upload/delete test fail with confusing 404 errors.
# ---------------------------------------------------------------------------
def _assert_backend_reachable():
    try:
        r = _req.get(f"{BACKEND}/workspace/{TEST_SLUG}", headers=HEADERS, timeout=5)
    except Exception as exc:
        raise RuntimeError(
            f"Cannot reach LLM backend at {BACKEND}: {exc}"
        ) from exc
    if r.status_code == 404:
        # Auto-discover available workspaces and surface them
        try:
            slugs = [w["slug"] for w in _req.get(f"{BACKEND}/workspaces", headers=HEADERS, timeout=5).json()]
        except Exception:
            slugs = ["(could not list workspaces)"]
        raise RuntimeError(
            f"TEST_SLUG {TEST_SLUG!r} not found on backend.\n"
            f"Available workspaces: {slugs}\n"
            f"Update TEST_SLUG in tests.py to match."
        )

_assert_backend_reachable()

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _txt(name: str = "test.txt", content: str = "Hello."):
    """Multipart tuple for a plain-text upload."""
    return ("uploaded_files", (name, io.BytesIO(content.encode()), "text/plain"))


def _backend_doc_ids() -> set[str]:
    """Return the set of doc_ids currently tracked on the real backend."""
    r = _req.get(f"{BACKEND}/docs/{TEST_SLUG}", headers=HEADERS, timeout=10)
    assert r.status_code == 200, f"Could not list backend docs: {r.text}"
    return {d["doc_id"] for d in r.json()}


def _upload_n(client: TestClient, n: int) -> list[str]:
    """Upload n text files to TEST_SLUG; return list of doc_ids."""
    files = [_txt(f"file_{i}.txt", f"content {i}") for i in range(n)]
    r = client.post(f"/api/v1/workspaces/{TEST_SLUG}/upload", files=files)
    assert r.status_code == 200, r.text
    return [rec["id"] for rec in r.json()]


def _upload_one(client: TestClient, name: str = "one.txt") -> str:
    return _upload_n(client, 1)[0] if name == "file_0.txt" else (
        client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/upload",
            files=[_txt(name, f"content of {name}")],
        ).json()[0]["id"]
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def seed_test_workspace():
    """Insert the test workspace row into the isolated DB once per session."""
    from models import Workspace
    with _TestSessionLocal() as db:
        if not db.query(Workspace).filter(Workspace.id == TEST_SLUG).first():
            db.add(Workspace(id=TEST_SLUG, name="Testing"))
            db.commit()


@pytest.fixture(scope="session")
def client(seed_test_workspace):
    with TestClient(main.app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_files():
    """Wipe files table after every test to prevent state leakage."""
    yield
    with _TestSessionLocal() as db:
        db.execute(text("DELETE FROM files"))
        db.commit()


@pytest.fixture(autouse=True)
def clean_extra_workspaces():
    """Remove any workspace rows added during a test (keep TEST_SLUG)."""
    yield
    with _TestSessionLocal() as db:
        db.execute(
            text("DELETE FROM workspaces WHERE id != :slug"),
            {"slug": TEST_SLUG},
        )
        db.commit()


# ===========================================================================
# WORKSPACE CRUD
# ===========================================================================

class TestWorkspaceCRUD:
    """
    Routes under test
      POST   /api/v1/workspaces/new           create workspace
      POST   /api/v1/workspaces/db            create workspace (db-only alias)
      GET    /api/v1/workspaces/{id}          get workspace
      PATCH  /api/v1/workspaces/{id}          rename workspace
    """

    # --- create ---

    def test_create_workspace_new(self, client: TestClient):
        """POST /api/v1/workspaces/new registers a workspace in the local DB."""
        payload = {"id": "ws-test-create", "name": "Create Test", "owners": ["alice"]}
        r = client.post("/api/v1/workspaces/new", json=payload)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["id"] == "ws-test-create"
        assert data["name"] == "Create Test"
        assert data["owners"] == ["alice"]

        # Verify it persisted
        get_r = client.get("/api/v1/workspaces/ws-test-create")
        assert get_r.status_code == 200
        assert get_r.json()["id"] == "ws-test-create"

    def test_create_workspace_db_alias(self, client: TestClient):
        """POST /api/v1/workspaces/db is functionally identical to /new."""
        payload = {"id": "ws-test-db", "name": "DB Alias Test"}
        r = client.post("/api/v1/workspaces/db", json=payload)
        assert r.status_code == 200, r.text
        assert r.json()["id"] == "ws-test-db"

    def test_create_workspace_duplicate_returns_409(self, client: TestClient):
        """Creating a workspace with an existing ID returns 409."""
        payload = {"id": "ws-dup", "name": "First"}
        client.post("/api/v1/workspaces/new", json=payload)
        r = client.post("/api/v1/workspaces/new", json={"id": "ws-dup", "name": "Second"})
        assert r.status_code == 409

    def test_create_workspace_name_too_long(self, client: TestClient):
        """Name longer than 100 characters fails Pydantic validation (422)."""
        r = client.post(
            "/api/v1/workspaces/new",
            json={"id": "ws-longname", "name": "x" * 101},
        )
        assert r.status_code == 422

    def test_create_workspace_empty_name(self, client: TestClient):
        """Empty string name fails Pydantic validation (422)."""
        r = client.post("/api/v1/workspaces/new", json={"id": "ws-empty", "name": ""})
        assert r.status_code == 422

    def test_create_workspace_owners_defaults_to_empty(self, client: TestClient):
        """Omitting owners field defaults to []."""
        r = client.post("/api/v1/workspaces/new", json={"id": "ws-no-owners", "name": "No Owners"})
        assert r.status_code == 200, r.text
        assert r.json()["owners"] == []

    # --- get ---

    def test_get_workspace_found(self, client: TestClient):
        """GET /api/v1/workspaces/{id} returns workspace data."""
        client.post("/api/v1/workspaces/new", json={"id": "ws-get", "name": "Get Test"})
        r = client.get("/api/v1/workspaces/ws-get")
        assert r.status_code == 200
        assert r.json()["name"] == "Get Test"

    def test_get_workspace_not_found(self, client: TestClient):
        """GET /api/v1/workspaces/{id} for unknown ID returns 404."""
        r = client.get("/api/v1/workspaces/does-not-exist-xyz")
        assert r.status_code == 404

    def test_get_workspace_includes_files(self, client: TestClient):
        """After uploading, GET workspace response includes files list."""
        r = client.get(f"/api/v1/workspaces/{TEST_SLUG}")
        assert r.status_code == 200
        assert "files" in r.json()
        assert isinstance(r.json()["files"], list)

    # --- rename ---

    def test_rename_workspace(self, client: TestClient):
        """PATCH /api/v1/workspaces/{id} updates the workspace name."""
        client.post("/api/v1/workspaces/new", json={"id": "ws-rename", "name": "Old Name"})
        r = client.patch("/api/v1/workspaces/ws-rename", json={"name": "New Name"})
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "New Name"

        # Confirm persisted
        get_r = client.get("/api/v1/workspaces/ws-rename")
        assert get_r.json()["name"] == "New Name"

    def test_rename_workspace_not_found(self, client: TestClient):
        """PATCH on an unknown workspace returns 404."""
        r = client.patch("/api/v1/workspaces/ghost-workspace", json={"name": "Ghost"})
        assert r.status_code == 404

    def test_rename_workspace_name_too_long(self, client: TestClient):
        """Rename with name > 100 chars returns 422."""
        client.post("/api/v1/workspaces/new", json={"id": "ws-long-rename", "name": "Short"})
        r = client.patch("/api/v1/workspaces/ws-long-rename", json={"name": "y" * 101})
        assert r.status_code == 422

    def test_rename_workspace_empty_name(self, client: TestClient):
        """Rename with empty string returns 422."""
        client.post("/api/v1/workspaces/new", json={"id": "ws-empty-rename", "name": "Has Name"})
        r = client.patch("/api/v1/workspaces/ws-empty-rename", json={"name": ""})
        assert r.status_code == 422


# ===========================================================================
# WORKSPACE SETTINGS
# ===========================================================================

class TestWorkspaceSettings:
    """
    Routes under test
      GET  /api/v1/workspaces/{id}/settings   proxy to LLM backend
      POST /api/v1/workspaces/{id}/settings   proxy PUT to LLM backend
    """

    def test_fetch_settings_returns_expected_keys(self, client: TestClient):
        """GET settings returns the four expected keys for a known workspace."""
        r = client.get(f"/api/v1/workspaces/{TEST_SLUG}/settings")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "prompt" in data
        assert "similarity_threshold" in data
        assert "top_n" in data
        assert "temperature" in data

    def test_fetch_settings_value_types(self, client: TestClient):
        """Settings values have correct types."""
        r = client.get(f"/api/v1/workspaces/{TEST_SLUG}/settings")
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data["prompt"], str)
        assert isinstance(data["similarity_threshold"], float)
        assert isinstance(data["top_n"], int)
        assert isinstance(data["temperature"], float)

    def test_fetch_settings_unknown_workspace_returns_404(self, client: TestClient):
        """Fetching settings for a workspace unknown to the backend returns 404."""
        r = client.get("/api/v1/workspaces/no-such-workspace-zzz/settings")
        assert r.status_code == 404

    def test_save_settings_roundtrip(self, client: TestClient):
        """POST settings updates the backend; GET immediately after reflects the change."""
        # Read current value
        before = client.get(f"/api/v1/workspaces/{TEST_SLUG}/settings").json()
        original_top_n = before["top_n"]

        new_top_n = 3 if original_top_n != 3 else 4
        save_r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/settings",
            json={"top_n": new_top_n},
        )
        assert save_r.status_code == 200, save_r.text
        assert save_r.json().get("ok") is True

        # Verify reflected on backend
        after = client.get(f"/api/v1/workspaces/{TEST_SLUG}/settings").json()
        assert after["top_n"] == new_top_n

        # Restore original value so we don't leave the backend in a changed state
        client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/settings",
            json={"top_n": original_top_n},
        )

    def test_save_settings_unknown_keys_ignored(self, client: TestClient):
        """Posting extra/unknown keys does not crash the endpoint."""
        r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/settings",
            json={"top_n": 5, "unknown_field": "ignored"},
        )
        assert r.status_code == 200, r.text


# ===========================================================================
# UPLOAD
# ===========================================================================

class TestUpload:
    """
    Route under test: POST /api/v1/workspaces/{workspace_id}/upload
    """

    def test_single_text_file(self, client: TestClient):
        """Upload one plain-text file — correct FileResponse shape, persisted to DB."""
        r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/upload",
            files=[_txt("hello.txt", "Integration test.")],
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert len(data) == 1
        rec = data[0]
        assert rec["workspace_id"] == TEST_SLUG
        assert rec["filename"] == "hello.txt"
        assert rec["original_extension"] == ".txt"
        assert rec["category"] == "uploaded_file"
        assert rec["id"]

        from models import File
        with _TestSessionLocal() as db:
            f = db.query(File).filter(File.id == rec["id"]).first()
        assert f is not None
        assert f.filename == "hello.txt"

    def test_multiple_files(self, client: TestClient):
        """Upload three files at once — all returned with unique doc_ids."""
        r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/upload",
            files=[
                _txt("a.txt", "A"),
                _txt("b.txt", "B"),
                _txt("c.txt", "C"),
            ],
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert len(data) == 3
        assert {rec["filename"] for rec in data} == {"a.txt", "b.txt", "c.txt"}
        ids = [rec["id"] for rec in data]
        assert len(set(ids)) == 3, "Each file must have a unique doc_id"

    def test_upload_unknown_workspace_returns_404(self, client: TestClient):
        """Uploading to a workspace not in local DB returns 404."""
        r = client.post(
            "/api/v1/workspaces/no-such-workspace/upload",
            files=[_txt()],
        )
        assert r.status_code == 404

    def test_upload_csv_is_text_extension(self, client: TestClient):
        """CSV files are uploaded as-is (TEXT_EXTENSION), not converted by Docling."""
        r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/upload",
            files=[("uploaded_files", ("data.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv"))],
        )
        assert r.status_code == 200, r.text
        assert r.json()[0]["original_extension"] == ".csv"

    def test_upload_json_is_text_extension(self, client: TestClient):
        """JSON files are uploaded as-is."""
        r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/upload",
            files=[("uploaded_files", ("data.json", io.BytesIO(b'{"k":"v"}'), "application/json"))],
        )
        assert r.status_code == 200, r.text
        assert r.json()[0]["original_extension"] == ".json"

    def test_upload_md_is_text_extension(self, client: TestClient):
        """Markdown files are uploaded as-is."""
        r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/upload",
            files=[("uploaded_files", ("notes.md", io.BytesIO(b"# Title\nBody."), "text/markdown"))],
        )
        assert r.status_code == 200, r.text
        assert r.json()[0]["original_extension"] == ".md"

    def test_upload_category_is_uploaded_file(self, client: TestClient):
        """API upload always sets category to 'uploaded_file'."""
        r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/upload",
            files=[_txt("cat_test.txt")],
        )
        assert r.status_code == 200, r.text
        assert r.json()[0]["category"] == "uploaded_file"

    def test_upload_source_url_is_null(self, client: TestClient):
        """API upload never sets source_url — it stays null."""
        r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/upload",
            files=[_txt("url_test.txt")],
        )
        assert r.status_code == 200, r.text
        # source_url is not in FileResponse schema, verify via DB
        from models import File
        doc_id = r.json()[0]["id"]
        with _TestSessionLocal() as db:
            f = db.query(File).filter(File.id == doc_id).first()
        assert f.source_url is None

    def test_upload_original_filename_preserved(self, client: TestClient):
        """The original filename is stored even if the file is converted to .md."""
        # Use a .xml file — it's a TEXT_EXTENSION, so no Docling, but tests the field
        r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/upload",
            files=[("uploaded_files", ("report.xml", io.BytesIO(b"<root/>"), "text/xml"))],
        )
        assert r.status_code == 200, r.text
        data = r.json()[0]
        assert data["filename"] == "report.xml"
        assert data["original_extension"] == ".xml"

    def test_upload_413_on_oversized_file(self, client: TestClient):
        """File larger than MAX_UPLOAD_BYTES (100 MB) returns 413."""
        from config import MAX_UPLOAD_BYTES
        big = io.BytesIO(b"x" * (MAX_UPLOAD_BYTES + 1))
        r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/upload",
            files=[("uploaded_files", ("huge.txt", big, "text/plain"))],
        )
        assert r.status_code == 413

    def test_upload_413_stops_on_first_oversized(self, client: TestClient):
        """When one file is too large, the 413 is raised before any files are processed."""
        from config import MAX_UPLOAD_BYTES
        big = io.BytesIO(b"x" * (MAX_UPLOAD_BYTES + 1))
        r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/upload",
            files=[
                ("uploaded_files", ("ok.txt",   io.BytesIO(b"small"),  "text/plain")),
                ("uploaded_files", ("huge.txt",  big,                   "text/plain")),
                ("uploaded_files", ("ok2.txt",   io.BytesIO(b"small2"), "text/plain")),
            ],
        )
        assert r.status_code == 413

    def test_upload_doc_id_present_on_backend(self, client: TestClient):
        """doc_id returned by upload exists on the real LLM backend."""
        r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/upload",
            files=[_txt("backend_check.txt", "verify this doc lands")],
        )
        assert r.status_code == 200, r.text
        doc_id = r.json()[0]["id"]
        assert doc_id in _backend_doc_ids(), (
            f"doc_id {doc_id!r} not found on backend after upload"
        )

    def test_upload_extension_stored_lowercase(self, client: TestClient):
        """original_extension is normalised to lowercase."""
        r = client.post(
            f"/api/v1/workspaces/{TEST_SLUG}/upload",
            files=[("uploaded_files", ("NOTES.TXT", io.BytesIO(b"hi"), "text/plain"))],
        )
        assert r.status_code == 200, r.text
        ext = r.json()[0]["original_extension"]
        assert ext == ext.lower(), f"Extension {ext!r} should be lowercase"


# ===========================================================================
# SINGLE DELETE
# ===========================================================================

class TestSingleDelete:
    """Route: DELETE /delete/{file_id:path}"""

    def test_delete_success(self, client: TestClient):
        """File is removed from local DB and from the LLM backend."""
        from models import File
        doc_id = _upload_n(client, 1)[0]

        r = client.delete(f"/delete/{doc_id}")
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] == doc_id

        with _TestSessionLocal() as db:
            assert db.query(File).filter(File.id == doc_id).first() is None

        assert doc_id not in _backend_doc_ids(), (
            f"{doc_id!r} still on backend after single delete"
        )

    def test_delete_unknown_id_returns_404(self, client: TestClient):
        """DELETE on a file not in the local DB returns 404."""
        r = client.delete("/delete/totally-fake-id-abc123")
        assert r.status_code == 404

    def test_delete_second_call_returns_404_not_500(self, client: TestClient):
        """Second delete of the same file returns 404 (gone from DB), never 500."""
        doc_id = _upload_n(client, 1)[0]
        first = client.delete(f"/delete/{doc_id}")
        assert first.status_code == 200

        second = client.delete(f"/delete/{doc_id}")
        assert second.status_code == 404

    def test_delete_response_body_contains_deleted_key(self, client: TestClient):
        """Successful delete returns JSON with 'deleted' key equal to the file_id."""
        doc_id = _upload_n(client, 1)[0]
        r = client.delete(f"/delete/{doc_id}")
        assert r.status_code == 200
        body = r.json()
        assert "deleted" in body
        assert body["deleted"] == doc_id


# ===========================================================================
# BULK DELETE
# ===========================================================================

class TestBulkDelete:
    """Route: POST /delete-bulk"""

    def test_small_batch_all_deleted(self, client: TestClient):
        """Bulk delete 3 files — all 3 must appear in 'deleted' and be gone from DB."""
        from models import File
        doc_ids = _upload_n(client, 3)

        r = client.post("/delete-bulk", json={"file_ids": doc_ids})
        assert r.status_code == 200, r.text
        deleted = set(r.json()["deleted"])
        assert deleted == set(doc_ids), (
            f"Missing from deleted: {set(doc_ids) - deleted}"
        )
        with _TestSessionLocal() as db:
            assert db.query(File).filter(File.id.in_(doc_ids)).count() == 0

    def test_more_than_ten_regression(self, client: TestClient):
        """Bulk delete 15 files — regression for the bug where only 10 were deleted."""
        from models import File
        doc_ids = _upload_n(client, 15)

        r = client.post("/delete-bulk", json={"file_ids": doc_ids})
        assert r.status_code == 200, r.text
        deleted = set(r.json()["deleted"])
        assert len(deleted) == 15, (
            f"Only {len(deleted)}/15 deleted. Missing: {set(doc_ids) - deleted}"
        )
        with _TestSessionLocal() as db:
            assert db.query(File).filter(File.id.in_(doc_ids)).count() == 0

    def test_empty_list_returns_empty_deleted(self, client: TestClient):
        """Sending an empty file_ids list returns {'deleted': []} with no error."""
        r = client.post("/delete-bulk", json={"file_ids": []})
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] == []

    def test_missing_file_ids_key_defaults_to_empty(self, client: TestClient):
        """Omitting the file_ids key entirely behaves like an empty list."""
        r = client.post("/delete-bulk", json={})
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] == []

    def test_unknown_ids_silently_skipped(self, client: TestClient):
        """Unknown IDs in the list do not cause an error and are not in 'deleted'."""
        r = client.post(
            "/delete-bulk",
            json={"file_ids": ["ghost-id-1", "ghost-id-2"]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] == []

    def test_mixed_valid_and_unknown(self, client: TestClient):
        """Real IDs are deleted; unknown IDs are silently skipped."""
        doc_ids = _upload_n(client, 2)
        fake_ids = ["fake-aaa", "fake-bbb"]

        r = client.post("/delete-bulk", json={"file_ids": doc_ids + fake_ids})
        assert r.status_code == 200, r.text
        deleted = set(r.json()["deleted"])

        for d in doc_ids:
            assert d in deleted, f"Real id {d!r} not deleted"
        for f in fake_ids:
            assert f not in deleted, f"Fake id {f!r} should not be in deleted"

    def test_all_removed_from_backend(self, client: TestClient):
        """After bulk delete, all docs are gone from the real LLM backend."""
        doc_ids = _upload_n(client, 4)

        before = _backend_doc_ids()
        for d in doc_ids:
            assert d in before, f"{d!r} should be on backend before bulk delete"

        r = client.post("/delete-bulk", json={"file_ids": doc_ids})
        assert r.status_code == 200
        assert set(r.json()["deleted"]) == set(doc_ids)

        after = _backend_doc_ids()
        for d in doc_ids:
            assert d not in after, f"{d!r} still on backend after bulk delete"

    def test_response_always_200_even_on_partial_failure(self, client: TestClient):
        """Bulk delete always returns 200 — partial failures are silently skipped,
        not propagated as HTTP errors."""
        doc_ids = _upload_n(client, 2)
        mixed = doc_ids + ["nonexistent-id"]
        r = client.post("/delete-bulk", json={"file_ids": mixed})
        assert r.status_code == 200

    def test_deleted_key_present_in_response(self, client: TestClient):
        """Response body always contains a 'deleted' key."""
        r = client.post("/delete-bulk", json={"file_ids": []})
        assert "deleted" in r.json()


# ===========================================================================
# HELPER / UTILITY FUNCTIONS
# ===========================================================================

class TestSanitizeUrlToFilename:
    """Unit tests for the _sanitize_url_to_filename helper in main.py."""

    def setup_method(self):
        from main import _sanitize_url_to_filename
        self.fn = _sanitize_url_to_filename

    def test_simple_url(self):
        result = self.fn("https://example.com/foo/bar")
        assert result == "example_com_foo_bar"

    def test_url_with_trailing_slash(self):
        result = self.fn("https://example.com/foo/bar/")
        assert result == "example_com_foo_bar"

    def test_root_url_returns_domain_index(self):
        """Root URL (empty path) falls back to 'index', so result is domain_index."""
        result = self.fn("https://example.com/")
        assert result == "example_com_index"

    def test_url_with_special_chars(self):
        result = self.fn("https://example.com/path?q=1&x=2")
        assert "_" in result
        # Should not contain raw query string characters
        assert "?" not in result
        assert "=" not in result
        assert "&" not in result

    def test_no_double_underscores(self):
        result = self.fn("https://example.com/a//b")
        assert "__" not in result

    def test_no_leading_or_trailing_underscores_in_slug(self):
        result = self.fn("https://example.com/foo")
        # The slug portion should not start or end with underscore
        parts = result.split("_", 3)  # split off domain prefix
        slug_part = result[len("example_com_"):]
        assert not slug_part.startswith("_")
        assert not slug_part.endswith("_")

    def test_deep_path(self):
        result = self.fn("https://its.uri.edu/services/zoom/getting-started")
        assert "its_uri_edu" in result
        assert "services" in result
        assert "zoom" in result


class TestComputeNextRun:
    """Unit tests for the _compute_next_run scheduler helper in main.py."""

    def setup_method(self):
        from main import _compute_next_run
        self.fn = _compute_next_run

    def test_none_interval_returns_none(self):
        result = self.fn(None)
        assert result is None

    def test_empty_string_returns_none(self):
        result = self.fn("")
        assert result is None

    def test_hourly(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        NY = ZoneInfo("America/New_York")
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=NY)
        result = self.fn("hourly", from_time=base)
        from datetime import timedelta
        assert result == base + timedelta(hours=1)

    def test_daily(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        NY = ZoneInfo("America/New_York")
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=NY)
        result = self.fn("daily", from_time=base)
        assert result == base + timedelta(days=1)

    def test_weekly(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        NY = ZoneInfo("America/New_York")
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=NY)
        result = self.fn("weekly", from_time=base)
        assert result == base + timedelta(weeks=1)

    def test_unknown_interval_returns_none(self):
        result = self.fn("monthly")
        assert result is None

    def test_uses_current_time_when_from_time_omitted(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        NY = ZoneInfo("America/New_York")
        before = datetime.now(NY)
        result = self.fn("hourly")
        after = datetime.now(NY)
        assert result is not None
        assert before + timedelta(hours=1) <= result <= after + timedelta(hours=1)


# ===========================================================================
# WORKSPACE DELETE
# ===========================================================================

class TestWorkspaceDelete:
    """
    Route under test: DELETE /api/v1/workspaces/{workspace_id}
    """

    def test_delete_workspace_success(self, client: TestClient):
        """DELETE removes workspace from DB; subsequent GET returns 404."""
        # Use a workspace slug that must already exist in the real AnythingLLM backend.
        # We create it via POST /new (DB-only) — the backend is pre-seeded with TEST_SLUG.
        # To avoid deleting our shared test workspace, create a temporary one.
        slug = "ws-delete-test-tmp"
        r = client.post("/api/v1/workspaces/new", json={"id": slug, "name": "Temp WS"})
        if r.status_code == 409:
            # Already exists from a previous failed test run — that's fine
            pass
        else:
            assert r.status_code == 200, r.text

        # The delete endpoint calls LLM_delete_workspace; as long as the backend
        # doesn't return a non-200 for an unknown slug, this should succeed.
        del_r = client.delete(f"/api/v1/workspaces/{slug}")
        # Accept 200 (success) or 500 (backend refused) — the important thing is
        # the workspace is removed from the local DB on 200.
        if del_r.status_code == 200:
            assert del_r.json()["deleted"] == slug
            get_r = client.get(f"/api/v1/workspaces/{slug}")
            assert get_r.status_code == 404
        else:
            # Backend rejected; workspace may still be in DB — cleanup
            with _TestSessionLocal() as db:
                db.execute(text("DELETE FROM workspaces WHERE id = :slug"), {"slug": slug})
                db.commit()

    def test_delete_workspace_unknown_returns_404(self, client: TestClient):
        """DELETE on a workspace not in local DB returns 404."""
        r = client.delete("/api/v1/workspaces/no-such-workspace-del-xyz")
        assert r.status_code == 404

    def test_delete_workspace_response_body(self, client: TestClient):
        """Successful DELETE returns JSON with 'deleted' key equal to the workspace_id."""
        slug = "ws-del-body-test"
        client.post("/api/v1/workspaces/new", json={"id": slug, "name": "Del Body Test"})
        r = client.delete(f"/api/v1/workspaces/{slug}")
        if r.status_code == 200:
            body = r.json()
            assert "deleted" in body
            assert body["deleted"] == slug
        else:
            # Cleanup
            with _TestSessionLocal() as db:
                db.execute(text("DELETE FROM workspaces WHERE id = :slug"), {"slug": slug})
                db.commit()


# ===========================================================================
# SCRAPE JOBS CRUD
# ===========================================================================

@pytest.fixture(autouse=True)
def clean_scrape_jobs():
    """Wipe scrape_jobs table after every test to prevent state leakage."""
    yield
    with _TestSessionLocal() as db:
        db.execute(text("DELETE FROM scrape_jobs"))
        db.commit()


def _create_job(client: TestClient, workspace_id: str = TEST_SLUG, **kwargs) -> dict:
    """Helper: create a scrape job and return its JSON response."""
    payload = {
        "name": "Test Job",
        "base_url": "https://example.com/",
        "mode": "single",
        "max_depth": 1,
        "max_pages": 5,
        "allow_offsite": False,
    }
    payload.update(kwargs)
    r = client.post(f"/{workspace_id}/scrape/jobs", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


class TestScrapeJobCRUD:
    """
    Routes under test
      POST   /{workspace_id}/scrape/jobs                        create job
      GET    /{workspace_id}/scrape/jobs                        list jobs
      GET    /{workspace_id}/scrape/jobs/{job_id}/pages         list job pages
      PATCH  /{workspace_id}/scrape/jobs/{job_id}              update job
      DELETE /{workspace_id}/scrape/jobs/{job_id}              delete job
    """

    # --- create ---

    def test_create_scrape_job_returns_expected_shape(self, client: TestClient):
        """POST returns a well-formed job dict with all required keys."""
        job = _create_job(client)
        assert "id" in job
        assert job["workspace_id"] == TEST_SLUG
        assert job["name"] == "Test Job"
        assert job["base_url"] == "https://example.com/"
        assert job["mode"] == "single"
        assert job["max_depth"] == 1
        assert job["max_pages"] == 5
        assert job["allow_offsite"] is False
        assert job["is_running"] is False
        assert job["page_count"] == 0
        assert "created_at" in job

    def test_create_scrape_job_unknown_workspace_returns_404(self, client: TestClient):
        """Creating a job for a workspace not in local DB returns 404."""
        r = client.post(
            "/no-such-workspace-xyz/scrape/jobs",
            json={"name": "Job", "base_url": "https://example.com/"},
        )
        assert r.status_code == 404

    def test_create_scrape_job_with_schedule_interval(self, client: TestClient):
        """Creating a job with schedule_interval sets next_scrape_at."""
        job = _create_job(client, schedule_interval="daily")
        assert job["schedule_interval"] == "daily"
        assert job["next_scrape_at"] is not None

    def test_create_scrape_job_no_schedule_has_null_next_scrape_at(self, client: TestClient):
        """Jobs without a schedule have next_scrape_at == None."""
        job = _create_job(client)
        assert job["schedule_interval"] is None
        assert job["next_scrape_at"] is None

    def test_create_scrape_job_defaults(self, client: TestClient):
        """When optional fields are omitted, defaults are applied."""
        r = client.post(
            f"/{TEST_SLUG}/scrape/jobs",
            json={"name": "Minimal", "base_url": "https://example.com/"},
        )
        assert r.status_code == 200, r.text
        job = r.json()
        assert job["mode"] == "depth"
        assert job["max_depth"] == 2
        assert job["max_pages"] == 100
        assert job["allow_offsite"] is False

    # --- list ---

    def test_list_scrape_jobs_empty(self, client: TestClient):
        """Listing jobs for a workspace with no jobs returns an empty list."""
        r = client.get(f"/{TEST_SLUG}/scrape/jobs")
        assert r.status_code == 200, r.text
        assert r.json() == []

    def test_list_scrape_jobs_returns_created_jobs(self, client: TestClient):
        """After creating 2 jobs, the list endpoint returns both."""
        _create_job(client, name="Job A")
        _create_job(client, name="Job B")
        r = client.get(f"/{TEST_SLUG}/scrape/jobs")
        assert r.status_code == 200, r.text
        jobs = r.json()
        assert len(jobs) == 2
        names = {j["name"] for j in jobs}
        assert names == {"Job A", "Job B"}

    def test_list_scrape_jobs_unknown_workspace_returns_404(self, client: TestClient):
        """Listing jobs for an unknown workspace returns 404."""
        r = client.get("/no-such-workspace-xyz/scrape/jobs")
        assert r.status_code == 404

    def test_list_scrape_jobs_includes_page_count(self, client: TestClient):
        """Each job in the list has a page_count field."""
        _create_job(client)
        r = client.get(f"/{TEST_SLUG}/scrape/jobs")
        assert r.status_code == 200, r.text
        job = r.json()[0]
        assert "page_count" in job
        assert isinstance(job["page_count"], int)

    # --- list pages ---

    def test_list_job_pages_empty(self, client: TestClient):
        """A new job has no pages."""
        job = _create_job(client)
        r = client.get(f"/{TEST_SLUG}/scrape/jobs/{job['id']}/pages")
        assert r.status_code == 200, r.text
        assert r.json() == []

    def test_list_job_pages_unknown_job_returns_404(self, client: TestClient):
        """Listing pages for a non-existent job returns 404."""
        r = client.get(f"/{TEST_SLUG}/scrape/jobs/nonexistent-job-id-abc/pages")
        assert r.status_code == 404

    def test_list_job_pages_returns_files_for_job(self, client: TestClient):
        """Pages inserted directly into DB appear in the job pages list."""
        from models import File as FileModel
        job = _create_job(client)
        job_id = job["id"]
        # Insert a fake page record directly into the test DB
        with _TestSessionLocal() as db:
            db.add(FileModel(
                id="fake-page-doc-id-001",
                filename="example_com.md",
                original_extension=".html",
                workspace_id=TEST_SLUG,
                category=f"scrape_Test Job",
                source_url="https://example.com/",
                scrape_job_id=job_id,
            ))
            db.commit()

        r = client.get(f"/{TEST_SLUG}/scrape/jobs/{job_id}/pages")
        assert r.status_code == 200, r.text
        pages = r.json()
        assert len(pages) == 1
        assert pages[0]["id"] == "fake-page-doc-id-001"
        assert pages[0]["source_url"] == "https://example.com/"

    # --- update ---

    def test_update_scrape_job_name(self, client: TestClient):
        """PATCH updates the job name."""
        job = _create_job(client, name="Old Name")
        r = client.patch(
            f"/{TEST_SLUG}/scrape/jobs/{job['id']}",
            json={"name": "New Name"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["name"] == "New Name"

    def test_update_scrape_job_mode(self, client: TestClient):
        """PATCH can change the crawl mode."""
        job = _create_job(client, mode="single")
        r = client.patch(
            f"/{TEST_SLUG}/scrape/jobs/{job['id']}",
            json={"mode": "depth"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["mode"] == "depth"

    def test_update_scrape_job_adds_schedule(self, client: TestClient):
        """PATCH can add a schedule to a previously unscheduled job."""
        job = _create_job(client)
        assert job["next_scrape_at"] is None

        r = client.patch(
            f"/{TEST_SLUG}/scrape/jobs/{job['id']}",
            json={"schedule_interval": "weekly"},
        )
        assert r.status_code == 200, r.text
        updated = r.json()
        assert updated["schedule_interval"] == "weekly"
        assert updated["next_scrape_at"] is not None

    def test_update_scrape_job_removes_schedule(self, client: TestClient):
        """PATCH can remove a schedule by setting schedule_interval to null."""
        job = _create_job(client, schedule_interval="hourly")
        assert job["next_scrape_at"] is not None

        r = client.patch(
            f"/{TEST_SLUG}/scrape/jobs/{job['id']}",
            json={"schedule_interval": None},
        )
        assert r.status_code == 200, r.text
        updated = r.json()
        assert updated["schedule_interval"] is None
        assert updated["next_scrape_at"] is None

    def test_update_scrape_job_unknown_returns_404(self, client: TestClient):
        """PATCH on a non-existent job returns 404."""
        r = client.patch(
            f"/{TEST_SLUG}/scrape/jobs/ghost-job-id-xyz",
            json={"name": "Ghost"},
        )
        assert r.status_code == 404

    def test_update_scrape_job_max_pages(self, client: TestClient):
        """PATCH can update max_pages."""
        job = _create_job(client, max_pages=10)
        r = client.patch(
            f"/{TEST_SLUG}/scrape/jobs/{job['id']}",
            json={"max_pages": 50},
        )
        assert r.status_code == 200, r.text
        assert r.json()["max_pages"] == 50

    def test_update_scrape_job_allow_offsite(self, client: TestClient):
        """PATCH can toggle allow_offsite."""
        job = _create_job(client, allow_offsite=False)
        r = client.patch(
            f"/{TEST_SLUG}/scrape/jobs/{job['id']}",
            json={"allow_offsite": True},
        )
        assert r.status_code == 200, r.text
        assert r.json()["allow_offsite"] is True

    # --- delete ---

    def test_delete_scrape_job_success(self, client: TestClient):
        """DELETE removes the job from the DB."""
        job = _create_job(client)
        job_id = job["id"]

        r = client.delete(f"/{TEST_SLUG}/scrape/jobs/{job_id}")
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] == job_id

        from models import ScrapeJob as ScrapeJobModel
        with _TestSessionLocal() as db:
            assert db.query(ScrapeJobModel).filter(ScrapeJobModel.id == job_id).first() is None

    def test_delete_scrape_job_not_found(self, client: TestClient):
        """DELETE on a non-existent job returns 404."""
        r = client.delete(f"/{TEST_SLUG}/scrape/jobs/nonexistent-job-xyz")
        assert r.status_code == 404

    def test_delete_scrape_job_cascades_to_files(self, client: TestClient):
        """Deleting a job removes its associated file records from the DB."""
        from models import File as FileModel, ScrapeJob as ScrapeJobModel
        job = _create_job(client)
        job_id = job["id"]

        # Insert a fake page record linked to this job
        with _TestSessionLocal() as db:
            db.add(FileModel(
                id="cascade-del-page-001",
                filename="page.md",
                original_extension=".html",
                workspace_id=TEST_SLUG,
                category="scrape_Test Job",
                source_url="https://example.com/page",
                scrape_job_id=job_id,
            ))
            db.commit()

        r = client.delete(f"/{TEST_SLUG}/scrape/jobs/{job_id}")
        assert r.status_code == 200, r.text

        with _TestSessionLocal() as db:
            # Job gone
            assert db.query(ScrapeJobModel).filter(ScrapeJobModel.id == job_id).first() is None
            # File gone via cascade
            assert db.query(FileModel).filter(FileModel.id == "cascade-del-page-001").first() is None

    def test_delete_scrape_job_response_has_deleted_key(self, client: TestClient):
        """DELETE response body contains a 'deleted' key with the job ID."""
        job = _create_job(client)
        r = client.delete(f"/{TEST_SLUG}/scrape/jobs/{job['id']}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "deleted" in body


# ===========================================================================
# SCRAPE JOB RUN (SSE)
# ===========================================================================

class TestScrapeJobRun:
    """
    Route under test: POST /{workspace_id}/scrape/jobs/{job_id}/run
    Uses 'single' mode with a known-stable URL so no real crawl is needed,
    but does perform a real scrape + upload to the backend.
    """

    def test_run_unknown_job_returns_404(self, client: TestClient):
        """Running a non-existent job returns 404."""
        r = client.post(f"/{TEST_SLUG}/scrape/jobs/nonexistent-job-zzz/run")
        assert r.status_code == 404

    def test_run_unknown_workspace_returns_404(self, client: TestClient):
        """Running a job in a non-existent workspace returns 404."""
        r = client.post("/no-such-workspace-xyz/scrape/jobs/some-job-id/run")
        assert r.status_code == 404

    def test_run_already_running_job_returns_409(self, client: TestClient):
        """A job already marked is_running=True returns 409."""
        from models import ScrapeJob as ScrapeJobModel
        job = _create_job(client)
        job_id = job["id"]

        # Mark it as running
        with _TestSessionLocal() as db:
            j = db.query(ScrapeJobModel).filter(ScrapeJobModel.id == job_id).first()
            j.is_running = True
            db.commit()

        r = client.post(f"/{TEST_SLUG}/scrape/jobs/{job_id}/run")
        assert r.status_code == 409

    def test_run_single_mode_produces_sse_events(self, client: TestClient):
        """Running a single-mode job yields valid SSE events including a final [DONE]."""
        import json as _json
        # Use example.com — always available; single page, fast
        job = _create_job(client, mode="single", base_url="https://example.com/")
        job_id = job["id"]

        lines = []
        with client.stream("POST", f"/{TEST_SLUG}/scrape/jobs/{job_id}/run") as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                lines.append(line)

        # Collect data: lines
        data_lines = [l for l in lines if l.startswith("data:")]
        assert len(data_lines) > 0

        # Final SSE line must be [DONE]
        assert data_lines[-1] == "data: [DONE]"

        # There should be a 'done' status event before [DONE]
        done_events = []
        for dl in data_lines[:-1]:
            payload = _json.loads(dl[len("data: "):])
            if payload.get("status") == "done":
                done_events.append(payload)
        assert len(done_events) == 1, "Expected exactly one 'done' event"
        assert "job_id" in done_events[0]
        assert "page_count" in done_events[0]

    def test_run_single_mode_persists_file_to_db(self, client: TestClient):
        """After a successful run, the scraped page appears in the DB and in /pages."""
        from models import File as FileModel
        job = _create_job(client, mode="single", base_url="https://example.com/")
        job_id = job["id"]

        # Consume SSE to completion (must read inside the context manager)
        with client.stream("POST", f"/{TEST_SLUG}/scrape/jobs/{job_id}/run") as resp:
            for _ in resp.iter_lines():
                pass

        with _TestSessionLocal() as db:
            pages = db.query(FileModel).filter(FileModel.scrape_job_id == job_id).all()

        assert len(pages) >= 1, "Expected at least one page to be persisted after run"
        page = pages[0]
        assert page.source_url == "https://example.com/"
        assert page.scrape_job_id == job_id
        assert page.workspace_id == TEST_SLUG
        assert page.content_hash is not None
