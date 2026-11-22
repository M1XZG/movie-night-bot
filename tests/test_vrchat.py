from email.message import Message
import http.client
import io
import json
import unittest
import urllib.error
from unittest import mock

import vrchat


def payload(**changes):
    result = vrchat.build_event_payload(
        "Movie Night: Hackers", "The right movie.",
        "2026-11-04T18:50:00Z", "2026-11-04T21:00:00Z",
        image_id="file_art")
    result.update(changes)
    return result


def event(**changes):
    result = payload()
    result["id"] = "cal_new"
    result.update(changes)
    return result


def response(data, status=200):
    return status, json.dumps(data), {}


def listing(*events, has_next=False):
    return response({"results": list(events), "hasNext": has_next})


class NoSleepTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(vrchat.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)


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

    def test_single_platform_and_duplicates_are_preserved_correctly(self):
        for requested, expected in [
            ("android", ["android"]),
            (["android", "android", "ios", None], ["android", "ios"]),
        ]:
            with self.subTest(requested=requested):
                result = vrchat.build_event_payload(
                    "Test", "Test", "2026-11-04T18:50:00Z",
                    "2026-11-04T21:00:00Z", platforms=requested)
                self.assertEqual(result["platforms"], expected)


class EventCreationTests(NoSleepTests):
    def test_server_error_recovers_event_created_by_vrchat(self):
        created = event(
            title="Movie Night˸ Hackers", startsAt="2026-11-04T19:50:00.000000+01:00",
            endsAt="2026-11-04T21:00:00.000Z")
        responses = [
            listing(),
            (500, json.dumps({"error": {"message": "Application error"}}), {}),
            listing(created),
        ]

        with mock.patch.object(vrchat, "_request", side_effect=responses) as request:
            created_event = vrchat._create_event_sync(
                {"auth": "test"}, "grp_test", payload())

        self.assertEqual(created_event["id"], "cal_new")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(
            request.call_args_list[2].args[:2],
            ("GET", "/calendar/grp_test?n=100&offset=0"),
        )
        self.assertEqual(sum(call.args[0] == "POST" for call in request.call_args_list), 1)

    def test_server_error_without_matching_event_is_uncertain(self):
        responses = [
            listing(),
            (500, json.dumps({"error": {"message": "Application error"}}), {}),
            listing(), listing(), listing(),
        ]

        with mock.patch.object(vrchat, "_request", side_effect=responses) as request:
            with self.assertRaisesRegex(vrchat.VRChatWriteUncertainError, "Application error") as caught:
                vrchat._create_event_sync({"auth": "test"}, "grp_test", payload())

        self.assertTrue(caught.exception.preserve_image)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual(sum(call.args[0] == "POST" for call in request.call_args_list), 1)

    def test_draft_response_is_deleted_and_rejected(self):
        created = event(isDraft=True, platforms=[])
        responses = [
            listing(), response(created), response(created),
            (204, "", {}),
        ]
        with mock.patch.object(vrchat, "_request", side_effect=responses) as request:
            with self.assertRaisesRegex(vrchat.VRChatError, "draft") as caught:
                vrchat._create_event_sync({"auth": "test"}, "grp_test", payload())

        self.assertNotIsInstance(caught.exception, vrchat.VRChatWriteUncertainError)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(request.call_args_list[3].args[:2], ("DELETE", "/calendar/grp_test/cal_new"))

    def test_timeout_after_accepted_post_is_reconciled(self):
        with mock.patch.object(vrchat, "_request", side_effect=[
            listing(), vrchat.VRChatError("network timeout"), listing(event())
        ]) as request:
            result = vrchat._create_event_sync({}, "grp_test", payload())
        self.assertEqual(result["id"], "cal_new")
        self.assertEqual(sum(call.args[0] == "POST" for call in request.call_args_list), 1)

    def test_persisted_before_write_baseline_can_be_passed_to_creation(self):
        old = event(id="cal_old")
        with mock.patch.object(vrchat, "_request", side_effect=[
            response({"error": "Application error"}, 500), listing(old, event())
        ]) as request:
            result = vrchat._create_event_sync(
                {}, "grp_test", payload(), existing_ids=["cal_old"])
        self.assertEqual(result["id"], "cal_new")
        self.assertEqual([call.args[0] for call in request.call_args_list], ["POST", "GET"])

    def test_unexpected_success_status_still_reconciles_accepted_event(self):
        with mock.patch.object(vrchat, "_request", side_effect=[
            listing(), (204, "", {}), listing(event())
        ]):
            self.assertEqual(
                vrchat._create_event_sync({}, "grp_test", payload())["id"], "cal_new")

    def test_bad_success_bodies_are_reconciled(self):
        for raw in ("not-json", "[]", "", '{"error":{"message":"Application error"}}', "{}"):
            with self.subTest(raw=raw), mock.patch.object(vrchat, "_request", side_effect=[
                listing(), (200, raw, {}), listing(event())
            ]):
                self.assertEqual(
                    vrchat._create_event_sync({}, "grp_test", payload())["id"], "cal_new")

    def test_old_identical_booking_is_not_proof_of_a_new_write(self):
        old = event(id="cal_old")
        with mock.patch.object(vrchat, "_request", side_effect=[
            listing(old), response({"error": "Application error"}, 500),
            listing(old), listing(old), listing(old),
        ]) as request:
            with self.assertRaises(vrchat.VRChatWriteUncertainError) as caught:
                vrchat._create_event_sync({}, "grp_test", payload())
        self.assertEqual(caught.exception.existing_ids, ("cal_old",))
        self.assertFalse(any(call.args[0] == "DELETE" for call in request.call_args_list))

    def test_returned_preexisting_id_is_never_deleted(self):
        old = event(id="cal_old", isDraft=True)
        with mock.patch.object(vrchat, "_request", side_effect=[
            listing(old), response(old), listing(old), listing(old), listing(old)
        ]) as request:
            with self.assertRaises(vrchat.VRChatWriteUncertainError):
                vrchat._create_event_sync({}, "grp_test", payload())
        self.assertFalse(any(call.args[0] == "DELETE" for call in request.call_args_list))

    def test_invalid_reconciliation_candidates_cannot_report_success(self):
        for wrong in (
            event(imageId="file_wrong"), event(isDraft=True),
            event(platforms=["android"]), event(description="Another movie."),
        ):
            with self.subTest(wrong=wrong), mock.patch.object(vrchat, "_request", side_effect=[
                listing(), response({"error": "Application error"}, 500),
                listing(wrong), listing(wrong), listing(wrong),
            ]):
                with self.assertRaises(vrchat.VRChatWriteUncertainError):
                    vrchat._create_event_sync({}, "grp_test", payload())

    def test_two_new_matches_are_not_arbitrarily_selected(self):
        candidates = listing(event(), event(id="cal_other"))
        with mock.patch.object(vrchat, "_request", side_effect=[
            listing(), response({"error": "Application error"}, 500),
            candidates, candidates, candidates,
        ]):
            with self.assertRaises(vrchat.VRChatWriteUncertainError):
                vrchat._create_event_sync({}, "grp_test", payload())

    def test_partial_success_response_is_read_back_before_rollback(self):
        with mock.patch.object(vrchat, "_request", side_effect=[
            listing(), response({"id": "cal_new"}), response(event())
        ]) as request:
            self.assertEqual(
                vrchat._create_event_sync({}, "grp_test", payload())["imageId"], "file_art")
        self.assertEqual(request.call_args_list[-1].args[:2], ("GET", "/calendar/grp_test/cal_new"))

    def test_rollback_failure_retains_live_artwork_and_calendar_id(self):
        invalid = event(imageId="file_wrong")
        with mock.patch.object(vrchat, "_request", side_effect=[
            listing(), response(invalid), response(invalid), response({"error": "Failed"}, 500)
        ]):
            with self.assertRaises(vrchat.VRChatWriteUncertainError) as caught:
                vrchat._create_event_sync({}, "grp_test", payload())
        self.assertTrue(caught.exception.preserve_image)
        self.assertEqual(caught.exception.calendar_id, "cal_new")

    def test_unreadable_created_event_is_not_blindly_deleted(self):
        with mock.patch.object(vrchat, "_request", side_effect=[
            listing(), response({"id": "cal_new"}), response({"error": "expired"}, 401)
        ]) as request:
            with self.assertRaises(vrchat.VRChatWriteUncertainError) as caught:
                vrchat._create_event_sync({}, "grp_test", payload())
        self.assertEqual(caught.exception.calendar_id, "cal_new")
        self.assertFalse(any(call.args[0] == "DELETE" for call in request.call_args_list))

    def test_session_expiring_after_post_retains_unknown_write_state(self):
        with mock.patch.object(vrchat, "_request", side_effect=[
            listing(), response({"error": "Application error"}, 500),
            response({"error": "expired"}, 401)
        ]):
            with self.assertRaises(vrchat.VRChatWriteUncertainError) as caught:
                vrchat._create_event_sync({}, "grp_test", payload())
        self.assertEqual(caught.exception.status, 401)
        self.assertTrue(caught.exception.preserve_image)
        self.sleep.assert_not_called()

    def test_failed_preflight_does_not_submit_post(self):
        with mock.patch.object(vrchat, "_request", return_value=response({"error": "expired"}, 401)) as request:
            with self.assertRaises(vrchat.VRChatAuthError):
                vrchat._create_event_sync({}, "grp_test", payload())
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], "GET")

    def test_explicit_rate_limit_or_auth_rejection_is_not_retried(self):
        for status, error_type in [(429, vrchat.VRChatError), (401, vrchat.VRChatAuthError)]:
            with self.subTest(status=status), mock.patch.object(vrchat, "_request", side_effect=[
                listing(), response({"error": "Rejected"}, status)
            ]) as request:
                with self.assertRaises(error_type) as caught:
                    vrchat._create_event_sync({}, "grp_test", payload())
                self.assertNotIsInstance(caught.exception, vrchat.VRChatWriteUncertainError)
                self.assertEqual(request.call_count, 2)

    def test_invalid_input_fails_before_any_network_request(self):
        for changes in (
            {"startsAt": "invalid"}, {"endsAt": "2026-11-04T18:00:00Z"},
            {"imageId": 123}, {"platforms": ["invalid"]}, {"isDraft": True},
        ):
            with self.subTest(changes=changes), mock.patch.object(vrchat, "_request") as request:
                with self.assertRaises(vrchat.VRChatError):
                    vrchat._create_event_sync({}, "grp_test", payload(**changes))
                request.assert_not_called()


class PaginationTests(NoSleepTests):
    def test_reconciliation_searches_later_pages(self):
        with mock.patch.object(vrchat, "_request", side_effect=[
            listing(event(id="cal_old"), has_next=True), listing(event())
        ]) as request:
            result = vrchat._find_created_event_sync({}, "grp_test", payload(), ("cal_old",))
        self.assertEqual(result["id"], "cal_new")
        self.assertEqual(request.call_args_list[1].args[1], "/calendar/grp_test?n=100&offset=1")

    def test_non_latin_titles_do_not_collapse_to_the_same_match(self):
        desired = payload(title="千と千尋")
        with mock.patch.object(vrchat, "_request", return_value=listing(
            event(title="別の映画"), event(id="cal_japanese", title="千と千尋")
        )):
            self.assertEqual(
                vrchat._find_created_event_sync({}, "grp_test", desired, ())["id"], "cal_japanese")

    def test_reconciliation_requires_the_saved_baseline(self):
        with mock.patch.object(vrchat, "_request") as request:
            with self.assertRaises(vrchat.VRChatError):
                vrchat._find_created_event_sync({}, "grp_test", payload(), None)
        request.assert_not_called()

    def test_empty_repeated_or_malformed_pages_are_rejected(self):
        for pages in (
            [listing(has_next=True)],
            [listing(event(), has_next=True), listing(event())],
            [response({"results": {}, "hasNext": False})],
            [response({"results": [None], "hasNext": False})],
            [response({"results": [], "hasNext": "false"})],
            [response([])],
        ):
            with self.subTest(pages=pages), mock.patch.object(vrchat, "_request", side_effect=pages):
                with self.assertRaises(vrchat.VRChatError):
                    vrchat._list_events_sync({}, "grp_test")

    def test_pagination_has_a_hard_limit(self):
        with mock.patch.object(vrchat, "MAX_CALENDAR_PAGES", 2), mock.patch.object(
            vrchat, "_request", side_effect=[
                listing(event(id="cal_one"), has_next=True),
                listing(event(id="cal_two"), has_next=True),
            ]
        ) as request:
            with self.assertRaisesRegex(vrchat.VRChatError, "pagination limit"):
                vrchat._list_events_sync({}, "grp_test")
        self.assertEqual(request.call_count, 2)

    def test_pagination_shares_a_wall_time_budget_and_discards_partial_results(self):
        clock = [0.0]

        def slow_page(*args, **kwargs):
            clock[0] += 35
            return listing(event(id=f"cal_page{int(clock[0])}"), has_next=True)

        with mock.patch.object(vrchat.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(vrchat, "_request", side_effect=slow_page) as request:
            with self.assertRaisesRegex(vrchat.VRChatError, "enumeration deadline"):
                vrchat._list_events_sync({}, "grp_test")

        self.assertEqual(request.call_count, 2)
        self.assertEqual(
            [call.kwargs["deadline"] for call in request.call_args_list], [60, 60])

    def test_expired_preflight_budget_never_submits_a_post(self):
        clock = [0.0]

        def expired_page(*args, **kwargs):
            clock[0] = 61
            return listing()

        with mock.patch.object(vrchat.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(vrchat, "_request", side_effect=expired_page) as request:
            with self.assertRaisesRegex(vrchat.VRChatError, "enumeration deadline"):
                vrchat._create_event_sync({}, "grp_test", payload())

        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET"])


class EventUpdateTests(NoSleepTests):
    def test_timeout_after_accepted_update_is_read_back_without_repeating_put(self):
        updated = event(imageId="file_new")
        with mock.patch.object(vrchat, "_request", side_effect=[
            vrchat.VRChatError("timeout"), response(updated)
        ]) as request:
            result = vrchat._update_event_sync({}, "grp_test", "cal_new", {"imageId": "file_new"})
        self.assertEqual(result["imageId"], "file_new")
        self.assertEqual([call.args[0] for call in request.call_args_list], ["PUT", "GET"])

    def test_success_response_for_wrong_id_is_not_trusted(self):
        with mock.patch.object(vrchat, "_request", side_effect=[
            response(event(id="cal_other")), response(event())
        ]):
            result = vrchat._update_event_sync({}, "grp_test", "cal_new", {"imageId": "file_art"})
        self.assertEqual(result["id"], "cal_new")

    def test_unconfirmed_update_protects_both_images(self):
        with mock.patch.object(vrchat, "_request", side_effect=[
            response(event()), response(event()), response(event()), response(event())
        ]) as request:
            with self.assertRaises(vrchat.VRChatWriteUncertainError) as caught:
                vrchat._update_event_sync({}, "grp_test", "cal_new", {"imageId": "file_new"})
        self.assertEqual(caught.exception.operation, "update")
        self.assertTrue(caught.exception.preserve_image)
        self.assertEqual(sum(call.args[0] == "PUT" for call in request.call_args_list), 1)

    def test_auth_expiry_during_readback_remains_uncertain(self):
        with mock.patch.object(vrchat, "_request", side_effect=[
            (200, "invalid", {}), response({"error": "expired"}, 401)
        ]):
            with self.assertRaises(vrchat.VRChatWriteUncertainError):
                vrchat._update_event_sync({}, "grp_test", "cal_new", {"imageId": "file_new"})
        self.sleep.assert_not_called()

    def test_definitive_not_found_or_expired_update_is_reported(self):
        for status, error in [(404, vrchat.VRChatError), (401, vrchat.VRChatAuthError)]:
            with self.subTest(status=status), mock.patch.object(
                vrchat, "_request", return_value=response({"error": "gone"}, status)
            ) as request:
                with self.assertRaises(error) as caught:
                    vrchat._update_event_sync({}, "grp_test", "cal_new", {"imageId": "file_new"})
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(request.call_count, 1)


class UploadAndCleanupTests(NoSleepTests):
    png = b"\x89PNG\r\n\x1a\n" + b"test image data"

    def test_upload_returns_only_a_valid_file_id(self):
        with mock.patch.object(vrchat, "_request_multipart", return_value=(
            201, json.dumps({"id": "file_art"})
        )) as request:
            self.assertEqual(vrchat._upload_image_sync({}, self.png), "file_art")
        self.assertEqual(request.call_args.args[:2], ("POST", "/file/image"))
        self.assertEqual(request.call_args.kwargs["file_bytes"], self.png)

    def test_ambiguous_upload_does_not_issue_a_second_post(self):
        for result in (
            vrchat.VRChatError("timeout"), (500, '{"error":"Application error"}'),
            (200, "invalid"), (200, "[]"), (200, '{"id":123}'),
            (200, '{"id":"cal_wrong"}'), (200, '{"error":"Application error"}'),
        ):
            with self.subTest(result=result), mock.patch.object(
                vrchat, "_request_multipart",
                side_effect=[result] if isinstance(result, Exception) else None,
                return_value=result,
            ) as request:
                with self.assertRaises(vrchat.VRChatWriteUncertainError) as caught:
                    vrchat._upload_image_sync({}, self.png)
                self.assertEqual(caught.exception.operation, "image_upload")
                self.assertEqual(request.call_count, 1)

    def test_invalid_png_is_rejected_before_upload(self):
        with mock.patch.object(vrchat, "_request_multipart") as request:
            with self.assertRaises(vrchat.VRChatError):
                vrchat._upload_image_sync({}, b"not a PNG")
        request.assert_not_called()

    def test_upload_expired_session_has_auth_error(self):
        with mock.patch.object(vrchat, "_request_multipart", return_value=(401, "{}")):
            with self.assertRaises(vrchat.VRChatAuthError):
                vrchat._upload_image_sync({}, self.png)

    def test_delete_error_body_is_not_a_success(self):
        for operation, arguments in [
            (vrchat._delete_event_sync, ({}, "grp_test", "cal_new")),
            (vrchat._delete_file_sync, ({}, "file_art")),
        ]:
            with self.subTest(operation=operation), mock.patch.object(
                vrchat, "_request", return_value=response({"error": "Application error"})
            ):
                with self.assertRaisesRegex(vrchat.VRChatError, "Application error"):
                    operation(*arguments)

    def test_deletes_accept_empty_success_and_already_absent(self):
        for status, expected in [(204, True), (200, True), (404, False)]:
            for operation, arguments in [
                (vrchat._delete_event_sync, ({}, "grp_test", "cal_new")),
                (vrchat._delete_file_sync, ({}, "file_art")),
            ]:
                with self.subTest(status=status, operation=operation), mock.patch.object(
                    vrchat, "_request", return_value=(status, "", {})
                ):
                    self.assertEqual(operation(*arguments), expected)


class AuthenticationTests(NoSleepTests):
    def test_login_requires_a_session_cookie_and_user_identity(self):
        for body, cookies in [
            ({"id": "usr_test"}, {}), ({}, {"auth": "test"}),
            ([], {"auth": "test"}), ({"error": "Rejected"}, {"auth": "test"}),
        ]:
            with self.subTest(body=body), mock.patch.object(
                vrchat, "_request", return_value=(200, json.dumps(body), cookies)
            ):
                with self.assertRaises(vrchat.VRChatError):
                    vrchat._login_sync("user", "password")

    def test_login_filters_unsupported_two_factor_methods(self):
        with mock.patch.object(vrchat, "_request", return_value=(
            200, json.dumps({"requiresTwoFactorAuth": ["unsupported", "totp"]}), {"auth": "test"}
        )):
            self.assertEqual(vrchat._login_sync("user", "password")["methods"], ["totp"])

    def test_verify_requires_boolean_confirmation_and_two_factor_cookie(self):
        for body, cookies in [
            ({"verified": True}, {}), ({"verified": "false"}, {"twoFactorAuth": "test"}),
            ({"verified": False}, {"twoFactorAuth": "test"}),
        ]:
            with self.subTest(body=body), mock.patch.object(
                vrchat, "_request", return_value=(200, json.dumps(body), cookies)
            ):
                with self.assertRaises(vrchat.VRChatError):
                    vrchat._verify_sync("auth", "totp", "123456")

    def test_expired_two_factor_session_has_auth_error(self):
        with mock.patch.object(vrchat, "_request", return_value=response({}, 401)):
            with self.assertRaises(vrchat.VRChatAuthError):
                vrchat._verify_sync("auth", "totp", "123456")

    def test_current_user_rejects_pending_two_factor_or_invalid_success_body(self):
        for body, error in [
            ({"requiresTwoFactorAuth": ["totp"]}, vrchat.VRChatAuthError),
            ([], vrchat.VRChatError), ({}, vrchat.VRChatError),
            ({"error": "Expired"}, vrchat.VRChatError),
        ]:
            with self.subTest(body=body), mock.patch.object(
                vrchat, "_request", return_value=response(body)
            ):
                with self.assertRaises(error):
                    vrchat._current_user_sync({})


class TransportTests(NoSleepTests):
    def http_error(self, status, retry_after=None):
        headers = Message()
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        return urllib.error.HTTPError(
            vrchat.API, status, "Error", headers, io.BytesIO(b'{"error":"Failed"}'))

    def http_response(self, status=200, body=b"{}"):
        result = mock.MagicMock()
        result.__enter__.return_value = result
        result.status = status
        result.headers = Message()
        result.read.return_value = body
        return result

    def test_get_retries_rate_limit_and_server_error_with_bounded_delays(self):
        with mock.patch.object(vrchat.urllib.request, "urlopen", side_effect=[
            self.http_error(429, "2"), self.http_error(503), self.http_response()
        ]) as request:
            self.assertEqual(vrchat._request("GET", "/auth/user")[0], 200)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(2), mock.call(2)])
        self.assertEqual(request.call_args.args[0].get_header("User-agent"), vrchat.USER_AGENT)

    def test_get_network_retries_are_bounded(self):
        with mock.patch.object(vrchat.urllib.request, "urlopen", side_effect=TimeoutError("timeout")) as request:
            with self.assertRaisesRegex(vrchat.VRChatError, "network error"):
                vrchat._request("GET", "/auth/user")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [mock.call(1), mock.call(2)])

    def test_incomplete_response_body_is_retried_for_reads(self):
        broken = self.http_response()
        broken.read.side_effect = http.client.IncompleteRead(b"partial")
        with mock.patch.object(vrchat.urllib.request, "urlopen", side_effect=[
            broken, self.http_response()
        ]) as request:
            self.assertEqual(vrchat._request("GET", "/auth/user")[0], 200)
        self.assertEqual(request.call_count, 2)

    def test_long_retry_after_does_not_sleep_or_retry_early(self):
        with mock.patch.object(
            vrchat.urllib.request, "urlopen", side_effect=self.http_error(429, "60")
        ) as request:
            self.assertEqual(vrchat._request("GET", "/auth/user")[0], 429)
        self.assertEqual(request.call_count, 1)
        self.sleep.assert_not_called()

    def test_auth_error_is_not_retried(self):
        with mock.patch.object(
            vrchat.urllib.request, "urlopen", side_effect=self.http_error(401)
        ) as request:
            self.assertEqual(vrchat._request("GET", "/auth/user")[0], 401)
        self.assertEqual(request.call_count, 1)
        self.sleep.assert_not_called()

    def test_calendar_retries_shrink_socket_timeout_to_remaining_budget(self):
        clock = [100.0]
        timeouts = []

        def open_response(*args, **kwargs):
            timeouts.append(kwargs["timeout"])
            if len(timeouts) == 1:
                clock[0] += 55
                raise TimeoutError("slow request")
            return self.http_response()

        def advance_clock(delay):
            clock[0] += delay

        self.sleep.side_effect = advance_clock
        with mock.patch.object(vrchat.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(vrchat.urllib.request, "urlopen", side_effect=open_response):
            self.assertEqual(vrchat._request("GET", "/calendar/grp_test", deadline=160)[0], 200)

        self.assertEqual(timeouts, [30, 4])

    def test_calendar_retry_after_cannot_outlive_its_budget(self):
        with mock.patch.object(vrchat.time, "monotonic", return_value=100), \
                mock.patch.object(
                    vrchat.urllib.request, "urlopen", side_effect=self.http_error(429, "2")
                ) as request:
            with self.assertRaisesRegex(vrchat.VRChatError, "enumeration deadline"):
                vrchat._request("GET", "/calendar/grp_test", deadline=101)
        self.assertEqual(request.call_count, 1)
        self.sleep.assert_not_called()

    def test_non_idempotent_writes_are_never_retried_by_transport(self):
        for method in ("POST", "PUT"):
            for error in (self.http_error(500), self.http_error(429), TimeoutError("timeout")):
                with self.subTest(method=method, error=error), mock.patch.object(
                    vrchat.urllib.request, "urlopen", side_effect=error
                ) as request:
                    if isinstance(error, urllib.error.HTTPError):
                        self.assertEqual(vrchat._request(method, "/calendar/grp_test/event")[0], error.code)
                    else:
                        with self.assertRaises(vrchat.VRChatError):
                            vrchat._request(method, "/calendar/grp_test/event")
                    self.assertEqual(request.call_count, 1)
        self.sleep.assert_not_called()

    def test_delete_retry_can_confirm_a_lost_success_as_already_gone(self):
        with mock.patch.object(vrchat.urllib.request, "urlopen", side_effect=[
            TimeoutError("response lost"), self.http_error(404)
        ]) as request:
            self.assertFalse(vrchat._delete_event_sync({}, "grp_test", "cal_new"))
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
