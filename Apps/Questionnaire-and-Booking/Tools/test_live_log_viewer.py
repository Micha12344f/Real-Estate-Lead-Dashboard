import importlib.util
import json
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parent / "live-log-viewer.py"
BOOKING_PAGE = MODULE_PATH.parents[1] / "frontend" / "booking" / "index.html"
spec = importlib.util.spec_from_file_location("live_log_viewer", MODULE_PATH)
viewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewer)


class PrivacyAndDeliveryTests(unittest.TestCase):
    def test_frontend_event_discards_untrusted_sensitive_values(self):
        item = viewer.safe_frontend_event({"event": "click", "email": "person@example.com", "token": "secret", "message": "42 High Street"})
        encoded = json.dumps(item)
        self.assertNotIn("person@example.com", encoded)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("High Street", encoded)
        self.assertEqual(item["message"], "User interaction.")

    def test_unknown_event_is_rejected(self):
        self.assertIsNone(viewer.safe_frontend_event({"event": "arbitrary", "message": "sensitive"}))

    def test_booking_api_event_keeps_only_allowlisted_route(self):
        item = viewer.safe_frontend_event({"event": "api_start", "route": "/booking/api/confirm?token=secret",
                                           "email": "lead@example.test"})
        self.assertNotIn("route", item)
        self.assertNotIn("secret", json.dumps(item))
        item = viewer.safe_frontend_event({"event": "api_start", "route": "/booking/api/confirm",
                                           "email": "lead@example.test"})
        self.assertEqual(item["route"], "/booking/api/confirm")
        self.assertNotIn("example.test", json.dumps(item))

    def test_booking_instrumentation_uses_cors_simple_event_posts_and_safe_routes(self):
        page = BOOKING_PAGE.read_text(encoding="utf-8")
        self.assertIn("'Content-Type':'text/plain;charset=UTF-8'", page)
        self.assertIn("send('api_start',route)", page)
        for route in ("/booking/api/available-properties", "/booking/api/booking-guest",
                      "/booking/api/availability", "/booking/api/confirm"):
            self.assertIn(route, page)

    def test_viewer_preflight_is_restricted_to_local_frontends(self):
        self.assertIn("Access-Control-Allow-Origin", viewer.Handler.do_OPTIONS.__code__.co_consts)

    def test_event_delivery_store(self):
        item = viewer.safe_frontend_event({"event": "fetch_error"})
        with viewer.LOCK:
            viewer.EVENTS.append(item)
            self.assertIn(item, viewer.EVENTS)

    def test_frontend_event_types_are_allowlisted_and_view_message_safe(self):
        item = viewer.safe_frontend_event({"event": "fetch_error", "url": "https://host/path?token=abc", "message": "12 High Street"})
        self.assertEqual(item["message"], "Frontend request failed.")
        self.assertNotIn("token", json.dumps(item))
        self.assertNotIn("High Street", json.dumps(item))
        self.assertTrue("/api/events" in viewer.PAGE and "Auto-scroll" in viewer.PAGE)

    def test_backend_event_redacts_untrusted_message_and_query_route(self):
        item = viewer.safe_backend_event({"event": "confirmation_request", "message": "lead@example.test 4 High Street",
                                          "route": "/confirm?token=secret", "request_id": "email@example.test"})
        serialized = json.dumps(item)
        self.assertNotIn("lead@example.test", serialized)
        self.assertNotIn("High Street", serialized)
        self.assertNotIn("token", serialized)
        self.assertNotIn("email@example.test", serialized)
        self.assertNotIn("route", item)
        email_item = viewer.safe_backend_event({"event": "email_simulated", "operation": "lead_and_tenant_simulated",
                                                "message": "to lead@example.test and tenant@example.test"})
        self.assertEqual(email_item["message"], "Sandbox email simulated for lead + tenant; no email delivered.")
        self.assertNotIn("example.test", json.dumps(email_item))

    def test_shared_room_backend_event_displays_exact_contact_ids_safely(self):
        item = viewer.safe_backend_event({"event": "email_simulated", "operation": "lead_and_tenant_simulated",
                                          "contact_ids": [42, 17, 42], "tenant_count": 2,
                                          "missing_email_count": 1, "simulation_status": "partial",
                                          "message": "private@example.test 1 High Street"})
        self.assertEqual(item["contact_ids"], [17, 42])
        self.assertIn("Contact IDs=17,42", item["message"])
        self.assertIn("tenants=2", item["message"])
        self.assertIn("missing_email=1", item["message"])
        self.assertIn("simulation=partial", item["message"])
        self.assertIn("no email delivered", item["message"])
        self.assertNotIn("example.test", json.dumps(item))
        self.assertNotIn("High Street", json.dumps(item))

    def test_tenant_email_content_is_never_included_in_event(self):
        item = viewer.safe_backend_event({"event": "email_simulated", "operation": "tenant_email_prepared",
                                          "contact_ids": [42], "simulation_status": "simulated",
                                          "subject": "Viewing request for <Sample Road>",
                                          "body": "Hello, Jane Applicant, budget $4,000, token=private-token, slot 10:00",
                                          "to": "tenant@example.test", "name": "Tenant"})
        serialized = json.dumps(item)
        self.assertNotIn("subject", item)
        self.assertNotIn("body", item)
        for private in ("Sample Road", "Jane Applicant", "4,000", "private-token", "10:00", "tenant@example.test"):
            self.assertNotIn(private, serialized)
        self.assertEqual(item["contact_ids"], [42])
        self.assertIn("textContent=x.event", viewer.PAGE)
        self.assertNotIn("x.subject", viewer.PAGE)
        self.assertNotIn("x.body", viewer.PAGE)
        self.assertNotIn("rows.innerHTML", viewer.PAGE)

    def test_per_recipient_contact_id_is_typed_allowlisted_and_explicitly_rendered(self):
        lead = viewer.safe_backend_event({"event": "email_simulated", "operation": "lead_email_prepared",
                                          "recipient_role": "lead", "contact_id": None,
                                          "simulation_status": "simulated", "email": "private@example.test"})
        tenant = viewer.safe_backend_event({"event": "email_simulated", "operation": "tenant_email_prepared",
                                            "recipient_role": "tenant", "contact_id": 9991,
                                            "simulation_status": "simulated"})
        self.assertEqual(lead["recipient_role"], "lead")
        self.assertIsNone(lead["contact_id"])
        self.assertEqual(tenant["contact_id"], 9991)
        self.assertNotIn("private@example.test", json.dumps(lead))
        self.assertIn("+\' Contact ID: ", viewer.PAGE)
        self.assertIn("unavailable", viewer.PAGE)

    def test_booking_confirmation_has_friendly_copy_and_no_preview(self):
        page = BOOKING_PAGE.read_text(encoding="utf-8")
        self.assertIn("Thanks — we’ve received your viewing request", page)
        self.assertNotIn("email-preview", page)
        self.assertNotIn("emailPreview", page)


if __name__ == "__main__":
    unittest.main()
