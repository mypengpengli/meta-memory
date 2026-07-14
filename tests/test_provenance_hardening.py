from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from meta_memory.config import AppConfig
from meta_memory.dream import run_dream
from meta_memory.legacy import bootstrap

bootstrap()
from _common import ensure_store_ready, open_db
from assemble_context import assemble_context
from build_hot_memory import build_hot_memory
from ingest_raw_event import insert_raw_event
from retrieve_memories import parse_args, retrieve


class ProvenanceHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = self.root / "store"
        ensure_store_ready(self.store)
        self.config = AppConfig(
            path=self.root / "config.toml",
            user_name="Ada",
            user_id="ada",
            store=self.store,
        )
        self.subject_id = self.config.subject_id
        self.profile_id = self.config.profile_id
        self.workspace_id = "project:provenance"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _claim(
        self,
        content: str,
        *,
        verification_state: str = "verified",
        prompt_eligible: bool = True,
        visibility_scope: str = "workspace",
        source_type: str = "conversation-user",
        origin_agent_id: str = "codex",
    ) -> str:
        event_id = int(
            insert_raw_event(
                self.store,
                subject_id=self.subject_id,
                subject_name="Ada",
                session_id="provenance-test",
                source_type=source_type,
                source_ref=f"test:{uuid.uuid4()}",
                content=content,
                profile_id=self.profile_id,
                workspace_id=self.workspace_id,
                origin_agent_id=origin_agent_id,
                visibility_scope=visibility_scope,
            )["raw_event_id"]
        )
        claim_id = str(uuid.uuid4())
        conn = open_db(self.store)
        try:
            conn.execute(
                """
                INSERT INTO claims(
                    id,subject_id,subject_name,memory_kind,domain,topic,title,content,content_hash,
                    status,verification_state,confidence,importance,sensitivity,prompt_eligible,
                    security_state,profile_id,workspace_id,visibility_scope,owner_agent_id,origin_agent_id
                ) VALUES(?, ?, 'Ada', 'state', 'work', 'provenance', ?, ?, ?, 'active', ?, .9, .9,
                         'normal', ?, 'clean', ?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    self.subject_id,
                    content[:48],
                    content,
                    hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    verification_state,
                    int(prompt_eligible),
                    self.profile_id,
                    self.workspace_id,
                    visibility_scope,
                    origin_agent_id if visibility_scope == "agent" else None,
                    origin_agent_id,
                ),
            )
            conn.execute(
                "INSERT INTO claim_sources(claim_id,raw_event_id,source_role) VALUES(?, ?, 'supports')",
                (claim_id, event_id),
            )
            conn.commit()
        finally:
            conn.close()
        return claim_id

    def _retrieval_args(self, *, query: str, include_inferred: bool) -> argparse.Namespace:
        return argparse.Namespace(
            store=str(self.store),
            query=query,
            query_file=None,
            top_k=12,
            candidate_pool=30,
            expand_hops=0,
            session_id="",
            workspace_id=self.workspace_id,
            profile_id=self.profile_id,
            agent_id="codex",
            active_subject_id=[],
            valid_at=None,
            no_chunks=True,
            include_embeddings=False,
            embedding_model="external",
            rrf_k=60,
            include_dreams=True,
            include_inferred_dreams=include_inferred,
            include_resources=False,
            subject_id=self.subject_id,
            subject_name=None,
            domain=[],
            memory_kind=[],
            include_candidates=False,
            no_basics=True,
        )

    def test_context_and_hot_projection_label_agent_or_tool_evidence(self) -> None:
        context = assemble_context(
            {
                "selected": [
                    {
                        "id": "agent-claim",
                        "title": "Observed service state",
                        "memory_kind": "state",
                        "summary": "A tool observed that the service is healthy.",
                        "query_score": 1.0,
                        "prompt_eligible": True,
                        "verification_state": "agent_observed",
                    },
                    {
                        "id": "inferred-dream",
                        "title": "Inferred Dream",
                        "memory_kind": "dream",
                        "summary": "must-not-enter-normal-context",
                        "query_score": 1.0,
                        "prompt_eligible": True,
                        "inference_level": "inferred",
                        "admin_only": True,
                    },
                ]
            },
            {
                "results": [
                    {
                        "id": 7,
                        "effective_time": "2026-07-14T00:00:00Z",
                        "source_type": "tool-result",
                        "source_ref": "tool:healthcheck",
                        "snippet": "healthy from a tool result",
                    }
                ]
            },
        )
        self.assertIn("[Agent-observed]", context)
        self.assertIn("Agent-observed (tool-result)", context)
        self.assertNotIn("must-not-enter-normal-context", context)

        self._claim(
            "The deployment tool observed a healthy service.",
            verification_state="agent_observed",
            source_type="tool-result",
        )
        hot = build_hot_memory(
            self.store,
            subject_id=self.subject_id,
            profile_id=self.profile_id,
            workspace_id=self.workspace_id,
            agent_id="codex",
            force=True,
        )
        current = (Path(str(hot["scope"])) / "CURRENT.md").read_text(encoding="utf-8")
        self.assertIn("[Agent-observed]", current)

    def test_dream_filters_private_resource_evidence_and_deep_retrieval_is_non_prompt(self) -> None:
        observed_id = self._claim(
            "An agent observed that provenance token alpha is active.",
            verification_state="agent_observed",
            source_type="agent-observation",
        )
        resource_id = self._claim(
            "resource-only-dream-leak-token",
            verification_state="resource",
            prompt_eligible=False,
            source_type="resource",
        )
        private_id = self._claim(
            "private-agent-dream-leak-token",
            verification_state="agent_observed",
            visibility_scope="agent",
            source_type="agent-observation",
            origin_agent_id="private-agent",
        )
        self.config.dream_provider = "command"
        self.config.dream_command = "unused-in-test"
        semantic_text = "deep-dream-signal-omega"
        with patch(
            "meta_memory.dream_provider.command_synthesize",
            return_value={
                "project_digest": [semantic_text],
                "patterns": [],
                "procedure_candidates": [],
                "open_questions": [],
            },
        ):
            result = run_dream(self.config, scan_days=1)
        self.assertEqual(result["status"], "ok")

        conn = open_db(self.store)
        try:
            extractive = conn.execute(
                """
                SELECT content,source_claim_ids FROM dream_nodes
                WHERE profile_id=? AND workspace_id=? AND subject_id=?
                  AND node_type='project_digest' AND inference_level='extractive'
                """,
                (self.profile_id, self.workspace_id, self.subject_id),
            ).fetchone()
            inferred = conn.execute(
                """
                SELECT content,prompt_eligible,status,inference_level,source_claim_ids FROM dream_nodes
                WHERE profile_id=? AND workspace_id=? AND subject_id=?
                  AND content=?
                """,
                (self.profile_id, self.workspace_id, self.subject_id, semantic_text),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(extractive)
        self.assertIn("[Agent-observed]", str(extractive[0]))
        source_ids = json.loads(str(extractive[1]))
        self.assertIn(observed_id, source_ids)
        self.assertNotIn(resource_id, source_ids)
        self.assertNotIn(private_id, source_ids)
        self.assertNotIn("resource-only-dream-leak-token", str(extractive[0]))
        self.assertNotIn("private-agent-dream-leak-token", str(extractive[0]))
        self.assertEqual(tuple(inferred[1:4]), (0, "inferred", "inferred"))
        self.assertEqual(json.loads(str(inferred[4])), [observed_id])

        normal = retrieve(self._retrieval_args(query=semantic_text, include_inferred=False))
        self.assertFalse(any(item["page_role"] == "dream-inference" for item in normal["selected"]))
        deep = retrieve(self._retrieval_args(query=semantic_text, include_inferred=True))
        inference = next(item for item in deep["selected"] if item["page_role"] == "dream-inference")
        self.assertTrue(inference["admin_only"])
        self.assertFalse(inference["prompt_eligible"])
        self.assertEqual(inference["inference_level"], "inferred")
        self.assertNotIn(semantic_text, assemble_context({"selected": deep["selected"]}))

        with patch.object(sys, "argv", ["retrieve_memories.py", "--query", semantic_text, "--deep"]):
            self.assertTrue(parse_args().include_inferred_dreams)


if __name__ == "__main__":
    unittest.main()
