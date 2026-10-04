"""Preservation, revision races and transaction tests without external services."""

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from graph.growi.client import GrowiAPIError, GrowiPage, GrowiPublisher, _growi_markdown, publish_pages, wrap_page
from graph.workspace.project import Project
from graph.workspace import writer
from graph.wiki.storage import read_json, write_json_atomic, write_text_atomic
from publisher.human_changes import HumanStore, RETAINED, DASHBOARD, merge, regions, strip_regions
from publisher.history import candidate, candidate_is_clean, commit_candidate, ensure_repository, last_good, promote
from publisher import pipeline


BASE = "# Page\n\nIntro.\n\n## Limits\n\nMaximum is 40°C.\nMode is AUTO.\n\n## Other\n\nUnchanged.\n"
HUMAN = BASE.replace("40°C", "60°C")


class HumanChangesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Project(Path(self.tmp.name) / "project").ensure()
        self.rel = "doc.md"
        self.folder = self.project.wiki_dir(self.rel)
        self.folder.mkdir(parents=True)
        self.project.raw_file(self.rel).write_text("source\n", encoding="utf-8")
        writer.write_source_stamp(self.folder, self.project.raw_file(self.rel), self.rel, identity_seed="source-1")
        write_json_atomic(self.folder / "_planning" / "linker.json", {"status": "complete"})
        self.path = "doc/001.md"
        self.store = HumanStore(self.project)
        self.store.generated(self.rel, {"001.md": BASE})
        self.store.render(self.rel)
        self.row = {"marker_id": "b1", "page_id": "p1", "growi_path": "/docs/doc/001", "revision_id": "r0"}
        self.store.remember_page(self.path, self.row, BASE, BASE, published=True)

    def capture(self, human=HUMAN, before=BASE, revision="r1"):
        self.row["observed_revision_id"] = revision
        self.store.capture(self.rel, self.path, self.row, before, human)
        return self.store.render(self.rel)

    def text(self, name="001.md"):
        return (self.folder / name).read_text(encoding="utf-8")

    def update(self, pages):
        self.store.generated(self.rel, pages)
        return self.store.render(self.rel)

    def active(self):
        return [e for e in self.store.document(self.rel)["edits"] if e["status"] != "deleted"]

    def test_human_edit_survives_four_updates_and_restart_without_duplicates(self):
        self.capture()
        for number in range(4):
            self.update({"001.md": BASE.replace("Unchanged.", f"Source version {number}.")})
            self.store = HumanStore(self.project)
            self.assertEqual(self.text().count("60°C"), 1)
            self.assertNotIn("60°C", self.store.get(self.store.document(self.rel)["pages"]["001.md"]["body_blob"]))
        self.assertFalse(self.store.render(self.rel).changed_pages)

    def test_different_lines_and_different_tokens_in_same_line_merge(self):
        self.capture()
        result = self.update({"001.md": BASE.replace("AUTO", "MANUAL")})
        self.assertFalse(result.conflicts)
        self.assertIn("60°C", self.text())
        self.assertIn("MANUAL", self.text())
        self.assertEqual(merge("Limit 40°C; mode AUTO.\n", "Limit 60°C; mode AUTO.\n", "Limit 40°C; mode MANUAL.\n"),
                         ("Limit 60°C; mode MANUAL.\n", "active"))

    def test_same_information_conflicts_and_both_versions_survive(self):
        self.capture()
        result = self.update({"001.md": BASE.replace("40°C", "55°C")})
        self.assertEqual(len(result.conflicts), 1)
        self.assertLess(self.text().index("60°C"), self.text().index("55°C"))
        self.assertIn("Updated source document says:", self.text())
        self.assertIn(result.conflicts[0], self.text(DASHBOARD))
        self.assertNotIn("60°C", self.text(DASHBOARD))

    def test_same_change_is_absorbed_and_reactivates_after_source_regresses(self):
        self.capture()
        self.update({"001.md": HUMAN.replace("Unchanged.", "Source also changed here.")})
        self.assertEqual(self.active()[0]["status"], "absorbed")
        self.assertEqual(self.text().count("60°C"), 1)
        self.update({"001.md": BASE})
        self.assertEqual(self.active()[0]["status"], "active")
        self.assertIn("60°C", self.text())

    def test_block_moves_to_another_page(self):
        self.capture()
        self.update({"002.md": BASE.replace("# Page", "# Moved")})
        self.assertIn("60°C", self.text("002.md"))
        self.assertFalse((self.folder / RETAINED).exists())

    def test_disappeared_block_retained_then_restored(self):
        self.capture()
        self.update({"001.md": "# New page\n\nNew subject.\n"})
        self.assertIn("60°C", self.text(RETAINED))
        self.assertEqual(self.active()[0]["status"], "orphaned")
        self.update({"001.md": BASE})
        self.assertIn("60°C", self.text())
        self.assertFalse((self.folder / RETAINED).exists())

    def test_retained_notes_never_overwrite_a_source_page_with_the_same_name(self):
        self.capture()
        source_page = "# Source page\n\nSource fact E-555.\n"
        self.update({RETAINED: source_page})
        self.assertEqual(self.text(RETAINED), source_page)
        retained_name = self.store.document(self.rel)["retained_filename"]
        self.assertNotEqual(retained_name, RETAINED)
        self.assertIn("60°C", self.text(retained_name))

    def test_human_deletion_from_retained_notes_tombstones_original_edit(self):
        from publisher.human_changes import _REGION
        self.capture()
        self.update({"001.md": "# New page\n\nNew subject.\n"})
        before = self.text(RETAINED)
        row = {"marker_id": "bnotes", "revision_id": "r1", "observed_revision_id": "r2"}
        self.store.capture(self.rel, "doc/" + RETAINED, row, before, _REGION.sub("", before))
        self.store.render(self.rel)
        self.assertFalse(self.active())
        self.assertFalse((self.folder / RETAINED).exists())
        self.update({"001.md": BASE})
        self.assertNotIn("60°C", self.text())

    def test_editing_retained_note_keeps_original_anchor_for_later_restoration(self):
        self.capture()
        self.update({"001.md": "# New page\n\nNew subject.\n"})
        before = self.text(RETAINED)
        row = {"marker_id": "bnotes", "revision_id": "r1", "observed_revision_id": "r2"}
        self.store.capture(self.rel, "doc/" + RETAINED, row, before, before.replace("60°C", "65°C"))
        self.store.render(self.rel)
        self.assertEqual(self.text(RETAINED).count("65°C"), 1)
        self.update({"001.md": BASE})
        self.assertIn("65°C", self.text())
        self.assertNotIn("60°C", self.text())

    def test_new_section_stays_on_its_page(self):
        self.capture(BASE + "\n## Operator notes\n\nHuman warning E-901.\n")
        self.assertIn("Human warning E-901.", self.text())
        for number in range(4):
            self.update({"001.md": BASE.replace("Unchanged.", str(number))})
            self.assertEqual(self.text().count("Human warning E-901."), 1)

    def test_human_deletes_own_replacement_and_it_does_not_return(self):
        self.capture()
        before = self.text()
        self.row["revision_id"] = "r1"
        self.capture(BASE, before, "r2")
        self.assertFalse(self.active())
        self.update({"001.md": BASE.replace("Unchanged.", "Later")})
        self.assertNotIn("60°C", self.text())
        self.assertTrue(any(e.get("deleted_from_revision") == "r2" for e in self.store.document(self.rel)["edits"]))

    def test_generated_fact_deletion_is_durable_suppression(self):
        self.capture(BASE.replace("Mode is AUTO.\n", ""))
        self.assertNotIn("Mode is AUTO.", self.text())
        self.update({"001.md": BASE.replace("Unchanged.", "Later")})
        self.assertNotIn("Mode is AUTO.", self.text())

    def test_changed_existing_human_region_supersedes_without_duplicate(self):
        self.capture()
        before = self.text()
        self.row["revision_id"] = "r1"
        self.capture(before.replace("60°C", "65°C"), before, "r2")
        self.update({"001.md": BASE.replace("AUTO", "MANUAL")})
        self.assertEqual(self.text().count("65°C"), 1)
        self.assertNotIn("60°C", self.text())
        self.assertEqual(len(self.active()), 1)

    def test_repeated_remote_revision_is_idempotent(self):
        self.capture()
        self.capture()
        self.assertEqual(len(self.store.document(self.rel)["edits"]), 1)

    def test_keep_human_resolution_and_accept_source_tombstone(self):
        self.capture()
        source = BASE.replace("40°C", "55°C")
        self.update({"001.md": source})
        before = self.text()
        edit_id = self.active()[0]["edit_id"]
        from publisher.human_changes import _SOURCE
        remote = _SOURCE.sub("", before)
        self.row["revision_id"] = "r1"
        self.capture(remote, before, "r2")
        self.assertIn("60°C", self.text())
        self.assertNotIn("55°C", self.text())
        self.update({"001.md": BASE.replace("40°C", "50°C")})
        self.assertIn("Updated source document says:", self.text())
        before = self.text()
        remote = before.replace(regions(before)[edit_id], self.store.get(self.active()[0]["conflict"]["source_blob"]))
        self.row["revision_id"] = "r2"
        self.capture(remote, before, "r3")
        self.assertNotIn("60°C", self.text())
        self.assertFalse(self.active())

    def test_accepting_the_quoted_source_does_not_suppress_it(self):
        from publisher.human_changes import _SOURCE
        self.capture()
        self.update({"001.md": BASE.replace("40°C", "55°C")})
        before = self.text()
        edit_id = self.active()[0]["edit_id"]
        region = regions(before)[edit_id]
        source_only = _SOURCE.search(region).group(0).lstrip("\n")
        remote = before.replace(region, source_only)
        row = dict(self.row, revision_id="r1", observed_revision_id="r2")
        self.store.capture(self.rel, self.path, row, before, remote)
        self.store.render(self.rel)
        self.assertIn("55°C", self.text())
        self.assertNotIn("60°C", self.text())
        self.assertFalse(self.active())

    def test_corrupt_or_missing_blob_fails_closed(self):
        self.capture()
        digest = self.active()[0]["human_after_blob"]
        (self.store.root / "snapshots" / f"{digest}.md").write_text("corrupted", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.store.render(self.rel)

    def test_table_and_code_are_atomic(self):
        for body, human, source in [
            ("| x | y |\n| - | - |\n| A | 1 |\n| B | 2 |\n", "A | 3", "B | 4"),
            ("```c\n#define A 1\n#define B 2\n```\n", "A 3", "B 4"),
        ]:
            h = body.replace("A | 1", human).replace("A 1", human)
            g = body.replace("B | 2", source).replace("B 2", source)
            text, status = merge(body, h, g)
            self.assertEqual((text, status), (h, "conflict"))

    def test_sign_and_number_changes_conflict_instead_of_inventing_a_value(self):
        self.assertEqual(merge("40°C\n", "-40°C\n", "45°C\n"), ("-40°C\n", "conflict"))

    def test_fenced_marker_examples_and_footer_text_survive_as_human_content(self):
        from graph.common.markdown import LINKS_FOOTER_END, LINKS_FOOTER_START
        from graph.growi.client import managed_page_markdown, merge_marked_sections
        from publisher.human_changes import editable, wrap_edit

        edit_id = "hedit-abc123"
        example = ("## Examples\n\n````markdown\n```\n"
                   f"<!-- llm-wiki-human:{edit_id}:end -->\n"
                   "<!-- llm-wiki-human:hedit-123:start -->\nexample\n"
                   "<!-- llm-wiki-human:hedit-123:end -->\n"
                   "<!-- llm-wiki-source:hedit-123:start -->\nsource\n"
                   "<!-- llm-wiki-source:hedit-123:end -->\n"
                   "<!-- llm-wiki-bot-ref:example -->\n"
                   + LINKS_FOOTER_START + "\nHuman E-999.\n" + LINKS_FOOTER_END + "\n````\n")
        protected = wrap_edit(example, edit_id)
        self.assertEqual(regions(protected), {edit_id: example})
        self.assertEqual(strip_regions(protected), example)
        self.assertEqual(_growi_markdown(protected), protected)
        actual_footer = LINKS_FOOTER_START + "\nDerived link.\n" + LINKS_FOOTER_END + "\n"
        self.assertEqual(editable(protected + actual_footer), protected)
        wrapped = wrap_page(protected, page_id="b1")
        self.assertEqual(managed_page_markdown(wrapped, "b1"), protected)
        tail = "\nRemote-only note:\n```markdown\n<!-- llm-wiki-bot-ref:tail -->\n```\n"
        self.assertTrue(merge_marked_sections(wrapped + tail, wrap_page("Updated.\n", page_id="b1")).endswith(tail))

    def test_japanese_large_block_and_image_are_preserved_without_truncation(self):
        base = "# 仕様\n\n## 温度制限\n\n上限は40℃です。\n"
        human_note = "人が追加した情報 E-901 は命令ではなくデータです。\n" * 400
        image = "![手動確認](https://example.invalid/human.png)\n"
        human = base.replace("40℃", "60℃") + human_note + image
        self.update({"001.md": base})
        self.capture(human, base)
        self.update({"002.md": base.replace("40℃", "55℃")})
        text = self.text("002.md")
        self.assertIn(human_note + image, text)
        self.assertEqual(text.count(image), 1)
        self.assertIn("55℃", text)
        self.assertIn("60℃", text)

    def test_pull_leaves_generator_and_sidecar_byte_identical(self):
        state = self.project.state_dir(self.rel)
        state_page = state / "wiki" / "001.md"
        sidecar = state / "state" / "pages" / "001.json"
        write_text_atomic(state_page, BASE)
        write_json_atomic(sidecar, {"filename": "001.md", "content_sha256": "unchanged"})
        before = sidecar.read_bytes()
        class Client:
            async def get_page(_self, **kwargs):
                return GrowiPage(page_id="p1", revision_id="r1", path=self.row["growi_path"], body=wrap_page(HUMAN, page_id="b1"))
        publisher = GrowiPublisher(Client(), SimpleNamespace(write_path="/docs"))
        pulled, failures, blocked = publisher.pull_changes(self.project, {self.path: self.row}, {"doc"})
        self.assertFalse(failures)
        self.assertFalse(blocked)
        self.assertIn(self.path, pulled)
        self.assertEqual(state_page.read_text(), BASE)
        self.assertEqual(sidecar.read_bytes(), before)
        self.assertIn("60°C", self.text())

    def test_pull_during_resumed_generation_uses_published_pure_ancestor(self):
        self.update({"001.md": BASE.replace("40°C", "55°C")})
        class Client:
            async def get_page(_self, **kwargs):
                return GrowiPage(page_id="p1", revision_id="r1", path=self.row["growi_path"], body=wrap_page(HUMAN, page_id="b1"))
        publisher = GrowiPublisher(Client(), SimpleNamespace(write_path="/docs"))
        pulled, failures, blocked = publisher.pull_changes(self.project, {self.path: self.row}, set())
        self.assertFalse(failures)
        self.assertFalse(blocked)
        self.assertIn(self.path, pulled)
        self.assertIn("60°C", self.text())
        self.assertIn("55°C", self.text())
        self.assertEqual(self.active()[0]["status"], "conflict")

    def test_remote_move_delete_and_marker_damage_block_publication(self):
        for remote, reason in [
            (None, "remote_deleted"),
            (GrowiPage(page_id="p1", revision_id="r0", path="/trash/docs/doc/001",
                       body=wrap_page(BASE, page_id="b1"), status="deleted"), "remote_deleted"),
            (GrowiPage(page_id="p1", revision_id="r0", path="/moved", body=wrap_page(BASE, page_id="b1")), "remote_moved"),
            (GrowiPage(page_id="p1", revision_id="r1", path=self.row["growi_path"], body=HUMAN), "ownership"),
        ]:
            class Client:
                async def get_page(_self, **kwargs):
                    return remote
            # Separate markers avoid one persistent block masking another case.
            row = dict(self.row, marker_id="b" + reason)
            publisher = GrowiPublisher(Client(), SimpleNamespace(write_path="/docs"))
            pulled, failures, blocked = publisher.pull_changes(self.project, {self.path: row}, {"doc"})
            self.assertFalse(pulled)
            self.assertEqual(blocked, {"doc"})
            self.assertIn(reason, failures[0])
            self.assertEqual(self.text(), BASE)

    def test_rollback_rebases_captured_human_journal_onto_old_source(self):
        staged = Project(Path(self.tmp.name) / "candidate").ensure()
        shutil.copytree(self.project.wiki, staged.wiki, dirs_exist_ok=True)
        shutil.copytree(self.project.raw, staged.raw, dirs_exist_ok=True)
        shutil.copytree(self.project.metadata / "human-sync", staged.metadata / "human-sync")
        store = HumanStore(staged)
        row = dict(self.row, observed_revision_id="r1")
        store.capture(self.rel, self.path, row, BASE, HUMAN)
        store.generated(self.rel, {"001.md": BASE.replace("40°C", "55°C")})
        store.render(self.rel)
        self.store.import_captured(staged, [self.rel])
        self.assertIn("60°C", self.text())
        self.assertNotIn("55°C", self.text())
        self.assertNotIn("60°C", self.store.get(self.store.document(self.rel)["pages"]["001.md"]["body_blob"]))

    def test_human_region_survives_remote_formatting_exactly(self):
        self.capture(HUMAN.replace("Mode is AUTO.", "Mode is `AA29`. <!-- human comment -->"))
        effective = self.text()
        self.assertEqual(regions(_growi_markdown(effective)), regions(effective))

    def test_footer_only_revision_does_not_create_human_operation(self):
        from graph.common.markdown import LINKS_FOOTER_START, LINKS_FOOTER_END
        before = BASE + f"\n{LINKS_FOOTER_START}\n## Related\nold\n{LINKS_FOOTER_END}\n"
        after = before.replace("old", "new")
        self.capture(after, before)
        self.assertFalse(self.active())

    def test_snapshot_preserves_crlf_bytes(self):
        body = "exact\r\nhuman\r\n"
        self.assertEqual(self.store.get(self.store.put(body)), body)

    def test_partial_human_marker_removal_fails_closed(self):
        self.capture()
        before = self.text()
        damaged = before.replace(":end -->", ":damaged -->", 1)
        with self.assertRaisesRegex(ValueError, "unbalanced"):
            self.capture(damaged, before, "r2")
        self.assertEqual(len(self.active()), 1)

    def test_linker_preserves_human_tables_code_comments_and_values_exactly(self):
        from graph.linker.render import RenderEdge, render_page
        human = HUMAN.replace("Mode is AUTO.", "Mode is `AA29`. <!-- operator comment -->\n\n```c\n#define AA29 60\n```\n")
        self.capture(human)
        original = self.text()
        linked = render_page(original, page_rel=self.path, mode="neo", edges=[
            RenderEdge("e1", "peer/001.md", "AA29", "", "defines", "peer definition", source="use", via=["AA29"])
        ])
        self.assertEqual(regions(linked), regions(original))

    def test_moved_source_keeps_journal_identity_and_intro_target(self):
        self.capture(BASE.replace("Intro.", "Human intro E-901."))
        new_rel = "archive/moved.md"
        self.project.raw_file(new_rel).parent.mkdir(parents=True)
        shutil.move(self.project.raw_file(self.rel), self.project.raw_file(new_rel))
        new_folder = self.project.wiki_dir(new_rel)
        new_folder.parent.mkdir(parents=True)
        shutil.move(self.folder, new_folder)
        writer.write_source_stamp(new_folder, self.project.raw_file(new_rel), new_rel, identity_seed="source-1")
        self.store.move(new_rel, "doc", "archive/moved")
        self.store.generated(new_rel, {"001.md": BASE.replace("Unchanged.", "Later source.")})
        self.store.render(new_rel)
        self.assertIn("Human intro E-901.", (new_folder / "001.md").read_text())
        self.assertEqual(self.store.document(new_rel)["source_id"], "source-1")

    def test_archived_journal_is_reapplied_when_same_identity_returns(self):
        self.capture()
        self.store.archive(self.rel)
        self.assertTrue(self.store.document(self.rel)["archived"])
        self.update({"001.md": BASE.replace("Unchanged.", "Restored source.")})
        self.assertFalse(self.store.document(self.rel)["archived"])
        self.assertIn("60°C", self.text())

    def test_journal_uses_stable_source_id_after_legacy_seed_migration(self):
        from publisher.ledger import Ledger, save_ledger
        self.capture()
        save_ledger(self.project.metadata / "pipeline.json", Ledger(
            {self.rel: {"source_id": "stable-uuid", "id_seed": "source-1", "raw_rel": self.rel}}, {},
        ))
        journal = self.store.document(self.rel)
        self.assertEqual(journal["source_id"], "stable-uuid")
        self.assertEqual(journal["edits"][0]["source_id"], "stable-uuid")
        self.store.archive(self.rel)
        save_ledger(self.project.metadata / "pipeline.json", Ledger({}, {}))
        writer.write_source_stamp(self.folder, self.project.raw_file(self.rel), self.rel, identity_seed="stable-uuid")
        self.update({"001.md": BASE})
        self.assertIn("60°C", self.text())
        self.assertEqual(self.store.document(self.rel)["source_id"], "stable-uuid")

    def test_reused_path_with_different_source_id_does_not_inherit_human_journal(self):
        from publisher.ledger import Ledger, save_ledger
        self.capture()
        save_ledger(self.project.metadata / "pipeline.json", Ledger(
            {self.rel: {"source_id": "old-uuid", "id_seed": "source-1", "raw_rel": self.rel}}, {},
        ))
        self.store.document(self.rel)
        self.store.archive(self.rel)
        save_ledger(self.project.metadata / "pipeline.json", Ledger(
            {self.rel: {"source_id": "new-uuid", "id_seed": "source-1", "raw_rel": self.rel}}, {},
        ))
        self.update({"001.md": BASE})
        self.assertNotIn("60°C", self.text())
        self.assertFalse(self.active())

    def test_legacy_page_without_pure_ancestor_is_pinned_before_any_publish(self):
        shutil.rmtree(self.store.root)
        legacy = HUMAN.replace("AUTO", "`AA29` <!-- human-only warning E-900 -->")
        write_text_atomic(self.folder / "001.md", legacy)
        class Client:
            async def get_page(_self, **kwargs):
                return GrowiPage(page_id="p1", revision_id="r0", path=self.row["growi_path"], body=wrap_page(legacy, page_id="b1"))
        publisher = GrowiPublisher(Client(), SimpleNamespace(write_path="/docs"))
        _pulled, failures, _blocked = publisher.pull_changes(self.project, {self.path: self.row}, {"doc"})
        self.assertFalse(failures)
        self.store = HumanStore(self.project)
        journal = self.store.document(self.rel)
        self.assertEqual(journal["pages"], {})
        self.assertTrue(journal["requires_pure_rebuild"])
        self.assertEqual(journal["edits"][0]["status"], "legacy_pinned")
        published = _growi_markdown(self.text())
        self.assertIn("`AA29` <!-- human-only warning E-900 -->", published)

    def test_discovered_revision_with_unverified_human_body_is_legacy_pinned(self):
        (self.store.root / "pages" / "b1.json").unlink()
        class Client:
            page = GrowiPage(page_id="p1", revision_id="discovered-r1", path=self.row["growi_path"], body=wrap_page(HUMAN, page_id="b1"))
            async def get_page(client, **kwargs):
                return client.page
        client = Client()
        publisher = GrowiPublisher(client, SimpleNamespace(write_path="/docs", root_path="/docs", mode="attach"))
        row = dict(self.row, revision_id="discovered-r1")
        pulled, failures, blocked = publisher.pull_changes(self.project, {self.path: row}, {"doc"})
        self.assertFalse(failures)
        self.assertFalse(blocked)
        self.assertIn(self.path, pulled)
        self.assertIn("60°C", self.text())
        self.assertEqual(len(self.active()), 1)
        self.assertEqual(self.active()[0]["status"], "legacy_pinned")
        client.page = client.page.model_copy(update={"revision_id": "human-r2", "body": wrap_page(HUMAN.replace("60°C", "65°C"), page_id="b1")})
        publisher.pull_changes(self.project, {self.path: row}, {"doc"})
        self.assertEqual(len(self.active()), 1)
        self.assertIn("65°C", self.store.get(self.active()[0]["human_after_blob"]))
        self.assertNotIn("60°C", self.text())

    def test_blocked_idle_pull_is_checkpointed_and_human_repair_can_retry(self):
        from publisher.ledger import Ledger, save_ledger
        self.project.mount.mkdir()
        (self.project.mount / self.rel).write_text("source\n", encoding="utf-8")
        save_ledger(self.project.metadata / "pipeline.json", Ledger(
            {self.rel: {"raw_rel": self.rel, "source_id": "source-1", "id_seed": "source-1"}},
            {"doc": {"raw_rel": self.rel, "content_sha256": pipeline._content_hash(self.folder)}},
            {self.path: self.row},
        ))
        base = ensure_repository(self.project)
        class Client:
            page = None
            async def get_page(self, **kwargs):
                return self.page
        client = Client()
        publisher = GrowiPublisher(client, SimpleNamespace(write_path="/docs"))
        settings = SimpleNamespace(data_root=str(self.project.root.parent), target_name=self.project.root.name,
                                   mount_path=str(self.project.mount))
        with patch.object(pipeline, "_publisher", return_value=publisher):
            result = pipeline.pull_growi_once(settings)
            self.assertTrue(result["failures"])
            self.assertNotEqual(last_good(self.project), base)
            self.assertTrue(candidate_is_clean(self.project, last_good(self.project)))
            client.page = GrowiPage(page_id="p1", revision_id="r1", path=self.row["growi_path"], body=wrap_page(BASE, page_id="b1"))
            repaired = pipeline.pull_growi_once(settings)
            self.assertFalse(repaired["failures"])
            self.assertTrue(candidate_is_clean(self.project, last_good(self.project)))

    def test_conflict_marker_damage_is_saved_and_blocks(self):
        self.capture()
        self.update({"001.md": BASE.replace("40°C", "55°C")})
        before = self.text()
        self.row["observed_revision_id"] = "r2"
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.store.capture(self.rel, self.path, self.row, before, "# Page\n\nUnknown final statement.\n")
        self.assertEqual(self.active()[0]["status"], "conflict")


class PublicationSafetyTest(unittest.TestCase):
    def test_pull_command_dispatches_once_and_reports_failures(self):
        import main

        args = main.build_parser().parse_args(["pull", "--project", "example"])
        settings = object()
        with patch.object(main, "_settings", return_value=settings), patch.object(pipeline, "pull_growi_once", return_value={"done": [], "failures": []}) as pull:
            self.assertEqual(args.fn(args), 0)
        pull.assert_called_once_with(settings)

    def test_409_aborts_without_second_put_or_create(self):
        class Client:
            calls = 0
            async def get_page(self, **kwargs):
                return GrowiPage(page_id="p", revision_id="r0", path="/docs/001", body=wrap_page("old", page_id="b1"))
            async def update_page(self, *args):
                self.calls += 1
                raise GrowiAPIError(409, "PUT", "/page")
            async def create_page(self, *args):
                raise AssertionError("must not recreate on conflict")
        client = Client()
        with self.assertRaises(GrowiAPIError):
            asyncio.run(publish_pages(client, [{"path": "/docs/001", "local_path": "doc/001.md", "body": wrap_page("new", page_id="b1")}],
                                     mode="attach", write_path="/docs", expected_pages={"doc/001.md": {"page_id": "p", "revision_id": "r0", "marker_id": "b1"}}))
        self.assertEqual(client.calls, 1)

    def test_preflight_checks_entire_batch_before_any_write(self):
        class Client:
            async def get_page(self, **kwargs):
                if kwargs["page_id"] == "p2":
                    return GrowiPage(page_id="p2", revision_id="human", path="/docs/002", body=wrap_page("human", page_id="b2"))
                return GrowiPage(page_id="p1", revision_id="r0", path="/docs/001", body=wrap_page("old", page_id="b1"))
            async def update_page(self, *args):
                raise AssertionError("wrote before completing preflight")
            async def create_page(self, *args):
                raise AssertionError("created before completing preflight")
        items = [{"path": f"/docs/00{i}", "local_path": f"doc/00{i}.md", "body": wrap_page("new", page_id=f"b{i}")} for i in (1, 2)]
        rows = {f"doc/00{i}.md": {"page_id": f"p{i}", "revision_id": "r0", "marker_id": f"b{i}"} for i in (1, 2)}
        with self.assertRaisesRegex(RuntimeError, "changed before publication"):
            asyncio.run(publish_pages(Client(), items, mode="attach", write_path="/docs", expected_pages=rows))

    def test_publish_preserves_remote_only_code_and_comments_below_stamp(self):
        tail = "\nRemote note `AA29` <!-- human warning E-900 -->\n```markdown\n<!-- llm-wiki-bot-ref:example -->\n```\n"
        class Client:
            async def get_page(self, **kwargs):
                return GrowiPage(page_id="p1", revision_id="r0", path="/docs/001", body=wrap_page("Old.\n", page_id="b1") + tail)
            async def update_page(self, page_id, revision_id, body):
                return GrowiPage(page_id=page_id, revision_id="r1", path="/docs/001", body=body)
        pages = asyncio.run(publish_pages(Client(), [{"path": "/docs/001", "local_path": "doc/001.md", "body": wrap_page("New.\n", page_id="b1")}],
                                         mode="attach", write_path="/docs", expected_pages={"doc/001.md": {"page_id": "p1", "revision_id": "r0", "marker_id": "b1"}}))
        self.assertTrue(pages[0].body.endswith(tail))

    def test_untracked_human_revision_is_not_adopted_as_bot_output(self):
        class Client:
            async def get_page(self, **kwargs):
                return GrowiPage(page_id="p", revision_id="human", path="/docs/001", body=wrap_page("human", page_id="b1"))
            async def update_page(self, *args):
                raise AssertionError("overwrote an unknown revision")
        with self.assertRaisesRegex(RuntimeError, "no inspected published baseline"):
            asyncio.run(publish_pages(Client(), [{"path": "/docs/001", "body": wrap_page("bot", page_id="b1")}],
                                     mode="attach", write_path="/docs"))

    def test_late_human_edit_blocks_page_deletion(self):
        class Client:
            async def list_all_pages(self, path):
                return [GrowiPage(page_id="p", revision_id="human", path="/docs/doc/001")]
            async def get_page(self, **kwargs):
                return GrowiPage(page_id="p", revision_id="human", path="/docs/doc/001", body=wrap_page("human", page_id="b1"))
            async def delete_pages(self, *args):
                raise AssertionError("deleted late human revision")
        publisher = GrowiPublisher(Client(), SimpleNamespace(write_path="/docs"))
        with self.assertRaisesRegex(RuntimeError, "changed before deletion"):
            asyncio.run(publisher._trash_under("/docs/doc", keep=set(), expected_revisions={"p": "r0"}))

    def test_sync_captures_before_parse_and_publishes_after_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp) / "mount"
            mount.mkdir()
            (mount / "doc.md").write_text("source", encoding="utf-8")
            settings = SimpleNamespace(data_root=str(Path(tmp) / "data"), target_name="project", mount_path=str(mount),
                                       ingest_mode="wiki", wiki_linker_enabled=False)
            calls = []
            def capture(*args, **kwargs):
                calls.append("capture")
                return [], [], set()
            def parse(*args, **kwargs):
                calls.append("parse")
                return "source"
            def generate(*args, **kwargs):
                calls.append("generate-and-overlay")
                return writer.WriteResult(target=Path(tmp), touched=[])
            def publish(*args, **kwargs):
                self.assertTrue(kwargs["captured"])
                calls.append("publish")
                return []
            with (patch.object(pipeline, "_publisher", return_value=object()),
                  patch.object(pipeline, "_capture_remote", side_effect=capture),
                  patch.object(pipeline, "_parse", side_effect=parse),
                  patch.object(pipeline, "write_wiki_pages", side_effect=generate),
                  patch.object(pipeline, "_publish_sweep", side_effect=publish),
                  patch.object(pipeline, "_model", return_value=object()),
                  patch.object(pipeline, "Embedder", return_value=object()),
                  patch.object(pipeline, "_pending_link_rels", return_value=[])):
                result = pipeline.sync_once(settings)
            self.assertFalse(result["failures"])
            self.assertEqual(calls, ["capture", "parse", "generate-and-overlay", "publish"])


class TransactionSafetyTest(unittest.TestCase):
    def test_store_is_part_of_candidate_clean_check_commit_and_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Project(Path(tmp) / "live").ensure()
            base = ensure_repository(project)
            with candidate(project, "human-store") as staged:
                store = HumanStore(staged)
                digest = store.put("exact human fact")
                self.assertFalse(candidate_is_clean(staged, base))
                commit = commit_candidate(staged, "human fact", {})
                self.assertTrue(candidate_is_clean(staged, commit))
                promote(project, staged, commit)
                self.assertEqual(last_good(project), commit)
                self.assertEqual(HumanStore(project).get(digest), "exact human fact")

    def _failed_publication(self, *, late_human=False, missing_revision_record=False,
                            old_page_removed=False, late_delete=False):
        from graph.wiki.storage import sha256_text
        from publisher.ledger import Ledger, load_ledger, save_ledger

        with tempfile.TemporaryDirectory() as tmp:
            live = Project(Path(tmp) / "live", Path(tmp) / "mount").ensure()
            raw_rel = "doc_md.md"
            document = "doc.md"
            live.mount.mkdir()
            (live.mount / "doc.md").write_text("source\n", encoding="utf-8")
            live.raw_file(raw_rel).write_text("source\n", encoding="utf-8")
            folder = live.wiki_dir(raw_rel)
            folder.mkdir()
            writer.write_source_stamp(folder, live.raw_file(raw_rel), raw_rel, identity_seed="source-1")
            store = HumanStore(live)
            store.generated(raw_rel, {"001.md": BASE})
            store.render(raw_rel)
            path = document + "/001.md"

            class Client:
                pages = {}
                updates = 0
                next_id = 1
                def copy(self, page):
                    return page.model_copy(deep=True) if page else None
                async def get_page(self, *, page_id=None, path=None):
                    if page_id:
                        return self.copy(self.pages.get(page_id))
                    return self.copy(next((p for p in self.pages.values() if p.path == path), None))
                async def create_page(self, path, body):
                    while f"p{self.next_id}" in self.pages:
                        self.next_id += 1
                    page = GrowiPage(page_id=f"p{self.next_id}", revision_id="created", path=path, body=body)
                    self.next_id += 1
                    self.pages[page.page_id] = page
                    return self.copy(page)
                async def update_page(self, page_id, revision_id, body):
                    page = self.pages[page_id]
                    if page.revision_id != revision_id:
                        raise GrowiAPIError(409, "PUT", "/page")
                    self.updates += 1
                    page.revision_id, page.body = f"bot-{self.updates}", body
                    return self.copy(page)
                async def list_all_pages(self, prefix):
                    return [self.copy(p) for p in self.pages.values() if p.path.startswith(prefix + "/")]
                async def delete_pages(self, revisions):
                    for page_id, revision in revisions.items():
                        if self.pages[page_id].revision_id != revision:
                            raise GrowiAPIError(409, "DELETE", "/page")
                    for page_id in revisions:
                        del self.pages[page_id]

            class FailOncePublisher(GrowiPublisher):
                fail = True
                def publish_documents(self, *args, **kwargs):
                    pages = super().publish_documents(*args, **kwargs)
                    if self.fail:
                        self.fail = False
                        raise RuntimeError("injected failure after remote writes")
                    return pages

            client = Client()
            publisher = FailOncePublisher(client, SimpleNamespace(write_path="/docs", root_path="/docs", mode="attach"))
            marker = publisher.page_marker_id(live, path)
            row = {"page_id": "p1", "revision_id": "r0", "marker_id": marker, "growi_path": "/docs/doc/001"}
            store.remember_page(path, row, BASE, BASE, published=True)
            client.pages["p1"] = GrowiPage(page_id="p1", revision_id="r0", path=row["growi_path"], body=wrap_page(BASE, page_id=marker))
            save_ledger(live.metadata / "pipeline.json", Ledger(
                {"doc.md": {"raw_rel": raw_rel, "source_id": "source-1", "id_seed": "source-1", "source_sha256": sha256_text("source\n")}},
                {document: {"raw_rel": raw_rel, "content_sha256": pipeline._content_hash(folder), "growi_path": "/docs/doc"}},
                {path: row},
            ))
            ensure_repository(live)
            client.pages["p1"].body = wrap_page(HUMAN, page_id=marker)
            client.pages["p1"].revision_id = "human-r1"
            known = {}
            def record(page):
                known.setdefault(page.page_id, set()).add(page.revision_id)
            def rebuild(**kwargs):
                docs = Path(kwargs["out_dir"]) / "docs"
                docs.mkdir(parents=True)
                write_text_atomic(docs / ("002.md" if old_page_removed else "001.md"), BASE.replace("40°C", "55°C"))
                return SimpleNamespace(out_dir=Path(kwargs["out_dir"]))
            live_settings = SimpleNamespace(data_root=tmp, target_name="live", mount_path=str(live.mount), ingest_mode="wiki", wiki_linker_enabled=False)
            with candidate(live, "failed-human-publish") as staged:
                write_text_atomic(staged.mount / "doc.md", "changed source\n")
                settings = SimpleNamespace(data_root=str(staged.root.parent), target_name=staged.root.name,
                                           mount_path=str(staged.mount), ingest_mode="wiki", wiki_linker_enabled=False)
                with (patch.object(pipeline, "_publisher", return_value=publisher),
                      patch.object(pipeline, "_model", return_value=object()),
                      patch.object(pipeline, "Embedder", return_value=object()),
                      patch.object(pipeline, "_parse", return_value="changed source\n"),
                      patch.object(writer, "build_wiki_output", side_effect=rebuild),
                      patch("publisher.index.build_index", return_value={"failures": []})):
                    result = pipeline.sync_once(settings, only=["doc.md"], on_revision=record)
                    self.assertTrue(result["failures"])
                    updated = next(page for page in client.pages.values() if page.path.endswith("/002" if old_page_removed else "/001"))
                    self.assertIn("55°C", updated.body)
                    if missing_revision_record:
                        known["p1"] = set()
                    if late_delete:
                        del client.pages["p1"]
                        known.pop("p1", None)
                        staged_ledger = load_ledger(staged.metadata / "pipeline.json")
                        staged_ledger.published_pages.pop(path)
                        save_ledger(staged.metadata / "pipeline.json", staged_ledger)
                        remaining = {key: value.body for key, value in client.pages.items()}
                        with self.assertRaisesRegex(RuntimeError, "disappeared during publication"):
                            pipeline.restore_publication(live_settings, staged, [], known_revisions=known)
                        self.assertEqual({key: value.body for key, value in client.pages.items()}, remaining)
                    elif late_human:
                        client.pages["p1"].body = wrap_page("Late human E-902.\n", page_id=marker)
                        client.pages["p1"].revision_id = "late-human"
                        updates = client.updates
                        with self.assertRaisesRegex(RuntimeError, "unknown GROWI revision"):
                            pipeline.restore_publication(live_settings, staged, [], known_revisions=known)
                        self.assertEqual(client.updates, updates)
                        self.assertIn("Late human E-902.", client.pages["p1"].body)
                    else:
                        pipeline.restore_publication(live_settings, staged, [], known_revisions=known)
                        restored = load_ledger(live.metadata / "pipeline.json").published_pages[path]
                        restored_page = client.pages[restored["page_id"]]
                        self.assertIn("60°C", restored_page.body)
                        self.assertNotIn("55°C", restored_page.body)
                        self.assertEqual(len(client.pages), 1)
                        self.assertTrue(candidate_is_clean(live, last_good(live)))
                        self.assertEqual(restored["revision_id"], restored_page.revision_id)

    def test_failed_publication_restores_old_source_with_new_human_journal(self):
        self._failed_publication()

    def test_unknown_human_revision_during_rollback_is_never_overwritten(self):
        self._failed_publication(late_human=True)

    def test_unrecorded_bot_revision_recovers_only_with_exact_prepared_body(self):
        self._failed_publication(missing_revision_record=True)

    def test_acknowledged_bot_page_deletion_can_be_restored(self):
        self._failed_publication(old_page_removed=True)

    def test_missing_base_only_page_without_bot_deletion_is_not_recreated(self):
        self._failed_publication(late_delete=True)


class TierIntegrationTest(unittest.TestCase):
    def test_human_change_survives_tiers_one_two_three_and_zero(self):
        from tests import test_update_tiers as tiers
        with tempfile.TemporaryDirectory() as tmp:
            page = "# P1\n\nline 5\n\nOperator limit 40°C.\n"
            project, rel, state = tiers.make_project(tmp, [(1, tiers.COUNT, page)], tiers.OLD_SOURCE)
            project.raw_file(rel).write_text(tiers.OLD_SOURCE, encoding="utf-8")
            folder = project.wiki_dir(rel)
            folder.mkdir(parents=True)
            writer.write_source_stamp(folder, project.raw_file(rel), rel)
            store = HumanStore(project)
            store.generated(rel, {"001.md": page})
            row = {"marker_id": "b1", "revision_id": "r0", "observed_revision_id": "r1"}
            path = folder.relative_to(project.wiki).as_posix() + "/001.md"
            store.capture(rel, path, row, page, page.replace("40°C", "60°C"))
            store.render(rel)
            project.raw_file(rel).write_text(tiers.OLD_SOURCE.replace("line 5\n", "line 5 changed\n"), encoding="utf-8")
            model = tiers.PatchModel(patches=[{"edit_ids": [1], "before": "line 5", "after": "line 5 changed"}])
            first = writer.write_wiki_pages(project, rel, mode="wiki", settings=tiers.SETTINGS, llm=model, embedder=None)
            self.assertEqual(first.tier, 1)
            self.assertNotIn("60°C", (state / "wiki" / "001.md").read_text())
            zero = writer.write_wiki_pages(project, rel, mode="wiki", settings=tiers.SETTINGS, llm=None, embedder=None)
            self.assertEqual(zero.tier, 0)
            self.assertEqual((folder / "001.md").read_text().count("60°C"), 1)
            project.raw_file(rel).write_text(tiers.OLD_SOURCE.replace("line 5\n", "line 5 second\n"), encoding="utf-8")
            def regenerate(**kwargs):
                write_text_atomic(state / "wiki" / "001.md", "# P1\n\nFresh source 55°C.\n")
                from graph.wiki.export import export_ingest_layout
                return SimpleNamespace(out_dir=export_ingest_layout(state, kwargs["out_dir"], document_name=rel))
            with patch.object(writer, "build_wiki_output", side_effect=regenerate), patch("graph.wiki.incremental.FULL_MIN_REGEN_SHARE", 1.0):
                second = writer.write_wiki_pages(project, rel, mode="wiki", settings=tiers.SETTINGS, llm=tiers.PatchModel(fail=True), embedder=None)
            self.assertEqual(second.tier, 2)
            self.assertIn("60°C", (folder / "001.md").read_text())
            def rebuild(**kwargs):
                docs = Path(kwargs["out_dir"]) / "docs"
                docs.mkdir(parents=True)
                write_text_atomic(docs / "001.md", "# P1\n\nFully rebuilt source.\n")
                return SimpleNamespace(out_dir=Path(kwargs["out_dir"]))
            with patch.object(writer, "build_wiki_output", side_effect=rebuild):
                third = writer.write_wiki_pages(project, rel, mode="wiki", settings=tiers.SETTINGS, llm=None, embedder=None, resume=False)
            self.assertEqual(third.tier, 3)
            self.assertEqual((folder / "001.md").read_text().count("60°C"), 1)
            store.render(rel)
            self.assertEqual((folder / "001.md").read_text().count("60°C"), 1)


class PartialPublicationTest(unittest.TestCase):
    """A standalone publish that dies mid-batch must never look like human intent."""

    raw_rel = "doc_md.md"
    document = "doc.md"
    bot = BASE.replace("40°C", "55°C")

    def _harness(self, tmp, *, pages=("001.md", "002.md"), ledgered=("001.md", "002.md")):
        from graph.wiki.storage import sha256_text
        from publisher.ledger import Ledger, load_ledger, save_ledger

        live = Project(Path(tmp) / "live", Path(tmp) / "mount").ensure()
        live.mount.mkdir()
        (live.mount / "doc.md").write_text("source\n", encoding="utf-8")
        live.raw_file(self.raw_rel).write_text("source\n", encoding="utf-8")
        folder = live.wiki_dir(self.raw_rel)
        folder.mkdir()
        writer.write_source_stamp(folder, live.raw_file(self.raw_rel), self.raw_rel, identity_seed="source-1")
        write_json_atomic(folder / "_planning" / "linker.json", {"status": "complete"})
        store = HumanStore(live)
        store.generated(self.raw_rel, dict.fromkeys(pages, BASE))
        store.render(self.raw_rel)

        class Client:
            def __init__(self):
                self.pages = {}
                self.updates = 0
                self.next_id = 3
                self.fail_update = ""
                self.lose_response = ""
                self.human_edit_on_put = ""
            def copy(self, page):
                return page.model_copy(deep=True) if page else None
            async def get_page(self, *, page_id=None, path=None):
                if page_id:
                    return self.copy(self.pages.get(page_id))
                return self.copy(next((p for p in self.pages.values() if p.path == path), None))
            async def create_page(self, path, body):
                page = GrowiPage(page_id=f"p{self.next_id}", revision_id="created", path=path, body=body)
                self.next_id += 1
                self.pages[page.page_id] = page
                if self.lose_response == "create":
                    raise ConnectionError("lost response after the server accepted the write")
                return self.copy(page)
            async def update_page(self, page_id, revision_id, body):
                page = self.pages[page_id]
                if self.human_edit_on_put == page_id:
                    # A human save lands after our preflight, so the PUT is rejected.
                    page.body = page.body.replace("Mode is AUTO.", "Mode is MANUAL.")
                    page.revision_id = "human-1"
                if page.revision_id != revision_id:
                    raise GrowiAPIError(409, "PUT", "/page")
                if self.fail_update == page_id:
                    raise GrowiAPIError(500, "PUT", "/page")
                self.updates += 1
                page.revision_id, page.body = f"bot-{self.updates}", body
                if self.lose_response == page_id:
                    raise ConnectionError("lost response after the server accepted the write")
                return self.copy(page)
            async def list_all_pages(self, prefix):
                return [self.copy(p) for p in self.pages.values() if p.path.startswith(prefix + "/")]
            async def delete_pages(self, revisions):
                for page_id, revision in revisions.items():
                    if self.pages[page_id].revision_id != revision:
                        raise GrowiAPIError(409, "DELETE", "/page")
                for page_id in revisions:
                    del self.pages[page_id]

        client = Client()
        publisher = GrowiPublisher(client, SimpleNamespace(write_path="/docs", root_path="/docs", mode="attach"))
        document = live.wiki_dir(self.raw_rel).relative_to(live.wiki).as_posix()
        doc_path = publisher.doc_path(live, self.raw_rel)
        rows = {}
        for index, page in enumerate(publisher._document_pages(live, self.raw_rel), start=1):
            path, page_path = page["local_path"], page["path"]
            if Path(path).name not in ledgered:
                continue
            marker = publisher.page_marker_id(live, path)
            row = {"page_id": f"p{index}", "revision_id": "r0", "marker_id": marker, "growi_path": page_path}
            store.remember_page(path, row, BASE, BASE, published=True)
            rows[path] = row
            client.pages[row["page_id"]] = GrowiPage(page_id=row["page_id"], revision_id="r0",
                                                     path=page_path, body=wrap_page(BASE, page_id=marker))
        save_ledger(live.metadata / "pipeline.json", Ledger(
            {self.document: {"raw_rel": self.raw_rel, "source_id": "source-1", "id_seed": "source-1",
                             "source_sha256": sha256_text("source\n")}},
            {document: {"raw_rel": self.raw_rel, "content_sha256": pipeline._content_hash(folder),
                        "growi_path": doc_path}},
            rows,
        ))
        ensure_repository(live)
        settings = SimpleNamespace(data_root=tmp, target_name="live", mount_path=str(live.mount),
                                   ingest_mode="wiki", wiki_linker_enabled=False)
        return live, folder, store, client, publisher, settings, rows

    def _ledger(self, live):
        from publisher.ledger import load_ledger

        return load_ledger(live.metadata / "pipeline.json")

    def _publish(self, publisher, settings):
        with (patch.object(pipeline, "_publisher", return_value=publisher),
              patch("publisher.index.build_index", return_value={"failures": []})):
            return pipeline.publish_only(settings)

    def _pull(self, publisher, settings):
        with patch.object(pipeline, "_publisher", return_value=publisher):
            return pipeline.pull_growi_once(settings)

    def _regenerate(self, folder, store, names):
        for name in names:
            write_text_atomic(folder / name, self.bot)
        # Mirror the real writer: save the pure generated pages before rendering
        # any authoritative human overlay into the effective wiki.
        store.generated(self.raw_rel, {page.name: page.read_text(encoding="utf-8") for page in folder.glob("*.md")})
        store.render(self.raw_rel)

    def test_linker_links_are_not_recorded_as_part_of_a_human_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, _client, _publisher, _settings, rows = self._harness(tmp)
            path = f"{self.document}/001.md"
            row = dict(rows[path], observed_revision_id="human-1")
            linked = BASE.replace("Mode is AUTO.", "[Mode](002.md) is AUTO.")  # what the linker published
            human = linked.replace("Maximum is 40°C.", "Maximum is 40°C (checked).")

            def stored(unlinked):
                fresh = HumanStore(live)
                fresh.capture(self.raw_rel, path, dict(row), linked, human, generated_before=BASE, unlinked=unlinked)
                edits = [e for e in fresh.document(self.raw_rel)["edits"] if e["status"] != "deleted"]
                self.assertEqual(len(edits), 1)
                return fresh.get(edits[0]["human_after_blob"])

            # only the human's change is stored against the pure block
            self.assertEqual(stored(BASE), "## Limits\n\nMaximum is 40°C (checked).\nMode is AUTO.\n\n")
            store.document(self.raw_rel)["edits"].clear()
            store.save(store.document(self.raw_rel))

    def test_unrecorded_published_page_is_not_captured_as_a_human_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, client, publisher, settings, _rows = self._harness(tmp)
            self._regenerate(folder, store, ("001.md", "002.md"))
            client.fail_update = "p1"  # 002 publishes, then 001 fails the batch.
            self.assertTrue(self._publish(publisher, settings)["failures"])
            self.assertIn("55°C", client.pages["p2"].body)
            self.assertNotIn("55°C", client.pages["p1"].body)
            marker = publisher.page_marker_id(live, f"{self.document}/002.md")
            prepared = store.page(marker)
            self.assertTrue(prepared["prepared_attempt_id"].startswith("hattempt-"))
            self.assertEqual(prepared["publication_confirmation"]["revision"], client.pages["p2"].revision_id)
            self.assertEqual(prepared["attempt_history"][-1]["status"], "confirmed")
            self.assertEqual(self._ledger(live).published_pages[f"{self.document}/002.md"]["revision_id"], "r0")
            # A failed publish still checkpoints durable state, so recovery is not blocked.
            self.assertTrue(candidate_is_clean(live, last_good(live)))
            self.assertFalse(self._pull(publisher, settings)["failures"])
            self.assertEqual(store.document(self.raw_rel)["edits"], [])
            self.assertEqual(self._ledger(live).published_pages[f"{self.document}/002.md"]["revision_id"],
                             client.pages["p2"].revision_id)
            settled = store.page(marker)
            self.assertNotIn("prepared_attempt_id", settled)
            self.assertEqual(settled["attempt_history"][-1]["status"], "recovered_exact")
            self.assertEqual(self._ledger(live).published_pages[f"{self.document}/001.md"]["revision_id"], "r0")
            client.fail_update = ""
            self.assertFalse(self._publish(publisher, settings)["failures"])
            self.assertIn("55°C", client.pages["p1"].body)
            self.assertEqual(store.document(self.raw_rel)["edits"], [])
            self.assertTrue(candidate_is_clean(live, last_good(live)))

    def test_lost_update_response_recovers_from_the_exact_prepared_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, client, publisher, settings, _rows = self._harness(tmp)
            self._regenerate(folder, store, ("001.md", "002.md"))
            client.lose_response = "p2"
            self.assertTrue(self._publish(publisher, settings)["failures"])
            self.assertIn("55°C", client.pages["p2"].body)
            self.assertFalse(self._pull(publisher, settings)["failures"])
            self.assertEqual(store.document(self.raw_rel)["edits"], [])
            self.assertEqual(self._ledger(live).published_pages[f"{self.document}/002.md"]["revision_id"],
                             client.pages["p2"].revision_id)
            client.lose_response = ""
            self.assertFalse(self._publish(publisher, settings)["failures"])
            self.assertEqual(client.updates, 2)
            self.assertTrue(candidate_is_clean(live, last_good(live)))

    def test_created_page_with_lost_create_response_is_published_on_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, client, publisher, settings, _rows = self._harness(tmp, ledgered=("001.md",))
            self._regenerate(folder, store, ("001.md", "002.md"))
            client.lose_response = "create"
            self.assertTrue(self._publish(publisher, settings)["failures"])
            self.assertNotIn(f"{self.document}/002.md", self._ledger(live).published_pages)
            client.lose_response = ""
            self.assertFalse(self._publish(publisher, settings)["failures"])
            published = self._ledger(live).published_pages[f"{self.document}/002.md"]
            self.assertEqual(published["page_id"], "p3")
            self.assertEqual(client.pages["p3"].body.count("55°C"), 1)
            self.assertEqual(store.document(self.raw_rel)["edits"], [])

    def test_human_edit_on_an_unledgered_created_page_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, client, publisher, settings, _rows = self._harness(tmp, ledgered=("001.md",))
            self._regenerate(folder, store, ("001.md", "002.md"))
            client.lose_response = "create"
            self.assertTrue(self._publish(publisher, settings)["failures"])
            path = f"{self.document}/002.md"
            marker = publisher.page_marker_id(live, path)
            client.pages["p3"].body = wrap_page(self.bot + "Reviewer note E-902.\n", page_id=marker)
            client.pages["p3"].revision_id = "late-human"
            self.assertTrue([problem for problem in self._publish(publisher, settings)["failures"]
                             if "no inspected published baseline" in problem])
            self.assertIn("Reviewer note E-902.", client.pages["p3"].body)
            self.assertEqual(store.page(marker)["publication_error"], "missing_published_snapshot")
            self.assertIn("Reviewer note E-902.", store.get(store.page(marker)["observed_remote_blob"]))

    def test_prepared_write_needs_exact_body_page_identity_and_a_moved_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, _folder, store, _client, publisher, _settings, _rows = self._harness(tmp)
            path = f"{self.document}/002.md"
            marker = publisher.page_marker_id(live, path)
            store.generated(self.raw_rel, {"001.md": BASE, "002.md": self.bot})
            generated_blob = store.document(self.raw_rel)["pages"]["002.md"]["body_blob"]
            effective = self.bot.replace("Mode is AUTO.", "Mode is MANUAL.")
            store.save_page(dict(store.page(marker), prepared_path="/docs/doc/002", prepared_page_id="p2",
                                 prepared_revision="r0", prepared_remote_blob=store.put("exact prepared body"),
                                 prepared_local_blob=store.put(effective), prepared_generated_blob=generated_blob))
            newer = self.bot.replace("55°C", "60°C")
            store.generated(self.raw_rel, {"001.md": BASE, "002.md": newer})
            ours = GrowiPage(page_id="p2", revision_id="bot-1", path="/docs/doc/002", body="exact prepared body")
            self.assertTrue(store.prepared_match(marker, ours))
            self.assertEqual(store.prepared_match(marker, ours.model_copy(update={"body": "someone changed it"})), {})
            self.assertEqual(store.prepared_match(marker, ours.model_copy(update={"page_id": "p9"})), {})
            # A page still at the inspected revision never received our write.
            stalled = ours.model_copy(update={"revision_id": "r0"})
            self.assertEqual(store.prepared_match(marker, stalled), {})
            self.assertEqual(store.account_prepared(dict(store.page(marker)), stalled), {})
            self.assertEqual(store.account_prepared(dict(store.page(marker)), ours), {"exact": True})
            self.assertEqual(store.get(store.document(self.raw_rel)["pages"]["002.md"]["body_blob"]), newer)
            self.assertEqual(store.page(marker)["generated_blob"], generated_blob)
            self.assertNotIn("MANUAL", store.get(generated_blob))
            for key in ("prepared_path", "prepared_page_id", "prepared_revision",
                        "prepared_remote_blob", "prepared_local_blob", "prepared_generated_blob"):
                self.assertNotIn(key, store.page(marker))
            store.save_page(dict(store.page(marker), prepared_path="/docs/doc/002", prepared_page_id="p2",
                                 prepared_revision="bot-1", prepared_remote_blob=store.put("exact prepared body"),
                                 prepared_local_blob=store.put(effective), prepared_generated_blob=generated_blob))
            late = GrowiPage(page_id="p2", revision_id="late-human", path="/docs/doc/002", body="someone changed it")
            rebased = store.account_prepared(dict(store.page(marker)), late)
            self.assertFalse(rebased["exact"])
            self.assertEqual((rebased["remote"], rebased["local"], rebased["generated"]),
                             ("exact prepared body", effective, self.bot))

    def test_human_edit_after_a_partial_write_is_captured_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, client, publisher, settings, _rows = self._harness(tmp)
            self._regenerate(folder, store, ("001.md", "002.md"))
            client.fail_update = "p1"
            self.assertTrue(self._publish(publisher, settings)["failures"])
            human = client.pages["p2"].body.replace("Mode is AUTO.", "Mode is MANUAL.")
            self.assertIn("55°C", human)
            client.pages["p2"].body = human
            client.pages["p2"].revision_id = "late-human"
            self.assertFalse(self._pull(publisher, settings)["failures"])
            edits = [edit for edit in store.document(self.raw_rel)["edits"] if edit["status"] != "deleted"]
            self.assertTrue(edits)
            # Only the human delta is intent: the bot's 40 to 55 change is generated state.
            self.assertIn("55°C", " ".join(store.get(edit["base_before_blob"]) for edit in edits))
            delta = " ".join(store.get(item["replacement_blob"]) for edit in edits for item in edit["human_delta"])
            self.assertIn("MANUAL", delta)
            self.assertNotIn("55°C", delta)
            self.assertNotIn("40°C", delta)
            self.assertEqual(client.pages["p2"].body, human)
            client.fail_update = ""
            self.assertFalse(self._publish(publisher, settings)["failures"])
            self.assertIn("Mode is MANUAL.", client.pages["p2"].body)

    def test_409_race_uses_the_old_baseline_not_the_write_that_never_landed(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, client, publisher, settings, _rows = self._harness(tmp)
            self._regenerate(folder, store, ("001.md", "002.md"))
            path = f"{self.document}/002.md"
            marker = publisher.page_marker_id(live, path)
            client.human_edit_on_put = "p2"
            self.assertTrue([problem for problem in self._publish(publisher, settings)["failures"]
                             if problem.startswith("publish:")])
            self.assertEqual(store.page(marker)["publication_error"], "revision_race")
            self.assertNotIn("55°C", client.pages["p2"].body)
            self.assertIn("Mode is MANUAL.", client.pages["p2"].body)
            self.assertFalse(self._pull(publisher, settings)["failures"])
            edits = [edit for edit in store.document(self.raw_rel)["edits"] if edit["status"] != "deleted"]
            self.assertTrue(edits)
            # The rejected transport is not human intent: the old published
            # baseline stays authoritative for the human delta. The legitimate
            # current source generation remains pure state and combines later.
            self.assertIn("40°C", " ".join(store.get(edit["base_before_blob"]) for edit in edits))
            delta = " ".join(store.get(item["replacement_blob"]) for edit in edits for item in edit["human_delta"])
            self.assertIn("MANUAL", delta)
            self.assertNotIn("55°C", delta)
            self.assertEqual(store.document(self.raw_rel)["pages"]["002.md"]["body_blob"], store.put(self.bot))
            self.assertIn("55°C", (folder / "002.md").read_text(encoding="utf-8"))
            self.assertIn("Mode is MANUAL.", (folder / "002.md").read_text(encoding="utf-8"))
            self.assertEqual(self._ledger(live).published_pages[path]["revision_id"], "human-1")
            self.assertFalse(self._publish(publisher, settings)["failures"])
            self.assertIn("Mode is MANUAL.", client.pages["p2"].body)
            self.assertIn("55°C", client.pages["p2"].body)
            self.assertNotIn("publication_error", store.page(marker))

    def test_source_change_after_a_rebased_edit_does_not_conflict_or_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, folder, store, client, publisher, settings, _rows = self._harness(tmp)
            self._regenerate(folder, store, ("001.md", "002.md"))
            client.fail_update = "p1"
            self.assertTrue(self._publish(publisher, settings)["failures"])
            client.pages["p2"].body = client.pages["p2"].body.replace("Mode is AUTO.", "Mode is MANUAL.")
            client.pages["p2"].revision_id = "late-human"
            self.assertFalse(self._pull(publisher, settings)["failures"])
            # The next build legitimately carries the published bot change.
            store.generated(self.raw_rel, {"001.md": BASE, "002.md": self.bot.replace("Unchanged.", "Revised.")})
            store.render(self.raw_rel)
            page = (folder / "002.md").read_text(encoding="utf-8")
            self.assertNotIn("conflict", [edit["status"] for edit in store.document(self.raw_rel)["edits"]])
            self.assertEqual(page.count("55°C"), 1)
            self.assertNotIn("40°C", page)
            self.assertIn("Mode is MANUAL.", page)
            self.assertIn("Revised.", page)


if __name__ == "__main__":
    unittest.main()


class TransportSpellingMergeTest(unittest.TestCase):
    """GROWI holds inline code without backticks; adjacent spelling-only lines must not
    swallow a human token edit when the capture rebases it onto the local page."""

    def test_human_edit_inside_a_multi_line_spelling_hunk_is_captured(self):
        from publisher.human_changes import merge

        remote = "- 供給温度は通常 18 °C に設定します。\n- 戻り温度が 26 °C を超えた場合は確認する。\n"
        human = "- 供給温度は通常 18 °C に設定します（夏季は 16 °C）。\n- 戻り温度が 26 °C を超えた場合は確認する。\n"
        local = "- 供給温度は通常 `18 °C` に設定します。\n- 戻り温度が `26 °C` を超えた場合は確認する。\n"
        merged, status = merge(remote, human, local)
        self.assertEqual(status, "active")
        self.assertEqual(merged, "- 供給温度は通常 `18 °C` に設定します（夏季は 16 °C）。\n- 戻り温度が `26 °C` を超えた場合は確認する。\n")

    def test_same_token_still_conflicts_inside_a_multi_line_hunk(self):
        from publisher.human_changes import merge

        base = "- 高温警報: 60 °C\n- 低圧警報: 0.15 MPa\n"
        human = "- 高温警報: 65 °C\n- 低圧警報: 0.15 MPa\n"
        source = "- 高温警報: 55 °C\n- 低圧警報: 0.20 MPa\n"
        self.assertEqual(merge(base, human, source)[1], "conflict")
