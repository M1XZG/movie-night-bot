import json
import unittest
from unittest import mock

import vrchat


class EventPayloadTests(unittest.TestCase):
    def test_event_is_published_for_every_client_platform(self):
        payload = vrchat.build_event_payload(
            "Movie Night: Test",
            "Test event",
            "2026-09-23T17:50:00Z",
            "2026-09-23T20:04:00Z",
        )

        self.assertFalse(payload["isDraft"])
        self.assertTrue(payload["sendCreationNotification"])
        self.assertEqual(
            payload["platforms"],
            ["standalonewindows", "android", "ios"],
        )

    def test_invalid_platforms_fall_back_to_all_platforms(self):
        payload = vrchat.build_event_payload(
            "Movie Night: Test",
            "Test event",
            "2026-09-23T17:50:00Z",
            "2026-09-23T20:04:00Z",
            platforms=["invalid"],
        )

        self.assertEqual(
            payload["platforms"],
            ["standalonewindows", "android", "ios"],
        )


class EventCreationTests(unittest.TestCase):
    def test_server_error_recovers_event_created_by_vrchat(self):
        payload = {
            "title": "Movie Night: Hackers",
            "startsAt": "2026-11-04T18:50:00Z",
            "endsAt": "2026-11-04T21:00:00Z",
        }
        created = {
            "id": "cal_hackers",
            "title": "Movie Night˸ Hackers",
            "startsAt": "2026-11-04T18:50:00.000Z",
            "endsAt": "2026-11-04T21:00:00.000Z",
            "isDraft": False,
            "platforms": ["standalonewindows", "android", "ios"],
        }
        responses = [
            (500, json.dumps({"error": {"message": "Application error"}}), {}),
            (200, json.dumps({"hasNext": False, "results": [created]}), {}),
        ]

        with mock.patch.object(vrchat, "_request", side_effect=responses) as request:
            event = vrchat._create_event_sync(
                {"auth": "test"}, "grp_test", payload)

        self.assertEqual(event["id"], "cal_hackers")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            request.call_args_list[1].args[:2],
            ("GET", "/calendar/grp_test?n=100&offset=0"),
        )

    @mock.patch.object(vrchat.time, "sleep")
    def test_server_error_without_matching_event_is_reported(self, sleep):
        responses = [
            (500, json.dumps({"error": {"message": "Application error"}}), {}),
            (200, json.dumps({"hasNext": False, "results": []}), {}),
            (200, json.dumps({"hasNext": False, "results": []}), {}),
            (200, json.dumps({"hasNext": False, "results": []}), {}),
        ]

        with mock.patch.object(vrchat, "_request", side_effect=responses):
            with self.assertRaisesRegex(vrchat.VRChatError, "Application error"):
                vrchat._create_event_sync(
                    {"auth": "test"},
                    "grp_test",
                    {
                        "title": "Movie Night: Missing",
                        "startsAt": "2026-11-04T18:50:00Z",
                        "endsAt": "2026-11-04T21:00:00Z",
                    },
                )

        self.assertEqual(sleep.call_count, 2)

    def test_draft_response_is_deleted_and_rejected(self):
        created = {
            "id": "cal_test",
            "isDraft": True,
            "platforms": [],
        }
        responses = [
            (200, json.dumps(created), {}),
            (204, "", {}),
        ]
        with mock.patch.object(vrchat, "_request", side_effect=responses) as request:
            with self.assertRaisesRegex(vrchat.VRChatError, "draft"):
                vrchat._create_event_sync(
                    {"auth": "test"},
                    "grp_test",
                    {"title": "Test"},
                )

        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[1].args[:2], ("DELETE", "/calendar/grp_test/cal_test"))


if __name__ == "__main__":
    unittest.main()
