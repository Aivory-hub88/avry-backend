#!/usr/bin/env python3
"""
WhatsApp deploy (Kapso) service + routes.
Run: JWT_SECRET=... python3 -m unittest tests.test_whatsapp_service
"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

from app.database.db_service import DatabaseService  # noqa: E402
from app.services import whatsapp_service as ws  # noqa: E402

USER = {"user_id": "user_1", "tier": "business", "is_active": True, "account_type": "paid"}
WA = "6281234567890"


def make_service(addon=True, user=USER):
    tmp = tempfile.TemporaryDirectory()
    db = DatabaseService(base_path=tmp.name)
    tg = mock.Mock()
    tg._load_user.return_value = user
    tg._is_active.return_value = True
    svc = ws.WhatsAppService(db, telegram=tg)
    if addon:
        svc.set_addon("user_1", True)
    svc._tmp = tmp  # keep the directory alive
    return svc


def inbound(text="halo", message_id="m1", **kw):
    return {"message_id": message_id, "from": WA, "name": "Budi", "type": "text", "text": text, **kw}


class FormatTest(unittest.TestCase):
    def test_parse_link_code(self):
        self.assertEqual(ws.parse_link_code("LINK-abc234"), "ABC234")
        self.assertEqual(ws.parse_link_code("  link-ABC234  "), "ABC234")
        self.assertIsNone(ws.parse_link_code("halo LINK-ABC234"))
        self.assertIsNone(ws.parse_link_code("LINK-AB"))

    def test_markdown_to_whatsapp(self):
        self.assertEqual(ws.to_whatsapp_format("**tebal** dan [situs](https://a.id)"), "*tebal* dan situs (https://a.id)")
        self.assertEqual(ws.to_whatsapp_format("## Judul"), "*Judul*")

    def test_split_message_respects_limit(self):
        text = "\n".join(["x" * 900] * 12)
        parts = ws.split_message(text, limit=4096)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 4096 for p in parts))
        self.assertEqual(ws.split_message(""), [])


class LinkTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"KAPSO_DISPLAY_PHONE": "+62 812-0000-1111"})
        patcher.start()
        self.addCleanup(patcher.stop)
        tier = mock.patch.object(ws, "agent_tier_error", return_value=None)
        tier.start()
        self.addCleanup(tier.stop)

    def test_creates_short_token_and_wa_me_link(self):
        svc = make_service()
        out = svc.create_link_token("user_1", "leads_qualifier")
        self.assertEqual(len(out["token"]), 6)
        self.assertEqual(out["deep_link"], f"https://wa.me/6281200001111?text=LINK-{out['token']}")
        self.assertEqual(svc.get_link_status(out["token"], "user_1")["status"], "pending")
        self.assertEqual(svc.get_link_status(out["token"], "someone_else")["status"], "not_found")

    def test_addon_required(self):
        svc = make_service(addon=False)
        with self.assertRaises(ws.AddonRequired):
            svc.create_link_token("user_1", "leads_qualifier")

    def test_superadmin_bypasses_addon(self):
        svc = make_service(addon=False)
        with mock.patch.object(ws, "is_superadmin", return_value=True):
            self.assertTrue(svc.create_link_token("user_1", "leads_qualifier")["token"])

    def test_expired_addon_is_inactive(self):
        svc = make_service(addon=False)
        svc.set_addon("user_1", True, (datetime.utcnow() - timedelta(days=1)).isoformat())
        self.assertFalse(svc.addon_active("user_1"))

    def test_unknown_agent(self):
        with self.assertRaises(ValueError):
            make_service().create_link_token("user_1", "nope")

    def test_redeeming_the_code_binds_once(self):
        svc = make_service()
        token = svc.create_link_token("user_1", "leads_qualifier")["token"]
        first = svc.classify_inbound(inbound(f"LINK-{token}", "m1"))
        self.assertEqual(first["status"], "handled")
        self.assertIn("Terhubung", first["reply"])
        self.assertEqual(svc.get_binding(WA)["agent_type"], "leads_qualifier")
        self.assertEqual(svc.get_link_status(token, "user_1")["status"], "connected")
        again = svc.classify_inbound(inbound(f"LINK-{token}", "m2"))
        self.assertIn("tidak valid", again["reply"])

    def test_expired_code(self):
        svc = make_service()
        token = svc.create_link_token("user_1", "leads_qualifier")["token"]
        rec = svc.db.load_json(ws.LINK_TOKENS_COLLECTION, token)
        rec["expires_at"] = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
        svc.db.save_json(ws.LINK_TOKENS_COLLECTION, token, rec)
        self.assertEqual(svc.get_link_status(token, "user_1")["status"], "expired")
        self.assertIn("kedaluwarsa", svc.classify_inbound(inbound(f"LINK-{token}"))["reply"])
        self.assertIsNone(svc.get_binding(WA))


class InboundTest(unittest.TestCase):
    def setUp(self):
        tier = mock.patch.object(ws, "agent_tier_error", return_value=None)
        tier.start()
        self.addCleanup(tier.stop)
        self.svc = make_service()
        with mock.patch.dict("os.environ", {"KAPSO_DISPLAY_PHONE": "6281200001111"}):
            token = self.svc.create_link_token("user_1", "leads_qualifier")["token"]
        self.svc.classify_inbound(inbound(f"LINK-{token}", "link"))

    def test_unbound_number_is_left_to_crm(self):
        svc = make_service()
        out = svc.classify_inbound(inbound("halo"))
        self.assertEqual(out["status"], "not_handled")
        self.assertIsNone(out["reply"])

    def test_bound_number_is_handled_and_window_refreshed(self):
        out = self.svc.classify_inbound(inbound("halo", "m10"))
        self.assertEqual(out["status"], "handled")
        self.assertEqual(out["binding"]["agent_type"], "leads_qualifier")
        self.assertIsNone(out["reply"])

    def test_duplicate_message_id(self):
        self.svc.classify_inbound(inbound("halo", "dup"))
        self.assertEqual(self.svc.classify_inbound(inbound("halo", "dup"))["status"], "duplicate")

    def test_lapsed_addon_replies_and_does_not_run(self):
        self.svc.set_addon("user_1", False)
        out = self.svc.classify_inbound(inbound("halo", "m20"))
        self.assertEqual(out["status"], "handled")
        self.assertIsNone(out["binding"])
        self.assertIn("Add-on", out["reply"])

    def test_inactive_account_drops_binding(self):
        self.svc.tg._is_active.return_value = False
        out = self.svc.classify_inbound(inbound("halo", "m30"))
        self.assertEqual(out["status"], "handled")
        self.assertIsNone(self.svc.get_binding(WA))

    def test_bad_input_is_not_handled(self):
        self.assertEqual(self.svc.classify_inbound({"message_id": "", "from": WA})["status"], "not_handled")
        self.assertEqual(self.svc.classify_inbound({"message_id": "x", "from": "abc"})["status"], "not_handled")

    def test_run_turn_routes_to_agent_and_replies(self):
        binding = self.svc.get_binding(WA)
        self.svc.tg._try_conversational_approval.return_value = None
        self.svc.tg._route_to_agent.return_value = {"reply": "Siap!", "pending_approval": None}
        self.svc.tg._remember_pending_result.side_effect = lambda b, r: r
        with mock.patch.object(self.svc, "send_text") as send:
            self.svc.run_turn(binding, inbound("tolong bantu"))
        self.svc.tg._route_to_agent.assert_called_once()
        self.assertEqual(self.svc.tg._route_to_agent.call_args.kwargs["channel"], "whatsapp")
        send.assert_called_once_with(WA, "Siap!")

    def test_run_turn_approval_reply_skips_the_agent(self):
        binding = self.svc.get_binding(WA)
        self.svc.tg._try_conversational_approval.return_value = {"reply": "Disetujui"}
        with mock.patch.object(self.svc, "send_text") as send:
            self.svc.run_turn(binding, inbound("ya"))
        self.svc.tg._route_to_agent.assert_not_called()
        send.assert_called_once_with(WA, "Disetujui")

    def test_voice_note_is_transcribed(self):
        msg = inbound("(audio)", type="audio", media_id="mid1", mime_type="audio/ogg; codecs=opus", duration=5)
        with mock.patch.object(self.svc, "download_media", return_value=b"ogg"), mock.patch(
            "app.services.attachment_extractor.transcribe_audio", return_value="tolong cek stok"
        ) as stt:
            prompt = self.svc.build_prompt(msg)
        stt.assert_called_once()
        self.assertIn("tolong cek stok", prompt)
        self.assertNotIn("(audio)", prompt)

    def test_voice_note_download_failure_degrades_gracefully(self):
        msg = inbound("(audio)", type="audio", media_id="mid1", duration=5)
        with mock.patch.object(self.svc, "download_media", return_value=None):
            prompt = self.svc.build_prompt(msg)
        self.assertIn("could not be transcribed", prompt)


class WindowTest(unittest.TestCase):
    def test_proactive_send_only_inside_24h_window(self):
        svc = make_service()
        fresh = {"wa_id": WA, "last_inbound_at": datetime.utcnow().isoformat()}
        stale = {"wa_id": WA, "last_inbound_at": (datetime.utcnow() - timedelta(hours=25)).isoformat()}
        with mock.patch.object(svc, "send_text", return_value=True) as send:
            self.assertTrue(svc.send_to_binding(fresh, "pengingat"))
            self.assertFalse(svc.send_to_binding(stale, "pengingat"))
            self.assertFalse(svc.send_to_binding({"wa_id": WA}, "pengingat"))
        send.assert_called_once()


class RouteTest(unittest.TestCase):
    def test_deploy_link_maps_addon_to_402_with_code(self):
        from app.routes import whatsapp as route

        svc = mock.Mock()
        svc.create_link_token.side_effect = ws.AddonRequired("nope")
        with mock.patch.object(route, "whatsapp_service", svc), mock.patch.object(route, "_require_paid_agents"):
            with self.assertRaises(HTTPException) as c:
                route.create_deploy_link(route.DeployLinkRequest(agent_type="leads_qualifier"), {"user_id": "user_1"})
        self.assertEqual(c.exception.status_code, 402)
        self.assertEqual(c.exception.detail["code"], "whatsapp_addon_required")

    def test_inbound_schedules_turn_only_for_bound_numbers(self):
        from app.routes import whatsapp as route

        svc = mock.Mock()
        svc.classify_inbound.return_value = {"status": "handled", "binding": {"wa_id": WA}, "reply": None}
        bg = mock.Mock()
        with mock.patch.object(route, "whatsapp_service", svc):
            out = route.inbound(route.InboundMessage(**{"message_id": "m1", "from": WA, "text": "hi"}), bg)
        self.assertEqual(out, {"status": "handled"})
        bg.add_task.assert_called_once()

        svc.classify_inbound.return_value = {"status": "not_handled", "binding": None, "reply": None}
        bg2 = mock.Mock()
        with mock.patch.object(route, "whatsapp_service", svc):
            self.assertEqual(route.inbound(route.InboundMessage(**{"message_id": "m2", "from": WA}), bg2), {"status": "not_handled"})
        bg2.add_task.assert_not_called()

    def test_inbound_link_reply_is_sent(self):
        from app.routes import whatsapp as route

        svc = mock.Mock()
        svc.classify_inbound.return_value = {"status": "handled", "binding": None, "reply": "✅ Terhubung"}
        with mock.patch.object(route, "whatsapp_service", svc):
            route.inbound(route.InboundMessage(**{"message_id": "m3", "from": "+62 812 3456 7890", "text": "LINK-ABC234"}), mock.Mock())
        svc.send_text.assert_called_once_with(WA, "✅ Terhubung")

    def test_list_bindings_masks_the_number_and_hides_internals(self):
        from app.routes import whatsapp as route

        svc = mock.Mock()
        svc.list_bindings.return_value = [{"binding_id": WA, "wa_id": WA, "agent_type": "leads_qualifier", "linked_token": "SECRET"}]
        with mock.patch.object(route, "whatsapp_service", svc):
            out = route.list_bindings({"user_id": "user_1"})["bindings"][0]
        self.assertNotIn("linked_token", out)
        self.assertNotIn("wa_id", out)
        self.assertTrue(out["masked_number"].endswith("7890"))


if __name__ == "__main__":
    unittest.main()
