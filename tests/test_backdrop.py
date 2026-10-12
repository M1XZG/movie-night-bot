import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from PIL import Image

import find_backdrop as backdrop


def response(data):
    result = MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = data if isinstance(data, bytes) else json.dumps(data).encode()
    return result


def image(width=640, height=360, format="JPEG"):
    output = io.BytesIO()
    Image.new("RGB", (width, height), "blue").save(output, format=format)
    return output.getvalue()


MOVIE = {"id": 13156, "title": "Secondhand Lions", "release_date": "2003-09-19"}
IMAGES = {"backdrops": [
    {"file_path": "/landscape.jpg", "width": 1920, "height": 1080, "iso_639_1": None},
    {"file_path": "/poster.jpg", "width": 500, "height": 750, "iso_639_1": "en"},
]}


class BackdropTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"TMDB_READ_ACCESS_TOKEN": "test-token"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def fetch(self, title="Secondhand Lions", year="2003", show_type="movie", imdb_id=None):
        return backdrop.fetch_backdrop(
            title, year, show_type, directory=self.directory, imdb_id=imdb_id,
        )

    @patch("find_backdrop.urlopen")
    def test_api_authentication_and_real_validated_16_by_9_image(self, get):
        get.side_effect = [response({"results": [MOVIE]}), response(IMAGES), response(image())]
        path = self.fetch()
        with Image.open(path) as result:
            self.assertEqual(result.size, (1280, 720))
            self.assertEqual(result.format, "JPEG")
        request = get.call_args_list[0].args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {
            "query": ["Secondhand Lions"], "year": ["2003"], "include_adult": ["false"],
        })
        self.assertEqual(urlsplit(request.full_url).path, "/3/search/movie")
        self.assertEqual(urlsplit(get.call_args_list[1].args[0].full_url).path, "/3/movie/13156/images")
        cdn_request = get.call_args_list[2].args[0]
        self.assertEqual(cdn_request.full_url, backdrop.IMAGE_BASE + "/landscape.jpg")
        self.assertIsNone(cdn_request.get_header("Authorization"))
        self.assertTrue(all(call.kwargs["timeout"] == backdrop.TIMEOUT for call in get.call_args_list))

    @patch("find_backdrop.urlopen")
    def test_cached_image_works_without_network_or_credential(self, get):
        get.side_effect = [response({"results": [MOVIE]}), response(IMAGES), response(image())]
        path = self.fetch()
        get.reset_mock()
        with patch("find_backdrop.load_credential", side_effect=AssertionError("Credential read")):
            self.assertEqual(self.fetch(), path)
        get.assert_not_called()

    @patch("find_backdrop.urlopen")
    def test_corrupt_cache_is_replaced_and_reported(self, get):
        get.side_effect = [response({"results": [MOVIE]}), response(IMAGES), response(image())] * 2
        path = self.fetch()
        path.write_bytes(b"not an image")
        with patch("sys.stderr", new_callable=io.StringIO) as errors:
            self.assertEqual(self.fetch(), path)
        self.assertIn("cached backdrop is invalid", errors.getvalue())
        with Image.open(path) as result:
            result.load()

    @patch("find_backdrop.urlopen")
    def test_rejects_wrong_year_or_unrelated_title(self, get):
        for item in ({**MOVIE, "release_date": "2004-01-01"}, {**MOVIE, "title": "Another Film"}):
            get.return_value = response({"results": [item]})
            with self.assertRaisesRegex(backdrop.BackdropError, "No exact"):
                self.fetch()

    @patch("find_backdrop.urlopen")
    def test_ambiguous_yearless_title_does_not_choose_first_result(self, get):
        get.return_value = response({"results": [MOVIE, {**MOVIE, "id": 999}]})
        with self.assertRaisesRegex(backdrop.BackdropError, "Multiple"):
            self.fetch(year=None)

    @patch("find_backdrop.urlopen")
    def test_tv_episode_uses_parent_series_and_tv_api(self, get):
        get.side_effect = [
            response({"results": []}),
            response({"results": [{"id": 123, "name": "My Series", "first_air_date": "2020-01-01"}]}),
            response(IMAGES), response(image()),
        ]
        self.fetch("My Series episode 10", "2020", "tv")
        self.assertEqual(urlsplit(get.call_args_list[1].args[0].full_url).path, "/3/search/tv")
        params = parse_qs(urlsplit(get.call_args_list[1].args[0].full_url).query)
        self.assertEqual(params["query"], ["My Series"])
        self.assertEqual(params["first_air_date_year"], ["2020"])
        self.assertEqual(urlsplit(get.call_args_list[2].args[0].full_url).path, "/3/tv/123/images")

    @patch("find_backdrop.urlopen")
    def test_anime_and_other_search_both_media_types(self, get):
        for show_type in ("anime", "other"):
            get.reset_mock()
            get.side_effect = [response({"results": []}), response({"results": [MOVIE]}),
                               response(IMAGES), response(image())]
            self.fetch(show_type=show_type)
            self.assertEqual([urlsplit(call.args[0].full_url).path for call in get.call_args_list[:2]],
                             ["/3/search/tv", "/3/search/movie"])

    @patch("find_backdrop.time.sleep")
    @patch("find_backdrop.urlopen")
    def test_auth_failure_is_not_retried_and_never_exposes_key(self, get, sleep):
        get.side_effect = HTTPError("https://example.test/?api_key=SECRET", 401, "bad key", {}, None)
        with self.assertRaisesRegex(backdrop.BackdropError, r"TMDB returned HTTP 401\.$") as error:
            self.fetch()
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    @patch("find_backdrop.time.sleep")
    @patch("find_backdrop.urlopen")
    def test_transient_errors_are_retried_once(self, get, sleep):
        get.side_effect = [
            HTTPError("", 429, "", {}, None),
            response({"results": [MOVIE]}), response(IMAGES), response(image()),
        ]
        self.fetch()
        sleep.assert_called_once_with(0.5)
        get.side_effect = URLError("secret URL")
        with self.assertRaisesRegex(backdrop.BackdropError, "could not be reached"):
            self.fetch("Different Title")

    @patch("find_backdrop.urlopen")
    def test_invalid_json_oversized_and_invalid_search_response(self, get):
        for data in (b"not JSON", b"x" * (backdrop.MAX_JSON_BYTES + 1), {"results": "bad"},
                     {"results": [], "total_pages": "bad"}):
            get.return_value = response(data)
            with self.assertRaises(backdrop.BackdropError):
                self.fetch()

    @patch("find_backdrop.urlopen")
    def test_only_landscape_paths_from_trusted_cdn_are_used(self, get):
        invalid = {"backdrops": [
            {"file_path": "https://example.test/image.jpg", "width": 1920, "height": 1080},
            {"file_path": "/../secret.jpg", "width": 1920, "height": 1080},
            {"file_path": "/portrait.jpg", "width": 500, "height": 750},
        ]}
        get.side_effect = [response({"results": [MOVIE]}), response(invalid)]
        with self.assertRaisesRegex(backdrop.BackdropError, "no suitable landscape"):
            self.fetch()
        self.assertEqual(get.call_count, 2)

    @patch("find_backdrop.urlopen")
    def test_bad_first_image_tries_another_without_caching_garbage(self, get):
        images = {"backdrops": [
            IMAGES["backdrops"][0],
            {"file_path": "/second.jpg", "width": 1280, "height": 720},
        ]}
        get.side_effect = [response({"results": [MOVIE]}), response(images),
                           response(b"<html>Blocked</html>"), response(image())]
        path = self.fetch()
        with Image.open(path) as result:
            result.load()
        self.assertEqual(get.call_count, 4)

    def test_manual_image_is_validated_and_cached_without_credentials(self):
        with patch("find_backdrop.load_credential", side_effect=AssertionError):
            path = backdrop.manual_backdrop(image(format="PNG"), self.directory)
        self.assertTrue(path.exists())
        for data in (b"not an image", image(360, 640), image(200, 100),
                     b"x" * (backdrop.MAX_IMAGE_BYTES + 1)):
            with self.assertRaises(backdrop.BackdropError):
                backdrop.manual_backdrop(data, self.directory)

    def test_bad_year_fails_before_credential_lookup(self):
        with patch("find_backdrop.load_credential") as credential:
            with self.assertRaisesRegex(backdrop.BackdropError, "four-digit"):
                self.fetch(year="bad")
        credential.assert_not_called()

    @patch("find_backdrop.subprocess.run")
    def test_pass_supports_read_token_or_v3_key_without_prompting(self, run):
        with patch.dict(os.environ, {}, clear=True):
            for value, bearer in (("jwt-token", True), ("a" * 32, False)):
                run.return_value = subprocess.CompletedProcess([], 0, value + "\nnotes", "")
                self.assertEqual(backdrop.load_credential(), backdrop.Credential(value, bearer))
        self.assertEqual(run.call_args.args[0], ["pass", "show", "--", "api/tmdb"])
        self.assertIn("--pinentry-mode error", run.call_args.kwargs["env"]["PASSWORD_STORE_GPG_OPTS"])

    @patch("find_backdrop.subprocess.run")
    def test_credentials_env_file_and_pass_failures_are_safe(self, run):
        self.assertEqual(backdrop.load_credential(), backdrop.Credential("test-token"))
        run.assert_not_called()
        file = self.directory / "credential"
        file.write_text("api-key")
        with patch.dict(os.environ, {"TMDB_API_KEY_FILE": str(file)}, clear=True):
            self.assertEqual(backdrop.load_credential(), backdrop.Credential("api-key", False))
        with patch.dict(os.environ, {}, clear=True):
            run.return_value = subprocess.CompletedProcess([], 1, "", "SECRET")
            with self.assertRaisesRegex(backdrop.BackdropError, "unavailable") as error:
                backdrop.load_credential()
            self.assertNotIn("SECRET", str(error.exception))

    @patch("find_backdrop.urlopen")
    def test_runner_2026_uses_known_imdb_identity_instead_of_ambiguous_search(self, get):
        runner = {"id": 1377237, "title": "Runner", "release_date": "2026-09-07"}
        get.side_effect = [
            response({"movie_results": [runner], "tv_results": []}),
            response(IMAGES), response(image()),
        ]
        self.fetch("Runner", "2026", imdb_id="tt31349844")
        request = get.call_args_list[0].args[0]
        self.assertEqual(urlsplit(request.full_url).path, "/3/find/tt31349844")
        self.assertEqual(parse_qs(urlsplit(request.full_url).query), {"external_source": ["imdb_id"]})
        self.assertEqual(urlsplit(get.call_args_list[1].args[0].full_url).path, "/3/movie/1377237/images")
        self.assertFalse(any("/search/" in call.args[0].full_url for call in get.call_args_list))

    @patch("find_backdrop.urlopen")
    def test_imdb_match_must_agree_with_requested_title_year_and_type(self, get):
        for item in ({**MOVIE, "title": "Wrong Title"}, {**MOVIE, "release_date": "2004-01-01"}):
            get.return_value = response({"movie_results": [item], "tv_results": []})
            with self.assertRaisesRegex(backdrop.BackdropError, "requested title, year and type"):
                self.fetch(imdb_id="tt0327137")
        get.return_value = response({"movie_results": [], "tv_results": [MOVIE]})
        with self.assertRaises(backdrop.BackdropError):
            self.fetch(imdb_id="tt0327137")

    @patch("find_backdrop.urlopen")
    def test_imdb_mapping_missing_or_multiple_does_not_fall_back_to_guessed_title(self, get):
        for results in ([], [MOVIE, {**MOVIE, "id": 999}]):
            get.reset_mock()
            get.return_value = response({"movie_results": results})
            with self.assertRaisesRegex(backdrop.BackdropError, "could not uniquely match"):
                self.fetch(imdb_id="tt0327137")
            self.assertEqual(get.call_count, 1)

    @patch("find_backdrop.urlopen")
    def test_tv_imdb_mapping_accepts_parent_series_for_episode(self, get):
        series = {"id": 123, "name": "My Series", "first_air_date": "2020-01-01"}
        get.side_effect = [
            response({"movie_results": [], "tv_results": [series]}),
            response(IMAGES), response(image()),
        ]
        self.fetch("My Series episode 10", "2020", "tv", imdb_id="tt1234567")
        self.assertEqual(urlsplit(get.call_args_list[1].args[0].full_url).path, "/3/tv/123/images")

    @patch("find_backdrop.urlopen")
    def test_cache_separates_different_imdb_ids_for_same_title_year(self, get):
        get.side_effect = [
            response({"movie_results": [MOVIE]}), response(IMAGES), response(image()),
            response({"movie_results": [{**MOVIE, "id": 999}]}), response(IMAGES), response(image()),
        ]
        first = self.fetch(imdb_id="tt0327137")
        second = self.fetch(imdb_id="tt1234567")
        self.assertNotEqual(first, second)
        self.assertEqual(get.call_count, 6)

    @patch("find_backdrop.load_credential")
    def test_invalid_imdb_id_fails_before_credentials_or_network(self, credential):
        with self.assertRaisesRegex(backdrop.BackdropError, "IMDb identifier"):
            self.fetch(imdb_id="../secrets")
        credential.assert_not_called()

    @patch("find_backdrop.urlopen")
    def test_fallback_search_finds_exact_match_on_second_page(self, get):
        get.side_effect = [
            response({"total_pages": 2, "results": [{**MOVIE, "title": "Wrong Title"}]}),
            response({"results": [MOVIE]}), response(IMAGES), response(image()),
        ]
        self.fetch()
        params = parse_qs(urlsplit(get.call_args_list[1].args[0].full_url).query)
        self.assertEqual(params["page"], ["2"])

    @patch("find_backdrop.urlopen")
    def test_fallback_search_detects_ambiguity_across_pages(self, get):
        get.side_effect = [
            response({"total_pages": 2, "results": [MOVIE]}),
            response({"results": [{**MOVIE, "id": 999}]}),
        ]
        with self.assertRaisesRegex(backdrop.BackdropError, "Multiple"):
            self.fetch()
        self.assertEqual(get.call_count, 2)

    @patch("find_backdrop.urlopen")
    def test_excessive_fallback_pagination_is_bounded(self, get):
        get.return_value = response({"total_pages": backdrop.MAX_SEARCH_PAGES + 1, "results": [MOVIE]})
        with self.assertRaisesRegex(backdrop.BackdropError, "too many candidates"):
            self.fetch()
        self.assertEqual(get.call_count, 1)

    @patch("find_backdrop.fetch_backdrop", side_effect=backdrop.BackdropError("Multiple matches."))
    def test_cli_exposes_a_safe_error_marker(self, fetch):
        with patch("sys.argv", ["find_backdrop.py", "Runner", "2026", "--imdb-id", "tt31349844"]), \
                patch("sys.stderr", new_callable=io.StringIO) as error, \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(backdrop.main(), 1)
        self.assertEqual(error.getvalue(), "ERROR: Multiple matches.\n")
        self.assertEqual(output.getvalue(), "NONE\n")
        self.assertEqual(fetch.call_args.kwargs["imdb_id"], "tt31349844")


if __name__ == "__main__":
    unittest.main()
